"""AI-C21 LLM Provider Adapter — one door to Anthropic, OpenAI and Google.

Plain English:
- Every AI call in the project goes through `LLM.call(...)`. Nothing else talks to a provider.
- It picks the fast or the detailed model, applies the time limit, tries the fallback
  provider if the first one fails, and records tokens and cost in rupees for the ledger.
- No LangChain: direct HTTPS calls keep prompts, cost and time limits fully visible.
- 'mock' provider = rule-based answers with no key, for tests and the dev console.
"""
import json
import re
import sys
import threading
import time
from dataclasses import dataclass, field

import httpx

from .settings import S


@dataclass
class LLMResult:
    ok: bool
    text: str = ""
    provider: str = ""
    model: str = ""
    purpose: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    ms: int = 0
    cost_inr: float = 0.0
    error: str = ""
    data: dict = field(default_factory=dict)  # parsed JSON when json_mode
    request_id: str = ""                      # the provider's own id for this call (audit / support tickets)


# One keep-alive connection pool per process (Spec 3.1 F5): no new TLS handshake per call.
# keepalive_expiry: customers are usually idle longer than httpx's default 5 s between messages, so every turn
# paid a new TLS handshake per AI call; 120 s keeps the connection warm across a normal conversation.
HTTP = httpx.Client(limits=httpx.Limits(max_connections=20, max_keepalive_connections=10, keepalive_expiry=120),
                    timeout=60)

# Circuit breaker for ACCOUNT-level refusals (no credit, bad key, no permission). Such an answer will not change
# in the next seconds, yet every call used to try that provider first: on 05-Oct the Anthropic account ran out of
# credit and each of the 3 calls per reply first got a refusal (0.4-1.4 s) before the fallback answered. The
# provider is skipped for BREAKER_SECONDS, then tried again (it recovers by itself once the account is fixed).
BREAKER_SECONDS = 300
_DOWN = {}                      # provider -> (until_epoch, reason)
_ACCOUNT_ERRORS = ("credit balance", "billing", "authentication_error", "permission_error", "invalid x-api-key",
                   "insufficient_quota", "invalid_api_key", "account is not active")


def _account_error(e) -> str:
    """The provider refused the ACCOUNT (not this request, not a busy moment): return a short reason, else ''."""
    status = getattr(getattr(e, "response", None), "status_code", None)
    text = str(e).lower()
    try:
        text += " " + e.response.text.lower()
    except Exception:
        pass
    if status in (401, 403) or (status in (400, 402, 429) and any(k in text for k in _ACCOUNT_ERRORS)):
        return next((k for k in _ACCOUNT_ERRORS if k in text), f"HTTP {status}")
    return ""


def provider_down(provider) -> str:
    until, reason = _DOWN.get(provider, (0, ""))
    return reason if until > time.time() else ""

_RID = threading.local()
_PURPOSE = threading.local()        # which AI job is calling (understand / reply / check / ...)

# OpenAI reasoning models (GPT-6 Luna / Sol) think before answering; their default effort is "medium", which made a
# Luna turn take 12-13 s (Spec Draft 3.1 §9.1 item 1). Measured 05-Oct on one reply prompt: default 4.3-5.4 s,
# low 3.2-5.1 s, none 2.0-2.2 s. Per job, overridable in .env: OPENAI_REASONING_EFFORT_<JOB> or
# OPENAI_REASONING_EFFORT. Spec C19 sets "low" for the compliance check; understand/reply use "none" for speed —
# confirm with the per-job accuracy set (Spec §9.1 item 4) before go-live.
REASONING_DEFAULTS = {"understand": "none", "reply": "none", "check": "low"}
_NO_REASONING = set()               # models that rejected reasoning_effort once — never sent again


def reasoning_effort(purpose):
    return (S.env(f"OPENAI_REASONING_EFFORT_{(purpose or '').upper()}", "")
            or S.env("OPENAI_REASONING_EFFORT", "")
            or REASONING_DEFAULTS.get(purpose, "low")).lower()


def _remember(r):
    """Keep the provider's request id of the last HTTP answer on this thread (Anthropic: request-id, OpenAI: x-request-id)."""
    _RID.value = r.headers.get("request-id") or r.headers.get("x-request-id") or ""


