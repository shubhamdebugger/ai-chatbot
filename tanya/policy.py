"""AI-C02 Mode Policy Engine + AI-C20 Kill Switch / Safe Mode + AI-C19 spend ceiling.

Plain English: before Tanya thinks about a message, these rules decide
whether she may answer at all, and in what mode:
- HUMAN mode: a staff member is handling the chat → Tanya stays silent.
- Kill switch: 'stopped' → silent; 'limited' → general and education answers only.
- Guarantee question: fixed line FX-20, no AI call.
- Small talk: the whole message is a greeting / how are you / bye / thanks / sorry
  → a fixed line only, no AI call (R-SMALLFX).
- Spend ceiling: at 100% of the day's AI budget → fixed line FX-05.
- Lead token budget: at 100% of THIS lead's token budget → no AI call; a warm closing
  (FX-32 first, FX-34 after) or, for a buying / complaint lead, the human handoff (R01-TOKENS).
- Consent: no DPDP agreement in the app → answer only, nothing remembered or sold.
"""
import re

from .settings import S
from .timeutil import iso, parse, hm

GATE_GO = "go"
GATE_SILENT = "silent"      # no reply at all (HUMAN mode, kill switch stopped)
GATE_FIXED = "fixed"        # a fixed line only, no AI call

GUARANTEE_RX = re.compile(r"guarantee|gurantee|gurrantee|guaranty|pakka|sure shot|100%", re.I)
MONEY_RX = re.compile(r"paisa|paise|money|return|profit|double|dugna", re.I)
GRIEVANCE_RX = re.compile(r"fraud|refund|cheat|dhokha|complaint", re.I)

# ---- the lead's own words for the two exceptions to the wind-up (no bare 'plan', 'pay', 'join',
# ---- 'problem' or 'issue': a normal trading message must never be mistaken for them)
BUY_INTENT_RX = re.compile(r"fees?\s*(kitni|kitna|hai)|kitni\s+fees|fees\s+kitni|how\s+to\s+join"
                           r"|joining|join\s+karna|plan\s+lena|plan\s+chahiye|payment\s+karna"
                           r"|pay\s+karna|subscribe|kharidna|enroll", re.I)
COMPLAINT_RX = re.compile(r"refund|complaint|fraud|cheat|dhokha|scam|payment\s+(failed|fail|nahi)"
                          r"|paisa\s+wapas|dikkat|problem\s+(ho|hai)|not\s+working|kaam\s+nahi", re.I)

# ---- zero-AI small talk (R-SMALLFX): the WHOLE message must be one plain category
FILLER_WORDS = {"tanya", "ji", "mam", "maam"}          # optional words, dropped before matching
GREETING_EMOJI = ("\U0001F64F", "\U0001F44B")          # 🙏 👋

SMALLTALK = {
    "greeting": ("hi", "hey", "hello", "helo", "namaste", "namaskar", "नमस्ते",
                 "good morning", "good afternoon", "good evening", "gm"),
    "how_are_you": ("kaisi ho", "kaise ho", "kese ho", "kaisi hain", "kya haal hai", "haal chaal",
                    "sab badhiya", "how are you", "how r u", "hru"),
    "what_doing": ("kya kar rahi ho", "kya kr rhi ho", "kya kri ho", "kya karti ho",
                   "what are you doing", "wyd"),
    "bye": ("bye", "tata", "alvida", "chalo bye", "good night", "gn", "see you"),
    "thanks": ("thanks", "thank you", "thankyou", "thnx", "thx", "ty", "shukriya",
               "dhanyavaad", "dhanyawad"),
    "sorry": ("sorry", "sry", "maaf karo", "maafi"),
}


def norm_smalltalk(text: str) -> str:
    """Lower case, no punctuation or emoji, repeated letters collapsed, filler words dropped."""
    t = (text or "").lower().replace("'", "").replace("’", "")
    t = "".join(ch if (ch.isalnum() or ch.isspace()) else " " for ch in t)
    words = [re.sub(r"(.)\1+", r"\1", w) for w in t.split()]     # "hiii" → "hi", "byyy" → "by"
    return " ".join(w for w in words if w not in FILLER_WORDS)


def smalltalk_category(text: str):
    """Which small-talk category the WHOLE message is, or None (a question or topic → normal flow)."""
    msg = norm_smalltalk(text)
    if not msg:
        # nothing but punctuation / emoji: 🙏 and 👋 greet
        return "greeting" if any(e in (text or "") for e in GREETING_EMOJI) else None
    for cat, phrases in SMALLTALK.items():
        for ph in phrases:
            p = norm_smalltalk(ph)
            # whole message = the phrase; the message may drop the phrase's last letter ("byyy" → "by")
            if msg == p or (len(msg) >= 2 and len(msg) >= len(p) - 1 and p.startswith(msg)):
                return cat
    return None


