"""Voice calls — what Ms Tanya knows about the caller before and during a call.

Plain English:
- Layer 1, the caller brief: when the PWA asks for a call token, tg-node-backend sends us what
  the app knows (trial, payment, trial-flow answers, activity). We add what Tanya knows (facts,
  chat session notes, last promise, earlier calls, plan by profile) and return ONE short text.
  It goes into the call as the dynamic variable {{user_context}}.
  That text passes through the caller's browser, so it holds only their own data, written plainly:
  never agent notes, temperature or other internal judgements.
- Layer 2, tools the voice agent calls during the call (ElevenLabs server tools). ElevenLabs
  calls them from its servers, never through the browser, so internal notes are allowed here:
    recent_chat     his last chat messages (CRM thread, else Tanya's memory)
    past_calls      earlier voice calls with their summaries
    internal_notes  agent notes, open cases, callbacks, temperature
    save_fact       store something new he said, so chat Tanya knows it too
    request_callback "our senior will call you" — only after he said yes (orch_callbacks, CRM note)
  Whose data: always the signed call context (voice.verify_context), never what the AI asks for.
- Everything is masked (guard_input.mask) and fails open: a missing piece is left out,
  a call is never blocked.
"""
import json

from . import memory_model as mm
from .content_pack import PACK
from .guard_input import mask
from .memory_store import VersionConflict
from .settings import S
from .timeutil import iso, parse, stamp
from .understand import FACT_FIELDS

BRIEF_MAX_CHARS = 3500

GOALS = {"learning": "learn trading", "research": "get research / trade calls", "both": "learn + get trade calls"}
MOODS = {"very_good": "very good", "not_watched": "did not watch"}


def _m(text, n=200) -> str:
    """Masked, one line, at most n characters."""
    return mask(" ".join(str(text or "").split()))[0][:n]


def _plain(v) -> str:
    """App answer codes in words: '1l' → '1 lakh', '3m' → '3-month plan', 'very_good' → 'very good'."""
    v = str(v or "").strip()
    low = v.lower()
    if low in GOALS:
        return GOALS[low]
    if low in MOODS:
        return MOODS[low]
    if low[:-1].replace(".", "", 1).isdigit() and low[-1:] in ("l", "m"):
        return f"{low[:-1]} lakh" if low[-1] == "l" else f"{low[:-1]}-month plan"
    return v.replace("_", " ")


def _date(s) -> str:
    try:
        return stamp(parse(s)) if "T" in str(s) else str(s or "")
    except ValueError:
        return str(s or "")


# ------------------------------------------------------------------ layer 1: the caller brief
def _account_lines(app) -> list:
    t, pay = app.get("trial") or {}, app.get("payment") or {}
    group = t.get("group") or "unknown"
    if group == "trial":
        line = (f"Free trial — {t.get('active_days_used', 0)} of {t.get('trial_days', '?')} trial days used, "
                f"{t.get('days_left', '?')} left (a day counts only when they open the app on a weekday)")
    elif group == "trial_extended":
        line = f"Trial extended until {t.get('expiry_date') or '?'}"
    elif group == "paid":
        line = "Paid member" + (f" until {t['expiry_date']}" if t.get("expiry_date") else "")
    elif group == "trial_expired":
        line = "Trial expired — not a paid member"
    elif group == "paid_expired":
        line = "Paid plan expired" + (f" on {t['expiry_date']}" if t.get("expiry_date") else "")
    else:
        line = "Plan status unknown"
    out = [f"ACCOUNT: {line}"]
    if pay.get("status"):
        amt = f" ₹{int(float(pay['amount'])):,}" if pay.get("amount") not in (None, "") else ""
        out.append(f"PAYMENT RECORD: {pay['status']}{amt}" + (f" (updated {pay['updated_at']})" if pay.get("updated_at") else ""))
    return out


