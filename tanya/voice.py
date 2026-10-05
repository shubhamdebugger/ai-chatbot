"""Voice calls with Ms Tanya (ElevenLabs agent, PWA support page) → our records (CRM voice panel) + Tanya memory.

Plain English:
1. The PWA asks tg-node-backend for a call token. tg-node-backend signs the call context
   (PWA user, CRM user, CRM conversation) with TANYA_VOICE_CTX_SECRET; the browser passes it
   into the call as dynamic variables.
2. When the call ends, ElevenLabs posts the transcript to /voice/post-call (signed with
   ELEVENLABS_WEBHOOK_SECRET). We check both signatures, so a caller can never get a note
   written into someone else's CRM conversation.
3. The call (masked) goes to orch_voice_calls through the normal persister, a short session
   note goes into Tanya's memory. Agents read the call (summary, transcript) in the CRM's
   "Voice calls · Tanya AI" panel, which reads orch_voice_calls — no CRM note is written.
   The only chat messages are "Voice call started / completed" (voice_live.py), posted as the Tanya
   agent account: the CRM webhook ignores those, so chat Tanya never wakes and HUMAN mode is never set.
"""
import hashlib
import hmac
import time
from datetime import datetime

from . import memory_model as mm
from .guard_input import mask
from .memory_store import VersionConflict
from .settings import S
from .timeutil import IST, iso
from .understand import FACT_FIELDS

SIG_TOLERANCE_SECS = 30 * 60   # ElevenLabs webhook timestamp may be this old (retries)
CTX_MAX_AGE_SECS = 10 * 60     # the call must start within this after the token was issued
TOOL_CTX_MAX_AGE_SECS = 2 * 3600   # in-call tools: the call is still running, so allow its whole length


def verify_webhook(raw: bytes, header: str, secret: str, now_ts=None) -> bool:
    """ElevenLabs-Signature: 't=<unix>,v0=<hex HMAC-SHA256 of "<t>.<raw body>">'."""
    if not secret or not header:
        return False
    parts = dict(p.strip().split("=", 1) for p in header.split(",") if "=" in p)
    t, v0 = parts.get("t", ""), parts.get("v0", "")
    if not t.isdigit() or not v0:
        return False
    if abs((now_ts or time.time()) - int(t)) > SIG_TOLERANCE_SECS:
        return False
    expected = hmac.new(secret.encode(), t.encode() + b"." + raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, v0)


def sign_context(uid, sb_user_id, sb_conversation_id, iat, secret) -> str:
    """Same string layout as tg-node-backend src/lib/tanyaVoice.ts signContext()."""
    payload = f"{uid}|{sb_user_id}|{sb_conversation_id}|{iat}"
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def verify_context(dyn: dict, start_ts, secret: str, max_age=CTX_MAX_AGE_SECS):
    """The signed call context from the dynamic variables, or None if missing/forged/stale.
    start_ts: when the call started (post-call) or now (in-call tools, with max_age=TOOL_CTX_MAX_AGE_SECS)."""
    uid, sbu, sbc, iat, sig = (str(dyn.get(k, "") or "") for k in
                               ("pwa_uid", "sb_user_id", "sb_conversation_id", "ctx_iat", "ctx_sig"))
    if not (secret and uid and iat.isdigit() and sig):
        return None
    if not hmac.compare_digest(sign_context(uid, sbu, sbc, iat, secret), sig):
        return None
    if start_ts and not (int(iat) - 60 <= int(start_ts) <= int(iat) + max_age):
        return None                                  # a replayed old context
    return {"pwa_uid": uid, "sb_user_id": sbu, "sb_conversation_id": sbc}


DEFAULT_CALL_LIMIT_SECS = 600   # same default as TANYA_VOICE_MAX_CALL_SECS in tg-node-backend
TIME_LIMIT_SLACK_SECS = 5       # the browser hangs up at the limit (+ up to 15s while Tanya finishes)


