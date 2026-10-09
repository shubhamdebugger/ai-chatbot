"""Trade summaries — "kal ke trades kaise gaye?", "aaj loss ho gaya" — answered from the RA's sheet.

Plain English:
- The RA uploads the 5-day sheet of each group (FREE GROUP 'A' / 'B' / PAID) on the RA panel.
- pwa-node-backend knows the customer's group and which days he may see (trial: from his first
  trial day; paid: from the day he became paid; expired: none) and gives Tanya ONLY those days:
  GET TRADE_SUMMARY_URL?phone=…&date=…   (header x-tanya-key = TANYA_API_KEY)
- Tanya reports the published past trades as they are (strike, entry, SL, target, exit, points, ₹),
  honestly on a loss day, never as advice for today. The guard checks every number against the sheet.
"""
import re
import time
from datetime import timedelta

from .settings import S

CACHE_SECS = 60
_cache = {}

# what the backend answers, when there is no day to report → the fixed line Tanya sends instead
STATUS_LINE = {
    "not_published": "FX-44",     # today's sheet not uploaded yet
    "before_start": "FX-45",      # before his trial / paid start
    "no_summary": "FX-46",        # a past day without a sheet: holiday / no trades
    "future": "FX-47",
    "expired": "FX-48",
    "unknown_user": "FX-49",
    "unavailable": "FX-49",       # lookup failed: never guess numbers
}
DISCLAIMER_LINE = "FX-50"
# he names no day → ask which day he traded (never list the days we have)
ASK_DAY_LOSS, ASK_DAY = "FX-51", "FX-52"
LOSS_RX = re.compile(r"\b(loss(?:es)?|lose|losing|lost|ghata|ghaata|nuksan|nuksaan)\b|लॉस|घाटा|नुकसान|नुक़सान", re.I)

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_WEEKDAYS = [
    ("monday", "mon", "somvar", "सोमवार"), ("tuesday", "tue", "mangalvar", "मंगलवार"),
    ("wednesday", "wed", "budhvar", "बुधवार"), ("thursday", "thu", "guruvar", "गुरुवार", "brihaspativar"),
    ("friday", "fri", "shukravar", "शुक्रवार"), ("saturday", "sat", "shanivar", "शनिवार"),
    ("sunday", "sun", "ravivar", "रविवार", "itvar"),
]