def parse_json(text: str):
    """Find and read the first {...} JSON object in a model's answer."""
    if not text:
        return None
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    start = t.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(t)):
            if t[i] == "{":
                depth += 1
            elif t[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(t[start:i + 1])
                    except json.JSONDecodeError:
                        break
        start = t.find("{", start + 1)
    return None


def normalise_messages(messages):
    """Providers need: first message from the user, roles alternating. Merge neighbours."""
    out = []
    for m in messages:
        role = "assistant" if m["role"] == "assistant" else "user"
        if out and out[-1]["role"] == role:
            out[-1]["content"] += "\n" + m["content"]
        else:
            out.append({"role": role, "content": m["content"]})
    if out and out[0]["role"] == "assistant":
        out.insert(0, {"role": "user", "content": "(conversation so far)"})
    if not out:
        out = [{"role": "user", "content": "(start)"}]
    return out


# ---------------------------------------------------------------- providers
_NO_TEMPERATURE = set()   # models that rejected 'temperature' once (e.g. claude-sonnet-5-5) — never sent again


def _anthropic(model, system, messages, temperature, max_tokens, json_mode, timeout, schema=None):
    body = {"model": model, "max_tokens": max_tokens, "system": system, "messages": messages}
    if model not in _NO_TEMPERATURE:
        body["temperature"] = temperature
    if schema:
        # structured outputs: the answer is guaranteed to be JSON matching the schema
        body["output_config"] = {"format": {"type": "json_schema", "schema": schema}}
    headers = {"x-api-key": S.api_key("anthropic"), "anthropic-version": "2023-06-01",
               "content-type": "application/json"}
    r = HTTP.post("https://api.anthropic.com/v1/messages", headers=headers, json=body, timeout=timeout)
    if r.status_code == 400 and "temperature" in r.text and "temperature" in body:
        _NO_TEMPERATURE.add(model)             # newer models accept only their default temperature
        body.pop("temperature")
        r = HTTP.post("https://api.anthropic.com/v1/messages", headers=headers, json=body, timeout=timeout)
    _remember(r)
    if r.status_code == 400:                   # say WHY the request was refused (was: a bare "400 Bad Request")
        raise httpx.HTTPStatusError(f"400 from Anthropic: {r.text[:300]}", request=r.request, response=r)
    r.raise_for_status()
    j = r.json()
    text = "".join(b.get("text", "") for b in j.get("content", []) if b.get("type") == "text")
    u = j.get("usage", {})
    return text, u.get("input_tokens", 0), u.get("output_tokens", 0)


def _openai(model, system, messages, temperature, max_tokens, json_mode, timeout, schema=None):
    body = {"model": model, "messages": [{"role": "system", "content": system}] + messages,
            "max_completion_tokens": max_tokens, "temperature": temperature}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    effort = reasoning_effort(getattr(_PURPOSE, "value", ""))
    if effort and effort != "default" and model not in _NO_REASONING:
        body["reasoning_effort"] = effort
    headers = {"Authorization": f"Bearer {S.api_key('openai')}", "content-type": "application/json"}
    r = HTTP.post("https://api.openai.com/v1/chat/completions", headers=headers, json=body, timeout=timeout)
    if r.status_code == 400 and "reasoning_effort" in r.text and "reasoning_effort" in body:
        _NO_REASONING.add(model)               # not a reasoning model: send without it from now on
        body.pop("reasoning_effort")
        r = HTTP.post("https://api.openai.com/v1/chat/completions", headers=headers, json=body, timeout=timeout)
    if r.status_code == 400 and "temperature" in r.text:
        body.pop("temperature", None)          # some models accept only their default temperature
        r = HTTP.post("https://api.openai.com/v1/chat/completions", headers=headers, json=body, timeout=timeout)
    _remember(r)
    r.raise_for_status()
    j = r.json()
    text = j["choices"][0]["message"].get("content") or ""
    u = j.get("usage", {})
    return text, u.get("prompt_tokens", 0), u.get("completion_tokens", 0)


def _google(model, system, messages, temperature, max_tokens, json_mode, timeout, schema=None):
    contents = [{"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": m["content"]}]}
                for m in messages]
    cfg = {"temperature": temperature, "maxOutputTokens": max_tokens}
    if model.startswith("gemini-3"):
        # Gemini 3 'thinking' tokens count toward maxOutputTokens: keep thinking low and give it its
        # own room, else the answer (e.g. the understand JSON) is cut off mid-way.
        cfg["thinkingConfig"] = {"thinkingLevel": S.get("google_thinking_level", "low")}
        cfg["maxOutputTokens"] = max_tokens + S.get("google_thinking_extra_tokens", 1024)
    if json_mode:
        cfg["responseMimeType"] = "application/json"
    r = HTTP.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        headers={"x-goog-api-key": S.api_key("google"), "content-type": "application/json"},
        json={"systemInstruction": {"parts": [{"text": system}]}, "contents": contents, "generationConfig": cfg},
        timeout=timeout)
    _remember(r)
    r.raise_for_status()
    j = r.json()
    parts = (j.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
    text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
    u = j.get("usageMetadata", {})
    return text, u.get("promptTokenCount", 0), u.get("candidatesTokenCount", 0) + u.get("thoughtsTokenCount", 0)


def _mock(model, system, messages, temperature, max_tokens, json_mode, timeout, schema=None):
    from .llm_mock import mock_complete
    text = mock_complete(system, messages)
    _RID.value = "mock"
    return text, len(system) // 4, len(text) // 4


_PROVIDERS = {"anthropic": _anthropic, "openai": _openai, "google": _google, "mock": _mock}


# ---------------------------------------------------------------- the one door
def _log(res):
    """One line per model call in the process log: proof of which provider really answered."""
    print(f"[llm] {res.purpose} provider={res.provider} model={res.model} ok={res.ok} ms={res.ms} "
          f"tokens={res.input_tokens}/{res.output_tokens} request_id={res.request_id or '-'}"
          + (f" error={res.error[:400]}" if res.error else ""), file=sys.stderr, flush=True)


# ---------------------------------------------------------------- hedged requests (06-Oct-2026, Feature 6)
# Measured 06-Oct (OpenAI gpt-6-luna): most understand / reply / check calls answer in ~2-3 s, but about one in ten
# takes 6-11 s (provider-side). The customer waits for three calls in a row, so one slow call made a 20-30 s reply.
# Hedge: if the first request has not answered after llm_hedge_after_seconds[purpose], the SAME request is sent once
# more and the first answer to arrive is used. Cost: one extra small request only in those slow cases (the unused
# answer's tokens are not counted in cost_inr). Off: LLM_HEDGE=0 in .env, or remove the purpose from the config.
_HEDGE_POOL = None
_HEDGE_LOCK = threading.Lock()


def _hedge_after(purpose):
    if S.env("LLM_HEDGE", "1") == "0":
        return None
    v = (S.get("llm_hedge_after_seconds", {}) or {}).get(purpose)
    return float(v) if v else None


def _provider_call(provider, purpose, args, kwargs):
    """Runs in a pool thread: thread-locals (purpose -> reasoning effort, request id) are this thread's own."""
    _PURPOSE.value = purpose
    _RID.value = ""
    out = _PROVIDERS[provider](*args, **kwargs)
    return out, getattr(_RID, "value", "")


def _call_provider(provider, purpose, *args, **kwargs):
    """One provider request, hedged when the purpose has a hedge delay. Returns (text, tokens_in, tokens_out)."""
    global _HEDGE_POOL
    after = _hedge_after(purpose)
    if not after or provider == "mock":
        return _PROVIDERS[provider](*args, **kwargs)
    from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
    with _HEDGE_LOCK:
        if _HEDGE_POOL is None:
            _HEDGE_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="llm-hedge")
    first = _HEDGE_POOL.submit(_provider_call, provider, purpose, args, kwargs)
    done, _ = wait([first], timeout=after)
    futures = [first]
    if not done:
        print(f"[llm] hedge {purpose}: no answer after {after}s, second request sent", file=sys.stderr, flush=True)
        futures.append(_HEDGE_POOL.submit(_provider_call, provider, purpose, args, kwargs))
    pending, error = set(futures), None
    while pending:
        done, pending = wait(pending, return_when=FIRST_COMPLETED)
        for f in done:
            try:
                out, rid = f.result()
            except Exception as e:            # this copy failed: use the other one if it is still running
                error = error or e
                continue
            _RID.value = rid
            if len(futures) > 1:
                print(f"[llm] hedge {purpose}: answered by request {futures.index(f) + 1}", file=sys.stderr, flush=True)
            return out
    raise error