def end_kind(termination_reason, duration_secs, limit_secs) -> str:
    """How the call ended, from ElevenLabs' free-text termination_reason:
    agent_end_call (Tanya closed it), time_limit_client (the PWA hung up at the limit),
    time_limit_server (the agent's max duration cut it), user_hangup, error, or other."""
    r = str(termination_reason or "").lower()
    if "end_call" in r or "end call" in r:
        return "agent_end_call"
    if "max" in r and "duration" in r:
        return "time_limit_server"
    if "client disconnected" in r or "user" in r:
        if limit_secs and int(duration_secs or 0) >= int(limit_secs) - TIME_LIMIT_SLACK_SECS:
            return "time_limit_client"
        return "user_hangup"
    if "error" in r or "fail" in r:
        return "error"
    return "other"


def _call_limit(dyn: dict) -> int:
    try:
        return int(dyn.get("call_limit_secs") or 0) or DEFAULT_CALL_LIMIT_SECS
    except (TypeError, ValueError):
        return DEFAULT_CALL_LIMIT_SECS


def build_event(data: dict, ctx: dict, now) -> dict:
    """One 'voice_call' event for the persister (orch_voice_calls). Transcript is masked."""
    md, an = data.get("metadata") or {}, data.get("analysis") or {}
    lines, masked, kb_queries, callback_asked, abuse  = [], set(), [], False, 0
    for turn in data.get("transcript") or []:
        text, kinds = mask((turn.get("message") or "").strip())
        masked.update(kinds)
        tools = [c.get("tool_name") for c in turn.get("tool_calls") or [] if c.get("tool_name")]
        for c in turn.get("tool_calls") or []:
            if c.get("tool_name") == "report_abuse":
                abuse += 1
            if c.get("tool_name") == "search_knowledge":
                kb_queries.append(mask(c.get("params_as_json") or "")[0][:200])
            elif c.get("tool_name") == "request_callback":
                callback_asked = True
        if text or tools:
            lines.append({"role": turn.get("role"), "at_secs": turn.get("time_in_call_secs"), "text": text,
                          **({"tools": tools} if tools else {})})
    start = md.get("start_time_unix_secs")
    summary = mask(an.get("transcript_summary") or "")[0]
    dyn = (data.get("conversation_initiation_client_data") or {}).get("dynamic_variables") or {}
    kind = end_kind(md.get("termination_reason"), md.get("call_duration_secs"), _call_limit(dyn))
    return {
        "type": "voice_call",
        "user_id": ctx["sb_user_id"] or f"pwa:{ctx['pwa_uid']}",
        "el_conversation_id": data.get("conversation_id"),
        "pwa_uid": ctx["pwa_uid"],
        "sb_user_id": ctx["sb_user_id"],
        "sb_conversation_id": ctx["sb_conversation_id"],
        "started_at": iso(datetime.fromtimestamp(start, IST)) if start else None,
        "duration_secs": md.get("call_duration_secs"),
        "status": data.get("status"),
        "end_kind": kind,
        # orch_voice_calls.ended_reason: "<end_kind>: <ElevenLabs' own words>"
        "ended_reason": f"{kind}: {md.get('termination_reason') or ''}".rstrip(": "),
        "callback_asked": callback_asked,
        "call_successful": an.get("call_successful"),
        "title": an.get("call_summary_title"),
        "summary": summary,
        "kb_queries": kb_queries,
        "abuse_strikes": abuse,
        "transcript": lines,
        "masked": sorted(masked),
        "cost": md.get("cost"),
        "at": iso(now),
    }


def agent_note(ev: dict) -> str:
    """The note agents see on the CRM conversation (never shown to the customer)."""
    secs = int(ev.get("duration_secs") or 0)
    when = datetime.fromisoformat(ev["started_at"]).strftime("%d %b %Y, %H:%M IST") if ev.get("started_at") else ""
    out = [f"🎙️ Voice call with Ms Tanya (AI) — {when} · {secs // 60}m {secs % 60:02d}s",
           f"Summary: {ev.get('summary') or '(no summary)'}"]
    if ev.get("kb_queries"):
        out.append("Knowledge searched: " + "; ".join(ev["kb_queries"][:6]))
    if ev.get("abuse_strikes"):
        n = ev["abuse_strikes"]
        out.append(f"⚠️ Abusive language: {n} warning{'s' if n != 1 else ''}" +
                   (" — Tanya ended the call." if n >= 3 else "."))
    if ev.get("ended_reason"):
        out.append(f"Ended: {ev['ended_reason']}")
    out.append(f"Full transcript: orch_voice_calls · {ev.get('el_conversation_id')}")
    return "\n".join(out)


