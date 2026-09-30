"""AI-C06 Lead Scoring + AI-C07 Trial Intelligence — lead temperature (Architecture v3.2 §8).

Plain English: curiosity is not a buying decision. Interest, purchase intent and
readiness are kept separate, each with evidence and an expiry, and CODE combines
them into Hot / Warm / Nurture / Not ready. Suppression (distress, open grievance,
no consent) always wins — never Hot, whatever words appear.
These are qualification rules, not a probability of buying.
"""
from . import memory_model as mm
from .settings import S


def readiness(rec, now):
    """Profile fits (experience and segment known), no open timing objection, no refused call."""
    known = mm.known_fields(rec)
    reasons = []
    if not {"experience", "segment"} <= known:
        reasons.append("experience/segment not known")
    if mm.signal_active(rec, "timing_objection", now, 7 * 24):
        reasons.append("timing objection open")
    if mm.signal_active(rec, "refused_call", now):
        reasons.append("refused a call")
    return not reasons, reasons


def temperature(rec, now):
    """Returns (label, evidence_lines)."""
    suppressed, why = mm.suppression(rec)
    intent = mm.signal_active(rec, "purchase_intent", now, S.get("intent_expiry_hours", 48))
    interest = mm.signal_active(rec, "interest", now, S.get("interest_expiry_hours", 72))
    ready, not_ready = readiness(rec, now)
    ev = []
    for name in ("purchase_intent", "interest", "timing_objection", "prefers_call"):
        s = rec["signals"].get(name)
        if s:
            ev.append(f"{name}: \"{s.get('evidence', '')}\" ({s['at'][:16].replace('T', ' ')})")
    if suppressed:
        ev.append("suppressed: " + ", ".join(why))
    if intent and ready and not suppressed:
        return "Hot", ev
    known = mm.known_fields(rec)
    if interest and not suppressed and "experience" in known and ("main_pain" in known or "past_loss" in known):
        return "Warm", ev
    if rec["counters"].get("user_msgs_total", 0) >= 3 and not intent:
        if not ready:
            ev.append("not ready: " + ", ".join(not_ready))
        return "Nurture", ev
    return "Not ready", ev
