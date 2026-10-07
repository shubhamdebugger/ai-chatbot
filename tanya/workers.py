"""Background workers — everything that runs behind the gateway.

Plain English:
- turn      : reads the intake lanes, runs Tanya's turn, posts the reply to the CRM
              (checking HUMAN mode once more just before posting).
- persist   : copies memory events from Redis into the MySQL orch_ tables (one way, seconds behind).
- loader    : fills a customer's picture from the CRM BEFORE he needs it (cold start, app-open, nightly).
- sessions  : closes sessions after 30 min silence → session note (AI) + Lead Brief to the CRM.
Run:  python -m tanya.workers turn 0-7 | persist | loader | sessions | reconcile

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


def seed_server_factory(store, adapter=None):
    """Server: a record not prepared yet is built from the CRM before the first turn (one api.php read),
    so the background loader can never save it in the middle of the turn and force a second AI run (PT7).
    If the CRM cannot be read: cold start + ask the loader, as before."""
    def seed(user_id):
        if adapter is not None:
            try:
                return load_from_crm(adapter, user_id)
            except Exception:
                pass
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
        if self._marked(event, "done"):               # a reclaimed job whose turn was already delivered (PT5)
            return None
        conv0 = str(event.get("conversation_id") or "")
        if conv0 and kind == "user_message":          # the PWA shows "typing" only while this is set (06-Oct)
            self.store.typing_set(conv0, "working", S.get("typing_ttl_seconds", 90))
        retrying = False
        try:
            return self._turn(event, kind)
        except RuntimeError as e:
            retrying = str(e).startswith("POST_FAILED")
            raise
        finally:
            if conv0 and retrying:
                # the reply is saved and is posted again when this job is redelivered (CRM was down): the customer
                # keeps seeing "Tanya is typing" until then instead of a silent chat
                self.store.typing_set(conv0, "retrying", S.get("reclaim_idle_seconds", 60) + 60)
            elif conv0:
                self.store.typing_clear(conv0)

    def _turn(self, event, kind):
        t_start = time.time()
        retry = self._retry_get(event)
        if retry:                                     # earlier post failed: post the saved reply, don't think again
            st, conv = None, retry["conversation_id"]
            suppressed, post_failed = self._post_bubbles(conv, retry["bubbles"], event, reconcile=True)
        else:
            attempts_ms = []
            for attempt in range(3):                  # redo the turn if a newer version was saved meanwhile
                ta = time.time()
                try:
                    st = run_turn({"user_id": event["user_id"], "kind": "app_open" if kind == "app_open" else "message",
                                   "text": event.get("text", ""), "event_id": event.get("event_id"),
                                   "conversation_id": event.get("conversation_id"), "now": tnow(),
                                   "store": self.store, "llm": self.llm, "kb": self.kb, "seed_fn": self.seed_fn})
                    break
                except VersionConflict:
                    attempts_ms.append(int((time.time() - ta) * 1000))
                    time.sleep(0.2 * (attempt + 1))
            else:
                raise RuntimeError("version conflict 3 times")
            attempts_ms.append(int((time.time() - ta) * 1000))
            st["attempts_ms"] = attempts_ms
            t_turn = time.time()
            conv, suppressed, post_failed = self._deliver(st, event)
            self._timing(event, st, t_start, t_turn)
            if st.get("gate_reason") == "HUMAN_MODE" or suppressed:
                # the customer now waits for staff: alert the team lead if nobody answers (v4 §7.6)
                self.store.staff_wait_start(conv, event.get("user_id", ""), tnow())
            if (st.get("gate_reason") == "HUMAN_MODE" and kind == "user_message" and conv
                    and str(event.get("event_id") or "").isdigit()):
                # HUMAN mode with nobody on the clock: this message gets the day/night deadline (handoff_recovery)
                from .handoff_recovery import customer_waiting_again
                customer_waiting_again(self.store, conv, event.get("user_id", ""), tnow(), event.get("event_id"))
            d = st.get("decision")
            if d and d.action in S.get("handoff_human_actions", ["HAND_OVER_PERSON"]) and not suppressed \
                    and not post_failed:
                # Tanya handed the chat to staff: HUMAN mode with a recovery deadline (handoff_recovery.py)
                from .handoff_recovery import start_handoff
                start_handoff(self.store, conv, event.get("user_id", ""), d.action, d.reason, tnow(),
                              last_message_id=event.get("event_id"))
            if kind == "user_message" and conv:          # conversation summary bookkeeping (summaries.py)
                self.store.summary_touch(conv, event.get("user_id", ""), time.time(),
                                         force=bool(d and d.action in S.get("handoff_human_actions", [])))
        if post_failed and self._retry_set(event, conv, post_failed):
            # not acknowledged: the reclaimer retries it, and after the last attempt it goes to the dead letter (PT3)
            self._outcome(event, "POST_FAILED")
            raise RuntimeError(f"POST_FAILED: {len(post_failed)} bubble(s) not accepted by the CRM")
        self._retry_clear(event)
        self._mark(event, "done")
        self._outcome(event, self._outcome_of(st, suppressed))
        return st

    @staticmethod
    def _outcome_of(st, suppressed):
        """v4 §12 job outcome, recorded per CRM message (orch_inbox.status)."""
        if suppressed or (st and st.get("gate_reason") == "HUMAN_MODE"):
            return "SKIPPED_HUMAN"
        if st and st.get("gate") == "silent":
            return "SKIPPED_GATE"
        if st is None or st.get("bubbles"):
            return "REPLIED"
        return "NO_REPLY"

    def _outcome(self, event, status):
        if event.get("event_id"):
            self.store.emit([{"type": "outcome", "event_id": str(event["event_id"]), "user_id": event.get("user_id"),
                              "conversation_id": event.get("conversation_id"), "status": status, "at": iso(tnow())}])

    def _timing(self, event, st, t_start, t_turn):
        """One line per turn: where the customer's waiting time went (queue, thinking, AI, posting)."""
        now = time.time()
        recv = event.get("received_ms")
        llm_ms = sum(c.get("ms", 0) for c in (st.get("llm_calls") or []))
        line = {"event": event.get("event_id"), "conv": event.get("conversation_id"), "source": event.get("source", "-"),
                "queue_ms": int(t_start * 1000 - recv) if recv else None, "turn_ms": int((t_turn - t_start) * 1000),
                "llm_ms": llm_ms, "llm_calls": len(st.get("llm_calls") or []), "post_ms": int((now - t_turn) * 1000),
                "bubbles": len(st.get("bubbles") or []), "total_ms": int(now * 1000 - recv) if recv else None,
                "nodes": ",".join(f"{k}:{v}" for k, v in (st.get("node_ms") or {}).items()),
                "attempts_ms": "/".join(str(x) for x in st.get("attempts_ms", []))}
        print("[turn] " + " ".join(f"{k}={v}" for k, v in line.items()), file=sys.stderr, flush=True)
        r = getattr(self.store, "r", None)
        if r is not None:
            try:                                      # last 500 turns, for /health/queues
                r.lpush("tanya:metrics:turns", json.dumps(line))
                r.ltrim("tanya:metrics:turns", 0, 499)
            except Exception:
                pass

    def _retry_get(self, event):
        r = getattr(self.store, "r", None)
        raw = r.get(f"tanya:retry:{event['event_id']}") if r is not None and event.get("event_id") else None
        return json.loads(raw) if raw else None

    def _retry_set(self, event, conv, bubbles):
        r = getattr(self.store, "r", None)
        if r is None or not event.get("event_id"):
            return False
        r.set(f"tanya:retry:{event['event_id']}", json.dumps({"conversation_id": conv, "bubbles": bubbles},
                                                              ensure_ascii=False), ex=86400)
        return True

    def _retry_clear(self, event):
        r = getattr(self.store, "r", None)
        if r is not None and event.get("event_id"):
            r.delete(f"tanya:retry:{event['event_id']}")

    def _marked(self, event, what):
        r = getattr(self.store, "r", None)
        return bool(r is not None and event.get("event_id") and r.exists(f"tanya:{what}:{event['event_id']}"))

    def _mark(self, event, what, value="1"):
        r = getattr(self.store, "r", None)
        if r is not None and event.get("event_id"):
            r.set(f"tanya:{what}:{event['event_id']}", str(value or "1"), ex=86400)

    def _deliver(self, st, event):
        """Post each bubble; if HUMAN mode started meanwhile, post nothing and mark undelivered."""
        rec = st["rec"]
        conv = event.get("conversation_id") or rec["conversation_id"]
        suppressed, post_failed = self._post_bubbles(conv, [[i, b["text"]] for i, b in enumerate(st.get("bubbles") or [])],
                                                     event)
        if suppressed or post_failed:
            self._mark_undelivered(st["user_id"], suppressed + [t for _, t in post_failed])
        d = st.get("decision")                       # absent when the gate stopped the turn (LangGraph state)
        if d and d.action in HANDOVER_ACTIONS:
            try:
                if S.get("crm_lead_brief_note", False):      # off: agents see only the Conversation Summary note
                    self.adapter.write_tanya_brief(st["user_id"], conv, st["brief"])
                if d.action in ("HAND_OVER_PERSON", "LOG_GRIEVANCE"):
                    self.adapter.hand_to_human(conv, d.action)
            except Exception as e:
                self.store.emit([{"type": "alert", "user_id": st["user_id"], "at": iso(tnow()),
                                  "kind": "crm_write_failed", "detail": str(e)[:200]}])
        return conv, suppressed, post_failed

    def _post_bubbles(self, conv, bubbles, event, reconcile=False):
        """bubbles = [[index, text], ...]. Returns (texts suppressed for HUMAN mode, [[index, text]] the CRM refused).
        reconcile: a retry first checks the CRM, because a post whose response was lost may have been saved (PT4)."""
        suppressed, post_failed = [], []
        crm_checked = False
        for i, text in bubbles:
            posted = dict(event, event_id=f"{event.get('event_id')}:{i}") if event.get("event_id") else event
            if self._marked(posted, "posted"):          # this bubble reached the CRM before a crash: never again
                continue
            if reconcile:
                try:
                    if self.adapter.own_message_saved(conv, event.get("event_id"), text):
                        self._mark(posted, "posted")
                        continue
                except Exception:
                    post_failed.append([i, text])       # cannot tell if it was saved: do not risk a duplicate now
                    continue
            t0 = time.time()
            # Redis HUMAN flag before every bubble (set within ms by the staff webhook); the slower CRM read
            # (catches a staff reply whose webhook is late or lost) once, just before the first bubble.
            if self.store.human_flag(conv, tnow()) or (not crm_checked and self._staff_in_crm(conv, event)):
                suppressed.append(text)
                continue
            crm_checked = True
            t1 = time.time()
            try:
                crm_id = self.adapter.post_message(conv, text)
                self._mark(posted, "posted", value=crm_id)
                try:
                    if str(crm_id or "").isdigit():
                        self.store.last_reply_set(conv, int(crm_id))   # PWA: typing stays until this is on screen
                except Exception:
                    pass
                self.store.emit([{"type": "reply_posted", "user_id": event.get("user_id"), "at": iso(tnow()),
                                  "source_event_id": event.get("event_id"), "bubble": i, "text": text,
                                  "crm_message_id": str(crm_id or "")}])
            except Exception:
                post_failed.append([i, text])
            print(f"[post] event={event.get('event_id')} bubble={i} check_ms={int((t1 - t0) * 1000)} "
                  f"post_ms={int((time.time() - t1) * 1000)}", file=sys.stderr, flush=True)
        return suppressed, post_failed

    def _staff_in_crm(self, conv, event):
        """Staff wrote in the CRM after this customer message but its webhook has not set the flag yet:
        set HUMAN now and post nothing (K3 / PT6). A failed CRM read does not block the reply."""
        try:
            if not self.adapter.staff_replied_after(conv, event.get("event_id")):
                return False
        except Exception:
            return False
        self.store.set_human_flag(conv, S.get("human_mode_release_hours", 12), tnow())
        return True

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
            human_takeover(rec, tnow(), by="staff", conversation_id=event.get("conversation_id"))
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
        try:
            self.conn.ping(reconnect=True)          # MySQL restarted or idle-timeout: reconnect, don't fail forever
        except Exception:
            self.conn = self._connect()
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
            n = c.execute("INSERT INTO orch_callbacks (callback_id,user_id,kind,state,requested_at,when_text,slot,updated_at,"
                          "conversation_id,source_message_id,reason,promise,due_at,status) "
                          "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending') ON DUPLICATE KEY UPDATE "
                          "state=VALUES(state), updated_at=VALUES(updated_at)",
                          (e["id"], u, e["kind"], e["state"], _dt(e.get("requested_at")), e.get("when_text"),
                           J(e.get("slot")), at, e.get("conversation_id"), e.get("source_message_id"), e.get("reason"),
                           e.get("promise"), _dt(e.get("due_at")) if e.get("due_at") else None))
            if n == 1:                                # a new callback (2 = existing one updated)
                c.execute("INSERT INTO orch_audit (entity,entity_id,event,actor,detail,at) VALUES "
                          "('callback',%s,'created','tanya',%s,%s)",
                          (e["id"], J({"kind": e["kind"], "reason": e.get("reason"), "due_at": e.get("due_at"),
                                       "conversation_id": e.get("conversation_id")}), at))
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
        elif t == "outcome":                          # v4 §12 lifecycle: what became of this CRM message
            c.execute("UPDATE orch_inbox SET status=%s, outcome_at=%s WHERE event_id=%s",
                      (e["status"], at, e["event_id"]))
        elif t == "reply_posted":                     # G3: reply -> source message and CRM message id
            c.execute("UPDATE orch_replies SET source_event_id=%s, crm_message_id=%s WHERE user_id=%s AND text=%s "
                      "AND crm_message_id IS NULL ORDER BY id DESC LIMIT 1",
                      (e.get("source_event_id"), e.get("crm_message_id"), u, e.get("text", "")))
        elif t == "handoff" and e.get("op") == "start":   # Tanya's handoff (handoff_recovery.py)
            c.execute("INSERT IGNORE INTO orch_handoffs (handoff_id,conversation_id,user_id,action,reason,period,"
                      "started_at,due_at,status,last_message_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'open',%s)",
                      (e["handoff_id"], e["conversation_id"], u, e.get("action"), e.get("reason"), e.get("period"),
                       _dt(e.get("started_at")), _dt(e.get("due_at")), e.get("last_message_id") or None))
        elif t == "handoff":
            cols = {k: e[k] for k in ("status", "agent_assigned_id", "agent_replied_id", "recovery_message_id")
                    if k in e}
            cols.update({k: _dt(e[k]) for k in ("agent_assigned_at", "agent_replied_at", "recovered_at") if e.get(k)})
            if e.get("agent_id") and "agent_replied_at" in e:
                cols["agent_replied_id"] = e["agent_id"]
            if cols:
                c.execute("UPDATE orch_handoffs SET " + ", ".join(f"{k}=%s" for k in cols) + " WHERE handoff_id=%s",
                          (*cols.values(), e["handoff_id"]))
        elif t == "audit":
            c.execute("INSERT INTO orch_audit (entity,entity_id,event,actor,detail,at) VALUES (%s,%s,%s,%s,%s,%s)",
                      (e["entity"], e["entity_id"], e["event"], e.get("actor"), J(e.get("detail") or {}), at))
        elif t == "conv_summary":                     # Conversation Summary (summaries.py) - the durable copy
            c.execute("INSERT INTO orch_conv_summaries (conversation_id,user_id,summary,msg_count,last_message_id,"
                      "crm_note_id,model,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE "
                      "summary=VALUES(summary), msg_count=VALUES(msg_count), last_message_id=VALUES(last_message_id), "
                      "crm_note_id=VALUES(crm_note_id), model=VALUES(model), updated_at=VALUES(updated_at)",
                      (e["conversation_id"], u, e["summary"], e.get("msg_count"), e.get("last_message_id"),
                       e.get("crm_note_id"), e.get("model"), at))
        elif t == "callback_event":                   # e.g. recovered_by_tanya for the chat's open callbacks
            c.execute("SELECT callback_id FROM orch_callbacks WHERE conversation_id=%s AND "
                      "COALESCE(status,'pending') <> 'completed'", (e["conversation_id"],))
            for (cb_id,) in c.fetchall():
                if e.get("event") == "recovered_by_tanya":
                    c.execute("UPDATE orch_callbacks SET recovered_at=%s WHERE callback_id=%s", (at, cb_id))
                c.execute("INSERT INTO orch_audit (entity,entity_id,event,actor,detail,at) VALUES "
                          "('callback',%s,%s,'tanya',%s,%s)", (cb_id, e.get("event"), J(e), at))
        elif t == "dead_letter":                      # v4 §12 point 5 / G7
            c.execute("INSERT INTO orch_dead_letters (stream,stream_id,body,at) VALUES (%s,%s,%s,%s)",
                      (e.get("stream", ""), e.get("stream_id", ""), J(e.get("body")), at))
        elif t == "audit":
            c.execute("INSERT INTO orch_compliance_audits (audit_date,user_id,item,lines_text,ai_verdict,model,created_at) "
                      "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                      (e["audit_date"], u, e["item"][:80], e.get("lines", ""), e.get("verdict", "flag"), e.get("model"), at))
        elif t == "voice_call":
            c.execute("INSERT IGNORE INTO orch_voice_calls (el_conversation_id,user_id,pwa_uid,sb_user_id,"
                      "sb_conversation_id,started_at,duration_secs,status,ended_reason,call_successful,title,"
                      "summary,kb_queries,transcript,masked,cost,received_at) "
                      "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                      (e["el_conversation_id"], u, e.get("pwa_uid"), e.get("sb_user_id"), e.get("sb_conversation_id"),
                       _dt(e.get("started_at")) if e.get("started_at") else None, e.get("duration_secs"),
                       e.get("status"), (e.get("ended_reason") or "")[:255], e.get("call_successful"),
                       (e.get("title") or "")[:255], e.get("summary"), J(e.get("kb_queries", [])),
                       J(e.get("transcript", [])), ",".join(e.get("masked", [])), e.get("cost"), at))
            c.execute("INSERT INTO orch_lead_events (user_id,type,detail,at) VALUES (%s,'voice_call',%s,%s)",
                      (u, J({k: e.get(k) for k in ("el_conversation_id", "duration_secs", "title", "call_successful")}),
                       at))
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
    conversation_id = u.get("conversation_id", user_id)
    seed = {
        "name": u.get("first_name") or u.get("name", ""),                      # TO CONFIRM (A5)
        "consent": str(extra.get("dpdp_consent", "")).lower() in ("1", "yes", "true"),   # TO CONFIRM (B3)
        "trial_start": extra.get("trial_start"),                                 # TO CONFIRM (A5)
        "language": extra.get("language", "hinglish"),
        "conversation_id": conversation_id,
        "source": "crm",
        "facts": [],
    }
    for note in adapter.read_notes(conversation_id) or []:
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
NOTE_SCHEMA = {"type": "object", "additionalProperties": False,
               "properties": {"note": {"type": "array", "items": {"type": "string"}}, "last_promise": {"type": "string"}},
               "required": ["note", "last_promise"]}


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
                       temperature=0.0, timeout=30, max_tokens=500, schema=NOTE_SCHEMA)
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
        if S.get("crm_lead_brief_note", False):             # the brief stays in the DB (event 'brief') either way
            try:
                adapter.write_tanya_brief(uid, rec["conversation_id"], brief)
            except Exception:
                pass
        done += 1
    return done