FACTS_SYSTEM = """TASK: VOICE FACTS
You read what ONE customer said on a voice call with Ms Tanya (TG Level's assistant).
Return ONLY JSON: {"facts": [{"field": "<field>", "value": "<short value>", "his_words": "<exact words>", "confidence": 0.0-1.0}]}
Only what he states about HIMSELF, explicitly. Allowed fields:
""" + "\n".join(f"- {k}: {v}" for k, v in FACT_FIELDS.items()) + """
Never infer facts he did not say. "his_words" must be copied exactly from his lines. Often there are none: []."""
FACTS_SCHEMA = {"type": "object", "additionalProperties": False,
                "properties": {"facts": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False,
                    "properties": {"field": {"type": "string", "enum": list(FACT_FIELDS)}, "value": {"type": "string"},
                                   "his_words": {"type": "string"}, "confidence": {"type": "number"}},
                    "required": ["field", "value", "his_words", "confidence"]}}},
                "required": ["facts"]}


def call_facts(llm, ev) -> list:
    """Facts he stated on the call (one small AI call over his masked lines). Never raises."""
    said = "\n".join(t["text"] for t in ev.get("transcript") or [] if t.get("role") == "user" and t.get("text"))
    if not llm or not said:
        return []
    try:
        res = llm.call("voice_facts", "fast", FACTS_SYSTEM, [{"role": "user", "content": said[:6000]}],
                       json_mode=True, temperature=0.0, timeout=30, max_tokens=600, schema=FACTS_SCHEMA)
    except Exception:
        return []
    if not res.ok or not isinstance(res.data, dict):
        return []
    min_conf, out = S.get("label_confidence_min", 0.6), []
    for f in res.data.get("facts") or []:
        if not isinstance(f, dict) or f.get("field") not in FACT_FIELDS or not f.get("value"):
            continue
        try:
            conf = float(f.get("confidence", 0))
        except (TypeError, ValueError):
            continue
        if conf >= min_conf:
            out.append({"field": f["field"], "value": mask(str(f["value"]))[0][:120],
                        "his_words": mask(str(f.get("his_words", "")))[0][:200], "confidence": round(conf, 2)})
    return out


def _remember(store, user_id, ev, facts=(), now=None) -> int:
    """Short session note in Tanya's memory, so chat Tanya's record shows the call, plus the facts he
    stated on it (with consent only, as in chat). Returns how many facts were stored."""
    for _ in range(3):
        rec = store.get(user_id)
        if not rec:
            return 0                                 # no chat record yet; orch_voice_calls still has it
        rec["session_notes"].append({"session": f"voice:{ev['el_conversation_id']}", "at": ev["at"],
                                     "lines": [f"Voice call: {ev.get('summary') or '(no summary)'}"]})
        rec["session_notes"] = rec["session_notes"][-10:]
        kept = list(facts) if rec["profile"].get("consent") else []
        for f in kept:
            mm.set_fact(rec, f["field"], f["value"], f["his_words"], "voice", None, now, True, f["confidence"])
        try:
            store.save(rec)
        except VersionConflict:
            continue
        store.emit([{"type": "fact", "user_id": user_id, "at": ev["at"], "source": "voice", "msg_no": None, **f}
                    for f in kept])
        return len(kept)
    return 0


def _senior_callback_safety_net(data, ctx, store, adapter, now) -> str:
    """ElevenLabs' post-call analysis (data collection 'senior_callback_confirmed') says he agreed to a
    senior's call → make sure the callback exists, even if the agent forgot request_callback.
    Returns '' (nothing to do), 'created' or 'already'."""
    from .voice_context import request_callback
    dc = (data.get("analysis") or {}).get("data_collection_results") or {}
    yes = (dc.get("senior_callback_confirmed") or {}).get("value")
    if str(yes).strip().lower() not in ("true", "yes", "1"):
        return ""
    reason = str((dc.get("callback_reason") or {}).get("value") or "other").strip().lower()
    pref = str((dc.get("callback_preferred_time") or {}).get("value") or "")
    out = request_callback(store, adapter, ctx, reason, pref, True, now, source="post_call")
    return "created" if out.get("created") else "already"