def human_takeover(rec, now, by="staff"):
    """A staff reply arrived → HUMAN mode at once (v4 rule)."""
    rec["mode"] = {"state": "HUMAN", "since": iso(now), "by": by}


def release_to_bot(rec, now, by="staff"):
    rec["mode"] = {"state": "BOT", "since": iso(now), "by": by}


def mode_is_human(rec, now) -> bool:
    """HUMAN mode ends by itself after human_mode_release_hours without staff activity."""
    m = rec.get("mode", {})
    if m.get("state") != "HUMAN":
        return False
    hours = S.get("human_mode_release_hours", 12)
    if (now - parse(m["since"])).total_seconds() > hours * 3600:
        release_to_bot(rec, now, by="timeout")
        return False
    return True


def gate(rec, store, now, injection_flag: bool, text: str = ""):
    """Returns (decision, fixed_line_id or small-talk category, reason_code)."""
    ks = store.killswitch()
    if ks == "stopped":
        return GATE_SILENT, None, "KILL_STOPPED"
    if store.human_flag(rec["conversation_id"], now) and rec["mode"].get("state") != "HUMAN":
        human_takeover(rec, now, by="webhook flag")
    if mode_is_human(rec, now) or store.human_flag(rec["conversation_id"], now):
        return GATE_SILENT, None, "HUMAN_MODE"
    if injection_flag:
        return GATE_FIXED, "FX-12", "R00"
    if GUARANTEE_RX.search(text) and MONEY_RX.search(text) and not GRIEVANCE_RX.search(text):
        return GATE_FIXED, "FX-20", "R07G"
    cat = smalltalk_category(text)
    if cat:
        # the whole message is small talk → compose picks the FX-xx line for this category
        return GATE_FIXED, cat, "R-SMALLFX"
    ceiling = S.get("daily_ai_spend_ceiling_inr", 200)
    if store.ledger_get(now) >= ceiling:
        return GATE_FIXED, "FX-05", "R01"
    if lead_budget_stop(store, rec, now):
        # this lead's own token budget is used up → no AI call at all this turn;
        # n_gate picks FX-32 / FX-34, or the human handoff for a buying / complaint lead
        return GATE_FIXED, "FX-32", "R01-TOKENS"
    return GATE_GO, None, "OK"


# ------------------------------------------------------------------ per-lead token budget
def lead_token_budget() -> int:
    """One lead's own AI token budget (config: lead_token_budget)."""
    return int(S.get("lead_token_budget", 50000))


def lead_token_estimate(rec) -> int:
    """What this turn will probably cost: the average of his recent turns (1500 when unknown)."""
    hist = rec.get("counters", {}).get("turn_tokens") or []
    if not hist:
        return 1500
    return int(round(sum(hist) / len(hist)))


def lead_tokens(store, rec, now) -> int:
    return store.tokens_get(rec["user_id"], now)


def lead_budget_stop(store, rec, now) -> bool:
    """True when this turn would take the lead to 100% of his budget → skip every AI call."""
    return lead_tokens(store, rec, now) + lead_token_estimate(rec) >= lead_token_budget()


def windup_due(store, rec, now) -> bool:
    """True once his usage first passes windup_trigger_pct of the budget."""
    return lead_tokens(store, rec, now) >= lead_token_budget() * S.get("windup_trigger_pct", 0.70)


def budget_exception_kind(text: str) -> str:
    """"complaint" | "buying" | "" — his own words, used before the understand call (no AI call)."""
    if COMPLAINT_RX.search(text or ""):
        return "complaint"
    if BUY_INTENT_RX.search(text or ""):
        return "buying"
    return ""


def budget_exception(text: str, labels=None) -> bool:
    """Buying intent or a complaint → never wind this lead up (at 100% → the human handoff instead).
    understand's labels win once it has run; before it, or if it failed, only his words count."""
    on = (labels or {}).get("labels") or {}
    if on and not labels.get("failed", False):
        return bool(on.get("purchase_intent", {}).get("on") or on.get("grievance", {}).get("on"))
    return bool(budget_exception_kind(text))


def spend_alert(store, now) -> bool:
    """True once spend passes the alert share of the ceiling (80%)."""
    return store.ledger_get(now) >= S.get("daily_ai_spend_ceiling_inr", 200) * S.get("spend_alert_percent", 80) / 100


def in_calling_hours(now) -> bool:
    sh, sm = hm(S.get("calling_window_start", "10:00"))
    eh, em = hm(S.get("calling_window_end", "19:00"))
    t = now.hour * 60 + now.minute
    return sh * 60 + sm <= t < eh * 60 + em
