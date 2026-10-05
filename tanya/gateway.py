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
import time
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

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
    return {"ok": True, "run_mode": S.run_mode, "provider": S.provider, "fallback": S.fallback_provider or None,
            "langgraph": lg, "knowledge_chunks": len(kb.chunks), "vectors": kb.has_vectors,
            "vector_store": "qdrant" if kb.qdrant else ("memory" if kb.has_vectors else "none"),
            "qdrant_error": kb.qdrant_error or None,
            "content_version": PACK.version_string(), "killswitch": store.killswitch()}


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

# ------------------------------------------------------------------ CRM webhook
@app.post("/webhook/crm")
async def webhook(request: Request):
    payload = await request.json()
    ev = adapter.parse_webhook(payload, {k.lower(): v for k, v in request.headers.items()})
    if ev is None:
        return JSONResponse({"ok": False, "ignored": True}, status_code=200)
    if ev.kind == "staff_message":                                   # HUMAN at once (v4 step 6)
        store.set_human_flag(ev.conversation_id, S.get("human_mode_release_hours", 12), timeutil.now())
    if ev.kind not in ("user_message", "staff_message"):
        return {"ok": True, "skipped": ev.kind}
    event = {"event_id": ev.event_id, "kind": ev.kind, "user_id": ev.user_id,
             "conversation_id": ev.conversation_id, "text": ev.text}
    if SERVER:
        return {"ok": True, "queued": intake.enqueue(event)}
    dev_handler.adapter = adapter
    dev_handler(event)                                               # dev: process inline
    return {"ok": True}


@app.post("/events/app")
async def app_event(request: Request):
    """TODO (app developer, B2): sign these calls; add plan_bought, consent_changed, journey events."""
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
