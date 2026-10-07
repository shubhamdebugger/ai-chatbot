"""Voice calls in progress → "On call" in the CRM, plus start / completed messages in the chat.

Plain English:
1. When the call connects, the PWA reports "started" (PWA → tg-node-backend → /voice/live, with the
   signed call context). We put a row in orch_voice_live, which the CRM voice panel shows as a red
   "On call with Tanya" banner, and post "Voice call started" into his CRM conversation.
2. When the call ends, the PWA reports "ended": the row is closed and "Voice call completed · 6m 12s"
   is posted (plus the senior's promised time if a callback was booked on the call).
3. The browser can die before it says "ended". Then the row stops counting at expires_unix, and the
   ElevenLabs post-call webhook (voice.handle_post_call) posts the completed message instead.
   Each message goes out once per call (keyed on the call's ctx_sig: one token = one call).

Messages are posted as the Tanya agent account, whose messages the CRM webhook ignores, so they never
wake chat Tanya or set HUMAN mode. The customer sees them in TG Lite, agents in the CRM.
Fails open: a missing table, MySQL or CRM only loses the banner or a message, never the call.
"""
from .settings import S
from .timeutil import parse

LIVE_MAX_SECS = 12 * 60          # longer than any call (limit 10 min + ElevenLabs' cap at 10:45)
START_TEXT = "📞 Voice call with Ms Tanya started"


def _fmt(secs) -> str:
    secs = int(secs)
    return f"{secs // 60}m {secs % 60:02d}s"


def done_text(duration_secs=None, promised="") -> str:
    out = "✅ Voice call with Ms Tanya completed" + (f" · {_fmt(duration_secs)}" if duration_secs else "")
    if promised:
        out += f"\n📞 Hamare senior aapko {promised} call karenge."
    return out


def _db_run(sql, args, fetch=False):
    """One statement on the orch database. None without MySQL or on any error (fails open)."""
    if not S.env("MYSQL_DB"):
        return None
    from .voice_context import _db
    try:
        conn = _db()
        try:
            with conn.cursor() as c:
                c.execute(sql, args)
                row = c.fetchone() if fetch else True
            conn.commit()
            return row
        finally:
            conn.close()
    except Exception:
        return None


def _post(store, adapter, ctx, text, kind) -> bool:
    if not ctx.get("sb_conversation_id"):
        return False
    try:
        adapter.post_message(ctx["sb_conversation_id"], text)
        return True
    except Exception as e:       # the call itself is recorded elsewhere; flag it for a person
        store.emit([{"type": "alert", "user_id": ctx.get("sb_user_id") or f"pwa:{ctx['pwa_uid']}",
                     "kind": f"voice_{kind}_message_failed", "error": f"{type(e).__name__}: {str(e)[:200]}"}])
        return False


def call_started(store, adapter, ctx, call_key, el_conversation_id, now) -> dict:
    """The PWA's call connected. call_key = the call's ctx_sig."""
    if not store.once(f"voicelive:start:{call_key}", days=1):
        return {"ok": True, "duplicate": True}
    from .voice_context import _user_key
    ts = int(now.timestamp())
    live = _db_run("INSERT INTO orch_voice_live (el_conversation_id,user_id,sb_conversation_id,started_unix,expires_unix) "
                   "VALUES (%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE started_unix=VALUES(started_unix), "
                   "expires_unix=VALUES(expires_unix), ended_unix=NULL",
                   (el_conversation_id, _user_key(ctx), ctx.get("sb_conversation_id") or None, ts, ts + LIVE_MAX_SECS))
    return {"ok": True, "live": bool(live), "chat_message": _post(store, adapter, ctx, START_TEXT, "start")}


def _promised_during(ctx, since_ts) -> str:
    """What Tanya promised if she booked a senior callback on this call ('' if none)."""
    from .voice_context import open_callbacks
    for cb in open_callbacks(ctx):
        try:
            if since_ts is None or parse(cb["requested_at"]).timestamp() >= since_ts - 5:
                return cb.get("promised") or ""
        except (TypeError, ValueError):
            continue
    return ""


def call_ended(store, adapter, ctx, call_key, el_conversation_id, now, duration_secs=None, started_unix=None) -> dict:
    """The call is over (PWA "ended", or the post-call webhook). Closes the banner and posts the
    completed message once. Duration: from our own start time when we have it, else the given one
    (the post-call webhook passes ElevenLabs' duration and start time)."""
    ts = int(now.timestamp())
    row = None
    if el_conversation_id:
        row = _db_run("SELECT started_unix FROM orch_voice_live WHERE el_conversation_id=%s", (el_conversation_id,),
                      fetch=True)
        _db_run("UPDATE orch_voice_live SET ended_unix=%s WHERE el_conversation_id=%s AND ended_unix IS NULL",
                (ts, el_conversation_id))
    if not store.once(f"voicelive:done:{call_key}", days=30):
        return {"ok": True, "duplicate": True}
    if row and row.get("started_unix") and duration_secs is None:
        started_unix, duration_secs = int(row["started_unix"]), ts - int(row["started_unix"])
    secs = int(duration_secs) if duration_secs is not None and 0 < int(duration_secs) <= 2 * 3600 else None
    promised = _promised_during(ctx, started_unix or (ts - secs if secs else None))
    return {"ok": True, "chat_message": _post(store, adapter, ctx, done_text(secs, promised), "done")}
