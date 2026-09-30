"""Background workers — everything that runs behind the gateway.

Plain English:
- turn      : reads the intake lanes, runs Tanya's turn, posts the reply to the CRM
              (checking HUMAN mode once more just before posting).
- persist   : copies memory events from Redis into the MySQL orch_ tables (one way, seconds behind).
- loader    : fills a customer's picture from the CRM BEFORE he needs it (cold start, app-open, nightly).
- sessions  : closes sessions after 30 min silence → session note (AI) + Lead Brief to the CRM.
Run:  python -m tanya.workers turn 0-7 | persist | loader | sessions

PLUMBING OWNERS: JUNIOR B (turn, persist), CODER D (loader mapping, CRM brief), JUNIOR C (sessions).
"""
import json
import sys
import time

from . import memory_model as mm
from .content_pack import PACK
from .crm_adapter import make_adapter
from .graph import run_turn
from .knowledge import KnowledgeIndex
from .llm import LLM
from .memory_store import VersionConflict, make_store
from .settings import S
from .timeutil import iso, now as tnow, parse

HANDOVER_ACTIONS = {"HAND_OVER_PERSON", "HAND_OVER_PURCHASE", "BOOK_CALL", "LOG_GRIEVANCE", "SUPPORT_CASE"}


# ------------------------------------------------------------------ seed (where a new record comes from)
def seed_dev(user_id):
    """Dev: test users from content/test_users.json."""
    return PACK.test_users.get(user_id)


def seed_server_factory(store):
    """Server: a record must be prepared by the loader; if missing → cold start + ask the loader."""
    def seed(user_id):
        try:
            store.r.rpush("tanya:loadq", user_id)
        except Exception:
            pass
        return None
    return seed


# ------------------------------------------------------------------ turn worker
class TurnHandler:
    def __init__(self, store, llm, kb, adapter, seed_fn):
        self.store, self.llm, self.kb, self.adapter, self.seed_fn = store, llm, kb, adapter, seed_fn

    def __call__(self, event: dict):
        kind = event.get("kind", "user_message")
        if kind == "staff_message":
            return self._staff(event)
        for attempt in range(3):                      # redo the turn if a newer version was saved meanwhile
            try:
                st = run_turn({"user_id": event["user_id"], "kind": "app_open" if kind == "app_open" else "message",
                               "text": event.get("text", ""), "event_id": event.get("event_id"), "now": tnow(),
                               "store": self.store, "llm": self.llm, "kb": self.kb, "seed_fn": self.seed_fn})
                break
            except VersionConflict:
                time.sleep(0.2 * (attempt + 1))
        else:
            raise RuntimeError("version conflict 3 times")
        self._deliver(st, event)
        return st

    def _deliver(self, st, event):
        """Post each bubble; if HUMAN mode started meanwhile, post nothing and mark undelivered."""
        rec = st["rec"]
        conv = event.get("conversation_id") or rec["conversation_id"]
        failed = []
        for b in st["bubbles"]:
            if self.store.human_flag(conv, tnow()):
                failed.append(b)
                continue
            try:
                self.adapter.post_message(conv, b["text"])
            except Exception:
                failed.append(b)
        if failed:
            self._mark_undelivered(st["user_id"], [b["text"] for b in failed])
        if st["decision"] and st["decision"].action in HANDOVER_ACTIONS:
            try:
                self.adapter.write_tanya_brief(st["user_id"], conv, st["brief"])
                if st["decision"].action in ("HAND_OVER_PERSON", "LOG_GRIEVANCE"):
                    self.adapter.hand_to_human(conv, st["decision"].action)
            except Exception as e:
                self.store.emit([{"type": "alert", "user_id": st["user_id"], "at": iso(tnow()),
                                  "kind": "crm_write_failed", "detail": str(e)[:200]}])

    def _mark_undelivered(self, user_id, texts):
        """A reply that never reached him is never remembered as something she said (§7.6.2)."""
        for _ in range(3):
            rec = self.store.get(user_id)
            for m in reversed(rec["messages"]):
                if m["role"] == "assistant" and m["text"] in texts and m.get("delivered", True):
                    m["delivered"] = False
            try:
                self.store.save(rec)
                self.store.emit([{"type": "delivery_failed", "user_id": user_id, "at": iso(tnow()), "texts": texts}])
                return
            except VersionConflict:
                continue

    def _staff(self, event):
        """A staff reply: HUMAN mode in the record (the flag was already set by the gateway)."""
        from .policy import human_takeover
        for _ in range(3):
            rec = self.store.get(event["user_id"])
            if rec is None:
                return
            human_takeover(rec, tnow(), by="staff")
            mm.add_message(rec, "staff", event.get("text", ""), tnow())
            try:
                self.store.save(rec)
                self.store.emit([{"type": "mode", "user_id": rec["user_id"], "at": iso(tnow()),
                                  "conversation_id": rec["conversation_id"], "mode": "HUMAN", "by": "staff"}])
                return
            except VersionConflict:
                continue


