"""AI-C14 Guardrail & Compliance — SEBI guard layers 2 and 3 on Tanya's written reply.

Plain English:
- Layer 1 (route before writing) is in decider.py: trade questions go to REFUSE_AND_TEACH.
- Layer 2 (this file, ai_check): a separate small AI call checks her reply against a strict list.
- Layer 3 (this file, net): plain rules — forbidden words and patterns, every ₹ figure must
  be an approved price or a figure he said himself, the emoji rule, and the fixed disclaimer.
Fixed lines are pre-approved and are not checked again.
"""
import json
import re

from .content_pack import PACK
from .settings import S

# ---------------------------------------------------------------- ₹ figures
_AMT = [
    re.compile(r"(?:₹|\brs\.?|\binr)\s*([\d,]+(?:\.\d+)?)\s*(lakh|lac|crore|cr|k|hazaar|hazar|thousand)?\b", re.I),
    re.compile(r"\b([\d,]+(?:\.\d+)?)\s*(lakh|lac|crore|cr|hazaar|hazar|thousand)?\s*(?:rupaye|rupees|rupay|rupiya|रुपये)", re.I),
    re.compile(r"\b(\d+(?:\.\d+)?)\s*(lakh|lac|crore)\b", re.I),
]
_MULT = {"lakh": 100_000, "lac": 100_000, "crore": 10_000_000, "cr": 10_000_000,
         "k": 1_000, "hazaar": 1_000, "hazar": 1_000, "thousand": 1_000}


def amounts(text: str) -> set:
    """Every rupee amount mentioned, as whole rupees. '₹3 lakh' → 300000, '₹15,000' → 15000."""
    out = set()
    for rx in _AMT:
        for m in rx.finditer(text or ""):
            num = m.group(1).replace(",", "")
            try:
                v = float(num)
            except ValueError:
                continue
            unit = (m.group(2) or "").lower() if m.lastindex and m.lastindex >= 2 else ""
            v *= _MULT.get(unit, 1)
            out.add(int(round(v)))
    return out


# ---------------------------------------------------------------- emoji rule
_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿️‍]")


def emoji_rule(text: str, allowed: bool) -> str:
    """At most one emoji; none when not allowed (distress, grievance, refusal, money, hand-off)."""
    found = [m for m in _EMOJI.finditer(text) if m.group(0) not in ("️", "‍")]
    if not found:
        return text
    keep_first = allowed
    out, seen = [], False
    for ch in text:
        if _EMOJI.match(ch):
            if ch in ("️", "‍"):
                if keep_first and seen and out and _EMOJI.match(out[-1]):
                    out.append(ch)
                continue
            if keep_first and not seen:
                out.append(ch)
                seen = True
            continue
        out.append(ch)
    return re.sub(r"[ \t]{2,}", " ", "".join(out)).strip()


# ---------------------------------------------------------------- layer 3 net
def _sentences(text):
    return [s for s in re.split(r"(?<=[.!?।])\s+|\n", text or "") if s.strip()]


# Hard rules a trade-summary answer may hit while reporting the published sheet (report mode):
# a strike / entry / SL / target passes only when its number is in the sheet; "BUY NIFTY …" passes because
# it describes a past published trade (the report check fails any advice for today). H7–H10 always apply.
REPORT_LEVEL_RULES = {"H1", "H4", "H5", "H6"}
REPORT_ACTION_RULES = {"H2", "H3"}


def net(reply: str, allowed_amounts: set, report_numbers: set = None):
    """Returns (blocked: bool, hits: list, warnings: list). report_numbers: numbers of the published trade
    summary when the reply reports it (else None = the normal rules)."""
    lists = PACK.guard
    hits, warns = [], []
    denials = [d.lower() for d in lists.get("denial_words", [])]
    for sent in _sentences(reply):
        low = sent.lower().replace("\u2019", "'").replace("\u2018", "'")   # "doesn’t" (curly) = "doesn't"
        for p in lists.get("promise", []):
            if re.search(p["pattern"], low, re.I):
                if not any(d in low for d in denials):
                    hits.append(f"{p['id']} {p['why']}: \"{sent.strip()[:80]}\"")
        for p in lists.get("hard", []):
            m = re.search(p["pattern"], low, re.I)
            if not m:
                continue
            if report_numbers is not None:
                if p["id"] in REPORT_ACTION_RULES:
                    continue
                if p["id"] in REPORT_LEVEL_RULES:
                    nums = [x.replace(",", "") for x in re.findall(r"\d[\d,]*(?:\.\d+)?", m.group(0))]
                    if nums and all(x in report_numbers for x in nums):
                        continue
            hits.append(f"{p['id']} {p['why']}: \"{sent.strip()[:80]}\"")
        for p in lists.get("warn", []):
            if re.search(p["pattern"], low, re.I):
                warns.append(f"{p['id']} {p['why']}")
    bad_amounts = sorted(a for a in amounts(reply) if a not in allowed_amounts)
    if bad_amounts:
        hits.append("AMT ₹ figure not in pricing or his words: " + ", ".join(f"₹{a:,}" for a in bad_amounts))
    return bool(hits), hits, warns


