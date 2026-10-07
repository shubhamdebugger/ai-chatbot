"""AI-C18 Human Approval Queue — cases, callbacks and honest times.

Plain English:
- Grievances and unanswerable support questions become a case with a number.
- Hand-overs become callbacks with three states: requested → booked → completed.
- Tanya only promises a time inside the confirmed calling window; at night she
  names the next opening time, never an invented one (Architecture §12).
"""
from datetime import timedelta

from .policy import in_calling_hours
from .settings import S
from .timeutil import iso, hm

_DAY_WORDS = {
    "hinglish": ("aaj", "kal"),
    "english": ("today", "tomorrow"),
    "hindi": ("आज", "कल"),
}


def _clock(h, m, lang):
    """10:00 → '10 baje' / '10:00 AM' / '10 बजे'."""
    if lang == "english":
        hh = h % 12 or 12
        return f"{hh}:{m:02d} {'AM' if h < 12 else 'PM'}"
    part = "subah" if h < 12 else ("dopahar" if h < 16 else ("shaam" if h < 19 else "raat"))
    part_hi = {"subah": "सुबह", "dopahar": "दोपहर", "shaam": "शाम", "raat": "रात"}[part]
    hh = h % 12 or 12
    mins = f":{m:02d}" if m else ""
    if lang == "hindi":
        return f"{part_hi} {hh}{mins} बजे"
    return f"{part} {hh}{mins} baje"


def phrase_at(dt, now, lang):
    """'kal subah 10 baje' / 'tomorrow at 10:00 AM'."""
    today, tomorrow = _DAY_WORDS.get(lang, _DAY_WORDS["hinglish"])
    if dt.date() == now.date():
        d = today
    elif dt.date() == (now + timedelta(days=1)).date():
        d = tomorrow
    else:
        d = dt.strftime("%a %d-%b")
    if lang == "english":
        return f"{d} at {_clock(dt.hour, dt.minute, lang)}"
    return f"{d} {_clock(dt.hour, dt.minute, lang)}"


def next_opening(now):
    """The next time the calling window opens (today or tomorrow)."""
    sh, sm = hm(S.get("calling_window_start", "10:00"))
    opening = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
    if now >= opening:
        opening += timedelta(days=1)
    return opening


def when_phrase(now, lang):
    """What Tanya may promise for a person's contact time."""
    if in_calling_hours(now):
        return {"hinglish": "jaldi hi", "english": "shortly", "hindi": "जल्द ही"}.get(lang, "jaldi hi")
    return phrase_at(next_opening(now), now, lang)


def two_slots(now, lang):
    """Two call slots, whole hours inside the calling window, at least 1 hour from now."""
    sh, _ = hm(S.get("calling_window_start", "10:00"))
    eh, _ = hm(S.get("calling_window_end", "19:00"))
    slots = []
    day = now
    while len(slots) < 2:
        for h in range(sh + 1, eh):
            cand = day.replace(hour=h, minute=0, second=0, microsecond=0)
            if cand >= now + timedelta(hours=1) and len(slots) < 2:
                if not slots or (cand - slots[-1]).total_seconds() >= 3 * 3600:
                    slots.append(cand)
        day = (day + timedelta(days=1)).replace(hour=0, minute=0)
    return slots, [phrase_at(s, now, lang) for s in slots]


def open_case(rec, store, kind, text, now) -> str:
    case_no = store.next_case_no(now)
    rec["cases"].append({"case_no": case_no, "kind": kind, "status": "open", "at": iso(now), "text": text[:300]})
    return case_no


def request_callback(rec, kind, now, when_text, slot=None) -> dict:
    """kind: person | purchase | call_preference. State starts at 'requested'.
    The same request again in the SAME conversation within callback_merge_hours is the same callback (no duplicates).
    A handoff in another conversation, or a day later, is a new callback — before 07-Oct-2026 one old open callback
    (whose dashboard status Tanya never sees) swallowed every later handoff, so none of them reached the dashboard."""
    from datetime import datetime
    conv = str(rec.get("conversation_id") or "")
    window = float(S.get("callback_merge_hours", 12)) * 3600
    for cb in rec["callbacks"]:
        if cb["kind"] != kind or cb["state"] not in ("requested", "booked") or not conv or cb.get("conversation_id") != conv:
            continue
        try:
            age = (now - datetime.fromisoformat(cb["requested_at"])).total_seconds()
        except Exception:
            continue
        if 0 <= age <= window:
            return cb   # same chat, same request, still recent: one callback
    cb = {"id": f"CB-{rec['user_id']}-{len(rec['callbacks']) + 1}", "kind": kind, "state": "requested",
          "requested_at": iso(now), "when_text": when_text, "slot": slot, "conversation_id": conv}
    rec["callbacks"].append(cb)
    return cb