# ------------------------------------------------------------------ reconciler (no customer message is ever lost)
STAFF_TYPES = ("agent", "admin", "bot")


def reconcile_once(store, adapter, intake, now_utc=None, window_s=600, grace_s=15):
    """The CRM sends each webhook ONCE and never retries (crm/include/functions.php sb_webhooks). If that call
    is lost (Tanya down, network, Redis down → HTTP 503), the message would never be answered. Every run reads
    the CRM's recent conversations and queues every customer message that is still unanswered and was never
    queued. Idempotent: the same tanya:seen:{message_id} key as the webhook path, so it is queued at most once.
    Safe after a Redis loss: a message already answered has a later Tanya/staff message and is skipped."""
    import datetime as _dt
    now_utc = now_utc or _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
    since = (now_utc - _dt.timedelta(seconds=window_s)).strftime("%Y-%m-%d %H:%M:%S")
    recovered = []
    for c in adapter.recent_conversations(since) or []:
        owner = str(c.get("conversation_user_id", ""))
        if str(c.get("message_user_id")) != owner or str(c.get("message_user_type")) in STAFF_TYPES:
            continue                                  # last word is Tanya's or staff's: nothing waiting
        conv = str(c.get("conversation_id"))
        msgs = adapter.get_conversation(conv, limit=30)
        waiting = []
        for m in msgs:                                # customer messages after the last non-customer message
            if str(m.get("user_id")) == owner and str(m.get("user_type")) not in STAFF_TYPES:
                waiting.append(m)
            else:
                waiting = []
        for m in waiting:
            try:
                age = (now_utc - _dt.datetime.strptime(str(m.get("creation_time")), "%Y-%m-%d %H:%M:%S")).total_seconds()
            except ValueError:
                continue
            if age < grace_s or age > window_s or store.r.exists(f"tanya:seen:{m['id']}"):
                continue                              # webhook still in flight / too old (staff) / already queued
            event = {"event_id": str(m["id"]), "kind": "user_message", "user_id": owner, "conversation_id": conv,
                     "text": str(m.get("message", "")), "received_ms": int(time.time() * 1000), "source": "reconciler"}
            if intake.enqueue(event):
                recovered.append(event["event_id"])
                store.r.incr("tanya:metrics:reconciled")
                print(f"[reconcile] recovered event={event['event_id']} conv={conv} age_s={int(age)}",
                      file=sys.stderr, flush=True)
    store.r.set("tanya:metrics:reconcile_last", int(time.time()))
    return recovered