# ------------------------------------------------------------------ persister (Redis → MySQL, one way)
def _dt(s):
    return (s or iso(tnow()))[:19].replace("T", " ")


class Persister:
    """Writes events into the orch_ tables. Needs MYSQL_HOST, MYSQL_PORT, MYSQL_USER, MYSQL_PASSWORD, MYSQL_DB."""

    def __init__(self, conn=None):
        self.conn = conn or self._connect()

    @staticmethod
    def _connect():
        import pymysql
        return pymysql.connect(host=S.env("MYSQL_HOST", "127.0.0.1"), port=int(S.env("MYSQL_PORT", "3306")),
                               user=S.env("MYSQL_USER"), password=S.env("MYSQL_PASSWORD"),
                               database=S.env("MYSQL_DB"), charset="utf8mb4", autocommit=False)

    def write(self, events):
        with self.conn.cursor() as c:
            for e in events:
                self._one(c, e)
        self.conn.commit()

    def _one(self, c, e):
        t, u, at = e["type"], e.get("user_id"), _dt(e.get("at"))
        J = lambda x: json.dumps(x, ensure_ascii=False)
        if t == "message" and e["role"] == "user":
            c.execute("INSERT IGNORE INTO orch_inbox (event_id,user_id,conversation_id,msg_no,text,received_at) "
                      "VALUES (%s,%s,%s,%s,%s,%s)", (e.get("event_id"), u, e.get("conversation_id"), e["n"], e["text"], at))
        elif t == "message" and e["role"] == "assistant":
            c.execute("INSERT IGNORE INTO orch_replies (user_id,msg_no,line_id,action,text,delivered,sent_at) "
                      "VALUES (%s,%s,%s,%s,%s,1,%s)", (u, e["n"], e.get("line_id", ""), e.get("action", ""), e["text"], at))
        elif t == "delivery_failed":
            for txt in e.get("texts", []):
                c.execute("UPDATE orch_replies SET delivered=0 WHERE user_id=%s AND text=%s", (u, txt))
        elif t == "fact":
            c.execute("INSERT INTO orch_lead_facts (user_id,field,value,his_words,source,msg_no,stated,confidence,at) "
                      "VALUES (%s,%s,%s,%s,%s,%s,1,%s,%s)",
                      (u, e["field"], e["value"][:255], e.get("his_words", "")[:500], e.get("source", "chat"),
                       e.get("msg_no"), e.get("confidence"), at))
        elif t == "signal":
            c.execute("INSERT INTO orch_lead_signals (user_id,name,evidence,at) VALUES (%s,%s,%s,%s)",
                      (u, e["name"], (e.get("evidence") or "")[:255], at))
        elif t == "case":
            c.execute("INSERT INTO orch_lead_events (user_id,type,detail,at) VALUES (%s,'case',%s,%s)", (u, J(e), at))
            c.execute("INSERT INTO orch_alerts (user_id,kind,detail,at) VALUES (%s,%s,%s,%s)",
                      (u, f"case_{e.get('kind')}", J(e), at))
        elif t == "callback":
            c.execute("INSERT INTO orch_callbacks (callback_id,user_id,kind,state,requested_at,when_text,slot,updated_at) "
                      "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE state=VALUES(state), updated_at=VALUES(updated_at)",
                      (e["id"], u, e["kind"], e["state"], _dt(e.get("requested_at")), e.get("when_text"),
                       J(e.get("slot")), at))
        elif t in ("value_card", "session_start", "session_end", "delivery_note"):
            c.execute("INSERT INTO orch_lead_events (user_id,type,detail,at) VALUES (%s,%s,%s,%s)", (u, t, J(e), at))
        elif t == "trace":
            tr = e["trace"]
            c.execute("INSERT INTO orch_reply_trace (user_id,action,reason,cost_inr,trace,at) VALUES (%s,%s,%s,%s,%s,%s)",
                      (u, tr.get("action"), tr.get("reason"), tr.get("cost_inr", 0), J(tr), at))
            for k in tr.get("llm_calls", []):
                c.execute("INSERT INTO orch_ai_usage (user_id,purpose,provider,model,tokens_in,tokens_out,ms,cost_inr,ok,error,at) "
                          "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                          (u, k["purpose"], k["provider"], k["model"], k["in"], k["out"], k["ms"], k["cost_inr"],
                           1 if k["ok"] else 0, (k.get("error") or "")[:255], at))
        elif t == "state":
            # write only if newer — an older job never overwrites a newer correction (§7.4)
            c.execute("INSERT INTO orch_lead_state (user_id,version,temperature,mode,journey,counters,updated_at) "
                      "VALUES (%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE "
                      "temperature=IF(VALUES(version)>version,VALUES(temperature),temperature),"
                      "mode=IF(VALUES(version)>version,VALUES(mode),mode),"
                      "journey=IF(VALUES(version)>version,VALUES(journey),journey),"
                      "counters=IF(VALUES(version)>version,VALUES(counters),counters),"
                      "updated_at=IF(VALUES(version)>version,VALUES(updated_at),updated_at),"
                      "version=GREATEST(version,VALUES(version))",
                      (u, e["version"], e.get("temperature"), e.get("mode"), J(e.get("journey")), J(e.get("counters")), at))
        elif t in ("brief", "session_note"):
            c.execute("INSERT INTO orch_ai_notes (user_id,kind,text,at) VALUES (%s,%s,%s,%s)",
                      (u, "lead_brief" if t == "brief" else "session_note",
                       e["text"] if isinstance(e.get("text"), str) else J(e.get("text")), at))
        elif t == "mode":
            c.execute("INSERT INTO orch_conversation_state (conversation_id,user_id,mode,since,by_who,updated_at) "
                      "VALUES (%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE mode=VALUES(mode), since=VALUES(since), "
                      "by_who=VALUES(by_who), updated_at=VALUES(updated_at)",
                      (e.get("conversation_id") or u, u, e["mode"], at, e.get("by"), at))
        elif t == "alert":
            c.execute("INSERT INTO orch_alerts (user_id,kind,detail,at) VALUES (%s,%s,%s,%s)",
                      (u, e.get("kind", "alert"), J(e), at))
        elif t == "audit":
            c.execute("INSERT INTO orch_compliance_audits (audit_date,user_id,item,lines_text,ai_verdict,model,created_at) "
                      "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                      (e["audit_date"], u, e["item"][:80], e.get("lines", ""), e.get("verdict", "flag"), e.get("model"), at))
        elif t == "kb_chunk":
            c.execute("REPLACE INTO orch_kb_chunks (chunk_id,doc_id,title,category,status,version,text,content_hash,updated_at) "
                      "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                      (e["chunk_id"], e["doc_id"], e["title"], e["category"], e["status"], e["version"], e["text"],
                       e["content_hash"], at))


