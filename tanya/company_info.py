"""Company / address / contact questions get the official website as the last line of the reply.

Plain English:
- Runs in n_guard AFTER the SEBI net, the AI check and the disclaimer, so it never touches checked text
  and still runs when the reply was replaced by FX-12 (FX-12 keeps its text and id; the line is appended).
- Decided from HIS message only (English, Hinglish, common misspellings). Trading / market questions never match.
- Never added twice: skipped if any line of this reply already has tglevels.com.
- Plain URL (no markdown/HTML): the CRM gets plain text; the dev console turns URLs into links.
- The addresses themselves come from the approved knowledge (content/kb/A02, A04), never from here.
"""
import re

WEBSITE_LINE = "For more info you can visit - https://tglevels.com/"

_ASKS = re.compile(
    r"\b(?:compan(?:y|i|ies)|compnay|comapny|compny|kampani|kumpani|kampny)\b"
    r"|\bad{1,2}re?s{1,2}e?\b|\bpin ?code\b"
    r"|\bof+ic+e?\b|\boffc\b|\bhead ?quarters?\b|\bregistered office\b"
    r"|\bloca?tion\b|\blocaton\b"
    r"|\bc[ao]n?t[ae]c?t\w*\b|\bphone (?:no|number)\b|\bmobile (?:no|number)\b|\bemail(?: id)?\b"
    r"|\bhelp ?line\b|\bcustomer (?:care|support)\b|\bweb ?site\b"
    r"|\btg ?levels?\b.*\b(?:ba+re? ?m[ea]i?n?|bareme|about)\b"
    r"|\baap log\b.*\bkaha+n?\b"
    r"|कंपनी|कम्पनी|ऑफिस|ऑफ़िस|एड्रेस|कॉन्टैक्ट",
    re.I)

_TRADING = re.compile(
    r"\b(?:nifty|bank ?nifty|fin ?nifty|sensex|stocks?|share price|options?|trading|trade|intraday|f&o|fno"
    r"|strike|target|stop ?loss|market|ipo|crypto|futures?|level kya)\b",
    re.I)

_SKIP_ACTIONS = {"REFUSE_AND_TEACH", "PAUSE_SELLING", "BOUNDARY_ABUSE", "END_CHAT_ABUSE"}


def is_company_query(text: str) -> bool:
    """True for 'company k bareme batao', 'address kya hai', 'office kaha hai', 'adress bhejo' and the like."""
    t = (text or "").lower()
    return bool(_ASKS.search(t)) and not _TRADING.search(t)


def add_website_line(bubbles: list, text: str, action: str = "") -> bool:
    """Append the website line to the answer bubble (her AI reply, or FX-12 if the guard replaced it; else the last
    bubble) when his message asks about the company. One combined message, no extra bubble. Returns True if added."""
    if action in _SKIP_ACTIONS or not bubbles or not is_company_query(text):
        return False
    if any("tglevels.com" in (b.get("text") or "").lower() for b in bubbles):
        return False
    answer = [b for b in bubbles if b.get("kind") == "ai" or b.get("blocked_text") is not None]
    b = answer[-1] if answer else bubbles[-1]
    b["text"] = (b.get("text") or "").rstrip() + "\n" + WEBSITE_LINE
    return True
