"""AI-C01 AI Gateway — Python reference version (the Node Gateway is ported from this file).

Plain English:
- /webhook/crm : the CRM calls this the moment a message is saved. We check the secret,
                 set HUMAN mode at once for staff replies, drop duplicates, and queue the event.
                 We answer the CRM in milliseconds; the thinking happens in the workers.
- /events/app  : app events (app opened; later: plan bought, consent changed …) — input B2.
- /kb/search   : knowledge search for the voice agent (ElevenLabs webhook tool). Needs KB_TOOL_SECRET.
- /voice/post-call : ElevenLabs post-call webhook → orch_voice_calls (CRM voice panel) + Tanya memory (voice.py).
- /voice/context : tg-node-backend, when it issues a call token → the caller brief {{user_context}}.
                   Needs TANYA_CONTEXT_SECRET (server to server only).
- /voice/live  : tg-node-backend, when the PWA's call connects / ends → CRM "On call" banner and
                   "Voice call started / completed" chat messages (voice_live.py). TANYA_CONTEXT_SECRET +
                   the signed call context.
- /voice/tools/... : ElevenLabs server tools during a call (recent chat, past calls, internal notes,
                   save fact, request callback). Need KB_TOOL_SECRET + the signed call context (voice_context.py).
- /health      : for monitoring.
- /dev/...     : the developer console (RUN_MODE=dev or DEV_CONSOLE=1) to chat with Tanya without the CRM.
"""
import hmac
import json
import os
import sys
import time
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from . import access
from . import memory_model as mm
from .brief import agent_card
from .content_pack import PACK
from .crm_adapter import ConsoleAdapter, make_adapter
from .knowledge import KnowledgeIndex, for_voice
from .llm import LLM
from .memory_store import make_store
from .settings import ROOT, S
from . import timeutil
from . import voice
from . import voice_context
from . import voice_live
from .workers import TurnHandler, close_idle_sessions, seed_dev, seed_server_factory

app = FastAPI(title="Ms Tanya — AI Gateway (Python reference)", version="1.0-frame")
os.environ.setdefault("REDIS_CONNECT_TIMEOUT_SECONDS", "2")   # gateway: fail fast, never hold the CRM
store = make_store()
llm = LLM()
kb = KnowledgeIndex()
SERVER = S.run_mode == "server"
DEV = not SERVER or S.env("DEV_CONSOLE", "0") == "1"
adapter = make_adapter()
console = ConsoleAdapter()
intake = None
if SERVER:
    from .streams import Intake
    intake = Intake(store.r)
dev_handler = TurnHandler(store, llm, kb, console, seed_dev if not SERVER else seed_server_factory(store))


@app.get("/health")
def health():
    try:
        from langgraph.graph import StateGraph  # noqa: F401
        lg = S.env("USE_LANGGRAPH", "1") != "0"
    except Exception:
        lg = False
    try:
        ks = store.killswitch()
    except Exception:
        ks = "off"
    return {"ok": True, "run_mode": S.run_mode, "provider": S.provider, "fallback": S.fallback_provider or None,
            "langgraph": lg, "knowledge_chunks": len(kb.chunks), "vectors": kb.has_vectors,
            "vector_store": "qdrant" if getattr(kb, "qdrant", None) else ("memory" if kb.has_vectors else "none"),
            "qdrant_error": getattr(kb, "qdrant_error", None),
            "content_version": PACK.version_string(), "killswitch": ks}