class LLM:
    def __init__(self, provider=None):
        """provider: force one provider (e.g. the morning audit uses a different one). Default: PROVIDER in .env."""
        self.usd_inr = S.get("usd_to_inr", 96)
        self.forced = provider if provider and S.api_key(provider) else None

    def call(self, purpose, tier, system, messages, json_mode=False, temperature=0.0,
             timeout=30, max_tokens=1200, schema=None) -> LLMResult:
        """purpose: understand | reply | check | note | audit. tier: fast | detailed.
        schema: JSON schema of the answer (json_mode) — Anthropic enforces it (structured outputs)."""
        # Attempts in order: main model, its 'backup' model (config models.<provider>.backup), then the
        # fallback provider. A busy/overloaded answer (429/5xx, timeout) is retried once on the same model.
        # An answer without readable JSON also moves on to the next attempt (its cost is still counted).
        attempts = []
        for provider in [self.forced or S.provider] + ([S.fallback_provider] if S.fallback_provider else []):
            for model in (S.model(provider, tier), S.raw["models"][provider].get("backup")):
                if model and (provider, model) not in attempts:
                    attempts.append((provider, model))
        # Skip a provider whose account was refused recently — unless that would leave nothing to try.
        live = [a for a in attempts if not provider_down(a[0])]
        if live and len(live) < len(attempts):
            attempts = live
        last = LLMResult(False, purpose=purpose, error="no provider tried")
        spent = 0.0                                    # cost of answers we could not use
        for provider, model in attempts:
            if provider_down(provider) and (provider, model) != attempts[-1]:
                continue                               # refused a moment ago: go straight to the next provider
            for attempt in range(2):
                t0 = time.time()
                _RID.value = ""
                _PURPOSE.value = purpose
                try:
                    text, tin, tout = _call_provider(provider, purpose, model, system, normalise_messages(messages),
                                                     temperature, max_tokens, json_mode, timeout,
                                                     schema=schema if json_mode else None)
                    p_in, p_out = S.price(model)
                    cost = (tin * p_in + tout * p_out) / 1_000_000 * self.usd_inr
                    res = LLMResult(True, text, provider, model, purpose, tin, tout,
                                    int((time.time() - t0) * 1000), round(cost + spent, 4),
                                    request_id=getattr(_RID, "value", ""))
                    if json_mode:
                        res.data = parse_json(text) or {}
                        if not res.data:
                            res.ok = False
                            res.error = "no JSON in answer"
                            spent += cost
                            last = res
                            break                      # try the next model / provider
                    _log(res)
                    return res
                except Exception as e:  # network, timeout, HTTP error — retry if busy, else next attempt
                    last = LLMResult(False, "", provider, model, purpose, ms=int((time.time() - t0) * 1000),
                                     cost_inr=round(spent, 4), error=f"{type(e).__name__}: {str(e)[:400]}",
                                     request_id=getattr(_RID, "value", ""))
                    _log(last)
                    reason = _account_error(e)
                    if reason:
                        if not provider_down(provider):
                            print(f"[llm] BREAKER {provider} skipped for {BREAKER_SECONDS}s: account refused "
                                  f"({reason}) — fix the {provider} account; fallback answers meanwhile",
                                  file=sys.stderr, flush=True)
                        _DOWN[provider] = (time.time() + BREAKER_SECONDS, reason)
                        break
                    status = getattr(getattr(e, "response", None), "status_code", None)
                    busy = status in (429, 500, 502, 503, 504) or isinstance(e, httpx.TimeoutException)
                    if not busy or attempt:
                        break
                    time.sleep(1)
        return last


