"""AI-C02 Mode Policy Engine + AI-C20 Kill Switch / Safe Mode + AI-C19 spend ceiling.

Plain English: before Tanya thinks about a message, these rules decide
whether she may answer at all, and in what mode:
- HUMAN mode: a staff member is handling the chat → Tanya stays silent.
- Kill switch: 'stopped' → silent; 'limited' → general and education answers only.
- Guarantee question: fixed line FX-20, no AI call.
- Founder question: fixed line FX-33, no AI call (plan/offer questions keep the pricing flow).
- Small talk: the whole message is a greeting / how are you / bye / thanks / sorry
  → a fixed line only, no AI call (R-SMALLFX).
- Spend ceiling: at 100% of the day's AI budget → fixed line FX-05.
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
FOUNDER_RX = re.compile(r"founder|founded|\bowner\b|\bowns\b|malik|\bceo\b|kisne banaya|kisne banayi|kisne shuru|"
                        r"who started|who runs|who made|tushar|ghone|sansthapak|संस्थापक|मालिक|तुषार", re.I)
# a founder word next to a plan / offer word is a pricing question (FX-32 promises the founder's offer)
OFFER_RX = re.compile(r"offer|plan|pric|kitne|kitna|fees|cost|discount|subscription|₹", re.I)

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
    if FOUNDER_RX.search(text) and not OFFER_RX.search(text) and not GRIEVANCE_RX.search(text):
        return GATE_FIXED, "FX-33", "R07F"
    cat = smalltalk_category(text)
    if cat:
        # the whole message is small talk → compose picks the FX-xx line for this category
        return GATE_FIXED, cat, "R-SMALLFX"
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