@app.get("/health/queues")
def health_queues():
    """Monitoring: is any message waiting, stuck or lost? (alarm on dead > 0, oldest_pending_s > 60,
    reconcile_age_s > 60, p95_total_ms over target)."""
    if not SERVER:
        return {"ok": True, "run_mode": "dev"}
    from .streams import GROUP, stream_name
    r, now_ms = store.r, int(time.time() * 1000)
    lanes, worst = [], 0
    for p in range(S.get("stream_partitions", 8)):
        s = stream_name(p)
        try:
            g = next((x for x in r.xinfo_groups(s) if x["name"] == GROUP), {})
        except Exception:
            g = {}
        oldest = 0
        if g.get("pending"):
            first = r.xpending_range(s, GROUP, min="-", max="+", count=1)
            if first:
                oldest = int(first[0]["time_since_delivered"] / 1000)
        worst = max(worst, oldest)
        lanes.append({"lane": s, "pending": g.get("pending", 0), "lag": g.get("lag"), "oldest_pending_s": oldest,
                      "consumers": g.get("consumers", 0)})
    turns = [json.loads(x) for x in r.lrange("tanya:metrics:turns", 0, 99)]
    tot = sorted(t["total_ms"] for t in turns if t.get("total_ms") is not None)
    pct = lambda q: tot[min(len(tot) - 1, int(len(tot) * q))] if tot else None
    last = lambda k: (int(time.time()) - int(r.get(k))) if r.get(k) else None
    out = {"ok": True, "lanes": lanes, "oldest_pending_s": worst, "dead_letters": r.xlen("tanya:dead"),
           "errors_total": r.xlen("tanya:errors"), "reconciled_total": int(r.get("tanya:metrics:reconciled") or 0),
           "reconcile_age_s": last("tanya:metrics:reconcile_last"), "webhook_age_s": last("tanya:metrics:webhook_last"),
           "turns_measured": len(tot), "p50_total_ms": pct(0.5), "p95_total_ms": pct(0.95)}
    out["alarms"] = [a for a, bad in (("dead_letters", out["dead_letters"] > 0), ("stuck_pending", worst > 60),
                                       ("reconciler_not_running", (out["reconcile_age_s"] or 999) > 60)) if bad]
    return out


