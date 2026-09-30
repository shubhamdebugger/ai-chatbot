"""AI-C23 Prompt Injection Detector + input masking (part of AI-C01 Gateway duties).

Plain English: before any AI sees a message, private numbers are hidden
(phone, email, PAN, card, Aadhaar, bank account, OTP) and obvious attempts
to trick the AI ("ignore your instructions") are caught.
Nothing here calls an AI — it is plain rules, so it is fast and testable.
"""
import re

# Order matters: longer / more specific patterns first.
_MASKS = [
    ("EMAIL", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")),
    ("PAN", re.compile(r"\b[A-Z]{5}[0-9]{4}[A-Z]\b", re.I)),
    ("CARD", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
    ("AADHAAR", re.compile(r"\b\d{4}[ -]\d{4}[ -]\d{4}\b")),
    ("PHONE", re.compile(r"(?:\+91[ -]?|\b0)?\b[6-9]\d{9}\b")),
    ("OTP", re.compile(r"\b(?:otp|one[ -]time[ -]password)\D{0,12}(\d{4,8})\b", re.I)),
    ("BANK", re.compile(r"\b\d{9,18}\b")),
]

_INJECTION = [
    r"ignore (all|your|the|previous|above)[^.]{0,30}(instruction|rule|prompt)",
    r"(system|developer) prompt",
    r"you are now",
    r"act as (a|an|my)",
    r"pretend (to be|you are)",
    r"forget (your|all|the) (rules|instructions)",
    r"jailbreak",
    r"developer mode",
    r"(pichle|purane) (instructions|rules) (bhool|ignore)",
    r"apne rules (bhool|ignore|todo)",
]
_INJ = re.compile("|".join(_INJECTION), re.I)


def mask(text: str):
    """Return (masked_text, list_of_kinds_masked)."""
    found = []
    out = text
    for kind, rx in _MASKS:
        if kind == "OTP":
            def _otp(m):
                return m.group(0).replace(m.group(1), "[OTP]")
            new = rx.sub(_otp, out)
        else:
            new = rx.sub(f"[{kind}]", out)
        if new != out:
            found.append(kind)
        out = new
    return out, found


def injection(text: str) -> bool:
    """True when the message tries to change the AI's instructions."""
    return bool(_INJ.search(text or ""))
