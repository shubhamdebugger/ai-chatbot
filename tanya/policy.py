"""AI-C02 Mode Policy Engine + AI-C20 Kill Switch / Safe Mode + AI-C19 spend ceiling.

Plain English: before Tanya thinks about a message, these rules decide
whether she may answer at all, and in what mode:
- HUMAN mode: a staff member is handling the chat → Tanya stays silent.
- Kill switch: 'stopped' → silent; 'limited' → general and education answers only.
- Spend ceiling: at 100% of the day's AI budget → fixed line FX-05.
- Consent: no DPDP agreement in the app → answer only, nothing remembered or sold.
"""
from .settings import S
from .timeutil import iso, parse, hm

GATE_GO = "go"
GATE_SILENT = "silent"      # no reply at all (HUMAN mode, kill switch stopped)
GATE_FIXED = "fixed"        # a fixed line only, no AI call


def human_takeover(rec, now, by="staff", conversation_id=None):
    """A staff reply arrived → HUMAN mode at once (v4 rule), for the chat the staff member wrote in."""
    rec["mode"] = {"state": "HUMAN", "since": iso(now), "by": by,
                   "conversation_id": str(conversation_id or rec.get("conversation_id", ""))}


def release_to_bot(rec, now, by="staff"):
    rec["mode"] = {"state": "BOT", "since": iso(now), "by": by}


def mode_is_human(rec, now) -> bool:
    """HUMAN mode ends by itself after human_mode_release_hours without staff activity."""
    m = rec.get("mode", {})
    if m.get("state") != "HUMAN":
        return False
    if m.get("conversation_id") and m["conversation_id"] != str(rec.get("conversation_id", "")):
        return False                           # staff took over another of his chats, not this one (PT8)
    hours = S.get("human_mode_release_hours", 12)
    if (now - parse(m["since"])).total_seconds() > hours * 3600:
        release_to_bot(rec, now, by="timeout")
        return False
    return True


def gate(rec, store, now, injection_flag: bool):
    """Returns (decision, fixed_line_id or None, reason_code)."""
    ks = store.killswitch()
    if ks == "stopped":
        return GATE_SILENT, None, "KILL_STOPPED"
    if store.human_flag(rec["conversation_id"], now) and rec["mode"].get("state") != "HUMAN":
        human_takeover(rec, now, by="webhook flag")
    if mode_is_human(rec, now) or store.human_flag(rec["conversation_id"], now):
        return GATE_SILENT, None, "HUMAN_MODE"
    if injection_flag:
        return GATE_FIXED, "FX-12", "R00"
    ceiling = S.get("daily_ai_spend_ceiling_inr", 200)
    if store.ledger_get(now) >= ceiling:
        return GATE_FIXED, "FX-05", "R01"
    return GATE_GO, None, "OK"


def spend_alert(store, now) -> bool:
    """True once spend passes the alert share of the ceiling (80%)."""
    return store.ledger_get(now) >= S.get("daily_ai_spend_ceiling_inr", 200) * S.get("spend_alert_percent", 80) / 100


def in_calling_hours(now) -> bool:
    sh, sm = hm(S.get("calling_window_start", "10:00"))
    eh, em = hm(S.get("calling_window_end", "19:00"))
    t = now.hour * 60 + now.minute
    return sh * 60 + sm <= t < eh * 60 + em