def staff_silent_check(store, now=None):
    """v4 §7.6: a customer has waited for staff (HUMAN mode, no staff reply) longer than staff_silent_alert_minutes
    -> one alert for the team lead (orch_alerts kind staff_silent). Runs in the reconcile loop."""
    now = now or tnow()
    limit = S.get("staff_silent_alert_minutes", 5) * 60
    alerted = []
    for conv, uid, since in store.staff_waiting_since(now.timestamp() - limit):
        store.emit([{"type": "alert", "user_id": uid or conv, "at": iso(now), "kind": "staff_silent",
                     "conversation_id": conv, "waited_s": int(now.timestamp() - since)}])
        store.staff_wait_end(conv)
        alerted.append(conv)
        print(f"[staff-silent] ALERT conv={conv} user={uid} waited_s={int(now.timestamp() - since)}",
              file=sys.stderr, flush=True)
    return alerted


def dead_letter_handler(store, adapter):
    """v4 §12 point 5 / G7 — a customer message that failed max_deliveries times: keep it in orch_dead_letters,
    alert staff, hand the chat to staff (HUMAN) and tell the customer with the approved fixed line FX-05
    (best effort: if the CRM itself is the failure, the post fails too and the alert remains)."""
    def on_dead(stream, msg_id, body):
        try:
            ev = json.loads(body.get("event", "{}"))
        except Exception:
            ev = {}
        uid, conv = ev.get("user_id") or "-", ev.get("conversation_id")
        store.emit([{"type": "dead_letter", "user_id": uid, "at": iso(tnow()), "stream": stream, "stream_id": msg_id,
                     "body": ev},
                    {"type": "alert", "user_id": uid, "at": iso(tnow()), "kind": "dead_letter",
                     "conversation_id": conv, "event_id": ev.get("event_id")}])
        if ev.get("event_id"):
            store.emit([{"type": "outcome", "event_id": str(ev["event_id"]), "user_id": uid, "conversation_id": conv,
                         "status": "DEAD", "at": iso(tnow())}])
        if not conv or ev.get("kind") != "user_message":
            return
        store.set_human_flag(conv, S.get("human_mode_release_hours", 12), tnow())
        try:
            rec = store.get(uid) if uid != "-" else None
            lang = ((rec or {}).get("profile") or {}).get("language", "hinglish")
            adapter.post_message(conv, PACK.fixed("FX-05", lang))
        except Exception as e:
            print(f"[dead] FX-05 not posted conv={conv}: {type(e).__name__}", file=sys.stderr, flush=True)
    return on_dead


