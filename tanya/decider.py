"""AI-C03 / AI-C09 — the Next-Action decider (Architecture v3.2 §6.2–6.3; Code Frame Spec §5).

Plain English: this is CODE, not AI. It reads the labels from the understand
call and his memory record, and picks ONE action for this turn by walking
down the priority table — the first row that matches wins. Every decision
carries a reason code (R01…R20) that goes into the trace.

The AI later writes the words for that action; it never chooses the action.
"""
from dataclasses import dataclass, field

from . import memory_model as mm
from .settings import S

# Actions whose whole reply is a fixed line (no AI writing)
FIXED_ONLY = {"LIMIT_SPEND", "BOUNDARY_ABUSE", "HAND_OVER_PERSON", "LIMIT_EDUCATION",
              "HAND_OVER_PURCHASE", "BOOK_CALL", "ASKS_IF_AI", "BOUNDARY_FIRM", "LIMITED_MODE",
              "ANSWER_GUARANTEE"}
# Actions that count toward the 30 education questions
COUNTS_AS_EDUCATION = {"ANSWER_EDUCATION"}
# Actions where emoji are not allowed (Personality Guide §3.5)
NO_EMOJI = {"PAUSE_SELLING", "LOG_GRIEVANCE", "BOUNDARY_ABUSE", "REFUSE_AND_TEACH", "HAND_OVER_PERSON",
            "HAND_OVER_PURCHASE", "ANSWER_PRICE", "LIMIT_SPEND", "LIMIT_EDUCATION", "BOUNDARY_FIRM",
            "ANSWER_GUARANTEE"}


@dataclass
class Decision:
    action: str
    reason: str
    fixed_line: str = ""          # FX-xx shown before or instead of AI words
    selling_allowed: bool = False
    addon: str = ""               # ASK_MISSING_FACT | SHOW_VALUE_CARD | ""
    addon_detail: dict = field(default_factory=dict)
    tier: str = "fast"            # fast | detailed model
    notes: list = field(default_factory=list)


def _on(labels, name) -> bool:
    return labels["labels"].get(name, {}).get("on", False)


def decide(labels: dict, rec: dict, now, limited: bool = False) -> Decision:
    """Walk the priority table (Spec §5). labels = normalised understand output."""
    sess = rec["session"]
    suppressed, why = mm.suppression(rec)
    consent = rec["profile"].get("consent", False)
    tier = "detailed" if labels.get("complexity") == "detailed" else "fast"

    # Row 2 — distress: support first, selling paused for the session
    if _on(labels, "distress"):
        return Decision("PAUSE_SELLING", "R02", notes=["selling paused for this session"])
    # Row 3 — grievance: case logged, handled first
    if _on(labels, "grievance"):
        return Decision("LOG_GRIEVANCE", "R03", fixed_line="FX-10")
    # Rows 4–5 — abuse (count already includes this message)
    if _on(labels, "abuse"):
        if sess.get("abuse", 0) >= S.get("abuse_count_for_handoff", 2):
            return Decision("HAND_OVER_PERSON", "R04")
        return Decision("BOUNDARY_ABUSE", "R05", fixed_line="FX-15")
    # Row 6 — asks for a person, or 3 failed turns in this session
    if _on(labels, "wants_person") or sess.get("failed", 0) >= S.get("failed_turns_for_handoff", 3):
        reason = "R06" if _on(labels, "wants_person") else "R06-FAILED"
        return Decision("HAND_OVER_PERSON", reason)
    # Row 7 — trade advice: refuse and teach (SEBI guard layer 1)
    if _on(labels, "trade_advice_seeking"):
        return Decision("REFUSE_AND_TEACH", "R07", fixed_line="FX-03", tier="fast")
    # Row 7G — money-guarantee question: fixed compliance line, no AI words
    if _on(labels, "asks_guarantee"):
        return Decision("ANSWER_GUARANTEE", "R07G", fixed_line="FX-20")
    # Kill switch 'limited': general and education only (Architecture §7.5)
    if limited and (_on(labels, "purchase_intent") or _on(labels, "interest") or _on(labels, "support_question")):
        return Decision("LIMITED_MODE", "R-LIMITED", fixed_line="FX-06")
    # Row 8 — no consent: answer only
    if not consent:
        d = Decision("ANSWER_ONLY", "R08", tier=tier)
        d.fixed_line = "FX-09" if not rec["journey"].get("consent_line_given") else ""
        return d
    # Row 9 — education limit
    if _on(labels, "education_question") and \
            rec["counters"].get("education_used", 0) >= S.get("education_questions_per_day", 30):
        return Decision("LIMIT_EDUCATION", "R09", fixed_line="FX-04")
    # Row 10 — purchase intent, no suppression
    if _on(labels, "purchase_intent") and not suppressed:
        return Decision("HAND_OVER_PURCHASE", "R10", fixed_line="FX-14", selling_allowed=True)
    # Row 11 — his preference is a call
    if _on(labels, "prefers_call"):
        return Decision("BOOK_CALL", "R11", fixed_line="FX-13")
    # Row 12 — is she an AI?
    if _on(labels, "asks_if_ai"):
        return Decision("ASKS_IF_AI", "R12", fixed_line="FX-11")
    # Row 13 — price / plan interest, no suppression
    if _on(labels, "interest") and not suppressed:
        return Decision("ANSWER_PRICE", "R13", selling_allowed=True)
    # Rows 14–15 — flirting (count includes this message)
    if _on(labels, "flirting"):
        if sess.get("flirt", 0) >= 2:
            return Decision("BOUNDARY_FIRM", "R14", fixed_line="FX-16")
        return Decision("BOUNDARY_LIGHT", "R15")
    # Row 16 — education question (+ one add-on)
    if _on(labels, "education_question"):
        d = Decision("ANSWER_EDUCATION", "R16", tier=tier)
        _addon(d, rec, suppressed)
        return d
    # Row 17 — support question
    if _on(labels, "support_question"):
        return Decision("ANSWER_SUPPORT", "R17")
    # Rows 18–19 — small talk, bridge after N turns (count includes this message)
    if _on(labels, "small_talk"):
        if sess.get("small_talk", 0) > S.get("small_talk_turns_before_bridge", 2):
            return Decision("GUIDE_NEXT_STEP", "R18")
        return Decision("ANSWER_SMALL_TALK", "R19")
    # Row 20 — anything else
    d = Decision("ANSWER", "R20", tier=tier)
    _addon(d, rec, suppressed)
    return d


def _addon(d: Decision, rec: dict, suppressed: bool):
    """At most one add-on, only after his own question is answered, never when suppressed."""
    if suppressed:
        return
    sess = rec["session"]
    missing = mm.missing_profile_fields(rec)
    if missing and sess.get("profile_q", 0) < S.get("profile_questions_per_session", 2):
        d.addon, d.addon_detail = "ASK_MISSING_FACT", {"field": missing[0]}
        return
    from .content_pack import PACK
    if rec["counters"].get("user_msgs_total", 0) >= S.get("value_card_min_user_messages", 3):
        card = PACK.card_for(mm.pain_text(rec), rec["journey"].get("cards_shown", []))
        if card:
            d.addon, d.addon_detail = "SHOW_VALUE_CARD", {"card": card["id"]}
            d.selling_allowed = True