def _app_lines(app) -> list:
    out = []
    ans = {k: v for k, v in (app.get("trial_answers") or {}).items() if v}
    if ans:
        names = {"goal": "goal", "capital": "capital", "day2_mood": "how day 2 went", "day3_goal": "goal on day 3",
                 "price_offered": "plan offered", "holdback_reason": "holding back because"}
        out.append("ANSWERS IN THE APP'S TRIAL QUESTIONS: " +
                   "; ".join(f"{names.get(k, k)}: {_m(_plain(v), 80)}" for k, v in ans.items()))
    a = app.get("activity") or {}
    bits = []
    if app.get("joined_at"):
        bits.append(f"joined {app['joined_at']}")
    if a.get("active_days_7d") is not None:
        bits.append(f"opened the app on {a['active_days_7d']} of the last 7 days")
    if a.get("last_active_date"):
        bits.append(f"last opened {a['last_active_date']}")
    if a.get("trade_calls_viewed_7d") is not None:
        bits.append(f"read {a['trade_calls_viewed_7d']} trade calls in 7 days")
    if a.get("quiz_attempts_14d"):
        graded = a.get("quiz_graded_14d") or 0
        bits.append(f"quiz: {a['quiz_attempts_14d']} answers in 14 days" +
                    (f", {a.get('quiz_correct_14d', 0)}/{graded} correct" if graded else ""))
    if a.get("support_chats_30d"):
        bits.append(f"opened support chat {a['support_chats_30d']} times in 30 days")
    if a.get("app_installed") is not None:
        bits.append("app installed" if a["app_installed"] else "app not installed (uses the browser)")
    if a.get("push_enabled") is not None:
        bits.append("notifications on" if a["push_enabled"] else "notifications off")
    if bits:
        out.append("APP ACTIVITY: " + "; ".join(bits))
    return out


def _memory_lines(rec, now) -> list:
    out, p = [], rec["profile"]
    lang = (rec["facts"].get("language_pref") or {}).get("value") or p.get("language")
    if lang:
        out.append(f"LANGUAGE: speak {lang}")
    if rec["facts"]:
        out.append("WHAT THE CALLER HAS TOLD US (never ask these again):")
        for k, f in rec["facts"].items():
            words = f' — in their words: "{_m(f["his_words"], 120)}"' if f.get("his_words") else ""
            out.append(f"  - {k.replace('_', ' ')}: {_m(f['value'], 120)}{words} ({_date(f.get('at'))})")
    missing = mm.missing_profile_fields(rec)
    if missing:
        out.append("STILL UNKNOWN (ask at most one, only if it fits): " + ", ".join(m.replace("_", " ") for m in missing))
    chats = [n for n in rec.get("session_notes", []) if not str(n.get("session", "")).startswith("voice:")][-3:]
    if chats:
        out.append("EARLIER CHATS WITH TANYA:")
        for n in chats:
            out.append(f"  - {_date(n.get('at'))}: " + " ".join(_m(x, 160) for x in n.get("lines", [])))
    if rec["journey"].get("last_promise"):
        out.append(f"TANYA'S LAST PROMISE (follow up on it): {_m(rec['journey']['last_promise'])}")
    plan, why = PACK.plan_for_profile(rec["facts"])
    if plan:
        why = "" if str(why).startswith("[") else why        # content pack placeholder, not written yet
        out.append(f"PLAN THAT FITS THE PROFILE: {plan['plan_name']}" + (f" — {_m(why, 160)}" if why else "") +
                   " (prices only from the knowledge base)")
    open_cases = [c for c in rec.get("cases", []) if c.get("status") == "open"]
    if open_cases:
        out.append("OPEN REQUESTS: " + "; ".join(f"{c['kind']} ({c.get('case_no', '')})" for c in open_cases))
    if not p.get("consent"):
        out.append("CONSENT: not given in the app — help and answer, but do not sell or push a plan.")
    return out


def _call_lines(calls, rec) -> list:
    rows = [f"  - {_date(c.get('started_at'))}: {_m(c.get('title') or '', 80)} — {_m(c.get('summary') or '', 220)}"
            for c in calls[:3]]
    if not rows and rec:                                   # no MySQL: the voice notes in Tanya's memory
        rows = [f"  - {_date(n.get('at'))}: " + " ".join(_m(x, 220) for x in n.get("lines", []))
                for n in rec.get("session_notes", []) if str(n.get("session", "")).startswith("voice:")][-3:]
    return (["EARLIER VOICE CALLS (refer back to them):"] + rows) if rows else []