def _time_limit_followup(ev, ctx, store, now) -> bool:
    """The call was cut by the time limit and no senior callback was booked → a 'followup' item on the
    Senior callbacks page, so an agent checks in instead of the note waiting to be found. Kind 'followup',
    not 'senior': the customer was promised nothing, and Tanya only treats 'senior' as already promised."""
    if not ev["end_kind"].startswith("time_limit") or ev["callback_asked"]:
        return False
    from .voice_context import _user_key, callback_window
    due_by, _ = callback_window(now)
    key = _user_key(ctx)
    store.emit([{"type": "callback", "user_id": key, "at": iso(now),
                 "id": f"CB-V-{key}-{now.strftime('%Y%m%d%H%M%S')}-F", "kind": "followup", "state": "requested",
                 "requested_at": iso(now), "when_text": "Nothing promised: call hit the time limit",
                 "slot": {"reason": "call_time_limit", "preferred_time": "", "due_by": iso(due_by),
                          "source": "post_call", "el_conversation_id": ev["el_conversation_id"],
                          "sb_conversation_id": ctx.get("sb_conversation_id") or ""}}])
    return True


def handle_post_call(payload: dict, store, adapter, now, llm=None) -> dict:
    """Process one verified ElevenLabs webhook. Always returns a small status dict (never raises
    for bad input: ElevenLabs disables a webhook that keeps failing). With llm, the facts he
    stated on the call go into Tanya's memory too."""
    if payload.get("type") != "post_call_transcription":
        return {"ok": True, "ignored": payload.get("type")}
    data = payload.get("data") or {}
    agent = S.env("ELEVENLABS_AGENT_ID")
    if agent and data.get("agent_id") != agent:
        return {"ok": True, "ignored": "other_agent"}
    dyn = (data.get("conversation_initiation_client_data") or {}).get("dynamic_variables") or {}
    start = (data.get("metadata") or {}).get("start_time_unix_secs")
    ctx = verify_context(dyn, start, S.env("TANYA_VOICE_CTX_SECRET"))
    if not ctx:
        return {"ok": True, "ignored": "no_valid_context"}     # e.g. a call not started from the PWA
    if not store.once(f"voice:{data.get('conversation_id')}", days=30):
        return {"ok": True, "duplicate": True}

    ev = build_event(data, ctx, now)
    store.emit([ev])
    learned = _remember(store, ev["user_id"], ev, call_facts(llm, ev), now)
    callback = _senior_callback_safety_net(data, ctx, store, adapter, now)
    ev["callback_asked"] = ev["callback_asked"] or bool(callback)
    ev["followup"] = _time_limit_followup(ev, ctx, store, now)
    # "Voice call completed" in the chat, if the PWA didn't already post it (browser died, old app).
    from .voice_live import call_ended
    chat = call_ended(store, adapter, ctx, dyn.get("ctx_sig"), data.get("conversation_id"), now,
                      duration_secs=ev.get("duration_secs"), started_unix=start)
    return {"ok": True, "stored": True, **({"facts": learned} if llm else {}),
            **({"senior_callback": callback} if callback else {}), **({"followup": True} if ev["followup"] else {}),
            **({"chat_message": True} if chat.get("chat_message") else {})}


def fetch_conversation(conversation_id: str) -> dict:
    """The same 'data' object the post-call webhook carries, read from the ElevenLabs API."""
    import httpx
    r = httpx.get(f"https://api.elevenlabs.io/v1/convai/conversations/{conversation_id}",
                  headers={"xi-api-key": S.env("ELEVENLABS_API_KEY")}, timeout=20)
    r.raise_for_status()
    return r.json()


def backfill(conversation_ids, store, adapter, now):
    """Process calls the webhook missed (webhook not set up yet, tunnel down…). Idempotent."""
    return {cid: handle_post_call({"type": "post_call_transcription", "data": fetch_conversation(cid)},
                                  store, adapter, now) for cid in conversation_ids}


if __name__ == "__main__":
    # python -m tanya.voice conv_abc conv_def     (needs ELEVENLABS_API_KEY in the environment)
    import sys
    from .crm_adapter import make_adapter
    from .memory_store import make_store
    from .timeutil import now as tnow
    for cid, res in backfill(sys.argv[1:], make_store(), make_adapter(), tnow()).items():
        print(cid, res)
