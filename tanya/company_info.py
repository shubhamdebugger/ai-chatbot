"""Company / address / contact questions get the official website as the last line of the reply.

Plain English:
- Runs in n_guard AFTER the SEBI net, the AI check and the disclaimer, so it never touches checked text
  and still runs when the reply was replaced by FX-12 (FX-12 keeps its text and id; the line is appended).
- Added only for a company-INFORMATION request, never for a mere mention of the company. All three must hold:
  1. a target: an info topic (address / pata, office, location, contact, phone, email, registration, website)
     or the company itself together with an ask-about cue ("ke baare mein", "details", "kahan", "about");
  2. an asking form: a question word, a request verb, a "?", or a bare short phrase ("office address");
  3. no opinion / complaint: no negative words, no complaint words, no trading words in his message, and the
     understand call did not see a grievance, abuse or distress, or an upset mood.
  English, Hinglish and Hindi (Devanagari), common misspellings.
- Only on a plain answer turn (ANSWER, ANSWER_ONLY, ANSWER_SUPPORT, ANSWER_EDUCATION, SUPPORT_CASE): never on a
  grievance, refusal, small talk, greeting, hand-over, boundary or any other fixed-line turn.
- Never added twice: skipped if any line of this reply already has tglevels.com.
- Plain URL (no markdown/HTML): the CRM gets plain text; the dev console turns URLs into links.
- The addresses themselves come from the approved knowledge (content/kb/A02, A04), never from here.
"""
import re

WEBSITE_LINE = "For more info you can visit - https://tglevels.com/"

# 1a. Info topics: what a company-information request asks for.
_INFO = re.compile(
    r"\bad{1,2}re?s{1,2}e?\b|\bpin ?code\b|\b(?:ka|ki|apna|aapka|apka|aapki|apki) pata\b(?! (?:nahi|nhi|nahin|chal))"
    r"|\bof+ic+e?s?\b|\boffc\b|\bhead ?quarters?\b|\bbranch\b"
    r"|\bloca(?:tion|ted)\b|\blocaton\b"
    r"|\bc[ao]n?t[ae]c?t\w*\b|\b(?:phone|mobile|helpline|whatsapp) ?(?:no|number|num)\b|\bnumber\b|\be-?mail(?: id)?\b"
    r"|\bregist(?:ration|ered|er)\b|\breg(?:d|n)?\.? ?(?:no|number)\b|\bincorporat\w*\b|\bcin\b|\binh\d*\b"
    r"|\bhelp ?line\b|\bcustomer (?:care|support)\b|\bweb ?site\b"
    r"|\baap log\b.*\b(?:kaha+n?|kidhar)\b"
    r"|ऑफिस|ऑफ़िस|कार्यालय|एड्रेस|पता|लोकेशन|कॉन्टैक्ट|संपर्क|नंबर|ईमेल|रजिस्ट्रेशन|पंजीकरण|वेबसाइट",
    re.I)

# 1b. The company itself: on its own it is NOT a request ("gandi company hai"); it needs an ask-about cue.
_COMPANY = re.compile(
    r"\b(?:compan(?:y|i|ies)|compnay|comapny|compny|kampani|kumpani|kampny|tg ?levels?|firm)\b|कंपनी|कम्पनी",
    re.I)
_ABOUT = re.compile(
    r"\b(?:ba+re? ?m[ea]i?n?|bareme|about|details?|info(?:rmation)?|ja+nka+ri|kaha+n?|kidhar|where|"
    r"who (?:are|is)|what (?:is|does)|kya (?:hai|karti|karte|karta)|kaun (?:hai|si hai)|kaisi company)\b"
    r"|बारे|जानकारी|कहाँ|कहां|किधर|क्या करती",
    re.I)

# 2. Asking form: a question or a request, not a statement.
_ASKING = re.compile(
    r"\?|\b(?:kya|kaha+n?|kidhar|kaise|kaun|kaunsa|kab|kitne|where|what|which|how|who|when|is there|do you|can i"
    r"|bata(?:o|iye|ye|do|na|ein|en)?|bhej(?:o|iye|do|na)?|dijiye|dedo|de do|chahiye|chaiye|share|send|give|tell|need"
    r"|want|please|pls|plz|milega|milegi|mil sakta|mil sakti|janna|jaanna|samjhao)\b"
    r"|क्या|कहाँ|कहां|किधर|कैसे|बताओ|बताइए|बताएं|भेजो|भेजिए|चाहिए|दीजिए",
    re.I)