class Housekeeping:
    """In the reconcile process: handoff recovery (every run), callback SLA (every 30 s), re-seed open handoffs
    from MySQL after a Redis loss (every 5 min). MySQL problems never stop the handoff recovery."""

    def __init__(self, store, adapter, intake):
        self.store, self.adapter, self.intake = store, adapter, intake
        self.conn, self.last_sla, self.last_seed = None, 0.0, 0.0

    def _db(self):
        if self.conn is None:
            self.conn = Persister._connect()
        else:
            self.conn.ping(reconnect=True)
        return self.conn

    def run(self):
        from .handoff_recovery import recovery_check, reseed_from_db
        from .callbacks import sla_check
        recovery_check(self.store, self.adapter, self.intake)
        now = time.time()
        try:
            if now - self.last_seed > S.get("handoff_reseed_seconds", 300):
                n = reseed_from_db(self.store, self._db())
                if n:
                    print(f"[handoff] re-seeded {n} open handoff(s) from MySQL", file=sys.stderr, flush=True)
                self.last_seed = now
            if now - self.last_sla > S.get("callback_sla_check_seconds", 30):
                sla_check(self._db(), self.adapter, tnow())
                self.last_sla = now
        except Exception as e:
            print(f"[housekeeping] MySQL step failed {type(e).__name__}: {str(e)[:150]}", file=sys.stderr, flush=True)
            self.conn = None