# ---------------------------------------------------------------- embeddings (for RAG, AI-C05)
def embed(texts, timeout=60):
    """Vectors for the knowledge search. Returns None when no embedding provider is set.

    EMBEDDING_PROVIDER=openai|google in .env (Anthropic has no embedding API).
    Model names are in config.json 'embedding_models' — verify on first run.
    """
    prov = S.env("EMBEDDING_PROVIDER", "").lower()
    if not prov or not S.api_key(prov) or prov == "mock":
        return None
    model = S.raw.get("embedding_models", {}).get(prov)
    try:
        if prov == "openai":
            r = HTTP.post("https://api.openai.com/v1/embeddings",
                           headers={"Authorization": f"Bearer {S.api_key('openai')}"},
                           json={"model": model, "input": texts}, timeout=timeout)
            r.raise_for_status()
            return [d["embedding"] for d in r.json()["data"]]
        if prov == "google":
            r = HTTP.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:batchEmbedContents",
                headers={"x-goog-api-key": S.api_key("google")},
                json={"requests": [{"model": f"models/{model}", "content": {"parts": [{"text": t}]}}
                                   for t in texts]}, timeout=timeout)
            r.raise_for_status()
            return [e["values"] for e in r.json()["embeddings"]]
    except Exception:
        return None
    return None