def persist_file(path="data/events.jsonl"):
    """Dev / test: copy the event file into MySQL."""
    from pathlib import Path
    from .settings import ROOT
    p = Persister()
    events = [json.loads(l) for l in (ROOT / path).read_text(encoding="utf-8").splitlines() if l.strip()]
    p.write(events)
    return len(events)


# ------------------------------------------------------------------ loader (CRM → memory, before he needs it)
def load_from_crm(adapter, user_id) -> dict:
    """Build a seed record from the CRM. TODO (CODER D, A4/A5): map the real field names and parse agent notes."""
    u = adapter.get_user(user_id) or {}
    extra = u.get("extra", {}) if isinstance(u, dict) else {}
    seed = {
        "name": u.get("first_name") or u.get("name", ""),                      # TO CONFIRM (A5)
        "consent": str(extra.get("dpdp_consent", "")).lower() in ("1", "yes", "true"),   # TO CONFIRM (B3)
        "trial_start": extra.get("trial_start"),                                 # TO CONFIRM (A5)
        "language": extra.get("language", "hinglish"),
        "conversation_id": u.get("conversation_id", user_id),
        "source": "crm",
        "facts": [],
    }
    for note in adapter.read_notes(user_id) or []:
        # TODO (CODER D + JUNIOR C): agree a simple agent-note format, e.g. lines 'segment: Nifty options'
        text = note.get("message", "") if isinstance(note, dict) else str(note)
        for line in text.splitlines():
            if ":" in line:
                k, v = [x.strip() for x in line.split(":", 1)]
                k = k.lower().replace(" ", "_")
                if k in mm.PROFILE_ASK_ORDER + ["occupation", "capital_band", "best_call_time", "language_pref"]:
                    seed["facts"].append({"field": k, "value": v, "source": "agent_note", "note": text[:120]})
    return seed


