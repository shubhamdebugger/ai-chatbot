"""AI-C25 Customer Memory — the record kept for each user and its rules.

Plain English: one record per user holds his profile, the facts he or the
CRM gave (each with source, date, day, time, stated/inferred), the journey
(trial day, last promise, cards shown), the current session, daily counters,
buying signals, cases, callbacks, the chat mode (BOT/HUMAN) and his messages.

Rules implemented here (Architecture v3.2 §7):
- Which fact wins: his latest words for self-reported facts; system for account facts;
  an agent note is never changed — a different value is kept as a conflict for the agent.
- Known facts are never asked again; at most N missing ones per session.
- Her own replies are never a source of facts.
"""
from .settings import S
from .timeutil import iso, parse, stamp, day_key

PROFILE_ASK_ORDER = ["experience", "segment", "main_pain", "time_available", "goal"]
KEEP_MESSAGES = 300  # older messages live in MySQL (persister) — searched by a background job


def new_record(user_id: str, seed: dict, now) -> dict:
    """A fresh record, from the CRM (production) or a test user (dev)."""
    start = now.replace(hour=9, minute=0, second=0, microsecond=0)
    days_ago = int(seed.get("trial_start_days_ago", 0))
    from datetime import timedelta
    trial_start = seed.get("trial_start") or iso(start - timedelta(days=days_ago))
    rec = {
        "user_id": user_id,
        "conversation_id": seed.get("conversation_id", user_id),
        "profile": {"name": seed.get("name", ""), "language": seed.get("language", "hinglish"),
                    "consent": bool(seed.get("consent", False)), "trial_start": trial_start,
                    "plan": seed.get("plan", "trial"), "source": seed.get("source", "test_users")},
        "facts": {}, "conflicts": [],
        "journey": {"disclosed": False, "chat_first_given": False, "last_promise": "", "last_promise_at": "",
                    "cards_shown": [], "greeted_at": ""},
        "session": {}, "sessions_count": 0, "session_notes": [],
        "counters": {"day": "", "education_used": 0, "user_msgs_total": 0},
        "signals": {}, "cases": [], "callbacks": [],
        "mode": {"state": "BOT", "since": iso(now), "by": "system"},
        "messages": [], "version": 0,
    }
    for f in seed.get("facts", []):
        rec["facts"][f["field"]] = {"value": f["value"], "his_words": f.get("his_words", ""),
                                    "source": f.get("source", "crm_field"), "msg_no": None,
                                    "at": iso(now), "stated": True, "confidence": 1.0,
                                    "note": f.get("note", ""), "history": []}
    return rec


# ------------------------------------------------------------------ sessions and days
def ensure_session(rec, now) -> bool:
    """Start a new session after a silence longer than session_gap_minutes. Returns True if new."""
    gap = S.get("session_gap_minutes", 30)
    s = rec.get("session") or {}
    if s and (now - parse(s["last"])).total_seconds() <= gap * 60:
        s["last"] = iso(now)
        return False
    rec["sessions_count"] += 1
    rec["session"] = {"id": rec["sessions_count"], "started": iso(now), "last": iso(now),
                      "profile_q": 0, "small_talk": 0, "failed": 0, "abuse": 0, "flirt": 0,
                      "selling_paused": False, "disclaimer_shown": False, "user_msgs": 0}
    return True


def ensure_day(rec, now):
    """Daily counters reset at 00:00 IST."""
    d = day_key(now)
    if rec["counters"].get("day") != d:
        rec["counters"]["day"] = d
        rec["counters"]["education_used"] = 0


def trial_day(rec, now) -> int:
    """Day 1 = the day the trial started."""
    start = parse(rec["profile"]["trial_start"])
    return max(1, (now.date() - start.date()).days + 1)


# ------------------------------------------------------------------ messages
def add_message(rec, role, text, now, delivered=True, meta=None) -> int:
    n = (rec["messages"][-1]["n"] + 1) if rec["messages"] else 1
    rec["messages"].append({"n": n, "role": role, "text": text, "at": iso(now),
                            "delivered": delivered, "meta": meta or {}})
    if len(rec["messages"]) > KEEP_MESSAGES:
        rec["messages"] = rec["messages"][-KEEP_MESSAGES:]
    return n


def history_for_prompt(rec, n=None):
    """The last N delivered messages, word for word (Architecture §7.6.4)."""
    n = n or S.get("history_messages_in_prompt", 10)
    msgs = [m for m in rec["messages"] if m.get("delivered", True)]
    return [{"role": "assistant" if m["role"] == "assistant" else "user", "content": m["text"]}
            for m in msgs[-n:]]


# ------------------------------------------------------------------ facts
SYSTEM_SOURCES = {"crm_field", "agent_note", "app"}


def set_fact(rec, field, value, his_words, source, msg_no, now, stated=True, confidence=0.9):
    """Store a fact with its source. Applies 'which fact wins' and records conflicts."""
    old = rec["facts"].get(field)
    entry = {"value": value, "his_words": his_words, "source": source, "msg_no": msg_no,
             "at": iso(now), "stated": stated, "confidence": confidence, "note": "", "history": []}
    if old:
        entry["history"] = (old.get("history", []) + [{k: old[k] for k in ("value", "source", "at")}])[-5:]
        if old["source"] in SYSTEM_SOURCES and source == "chat" and \
                old["value"].strip().lower() != str(value).strip().lower():
            # The agent's note is never changed; the difference is kept for the agent (§9.4)
            rec["conflicts"].append({"field": field, "agent_value": old["value"], "agent_source": old["source"],
                                     "customer_value": value, "his_words": his_words, "at": iso(now),
                                     "status": "open"})
    rec["facts"][field] = entry  # his latest words are used in conversation (§7.2)


def known_fields(rec) -> set:
    return {k for k, v in rec["facts"].items() if v.get("value")}


def missing_profile_fields(rec) -> list:
    known = known_fields(rec)
    return [f for f in PROFILE_ASK_ORDER if f not in known]


def fact_lines(rec) -> list:
    """Known facts for the prompt — 'never ask these again'."""
    out = []
    for k, v in rec["facts"].items():
        who = {"chat": "he told Tanya", "agent_note": "agent note", "crm_field": "CRM", "app": "app"}.get(v["source"], v["source"])
        words = f' — his words: "{v["his_words"]}"' if v.get("his_words") else ""
        out.append(f"{k}: {v['value']} ({who}, {stamp(parse(v['at']))}){words}")
    return out


def pain_text(rec) -> str:
    """His pain in his own words, for value cards and price answers."""
    parts = []
    for k in ("main_pain", "past_loss"):
        f = rec["facts"].get(k)
        if f:
            parts.append(f.get("his_words") or f["value"])
    return " | ".join(parts)


# ------------------------------------------------------------------ signals
def set_signal(rec, name, now, evidence=""):
    rec["signals"][name] = {"at": iso(now), "evidence": evidence}


def signal_active(rec, name, now, hours=None) -> bool:
    s = rec["signals"].get(name)
    if not s:
        return False
    if hours is None:
        return True
    return (now - parse(s["at"])).total_seconds() <= hours * 3600


# ------------------------------------------------------------------ suppression
def suppression(rec):
    """Selling is blocked by distress this session, an open grievance, or no consent (§8.5)."""
    reasons = []
    if rec["session"].get("selling_paused"):
        reasons.append("distress this session")
    if any(c["kind"] == "grievance" and c["status"] == "open" for c in rec["cases"]):
        reasons.append("open grievance")
    if not rec["profile"].get("consent"):
        reasons.append("no consent")
    return bool(reasons), reasons