# ------------------------------------------------------------------ voice agent knowledge tool
class KBQuery(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    top_k: int | None = Field(default=None, ge=1, le=5)


@app.post("/kb/search")
def kb_search(body: KBQuery, x_tool_secret: str = Header("")):
    """ElevenLabs 'search_knowledge' webhook tool. Same hybrid search (BM25 + Qdrant) as chat.
    Plain def, not async: search() makes blocking embedding/Qdrant calls, so FastAPI runs it in a thread."""
    secret = S.env("KB_TOOL_SECRET")
    if not secret or not hmac.compare_digest(x_tool_secret, secret):
        raise HTTPException(401, "invalid tool secret")
    return for_voice(kb.search(body.query.strip(), top_k=body.top_k))


@app.post("/voice/post-call")
async def voice_post_call(request: Request):
    """ElevenLabs post-call webhook (transcript). Signature is over the raw body, so read it first."""
    raw = await request.body()
    if not voice.verify_webhook(raw, request.headers.get("elevenlabs-signature", ""),
                                S.env("ELEVENLABS_WEBHOOK_SECRET")):
        raise HTTPException(401, "invalid signature")
    try:
        payload = json.loads(raw)
    except ValueError:
        raise HTTPException(400, "invalid JSON")
    # CRM note call is blocking httpx — keep it off the event loop.
    return await run_in_threadpool(voice.handle_post_call, payload, store, adapter, timeutil.now(), llm)


class CallerContextQuery(BaseModel):
    pwa_uid: str = Field(min_length=1, max_length=64)
    sb_user_id: str = Field(default="", max_length=64)
    sb_conversation_id: str = Field(default="", max_length=64)
    app: dict = Field(default_factory=dict)          # what the PWA knows: trial, payment, answers, activity


@app.post("/voice/context")
def voice_caller_context(body: CallerContextQuery, x_context_secret: str = Header("")):
    """Layer 1: the caller brief for {{user_context}}. Called by tg-node-backend only (never the browser)."""
    secret = S.env("TANYA_CONTEXT_SECRET")
    if not secret or not hmac.compare_digest(x_context_secret, secret):
        raise HTTPException(401, "invalid context secret")
    return voice_context.build_context(store, body.model_dump(), body.app, timeutil.now())


class LiveCall(BaseModel):
    """The PWA's call connected / ended, with the signed call context from the token."""
    state: Literal["started", "ended"]
    el_conversation_id: str = Field(default="", pattern=r"^[A-Za-z0-9_-]{0,64}$")
    duration_secs: int | None = Field(default=None, ge=0, le=7200)
    pwa_uid: str = ""
    sb_user_id: str = ""
    sb_conversation_id: str = ""
    ctx_iat: str = ""
    ctx_sig: str = ""


@app.post("/voice/live")
def voice_live_status(body: LiveCall, x_context_secret: str = Header("")):
    """Call started / ended. Called by tg-node-backend only (the browser goes through it)."""
    secret = S.env("TANYA_CONTEXT_SECRET")
    if not secret or not hmac.compare_digest(x_context_secret, secret):
        raise HTTPException(401, "invalid context secret")
    ctx = voice.verify_context(body.model_dump(), int(time.time()), S.env("TANYA_VOICE_CTX_SECRET"),
                               max_age=voice.TOOL_CTX_MAX_AGE_SECS)
    if not ctx:
        raise HTTPException(403, "invalid call context")
    now = timeutil.now()
    if body.state == "started":
        return voice_live.call_started(store, adapter, ctx, body.ctx_sig, body.el_conversation_id, now)
    return voice_live.call_ended(store, adapter, ctx, body.ctx_sig, body.el_conversation_id, now,
                                 duration_secs=body.duration_secs)


class ToolCall(BaseModel):
    """Every voice tool gets the signed call context from the dynamic variables (set as
    'dynamic variable' parameters in ElevenLabs, so the AI cannot choose whose data it reads)."""
    pwa_uid: str = ""
    sb_user_id: str = ""
    sb_conversation_id: str = ""
    ctx_iat: str = ""
    ctx_sig: str = ""
    limit: int | None = Field(default=None, ge=1, le=30)
    field: str = ""
    value: str = Field(default="", max_length=300)
    his_words: str = Field(default="", max_length=500)
    reason: str = Field(default="", max_length=40)
    preferred_time: str = Field(default="", max_length=200)
    preferred_day: str = Field(default="", max_length=20)          # today | tomorrow | monday … sunday
    preferred_hour: int | None = Field(default=None, ge=0, le=23)  # 24h, IST
    confirmed: bool = False


def _tool_ctx(body: ToolCall, x_tool_secret: str) -> dict:
    secret = S.env("KB_TOOL_SECRET")
    if not secret or not hmac.compare_digest(x_tool_secret, secret):
        raise HTTPException(401, "invalid tool secret")
    ctx = voice.verify_context(body.model_dump(), int(time.time()), S.env("TANYA_VOICE_CTX_SECRET"),
                               max_age=voice.TOOL_CTX_MAX_AGE_SECS)
    if not ctx:
        raise HTTPException(403, "invalid call context")
    return ctx


@app.post("/voice/tools/recent-chat")
def voice_tool_recent_chat(body: ToolCall, x_tool_secret: str = Header("")):
    return voice_context.recent_chat(store, adapter, _tool_ctx(body, x_tool_secret), body.limit)


@app.post("/voice/tools/past-calls")
def voice_tool_past_calls(body: ToolCall, x_tool_secret: str = Header("")):
    return voice_context.past_calls(_tool_ctx(body, x_tool_secret), body.limit)


@app.post("/voice/tools/internal-notes")
def voice_tool_internal_notes(body: ToolCall, x_tool_secret: str = Header("")):
    return voice_context.internal_notes(store, adapter, _tool_ctx(body, x_tool_secret), timeutil.now())


@app.post("/voice/tools/request-callback")
def voice_tool_request_callback(body: ToolCall, x_tool_secret: str = Header("")):
    """'Our senior will call you' — only after the caller said yes."""
    return voice_context.request_callback(store, adapter, _tool_ctx(body, x_tool_secret), body.reason,
                                          body.preferred_time, body.confirmed, timeutil.now(),
                                          preferred_day=body.preferred_day, preferred_hour=body.preferred_hour)


@app.post("/voice/tools/save-fact")
def voice_tool_save_fact(body: ToolCall, x_tool_secret: str = Header("")):
    return voice_context.save_fact(store, _tool_ctx(body, x_tool_secret), body.field, body.value,
                                   body.his_words, timeutil.now())

@app.post("/voice/tools/report-abuse")
def voice_tool_report_abuse(body: ToolCall, x_tool_secret: str = Header("")):
    """Caller used abusive words (once per message). Returns the fixed line and warn/end; 3rd strike ends the call."""
    return voice_context.report_abuse(store, _tool_ctx(body, x_tool_secret), body.ctx_iat)


# ------------------------------------------------------------------ CRM webhook
@app.post("/webhook/crm")
async def webhook(request: Request):
    """The CRM's PHP request (the customer's 'Sending...') waits for this answer, so it must be fast and must
    never block other webhooks: the Redis work runs in the thread pool, not on the event loop."""
    t0 = time.perf_counter()
    payload = await request.json()
    headers = {k.lower(): v for k, v in request.headers.items()}
    try:
        status, body, ev = await run_in_threadpool(_webhook, payload, headers)
    except Exception as e:                                           # Redis down etc.: say so (503); the
        status, body, ev = 503, {"ok": False, "error": type(e).__name__}, None   # reconciler recovers the message
    print(f"[webhook] kind={getattr(ev, 'kind', '-')} event={getattr(ev, 'event_id', '-')} status={status} "
          f"result={json.dumps(body)} ms={int((time.perf_counter() - t0) * 1000)}", file=sys.stderr, flush=True)
    return JSONResponse(body, status_code=status)


def _webhook(payload, headers):
    ev = adapter.parse_webhook(payload, headers)
    if ev is None:
        return 200, {"ok": False, "ignored": True}, None
    if ev.kind == "invalid_event":                                   # G5: no numeric message id — never guessed
        store.emit([{"type": "alert", "user_id": ev.user_id or "-", "at": timeutil.iso(timeutil.now()),
                     "kind": "webhook_without_message_id", "conversation_id": ev.conversation_id}])
        return 200, {"ok": False, "rejected": "missing message_id"}, ev
    if ev.kind in ("conversation_closed", "release_to_bot"):         # HUMAN -> BOT (v4 §7): chat closed / #bot
        from .handoff_recovery import released_by_staff
        released_by_staff(store, ev.conversation_id, timeutil.now(), ev.kind)
        store.release_conversation(ev.conversation_id, timeutil.now())
        if ev.kind == "conversation_closed":                         # final summary of the closed chat
            store.summary_touch(ev.conversation_id, ev.user_id, time.time(), force=True)
        store.emit([{"type": "mode", "user_id": ev.user_id or ev.conversation_id, "at": timeutil.iso(timeutil.now()),
                     "conversation_id": ev.conversation_id, "mode": "BOT", "by": ev.kind}])
        return 200, {"ok": True, "released": ev.conversation_id}, ev
    if ev.kind == "staff_message":                                   # HUMAN at once (v4 step 6)
        store.set_human_flag(ev.conversation_id, S.get("human_mode_release_hours", 12), timeutil.now())
        store.staff_wait_end(ev.conversation_id)                     # staff answered: no staff-silent alert
        from .handoff_recovery import agent_replied
        sender = str((payload.get("data") or {}).get("user_id", ""))
        agent_replied(store, ev.conversation_id, timeutil.now(), agent_id=sender)   # keeps HUMAN, cancels recovery
        from .handoff_recovery import agent_activity                 # Tanya back after N quiet agent minutes
        agent_activity(store, ev.conversation_id, ev.user_id, ev.event_id, timeutil.now())
        store.summary_touch(ev.conversation_id, ev.user_id, time.time())
    if ev.kind not in ("user_message", "staff_message"):
        return 200, {"ok": True, "skipped": ev.kind}, ev
    event = {"event_id": ev.event_id, "kind": ev.kind, "user_id": ev.user_id,
             "conversation_id": ev.conversation_id, "text": ev.text,
             "received_ms": int(time.time() * 1000), "source": "webhook"}
    if SERVER:
        queued = intake.enqueue(event)                              # Redis down -> raises -> 503 at once
        if queued and ev.kind == "user_message" and not store.human_flag(ev.conversation_id, timeutil.now()):
            store.typing_set(ev.conversation_id, "queued", S.get("typing_ttl_seconds", 90))   # PWA typing truth
        store.r.set("tanya:metrics:webhook_last", int(time.time()))   # monitoring, only after a good enqueue
        return 200, {"ok": True, "queued": queued}, ev
    dev_handler.adapter = adapter
    dev_handler(event)                                               # dev: process inline
    return 200, {"ok": True}, ev


# ------------------------------------------------------------------ PWA status + CRM callbacks API (06-Oct-2026)
def _key_ok(request: Request) -> bool:
    """Server-to-server calls only (PWA backend, CRM page). Key = TANYA_API_KEY, else the CRM webhook secret."""
    import hmac as _h
    want = S.env("TANYA_API_KEY", "") or S.env("CRM_WEBHOOK_SECRET", "")
    got = request.headers.get("x-tanya-key", "")
    return bool(want) and _h.compare_digest(str(got), str(want))


@app.get("/pwa/status/{conversation_id}")
def pwa_status(conversation_id: str, request: Request):
    """What the customer's chat should show: Tanya working (typing), or waiting for staff (HUMAN)."""
    if not _key_ok(request):
        return JSONResponse({"ok": False}, status_code=403)
    now = timeutil.now()
    h = store.handoff_get(conversation_id)
    return {"ok": True, "typing": store.typing_get(conversation_id),
            "last_reply_id": store.last_reply_get(conversation_id),
            "last_received_id": int(store.r.get(f"tanya:lastin:{conversation_id}") or 0) if hasattr(store, "r") else 0,
            # not on the dashboard's AI list: the team answers this chat, so the PWA shows "team will reply"
            "mode": "HUMAN" if store.human_flag(conversation_id, now) or access.is_off(store, conversation_id) else "BOT",
            "handoff": {k: h.get(k) for k in ("status", "period", "started_at", "due_at")} if h else None}


def _cb_conn():
    from .callbacks import connect
    return connect()


@app.get("/crm/callbacks")
def crm_callbacks(request: Request, view: str = "open", days: int = 7):
    if not _key_ok(request):
        return JSONResponse({"ok": False}, status_code=403)
    from .callbacks import list_callbacks
    conn = _cb_conn()
    try:
        return {"ok": True, **list_callbacks(conn, view, max(1, min(days, 90)))}
    finally:
        conn.close()


@app.post("/crm/callbacks/{callback_id}/done")
async def crm_callback_done(callback_id: str, request: Request):
    if not _key_ok(request):
        return JSONResponse({"ok": False}, status_code=403)
    body = await request.json()
    from .callbacks import mark_done
    conn = _cb_conn()
    try:
        ok = await run_in_threadpool(mark_done, conn, callback_id, str(body.get("actor") or "crm"), str(body.get("note") or ""))
        return {"ok": ok}
    finally:
        conn.close()


@app.get("/crm/callbacks/{callback_id}/audit")
def crm_callback_audit(callback_id: str, request: Request):
    if not _key_ok(request):
        return JSONResponse({"ok": False}, status_code=403)
    from .callbacks import audit_for
    conn = _cb_conn()
    try:
        return {"ok": True, "audit": audit_for(conn, callback_id)}
    finally:
        conn.close()


@app.post("/events/app")
async def app_event(request: Request, x_app_secret: str = Header("")):
    """App events from TG Lite's backend (never the browser), with the shared APP_EVENTS_SECRET.
    Empty secret = endpoint off. TODO (app developer, B2): add plan_bought, consent_changed, journey events."""
    secret = S.env("APP_EVENTS_SECRET")
    if not secret:
        raise HTTPException(404)
    if not hmac.compare_digest(x_app_secret, secret):
        raise HTTPException(401, "invalid app secret")
    body = await request.json()
    if body.get("event") != "app_open":
        return {"ok": True, "skipped": body.get("event")}
    event = {"event_id": body.get("event_id"), "kind": "app_open", "user_id": body["user_id"],
             "conversation_id": body.get("conversation_id", body["user_id"])}
    if SERVER:
        return {"ok": True, "queued": intake.enqueue(event)}
    dev_handler(event)
    return {"ok": True}


# ------------------------------------------------------------------ developer console
def _need_dev():
    if not DEV:
        raise HTTPException(404)


def _state(uid):
    rec = store.get(uid)
    if not rec:
        return {"user_id": uid, "exists": False}
    now = timeutil.now()
    return {"user_id": uid, "exists": True, "profile": rec["profile"], "facts": rec["facts"],
            "conflicts": rec["conflicts"], "journey": rec["journey"], "session": rec["session"],
            "counters": rec["counters"], "signals": rec["signals"], "cases": rec["cases"],
            "callbacks": rec["callbacks"], "mode": rec["mode"], "trial_day": mm.trial_day(rec, now),
            "temperature": rec.get("last_temperature"), "session_notes": rec["session_notes"],
            "messages": rec["messages"][-60:], "agent_card": agent_card(rec, now),
            "version": rec["version"]}


@app.get("/")
def console_page():
    _need_dev()
    return FileResponse(ROOT / "static" / "devconsole.html")


@app.get("/dev/users")
def dev_users():
    _need_dev()
    return {"users": list(PACK.test_users.values()), "provider": S.provider,
            "clock": timeutil.stamp(timeutil.now()), "killswitch": store.killswitch()}


@app.get("/dev/state/{uid}")
def dev_state(uid: str):
    _need_dev()
    return _state(uid)


@app.post("/dev/chat")
async def dev_chat(request: Request):
    _need_dev()
    b = await request.json()
    dev_handler.adapter = console
    st = dev_handler({"event_id": None, "kind": "user_message", "user_id": b["user_id"],
                      "conversation_id": b["user_id"], "text": b["text"]})
    return {"bubbles": st["bubbles"], "trace": st["trace"], "brief": st["brief"], "state": _state(b["user_id"])}


@app.post("/dev/event")
async def dev_event(request: Request):
    _need_dev()
    b = await request.json()
    dev_handler.adapter = console
    st = dev_handler({"event_id": None, "kind": "app_open", "user_id": b["user_id"], "conversation_id": b["user_id"]})
    return {"bubbles": st["bubbles"], "trace": st["trace"], "brief": st["brief"], "state": _state(b["user_id"])}


@app.post("/dev/staff")
async def dev_staff(request: Request):
    """Simulate a staff reply in the CRM → HUMAN mode."""
    _need_dev()
    b = await request.json()
    store.set_human_flag(b["user_id"], S.get("human_mode_release_hours", 12), timeutil.now())
    dev_handler({"kind": "staff_message", "user_id": b["user_id"], "text": b.get("text", "(staff reply)")})
    return _state(b["user_id"])


@app.post("/dev/release")
async def dev_release(request: Request):
    _need_dev()
    b = await request.json()
    from .policy import release_to_bot
    store.clear_human_flag(b["user_id"])
    rec = store.get(b["user_id"])
    if rec:
        release_to_bot(rec, timeutil.now())
        store.save(rec)
    return _state(b["user_id"])


@app.post("/dev/reset")
async def dev_reset(request: Request):
    _need_dev()
    b = await request.json()
    store.reset(b["user_id"])
    store.clear_human_flag(b["user_id"])
    return {"ok": True}


@app.post("/dev/end_session")
async def dev_end_session(request: Request):
    """Close his session now → session note + Lead Brief (normally after 30 min silence)."""
    _need_dev()
    b = await request.json()
    rec = store.get(b["user_id"])
    if not rec:
        return {"ok": False}
    from datetime import timedelta
    later = timeutil.now() + timedelta(minutes=S.get("session_gap_minutes", 30) + 1)
    n = close_idle_sessions(store, llm, console, now=later)
    return {"closed": n, "state": _state(b["user_id"])}


@app.post("/dev/killswitch")
async def dev_killswitch(request: Request):
    _need_dev()
    b = await request.json()
    if b.get("state") not in ("off", "limited", "stopped"):
        raise HTTPException(400, "state must be off | limited | stopped")
    store.set_killswitch(b["state"])
    return {"killswitch": store.killswitch()}


@app.post("/dev/clock")
async def dev_clock(request: Request):
    """Set the clock (e.g. to test night hand-offs). Empty = real time."""
    _need_dev()
    b = await request.json()
    from datetime import datetime
    timeutil.set_clock(datetime.fromisoformat(b["iso"]).replace(tzinfo=timeutil.IST) if b.get("iso") else None)
    return {"clock": timeutil.stamp(timeutil.now())}


@app.get("/dev/audit", response_class=PlainTextResponse)
def dev_audit():
    _need_dev()
    from .morning_audit import run
    path, n = run(timeutil.now().strftime("%Y-%m-%d"), store)
    return Path(path).read_text(encoding="utf-8")