def caller_brief(rec, app: dict, calls: list, now, callbacks=None) -> str:
    """The text for {{user_context}}. rec may be None (he never chatted), app may be {}.
    callbacks: OPEN REQUEST lines for his pending senior callbacks."""
    app = app or {}
    name = (rec or {}).get("profile", {}).get("name") or app.get("name") or ""
    lines = [f"NAME: {_m(name, 60)}" if name else "NAME: unknown — you may ask how to address them"]
    lines += _account_lines(app) if app.get("trial") or app.get("payment") else []
    lines += _app_lines(app)
    lines += _memory_lines(rec, now) if rec else ["NO CHAT HISTORY WITH TANYA YET."]
    lines += _call_lines(calls, rec)
    lines += callbacks or []
    text = "\n".join(lines)
    return text if len(text) <= BRIEF_MAX_CHARS else text[:BRIEF_MAX_CHARS - 1] + "…"


def _db():
    import pymysql
    return pymysql.connect(host=S.env("MYSQL_HOST", "127.0.0.1"), port=int(S.env("MYSQL_PORT", "3306")),
                           user=S.env("MYSQL_USER"), password=S.env("MYSQL_PASSWORD"), database=S.env("MYSQL_DB"),
                           charset="utf8mb4", connect_timeout=3, read_timeout=5,
                           cursorclass=pymysql.cursors.DictCursor)


def recent_calls(ctx, limit=3, transcript=False) -> list:
    """His earlier voice calls from orch_voice_calls, newest first. [] if MySQL is not set up."""
    if not S.env("MYSQL_DB"):
        return []
    keys = [k for k in (ctx.get("sb_user_id"), f"pwa:{ctx['pwa_uid']}" if ctx.get("pwa_uid") else "") if k]
    cols = "started_at, duration_secs, title, summary, kb_queries" + (", transcript" if transcript else "")
    try:
        conn = _db()
        try:
            with conn.cursor() as c:
                c.execute(f"SELECT {cols} FROM orch_voice_calls WHERE user_id IN ({','.join(['%s'] * len(keys))}) "
                          "ORDER BY started_at DESC LIMIT %s", (*keys, int(limit)))
                rows = c.fetchall()
        finally:
            conn.close()
    except Exception:
        return []
    return [{**r, "started_at": r["started_at"].isoformat() if r.get("started_at") else None} for r in rows]


def build_context(store, ctx: dict, app: dict, now) -> dict:
    rec = store.get(ctx["sb_user_id"]) if ctx.get("sb_user_id") else None
    return {"user_context": caller_brief(rec, app, recent_calls(ctx), now, _callback_lines(ctx))}


# ------------------------------------------------------------------ layer 2: tools during the call
def _who(m, tanya_agent) -> str:
    if str(m.get("user_id", "")) == str(tanya_agent or "-"):
        return "Tanya"
    return "agent" if str(m.get("user_type", "")) in ("agent", "admin", "bot") else "customer"


def recent_chat(store, adapter, ctx, limit=12) -> dict:
    """His last chat messages: the CRM thread (includes human agents), else Tanya's memory."""
    limit = max(1, min(int(limit or 12), 30))
    if ctx.get("sb_conversation_id"):
        try:
            msgs = adapter.get_conversation(ctx["sb_conversation_id"], limit=limit) or []
            out = [{"from": _who(m, getattr(adapter, "tanya_agent", "")), "at": str(m.get("creation_time", "")),
                    "text": _m(m.get("message"), 300)} for m in msgs if isinstance(m, dict) and m.get("message")]
            if out:
                return {"source": "crm", "messages": out}
        except Exception:
            pass
    rec = store.get(ctx["sb_user_id"]) if ctx.get("sb_user_id") else None
    msgs = (rec or {}).get("messages", [])[-limit:]
    return {"source": "memory", "messages": [{"from": "Tanya" if m["role"] == "assistant" else "customer",
                                              "at": m["at"], "text": _m(m["text"], 300)} for m in msgs]}


def past_calls(ctx, limit=3) -> dict:
    """Earlier voice calls with summary, what was searched and his own words (last lines)."""
    out = []
    for c in recent_calls(ctx, max(1, min(int(limit or 3), 5)), transcript=True):
        said = [t.get("text", "") for t in json.loads(c.get("transcript") or "[]") if t.get("role") == "user"]
        out.append({"when": c["started_at"], "minutes": round((c.get("duration_secs") or 0) / 60, 1),
                    "title": c.get("title"), "summary": _m(c.get("summary"), 600),
                    "searched": json.loads(c.get("kb_queries") or "[]")[:5],
                    "caller_said_last": [_m(x, 200) for x in said[-4:]]})
    return {"calls": out}