def _prev_weekday(d, n=1):
    """n trading days (Mon–Fri) back from d."""
    while n:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def resolve_date(text: str, now) -> str:
    """The day he asks about: 'YYYY-MM-DD', or 'latest' when he names none.
    "kal" in a results question means the last trading day before today (Monday → Friday)."""
    t = (text or "").lower()
    today = now.date()
    m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s*[-/ ]?\s*(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b", t)
    if m:
        return _in_year(today, _MONTHS[m.group(2)], int(m.group(1)))
    m = re.search(r"\b(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?\b", t)
    if m:
        y = int(m.group(3)) if m.group(3) else None
        y = (2000 + y if y and y < 100 else y)
        return _in_year(today, int(m.group(2)), int(m.group(1)), y)
    if re.search(r"\b(parso|day before yesterday)\b|परसों", t):
        return _prev_weekday(today, 2).isoformat()
    if re.search(r"\b(kal|yesterday|kl|pichle din|previous day|last day)\b|कल", t):
        return _prev_weekday(today).isoformat()
    if re.search(r"\b(aaj|aj|today|abhi ke)\b|आज", t):
        return today.isoformat()
    for i, names in enumerate(_WEEKDAYS):
        if any(re.search(rf"(?<!\w){re.escape(n)}(?!\w)", t) for n in names):
            back = (today.weekday() - i) % 7
            return (today - timedelta(days=back)).isoformat()
    return "latest"


def _in_year(today, month, day, year=None):
    from datetime import date
    try:
        d = date(year or today.year, month, day)
    except ValueError:
        return "latest"
    if not year and d > today + timedelta(days=1):    # "28 Dec" asked in early January = last year
        d = date(today.year - 1, month, day)
    return d.isoformat()


def lookup(phone: str, date: str) -> dict:
    """The backend's answer for this customer and day; {'status': 'unavailable'} on any failure."""
    url, key = S.env("TRADE_SUMMARY_URL", ""), S.env("TANYA_API_KEY", "")
    if not url or not key or len(re.sub(r"\D", "", phone or "")) < 10:
        return {"status": "unavailable" if url else "unknown_user"}
    ck = (phone[-10:], date)
    hit = _cache.get(ck)
    if hit and time.time() - hit[0] < CACHE_SECS:
        return hit[1]
    try:
        import httpx
        r = httpx.get(url, params={"phone": phone[-10:], "date": date}, headers={"x-tanya-key": key}, timeout=4)
        r.raise_for_status()
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError("not ok")
    except Exception as e:
        print(f"[trade_summary] lookup failed {type(e).__name__}: {str(e)[:120]}", flush=True)
        return {"status": "unavailable"}
    _cache[ck] = (time.time(), data)
    return data


GROUP_NAME = {"trial_a": "Free Group A", "trial_b": "Free Group B", "paid": "Paid Group"}


def day_label(iso: str) -> str:
    from datetime import date
    d = date.fromisoformat(iso)
    return f"{d.day} {d.strftime('%b')} ({d.strftime('%a')})"


def _num(v):
    return ("" if v is None else (str(int(v)) if float(v).is_integer() else str(v)))


def rupees(v) -> str:
    v = float(v)
    s = f"₹{abs(v):,.0f}"
    return f"−{s}" if v < 0 else s


def as_knowledge(data: dict) -> dict:
    """The published day as one APPROVED KNOWLEDGE item for the reply and the guard."""
    day, g = data["day"], data.get("group", "")
    lines = [f"APPROVED TRADE SUMMARY — {GROUP_NAME.get(g, g)}, {day_label(day['date'])}. Past results, published by "
             f"the research team. Report them exactly; never as advice for today or later.",
             f"Day total: {len(day['trades'])} trades, {_num(day['total_points'])} points, {rupees(day['total_amount'])} "
             f"({'a loss day' if day['total_amount'] < 0 else 'a profit day' if day['total_amount'] > 0 else 'flat'}).",
             "Trades (sr | index | strike | action | qty | entry | SL | target | exit | high | points | ₹):"]
    for t in day["trades"]:
        lines.append(" | ".join([_num(t["sr_no"]), t["index_name"], t["strike"], t["action"], _num(t["quantity"]),
                                 _num(t["entry"]), _num(t["sl"]), _num(t["target"]), _num(t["exit"]), _num(t["high"]),
                                 _num(t["points"]), rupees(t["amount"])]))
    return {"chunk_id": f"TS-{g}-{day['date']}", "doc_id": "TRADE-SUMMARY", "title": f"Trade summary {day['date']}",
            "category": "TradeSummary", "status": "APPROVED", "version": "", "score": 1.0, "text": "\n".join(lines),
            "never_say": "a guarantee, future returns, advice to take a trade today, 'no loss' on a loss day"}


def line_values(data: dict, lang: str) -> dict:
    """Values for FX-44…FX-49: the day asked and his first day (the other days are never listed)."""
    win = data.get("window") or {}
    return {"date": day_label(data["date"]) if data.get("date") else "",
            "start": day_label(win["from"]) if win.get("from") else ""}


def report_numbers(text: str) -> set:
    """Every number in the approved summary (strikes, prices, points, ₹) — the guard allows only these."""
    return {n.replace(",", "") for n in re.findall(r"\d[\d,]*(?:\.\d+)?", text or "")}


def for_voice(store, adapter, ctx: dict, date_text: str, now) -> dict:
    """Voice tool get_trade_summary: the same lookup and rules as chat. The agent reads 'say' when there
    is no day to report, and reports a published day exactly (past results, then the disclaimer)."""
    from .content_pack import PACK
    phone = ""
    if ctx.get("sb_user_id") and adapter is not None:
        try:
            from .access import phone_for
            phone = phone_for(adapter, store, ctx["sb_user_id"])
        except Exception:
            phone = ""
    date = resolve_date(date_text or "", now)
    if date == "latest":                       # no day named → ask which day he traded, never list ours
        return {"found": False, "status": "ask_day", "say": PACK.fixed(ASK_DAY, "hinglish")}
    data = lookup(phone, date)
    status = data.get("status", "unavailable")
    if status != "ok" or not data.get("day"):
        line = STATUS_LINE.get(status, "FX-49")
        return {"found": False, "status": status,
                "say": PACK.fixed(line, "hinglish", **line_values(data, "hinglish") if data.get("date") else {})}
    day = data["day"]
    return {"found": True, "status": "ok", "group": GROUP_NAME.get(data.get("group"), data.get("group")),
            "date": day_label(day["date"]), "trades": len(day["trades"]),
            "total_points": day["total_points"], "total_rupees": rupees(day["total_amount"]),
            "result": "loss day" if day["total_amount"] < 0 else "profit day" if day["total_amount"] > 0 else "flat",
            "trade_list": [f"{t['strike']} {t['action']} entry {_num(t['entry'])} SL {_num(t['sl'])} target {_num(t['target'])} "
                           f"exit {_num(t['exit'])}: {_num(t['points'])} points, {rupees(t['amount'])}" for t in day["trades"]],
            "rules": "Past published results only; report exactly, say a loss plainly, no advice for today.",
            "disclaimer": PACK.fixed(DISCLAIMER_LINE, "hinglish")}