def run_loader(store, adapter):
    """Pops user IDs from tanya:loadq and prepares their records (never in the chat path)."""
    while True:
        item = store.r.blpop("tanya:loadq", timeout=5)
        if not item:
            continue
        uid = item[1]
        if store.get(uid):
            continue
        try:
            rec = mm.new_record(uid, load_from_crm(adapter, uid), tnow())
            store.save(rec)
        except Exception as e:
            store.emit([{"type": "alert", "user_id": uid, "at": iso(tnow()), "kind": "loader_failed", "detail": str(e)[:200]}])


# ------------------------------------------------------------------ session end → note + brief
NOTE_SYSTEM = """TASK: NOTE
Summarise ONE finished chat session between a customer and Ms Tanya (TG Level's AI assistant).
Use ONLY delivered messages. Never invent. Return ONLY JSON:
{"note": ["3 short lines: what he asked / shared, what was taught, what was agreed"],
 "last_promise": "<the next step she promised, or empty>"}"""


def close_idle_sessions(store, llm, adapter, now=None):
    """Sessions silent for session_gap_minutes → session note (AI) + Lead Brief to the CRM."""
    from .brief import lead_brief
    from .scoring import temperature
    now = now or tnow()
    gap = S.get("session_gap_minutes", 30) * 60
    done = 0
    for uid in store.ids():
        rec = store.get(uid)
        s = rec.get("session") or {}
        if not s or s.get("noted") or (now - parse(s["last"])).total_seconds() < gap:
            continue
        msgs = [m for m in rec["messages"] if m.get("delivered", True) and m["at"] >= s["started"]]
        if not msgs:
            continue
        convo = "\n".join(f"{m['role']}: {m['text']}" for m in msgs)
        res = llm.call("note", "fast", NOTE_SYSTEM, [{"role": "user", "content": convo}], json_mode=True,
                       temperature=0.0, timeout=30, max_tokens=500)
        note = res.data.get("note", []) if res.ok else ["(note failed — see transcript)"]
        rec["session_notes"].append({"session": s["id"], "at": iso(now), "lines": note})
        rec["session_notes"] = rec["session_notes"][-10:]
        s["noted"] = True
        temp, ev = temperature(rec, now)
        brief = lead_brief(rec, now, temp, ev)
        try:
            store.save(rec)
        except VersionConflict:
            continue
        store.emit([{"type": "session_note", "user_id": uid, "at": iso(now), "text": note},
                    {"type": "brief", "user_id": uid, "at": iso(now), "text": brief},
                    {"type": "session_end", "user_id": uid, "at": iso(now), "session": s["id"]}])
        try:
            adapter.write_tanya_brief(uid, rec["conversation_id"], brief)
        except Exception:
            pass
        done += 1
    return done


# ------------------------------------------------------------------ command line
def main(argv):
    from .streams import GROUP, StreamWorker
    store, llm, adapter = make_store(), LLM(), make_adapter()
    cmd = argv[1] if len(argv) > 1 else "turn"
    if cmd == "turn":
        a, b = (argv[2] if len(argv) > 2 else f"0-{S.get('stream_partitions', 8) - 1}").split("-")
        parts = list(range(int(a), int(b) + 1))
        handler = TurnHandler(store, llm, KnowledgeIndex(), adapter, seed_server_factory(store))
        StreamWorker(store.r, parts, handler).run_forever()
    elif cmd == "persist":
        p = Persister()
        StreamWorker(store.r, [], lambda ev: p.write([ev]), group="persister", streams=["tanya:persist"]).run_forever()
    elif cmd == "loader":
        run_loader(store, adapter)
    elif cmd == "sessions":
        while True:
            close_idle_sessions(store, llm, adapter)
            time.sleep(60)
    elif cmd == "persist-file":
        print(persist_file(), "events written")
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv)
