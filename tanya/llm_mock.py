"""Mock provider — rule-based stand-in for the AI, used with PROVIDER=mock.

Plain English: lets the whole system run and be tested without an API key.
The understand step is done by keyword rules; replies are simple templates
that name the action taken. Real quality needs a real provider.
"""
import json
import re

_RULES = {
    "trade_advice_seeking": r"(upar jayega|neeche jayega|kal nifty|nifty kal|kaunsa stock|which stock|kaun sa strike|kaunsa strike|entry kahan|target kya|sl kya|buy karu|buy karoon|sell karu|le lu kya|lelu kya|call do|tip do|should i buy|should i sell|will .* go up|jayega kya)",
    "distress": r"(loss ho gaya|loss ho chuka|doob gay|doob gaye|barbaad|sab chala gaya|bahut loss|lost money|lost everything|nuksaan ho|pareshan hoon|depressed)",
    "grievance": r"(refund|complaint|fraud|cheat|dhokha|paise wapas|late aaye|late aate|late alert|scam)",
    "wants_person": r"(insaan se|human|agent se|kisi se baat|real person se baat|team se baat|talk to (a )?person)",
    "purchase_intent": r"(plan lena hai|plan le leta|subscribe karna|payment kaise|kharidna hai|buy the plan|want to buy|join karna hai|le leta hoon)",
    "interest": r"(kitne ka|price|fees|kitna hai|cost|plan mein kya|what is included|kya milega|₹)",
    "timing_objection": r"(salary|next week|agle mahine|next month|baad mein lunga)",
    "prefers_call": r"(call karna|call karo|call kar|time nahi|phone pe|phone par baat|call me)",
    "abuse": r"(idiot|stupid|bewakoof|bakwas|pagal|shut up)",
    "flirting": r"(single ho|single hai|date pe|love you|shaadi|photo bhejo|girlfriend|number do)",
    "asks_if_ai": r"(bot ho|robot|real person ho|insaan ho|are you human|are you a bot|ai ho)",
    "not_helpful": r"(same bol|samajh nahi aaya|not helpful|bekaar|kuch nahi samjha)",
    "refers_to_past": r"(kal kya|pichli baar|last time|maine kaha tha|kal bataya)",
    "support_question": r"(\bapp\b|notification|login|access|install|password|payment fail|alert nahi|locked)",
    "education_question": r"(kya hota|kya hai\?|kaise kaam|what is|samjhao|samjha do|explain|stop[- ]?loss|option|premium|expiry|risk|position size|overtrad|ce pe|example)",
    "small_talk": r"(kya kar rahi|kaise ho|how are you|chai|good night|good morning|hello|^hi\b|^hey\b|what are you doing|thank)",
}


def _label(text):
    t = text.lower()
    labels = {}
    for k, rx in _RULES.items():
        m = re.search(rx, t)
        labels[k] = {"on": bool(m), "evidence": m.group(0) if m else "", "confidence": 0.9 if m else 0.0}
    if labels["purchase_intent"]["on"] and re.search(r"(nahi|nahin|not)", t) and "le leta" not in t:
        labels["purchase_intent"] = {"on": False, "evidence": "", "confidence": 0.0}
    facts = []
    m = re.search(r"(₹?\s?\d+(?:[.,]\d+)?\s*(?:lakh|lac|hazaar|k|000)?\s*(?:ka|ki)?\s*loss)", t)
    if m:
        facts.append({"field": "past_loss", "value": m.group(1).strip(), "his_words": text, "stated": True, "confidence": 0.9})
    if re.search(r"(beginner|naya hoon|new to trading|kabhi trade nahi)", t):
        facts.append({"field": "experience", "value": "beginner", "his_words": text, "stated": True, "confidence": 0.9})
    if re.search(r"(options|option trader|nifty options)", t) and "kya" not in t:
        facts.append({"field": "segment", "value": "options", "his_words": text, "stated": True, "confidence": 0.8})
    if re.search(r"(housewife|homemaker|ghar sambhalti)", t):
        facts.append({"field": "occupation", "value": "homemaker", "his_words": text, "stated": True, "confidence": 0.9})
    if re.search(r"(technical english|simple hinglish|hindi mein samjhao)", t):
        facts.append({"field": "language_pref", "value": "simple Hinglish", "his_words": text, "stated": True, "confidence": 0.9})
    if re.search(r"(abhi plan nahi|abhi nahi lena|not buying now)", t):
        facts.append({"field": "decision", "value": "not buying now", "his_words": text, "stated": True, "confidence": 0.9})
    lang = "hindi" if re.search(r"[ऀ-ॿ]", text) else (
        "english" if not re.search(r"\b(hai|kya|nahi|kaise|mein|mujhe|aap|karna|ho|hoon|kal|aaj|toh|ye|koi|kuch|raha|rahi)\b", t) else "hinglish")
    neg = "abhi plan nahi" if "abhi plan nahi" in t or "abhi nahi" in t else ""
    return {"language": lang, "labels": labels, "topics": [], "negation": neg, "new_facts": facts,
            "mood": "upset" if labels["distress"]["on"] or labels["grievance"]["on"] else "neutral",
            "complexity": "simple"}