def forever(step, name, pause=0.0):
    """Run step() forever. A Redis/network/CRM error is logged and retried with back-off instead of killing the
    process (a dead worker silently stops replies until something restarts it)."""
    delay = 1
    while True:
        try:
            step()
            delay = 1
            if pause:
                time.sleep(pause)
        except Exception as e:
            print(f"[{name}] error {type(e).__name__}: {str(e)[:200]} — retry in {delay}s", file=sys.stderr, flush=True)
            time.sleep(delay)
            delay = min(delay * 2, 30)


# ------------------------------------------------------------------ command line
def main(argv):
    from .streams import GROUP, StreamWorker
    store, llm, adapter = make_store(), LLM(), make_adapter()
    cmd = argv[1] if len(argv) > 1 else "turn"
    if cmd == "turn":
        a, b = (argv[2] if len(argv) > 2 else f"0-{S.get('stream_partitions', 8) - 1}").split("-")
        parts = list(range(int(a), int(b) + 1))
        handler = TurnHandler(store, llm, KnowledgeIndex(), adapter, seed_server_factory(store, adapter))
        StreamWorker(store.r, parts, handler, on_dead=dead_letter_handler(store, adapter)).run_forever()
    elif cmd == "persist":
        p = Persister()
        StreamWorker(store.r, [], lambda ev: p.write([ev]), group="persister", streams=["tanya:persist"]).run_forever()
    elif cmd == "loader":
        forever(lambda: run_loader(store, adapter), "loader")
    elif cmd == "sessions":
        from .summaries import summarize_due
        forever(lambda: (close_idle_sessions(store, llm, adapter), summarize_due(store, llm, adapter)), "sessions",
                pause=S.get("summary_check_seconds", 30))
    elif cmd == "reconcile":
        from .streams import Intake
        intake = Intake(store.r)
        every = float(S.env("RECONCILE_EVERY_SECONDS", "15"))
        print(f"[reconcile] started: every {every:g} s, window 10 min", file=sys.stderr, flush=True)
        housekeeping = Housekeeping(store, adapter, intake)
        forever(lambda: (reconcile_once(store, adapter, intake), staff_silent_check(store), housekeeping.run()),
                "reconcile", pause=every)
    elif cmd == "persist-file":
        print(persist_file(), "events written")
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv)
