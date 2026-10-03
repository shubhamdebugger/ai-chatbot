"""AI-C01 AI Gateway — Python reference version (the Node Gateway is ported from this file).

Plain English:
- /webhook/crm : the CRM calls this the moment a message is saved. We check the secret,
                 set HUMAN mode at once for staff replies, drop duplicates, and queue the event.
                 We answer the CRM in milliseconds; the thinking happens in the workers.
- /events/app  : app events (app opened; later: plan bought, consent changed …) — input B2.
- /health      : for monitoring.
- /dev/...     : the developer console (RUN_MODE=dev or DEV_CONSOLE=1) to chat with Tanya without the CRM.
"""
import json
import os
import sys
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from starlette.concurrency import run_in_threadpool

from . import memory_model as mm
from .brief import agent_card
from .content_pack import PACK
from .crm_adapter import ConsoleAdapter, make_adapter
from .knowledge import KnowledgeIndex
from .llm import LLM
from .memory_store import make_store
from .settings import ROOT, S
from . import timeutil
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
    if ev.kind == "staff_message":                                   # HUMAN at once (v4 step 6)
        store.set_human_flag(ev.conversation_id, S.get("human_mode_release_hours", 12), timeutil.now())
    if ev.kind not in ("user_message", "staff_message"):
        return 200, {"ok": True, "skipped": ev.kind}, ev
    event = {"event_id": ev.event_id, "kind": ev.kind, "user_id": ev.user_id,
             "conversation_id": ev.conversation_id, "text": ev.text,
             "received_ms": int(time.time() * 1000), "source": "webhook"}
    if SERVER:
        queued = intake.enqueue(event)                              # Redis down -> raises -> 503 at once
        store.r.set("tanya:metrics:webhook_last", int(time.time()))   # monitoring, only after a good enqueue
        return 200, {"ok": True, "queued": queued}, ev
    dev_handler.adapter = adapter
    dev_handler(event)                                               # dev: process inline
    return 200, {"ok": True}, ev


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