def _reply(system, last):
    action = re.search(r"ACTION:\s*([A-Z_]+)", system)
    action = action.group(1) if action else "ANSWER"
    k = re.search(r"APPROVED KNOWLEDGE:\n(.*?)(?:\n\n[A-Z ]+:|\Z)", system, flags=re.S)
    snippet = ""
    if k:
        lines = [l for l in k.group(1).splitlines() if l.strip() and not l.startswith("[")]
        snippet = " ".join(lines[:2])[:220]
    texts = {
        "ANSWER_SMALL_TALK": "Abhi toh aapse baat kar rahi hoon 😊 Aap bataiye, aaj ka din kaisa ja raha hai?",
        "PAUSE_SELLING": "Ye sunkar bura laga. Aise din bahut bhaari lagte hain. Jab aap ready hon, hum dekhenge ki risk ko limit kaise karte hain.",
        "REFUSE_AND_TEACH": "Aise sawaal ko khud padhne ke liye ek checklist yaad rakhiye: Logic, Risk aur Exit. Kya main ise ek simple example se samjhaun?",
        "GUIDE_NEXT_STEP": "Achha laga baat karke 🙂 Chaliye, aaj ka ek chhota step lete hain — Logic, Risk aur Exit wali checklist dekhein?",
        "BOUNDARY_LIGHT": "Main ek AI assistant hoon, toh ye sawal mere liye bante hi nahi 🙂 Aaj kya seekhna chahenge?",
    }
    reply = texts.get(action) or (f"[MOCK {action}] " + (snippet or "Main aapki madad ke liye yahin hoon."))
    asked = ""
    m = re.search(r"ASK ONE QUESTION to learn his '([a-z_]+)'", system)
    if m:
        asked = m.group(1)
        reply += {"experience": " Aap kitne samay se trade kar rahe hain?",
                  "segment": " Aap kis cheez mein trade karte hain — options, equity ya kuch aur?",
                  "main_pain": " Trading mein aapko sabse zyada kya mushkil lagta hai?",
                  "time_available": " Market ke time aap screen dekh paate hain?",
                  "goal": " Aap trading se kya haasil karna chahte hain?"}.get(asked, "")
    return json.dumps({"reply": reply, "last_promise": "", "asked_field": asked}, ensure_ascii=False)


def mock_complete(system, messages):
    last = messages[-1]["content"] if messages else ""
    if "TASK: UNDERSTAND" in system:
        text = last.split("LAST CUSTOMER MESSAGE:\n", 1)[-1]
        return json.dumps(_label(text), ensure_ascii=False)
    if "TASK: CHECK" in system:
        bad = re.search(r"(guaranteed profit|sure shot call|buy nifty|target \d+)", last.lower())
        return json.dumps({"pass": not bad, "problems": [bad.group(0)] if bad else []})
    if "TASK: NOTE" in system:
        return json.dumps({"note": ["(mock) session summary line 1", "(mock) line 2", "(mock) line 3"],
                           "last_promise": ""})
    if "TASK: AUDIT" in system:
        return json.dumps({"findings": []})
    return _reply(system, last)