_SHORT = 4   # a bare phrase of up to 4 words ("office address", "contact details") also counts as asking

# 3. Opinion / complaint / trading: a mention of the company, not a request for its information.
_NEGATIVE = re.compile(
    r"\b(?:hate|bad|worst|useless|pathetic|terrible|horrible|rubbish|angry|upset|disappointed|cheaters?|liars?"
    r"|bekar|bekaar|bakwas|bakwaas|ghatiya|gandi|gande|gandu|ganda|faltu|kharab|bura|buri|naraz|naraaz|gussa"
    r"|chor|chu?tiy\w*|bewakoof|loot\w*|lutere|dhokebaaz|dhokhebaaz|nafrat|sharm|ek number ki|ek number ka)\b"
    r"|\b(?:company|aap|aapne|tumne|you|your team) ne\b|\bmera paisa\b|\bmere paise\b"
    r"|बेकार|घटिया|बकवास|नफरत|नाराज|नाराज़|गुस्सा|चोर|लूट|गंदी|गंदा|खराब|बुरी",
    re.I)
_GRIEVANCE = re.compile(
    r"\b(?:complaint?s?|complain\w*|grievance\w*|shikayat\w*|refund\w*|fraud|scam\w*|cheat\w*|dhok?ha"
    r"|paise? wapas|money back|escalat\w*|not working|nahi? ho raha|kaam nahi?|band ho|problem|issue)\b"
    r"|शिकायत|धोखा|रिफंड",
    re.I)
_TRADING = re.compile(
    r"\b(?:nifty|bank ?nifty|fin ?nifty|sensex|stocks?|share price|options?|trading|trade|intraday|f&o|fno"
    r"|strike|target|stop ?loss|market|ipo|crypto|futures?|level kya|shares?(?! (?:karo|kar do|kariye|kijiye|karein|karna))|buy|sell|invest\w*|portfolio"
    r"|demat|chart|profit|loss|dividend|mutual funds?)\b",
    re.I)
# the understand call's own reading of his message: any of these means opinion / complaint, not a request
_UPSET_LABELS = ("grievance", "abuse", "distress", "not_helpful")

# Only turns where she answers his question; everything else (LOG_GRIEVANCE, REFUSE_AND_TEACH, small talk,
# greeting, hand-overs, boundaries, prices, fixed gates ...) never gets the line.
_ANSWER_ACTIONS = {"ANSWER", "ANSWER_ONLY", "ANSWER_SUPPORT", "ANSWER_EDUCATION", "SUPPORT_CASE"}


def _upset(labels: dict | None) -> bool:
    if not labels:
        return False
    on = labels.get("labels", {})
    return labels.get("mood") == "upset" or any(on.get(k, {}).get("on") for k in _UPSET_LABELS)


def is_company_query(text: str, labels: dict | None = None) -> bool:
    """True when he asks FOR company information ('company ka pata batao', 'office kidhar hai',
    'company kaha located hai'); False when he only mentions or judges it ('gandi company hai')."""
    t = (text or "").lower()
    if _NEGATIVE.search(t) or _GRIEVANCE.search(t) or _TRADING.search(t) or _upset(labels):
        return False
    target = _INFO.search(t) or (_COMPANY.search(t) and _ABOUT.search(t))
    asking = _ASKING.search(t) or len(re.findall(r"\w+", t)) <= _SHORT
    return bool(target and asking)


def add_website_line(bubbles: list, text: str, action: str = "", labels: dict | None = None) -> bool:
    """Append the website line to the answer bubble (her AI reply, or FX-12 if the guard replaced it; else the last
    bubble) when his message asks about the company. One combined message, no extra bubble. Returns True if added."""
    if action not in _ANSWER_ACTIONS or not bubbles or not is_company_query(text, labels):
        return False
    if any("tglevels.com" in (b.get("text") or "").lower() for b in bubbles):
        return False
    answer = [b for b in bubbles if b.get("kind") == "ai" or b.get("blocked_text") is not None]
    b = answer[-1] if answer else bubbles[-1]
    b["text"] = (b.get("text") or "").rstrip() + "\n" + WEBSITE_LINE
    return True