# ---------------------------------------------------------------- layer 2 AI check
CHECK_SYSTEM = """TASK: CHECK
You are a strict SEBI compliance checker for replies written by an AI assistant of a SEBI-registered Research Analyst.
FAIL the reply if it contains ANY of:
1. A view or prediction on market direction (up/down) for any index, stock or instrument.
2. An entry, exit, target, stop-loss level or strike for a specific instrument, or a named stock/index to buy or sell.
3. A promise or implication of profit, returns, income, accuracy, success rate, or loss recovery (a clear denial is fine).
4. A price, discount, plan content or benefit that is NOT in the APPROVED TEXT given below.
5. An invented human life for the assistant (eating, drinking, travelling, family, age, tiredness).
6. Addressing the user as 'tum' or 'tu'.
7. Superlatives about the company ('best', 'No. 1', 'top', 'leading') or fake urgency.
Teaching general concepts (what a stop-loss is, how to read an alert, risk management) is ALLOWED.
Explaining that there is no guarantee is ALLOWED.
Return ONLY JSON: {"pass": true|false, "problems": ["short reason", ...]}"""
# Trade summary answers: reporting the RA's published PAST trades is the job; everything else stays strict.
CHECK_SYSTEM_REPORT = """TASK: CHECK
You are a strict SEBI compliance checker for a reply that REPORTS the past trades a SEBI-registered Research Analyst
already published (the APPROVED TRADE SUMMARY below).
ALLOWED: stating the published past trades and day totals exactly as in the summary — index, strike, buy/sell,
entry, stop-loss, target, exit, high, points, rupee amounts — in past tense; saying honestly that a day was a loss;
acknowledging what the CUSTOMER MESSAGE says about his own trading (e.g. that he made a loss) — that is his word, not a
claim; asking how he traded it (entry, stop-loss, quantity); general lessons (risk, stop-loss, position size).
FAIL the reply if it contains ANY of:
1. A trade, level, number or total that is NOT in the APPROVED TRADE SUMMARY, or one that is changed.
2. Advice or a view for today or the future: what to buy/sell now, whether to take or hold a trade, market direction.
3. A promise or implication of future profit, returns, accuracy or loss recovery (a clear denial is fine).
4. Hiding or spinning a loss: calling a loss day profitable, "we are not in loss" when the day total is negative.
5. A price, discount, plan or benefit not in the APPROVED TEXT.
6. An invented human life for the assistant, or addressing the user as 'tum' / 'tu'.
7. Superlatives about the company ('best', 'No. 1', 'top', 'leading') or fake urgency.
Return ONLY JSON: {"pass": true|false, "problems": ["short reason", ...]}"""
CHECK_SCHEMA = {"type": "object", "additionalProperties": False,
                "properties": {"pass": {"type": "boolean"}, "problems": {"type": "array", "items": {"type": "string"}}},
                "required": ["pass", "problems"]}


def ai_check(llm, reply: str, approved_text: str, report: bool = False, customer: str = ""):
    """Layer 2. Returns (passed, problems, LLMResult). A failed check call = not passed (safe side).
    report=True: the reply reports the published trade summary (CHECK_SYSTEM_REPORT); customer = his message,
    so acknowledging his own words ("you made a loss") is not taken for an invented claim."""
    msg = f"APPROVED TEXT (prices, plans, knowledge the reply may use):\n{approved_text or '(none)'}\n\nREPLY TO CHECK:\n{reply}"
    if report and customer:
        msg = f"CUSTOMER MESSAGE (context only, not approved figures):\n{customer[:500]}\n\n" + msg
    res = llm.call("check", "fast", CHECK_SYSTEM_REPORT if report else CHECK_SYSTEM, [{"role": "user", "content": msg}], json_mode=True,
                   temperature=S.get("temperature_check", 0.0), timeout=S.get("timeout_check_seconds", 20),
                   max_tokens=400, schema=CHECK_SCHEMA)
    if not res.ok:
        return False, [f"check call failed: {res.error}"], res
    passed = bool(res.data.get("pass"))
    return passed, [str(p) for p in res.data.get("problems", [])][:5], res


def approved_text_for(hits, action):
    """What the checker may accept as approved: knowledge used + pricing (for price answers)."""
    parts = [h["text"] for h in hits or []]
    parts.append(json.dumps([{k: r[k] for k in ("plan_name", "duration", "price_inr", "inclusions")}
                              for r in PACK.pricing], ensure_ascii=False))
    return "\n".join(parts)
