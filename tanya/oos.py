"""Out-of-scope (OOS) questions — stop spending AI on messages that are not about TG Level or finance.

Plain English:
- Keyword bypass first (zero cost): product, finance, complaint or distress words → never OOS.
- Two signals: the understand call says out_of_scope AND his message is far from every approved
  knowledge piece (max cosine similarity < oos_relevance_threshold). Either one says relevant → normal flow.
- Locked (strikes >= oos_strikes_before_block - 1): similarity first — below the hard floor
  (oos_locked_skip_below) out of scope with no understand call; at or above it the understand call
  runs and the same two-signal rule applies.
- Strikes belong to the user (not the chat session) and are forgotten after oos_strike_expiry_hours.
- Strike 1 → FX-34, strike 2 → FX-35 (warning), strike 3 → FX-36 and a block of oos_block_minutes.
  During the block every message gets FX-37 with no AI call at all, except complaint / distress words.
  When the block ends the strikes go back to 0. No AI words, no AI check.
"""
import re
from datetime import timedelta

from .policy import GRIEVANCE_RX
from .settings import S
from .timeutil import iso, parse

# Safety words — always bypass (a hurt or complaining customer is never told "off topic")
DISTRESS_WORDS = (
    "loss ho gaya", "loss ho gya", "loss ho chuka", "bahut loss", "doob gaya", "doob gaye", "dub gaye", "barbaad",
    "sab chala gaya", "nuksaan ho", "nuksan ho", "pareshan hoon", "depressed", "lost money", "lost everything",
    "suicide", "mar jaunga", "jaan de", "नुकसान हो", "डूब गए", "डूब गया", "बर्बाद", "परेशान हूँ",
)

# Used only when config.json has no oos_bypass_keywords
DEFAULT_BYPASS = (
    "tg", "tg level", "tg levels", "tg lite", "tg app", "trial", "subscription", "payment", "refund", "login",
    "demat", "broker", "support", "agent", "stop loss", "share market", "stock market", "nifty", "sensex", "sebi",
    "ipo", "mutual fund", "crypto", "inflation", "fd", "trading", "invest",
    "शेयर", "ट्रेडिंग", "निवेश", "सेबी", "निफ्टी", "सेंसेक्स",
)


def _norm(text: str) -> str:
    """Lower case; hyphens and underscores as spaces; single spaces ('Stop-Loss' → 'stop loss')."""
    return " ".join(re.sub(r"[-_]", " ", (text or "").lower()).split())


def bypass_hit(text: str, keywords=None) -> str:
    """The product / finance / safety word in his message, or ''. Latin words match as whole words;
    Devanagari words must start a word but may take a suffix (शेयरों) — 'सपोर्ट' never matches inside 'पासपोर्ट'.
    keywords: another list instead of config.json (eval what-ifs only)."""
    m = GRIEVANCE_RX.search(text or "")
    if m:
        return m.group(0)
    low = _norm(text)
    if keywords is None:
        keywords = S.get("oos_bypass_keywords", DEFAULT_BYPASS)
    for kw in list(keywords) + list(DISTRESS_WORDS):
        k = _norm(kw)
        if not k:
            continue
        if k.isascii():
            if re.search(r"(?<!\w)" + re.escape(k) + r"(?!\w)", low):
                return kw
        elif re.search(r"(?<![\wऀ-ॿ])" + re.escape(k), low):
            return kw
    return ""


def urgent(text: str) -> bool:
    """Complaint or distress words (code only) — such a message is never held behind the block."""
    low = _norm(text)
    return bool(GRIEVANCE_RX.search(text or "")) or any(_norm(w) in low for w in DISTRESS_WORDS)


def state(rec: dict, now) -> dict:
    """His strike record (per user). Missing fields default to 0 / None; strikes older than
    oos_strike_expiry_hours are forgotten; an ended block is removed and the strikes reset."""
    o = rec.setdefault("oos", {})
    o.setdefault("strikes", 0)
    o.setdefault("last_at", None)
    if o.get("blocked_until") and now >= parse(o["blocked_until"]):
        o.pop("blocked_until")
        o["strikes"] = 0
    if o["strikes"] and o["last_at"] and \
            (now - parse(o["last_at"])).total_seconds() > S.get("oos_strike_expiry_hours", 24) * 3600:
        o["strikes"] = 0
    return o


def before_block() -> int:
    return int(S.get("oos_strikes_before_block", 3))


def locked(o: dict) -> bool:
    """One strike away from the block: the cheap similarity check comes first."""
    return o.get("strikes", 0) >= before_block() - 1


def blocked(o: dict) -> bool:
    """True while the block is on (call state() first, which removes an ended block)."""
    return bool(o.get("blocked_until"))


def start_block(o: dict, now) -> None:
    o["blocked_until"] = iso(now + timedelta(minutes=S.get("oos_block_minutes", 30)))


def threshold() -> float:
    return float(S.get("oos_relevance_threshold", 0.30))


def floor() -> float:
    """Locked: below this similarity no understand call is made (the message is out of scope at once)."""
    return float(S.get("oos_locked_skip_below", 0.12))


def similarity(kb, text: str):
    """Max cosine similarity to the approved knowledge (0..1), or None when it cannot be measured
    (no vectors, embedding failed or too slow) — None always counts as relevant."""
    if kb is None or not hasattr(kb, "relevance"):
        return None
    try:
        return kb.relevance(text)
    except Exception:
        return None


def fixed_line(strike_no: int) -> str:
    """FX-34 for strike 1, FX-35 (warning) before the block, FX-36 when the block starts."""
    if strike_no <= 1:
        return "FX-34"
    return "FX-35" if strike_no < before_block() else "FX-36"