def internal_notes(store, adapter, ctx, now) -> dict:
    """What staff know: agent notes, open cases, callbacks, signals, temperature. Never read out."""
    out = {"agent_notes": [], "cases": [], "callbacks": [], "senior_callbacks": open_callbacks(ctx),
           "signals": {}, "temperature": None}
    if ctx.get("sb_user_id"):
        try:
            for n in adapter.read_notes(ctx["sb_user_id"]) or []:
                n = n if isinstance(n, dict) else {"message": str(n)}
                if str(n.get("name", "")).startswith("Ms Tanya"):
                    continue                             # her own brief and call notes: already in the brief
                out["agent_notes"].append({"title": _m(n.get("name"), 80), "at": str(n.get("creation_time", "")),
                                           "text": _m(n.get("message"), 500)})
        except Exception:
            pass
        out["agent_notes"] = out["agent_notes"][-6:]
    rec = store.get(ctx["sb_user_id"]) if ctx.get("sb_user_id") else None
    if rec:
        from .scoring import temperature
        out["cases"] = [{k: c.get(k) for k in ("case_no", "kind", "status")} for c in rec.get("cases", [])][-5:]
        out["callbacks"] = [{k: c.get(k) for k in ("kind", "state", "when_text")} for c in rec.get("callbacks", [])][-3:]
        out["signals"] = {k: _m(v.get("evidence"), 120) for k, v in rec.get("signals", {}).items()}
        out["temperature"] = temperature(rec, now)[0]
    return out


def save_fact(store, ctx, field, value, his_words, now) -> dict:
    """Store one fact he just said on the call (consent only, like chat). The post-call webhook also
    extracts facts from the whole transcript; this one is for things worth keeping at once."""
    if field not in FACT_FIELDS or not str(value or "").strip():
        return {"saved": False, "reason": "unknown field or empty value", "fields": list(FACT_FIELDS)}
    if not ctx.get("sb_user_id"):
        return {"saved": False, "reason": "no customer record yet; the call summary will keep it"}
    value, words = _m(value, 120), _m(his_words, 200)
    for _ in range(3):
        rec = store.get(ctx["sb_user_id"])
        if not rec:
            return {"saved": False, "reason": "no customer record yet; the call summary will keep it"}
        if not rec["profile"].get("consent"):
            return {"saved": False, "reason": "no consent — do not store personal facts"}
        mm.set_fact(rec, field, value, words, "voice", None, now, True, 0.9)
        try:
            store.save(rec)
        except VersionConflict:
            continue
        store.emit([{"type": "fact", "user_id": ctx["sb_user_id"], "at": iso(now), "field": field, "value": value,
                     "his_words": words, "source": "voice", "msg_no": None, "confidence": 0.9}])
        return {"saved": True}
    return {"saved": False, "reason": "busy, try once more"}


# ------------------------------------------------------------------ "our senior will call you"
# Only after he said YES to the offer. The same support team calls; to him they are "senior".
# Source of truth: orch_callbacks (kind 'senior'); the CRM marks it done there.
CALLBACK_REASONS = ("support", "payment", "refund", "purchase", "complaint", "wants_person", "other")
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _user_key(ctx) -> str:
    return ctx.get("sb_user_id") or f"pwa:{ctx['pwa_uid']}"       # same key as orch_voice_calls


def callback_window(now):
    """(due_by, phrase): the next time the team can call inside working days and hours.
    Inside hours with an hour left → 'aaj shaam 7 baje se pehle'; else the next working morning."""
    from datetime import timedelta
    from .handoff import phrase_at
    from .timeutil import hm
    sh, sm = hm(S.get("calling_window_start", "10:00"))
    eh, em = hm(S.get("calling_window_end", "19:00"))
    days = set(S.get("calling_days", DAYS[:5]))
    close = now.replace(hour=eh, minute=em, second=0, microsecond=0)
    if DAYS[now.weekday()] in days and now.replace(hour=sh, minute=sm) <= now and now <= close - timedelta(hours=1):
        end = f"{'shaam' if eh < 20 else 'raat'} {eh % 12 or 12}{f':{em:02d}' if em else ''} baje"   # 7 PM = shaam
        return close, f"aaj {end} se pehle"
    day = now if now < now.replace(hour=sh, minute=sm, second=0, microsecond=0) else now + timedelta(days=1)
    while DAYS[day.weekday()] not in days:
        day += timedelta(days=1)
    opening = day.replace(hour=sh, minute=sm, second=0, microsecond=0)
    when = phrase_at(opening, now, "hinglish")
    if opening.date() - now.date() > timedelta(days=1):           # 'Mon 05-Oct' → 'Monday'
        when = opening.strftime("%A") + " " + when.split(" ", 2)[-1]
    return opening.replace(hour=eh, minute=em), f"{when} ke baad"


