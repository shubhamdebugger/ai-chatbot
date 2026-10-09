"""Who gets Tanya: one list for chat AND voice (dashboard → AI tab, 07-Oct-2026).

Plain English:
- The dashboard writes the numbers to tg_level.tanya_ai_phones and the master switch to
  tg_level.app_settings 'tanya_ai_enabled_all' ('1' = every customer). The PWA backend reads the same
  two for voice (pwa-node-backend/src/lib/tanyaVoice.ts).
- Chat: the turn worker asks chat_enabled() before Tanya does anything. Not on the list → she stays silent,
  the message is left to the team, and the PWA shows "Our team will reply here" instead of her typing bubble.
- The customer's phone comes from his CRM profile (details → phone), cached for a day.
- Settings are read with a 60 s cache. If the database cannot be read we keep the last good value, else
  nobody — a failure never switches Tanya on for everyone.
"""
import sys
import time

from .settings import S

CACHE_SECS = 60
PHONE_TTL = 86400
_cache = {"at": 0.0, "value": None}


def ten(value) -> str:
    d = "".join(ch for ch in str(value or "") if ch.isdigit())[-10:]
    return d if len(d) == 10 else ""


def _connect():
    import pymysql
    return pymysql.connect(host=S.env("ACCESS_DB_HOST") or S.env("MYSQL_HOST", "127.0.0.1"),
                           port=int(S.env("ACCESS_DB_PORT") or S.env("MYSQL_PORT", "3306")),
                           user=S.env("ACCESS_DB_USER") or S.env("MYSQL_USER"),
                           password=S.env("ACCESS_DB_PASSWORD") or S.env("MYSQL_PASSWORD"),
                           database=S.env("ACCESS_DB_NAME", "tg_level"), charset="utf8mb4", connect_timeout=3)


def settings(now=None) -> dict:
    """{'everyone': bool, 'phones': set of 10-digit numbers}."""
    now = now or time.time()
    if _cache["value"] is not None and now - _cache["at"] < CACHE_SECS:
        return _cache["value"]
    value = _cache["value"] or {"everyone": False, "phones": set()}
    try:
        url = S.env("ACCESS_URL", "")
        if url:
            # The dashboard's AI tab writes the list on the PWA box, whose database is closed to the outside;
            # that box serves it here (GET, x-tanya-key = TANYA_API_KEY). Same rules: any failure raises and
            # we keep the last good value, never "everyone".
            import httpx
            r = httpx.get(url, headers={"x-tanya-key": S.env("TANYA_API_KEY", "")}, timeout=3)
            r.raise_for_status()
            j = r.json()
            if not j.get("ok"):
                raise RuntimeError("access endpoint not ok")
            value = {"everyone": j.get("everyone") is True, "phones": {ten(p) for p in j.get("phones") or []} - {""}}
        else:
            conn = _connect()
            try:
                with conn.cursor() as c:
                    c.execute("SELECT value FROM app_settings WHERE name = 'tanya_ai_enabled_all'")
                    row = c.fetchone()
                    c.execute("SELECT phone FROM tanya_ai_phones")
                    phones = {ten(r[0]) for r in c.fetchall()} - {""}
                value = {"everyone": bool(row) and str(row[0]) == "1", "phones": phones}
            finally:
                conn.close()
    except Exception as e:
        print(f"[access] settings read failed {type(e).__name__}: {str(e)[:120]} - keeping last value",
              file=sys.stderr, flush=True)
    _cache.update(at=now, value=value)
    return value


def phone_for(adapter, store, user_id) -> str:
    """His 10-digit phone from the CRM profile, cached in Redis for a day ('' if the CRM has none)."""
    r = getattr(store, "r", None)
    key = f"tanya:phone:{user_id}"
    if r is not None:
        cached = r.get(key)
        if cached is not None:
            return cached
    u = adapter.get_user(user_id) or {}
    phone = ""
    for d in (u.get("details") or []) if isinstance(u, dict) else []:
        if isinstance(d, dict) and d.get("slug") == "phone":
            phone = ten(d.get("value"))
            break
    if r is not None:
        r.set(key, phone, ex=PHONE_TTL if phone else 600)   # no phone yet: look again in 10 min
    return phone


def chat_enabled(adapter, store, user_id) -> bool:
    s = settings()
    if s["everyone"]:
        return True
    if not s["phones"]:
        return False
    try:
        return phone_for(adapter, store, user_id) in s["phones"]
    except Exception as e:      # CRM unreachable: no way to tell who he is, so stay silent this time
        print(f"[access] phone lookup failed user={user_id}: {type(e).__name__}", file=sys.stderr, flush=True)
        return False


def mark_off(store, conversation_id, off: bool):
    """Remembered per chat so the PWA status says 'team will reply' instead of Tanya typing."""
    r = getattr(store, "r", None)
    if r is None or not conversation_id:
        return
    if off:
        r.set(f"tanya:off:{conversation_id}", "1", ex=PHONE_TTL)
    else:
        r.delete(f"tanya:off:{conversation_id}")


def is_off(store, conversation_id) -> bool:
    r = getattr(store, "r", None)
    return bool(r is not None and conversation_id and r.exists(f"tanya:off:{conversation_id}"))


def off_now(store, conversation_id, user_id) -> bool:
    """For the webhook (must stay fast, no CRM call): is Tanya muted for this chat? The remembered per-chat flag,
    or the cached AI list and the cached phone. Unknown (phone not cached yet) → False: the worker decides."""
    if is_off(store, conversation_id):
        return True
    r = getattr(store, "r", None)
    v = _cache["value"]
    if r is None or not user_id or v is None or v["everyone"] or time.time() - _cache["at"] >= CACHE_SECS:
        return False
    try:
        phone = r.get(f"tanya:phone:{user_id}")
    except Exception:
        return False
    if phone is None:
        return False
    off = phone not in v["phones"]
    if off:
        mark_off(store, conversation_id, True)
    return off
