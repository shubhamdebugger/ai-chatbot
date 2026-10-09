"""DPDP consent from the app — "has he agreed to the app's terms?"

Plain English:
- The app stores every acceptance in terms_acceptances. pwa-node-backend answers for a phone:
  GET CONSENT_URL?phone=…   (header x-tanya-key = TANYA_API_KEY)
  agreed = a terms_acceptances row, or a completed profile (what the app itself treats as agreed).
- Checked at the start of every session, so an agreement given after his first chat is picked up.
- Only ever turns consent ON: nothing in the app withdraws it, and an unknown answer changes nothing.
"""
import re
import time

from .settings import S

CACHE_SECS = 300
_cache = {}


def lookup(phone: str):
    """True / False from the app, or None when it is not known (not set up, no phone, lookup failed)."""
    url, key = S.env("CONSENT_URL", ""), S.env("TANYA_API_KEY", "")
    phone = re.sub(r"\D", "", phone or "")[-10:]
    if not url or not key or len(phone) < 10:
        return None
    hit = _cache.get(phone)
    if hit and time.time() - hit[0] < CACHE_SECS:
        return hit[1]
    try:
        import httpx
        r = httpx.get(url, params={"phone": phone}, headers={"x-tanya-key": key}, timeout=4)
        r.raise_for_status()
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError("not ok")
    except Exception as e:
        print(f"[consent] lookup failed {type(e).__name__}: {str(e)[:120]}", flush=True)
        return None
    _cache[phone] = (time.time(), bool(data.get("consent")))
    return _cache[phone][1]


def refresh(rec: dict, phone: str) -> bool:
    """Turn his consent on when the app says he has agreed. Returns True if it changed now."""
    if rec["profile"].get("consent"):
        return False
    if lookup(phone):
        rec["profile"]["consent"] = True
        return True
    return False