def open_callbacks(ctx) -> list:
    """His pending senior callbacks (requested / booked), newest first. [] without MySQL."""
    if not S.env("MYSQL_DB"):
        return []
    try:
        conn = _db()
        try:
            with conn.cursor() as c:
                c.execute("SELECT callback_id, state, requested_at, when_text, slot FROM orch_callbacks "
                          "WHERE user_id=%s AND kind='senior' AND state IN ('requested','booked') "
                          "ORDER BY requested_at DESC LIMIT 5", (_user_key(ctx),))
                rows = c.fetchall()
        finally:
            conn.close()
    except Exception:
        return []
    out = []
    for r in rows:
        slot = json.loads(r.get("slot") or "{}") if str(r.get("slot") or "").startswith("{") else {}
        out.append({"id": r["callback_id"], "state": r["state"], "requested_at": r["requested_at"].isoformat(),
                    "promised": r.get("when_text"), "reason": slot.get("reason"), "due_by": slot.get("due_by")})
    return out


def _callback_lines(ctx) -> list:
    return [f"OPEN REQUEST: a senior was asked to call them ({_date(c['requested_at'])}, reason: "
            f"{(c['reason'] or 'not given').replace('_', ' ')}) — promised {c['promised']} — still pending. "
            "Do not promise again; say the request is with the team." for c in open_callbacks(ctx)]


def request_callback(store, adapter, ctx, reason, preferred_time, confirmed, now, source="tool") -> dict:
    """Book 'our senior will call you'. Only with his explicit yes; one open request per caller."""
    if not confirmed:
        return {"created": False, "say": "First ask: 'Kya aap chahenge ki hamare senior aapko call karein?' "
                                         "Call this tool only after a clear yes."}
    reason = reason if reason in CALLBACK_REASONS else "other"
    existing = open_callbacks(ctx)
    if existing:
        return {"created": False, "already_requested": True, "promised": existing[0]["promised"],
                "say": f"Their request is already with the team; a senior will call {existing[0]['promised']}."}
    due_by, phrase = callback_window(now)
    key = _user_key(ctx)
    pref = _m(preferred_time, 120)
    cb = {"type": "callback", "user_id": key, "at": iso(now), "id": f"CB-V-{key}-{now.strftime('%Y%m%d%H%M%S')}",
          "kind": "senior", "state": "requested", "requested_at": iso(now), "when_text": phrase,
          "slot": {"reason": reason, "preferred_time": pref, "due_by": iso(due_by), "source": source,
                   "sb_conversation_id": ctx.get("sb_conversation_id") or ""}}
    store.emit([cb])
    if ctx.get("sb_conversation_id"):
        note = (f"📞 Senior callback requested on a voice call — the caller said YES.\n"
                f"Reason: {reason.replace('_', ' ')}" + (f"\nCaller prefers: {pref}" if pref else "") +
                f"\nPromised to the caller: a senior will call {phrase} (Mon–Fri working hours)."
                f"\nDue by: {stamp(due_by)} · Mark it done in the Voice calls panel after calling.")
        try:
            adapter.add_agent_note(ctx.get("sb_user_id"), ctx["sb_conversation_id"],
                                   "Ms Tanya — Senior callback (auto)", note)
            if S.env("CRM_HUMAN_DEPARTMENT_ID"):
                adapter.hand_to_human(ctx["sb_conversation_id"], "senior_callback")
        except Exception as e:      # the callback itself is safe in orch_callbacks; flag it for a person
            store.emit([{"type": "alert", "user_id": key, "at": iso(now), "kind": "callback_note_failed",
                         "detail": f"{type(e).__name__}: {str(e)[:200]}"}])
    return {"created": True, "promised": phrase,
            "say": f"Tell them a senior will call {phrase}. Do not promise any other time."}
