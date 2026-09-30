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
def _anthropic(model, system, messages, temperature, max_tokens, json_mode, timeout):
    r = httpx.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": S.api_key("anthropic"), "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json={"model": model, "max_tokens": max_tokens, "system": system,
              "messages": messages, "temperature": temperature},
        timeout=timeout)
    r.raise_for_status()
    j = r.json()
    text = "".join(b.get("text", "") for b in j.get("content", []) if b.get("type") == "text")
    u = j.get("usage", {})
    return text, u.get("input_tokens", 0), u.get("output_tokens", 0)


def _openai(model, system, messages, temperature, max_tokens, json_mode, timeout):
    body = {"model": model, "messages": [{"role": "system", "content": system}] + messages,
            "max_completion_tokens": max_tokens, "temperature": temperature}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    headers = {"Authorization": f"Bearer {S.api_key('openai')}", "content-type": "application/json"}
    r = httpx.post("https://api.openai.com/v1/chat/completions", headers=headers, json=body, timeout=timeout)
    if r.status_code == 400 and "temperature" in r.text:
        body.pop("temperature", None)          # some models accept only their default temperature
        r = httpx.post("https://api.openai.com/v1/chat/completions", headers=headers, json=body, timeout=timeout)
    r.raise_for_status()
    j = r.json()
    text = j["choices"][0]["message"].get("content") or ""
    u = j.get("usage", {})
    return text, u.get("prompt_tokens", 0), u.get("completion_tokens", 0)


def _google(model, system, messages, temperature, max_tokens, json_mode, timeout):
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
    r = httpx.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        headers={"x-goog-api-key": S.api_key("google"), "content-type": "application/json"},
        json={"systemInstruction": {"parts": [{"text": system}]}, "contents": contents, "generationConfig": cfg},
        timeout=timeout)
    r.raise_for_status()
    j = r.json()
    parts = (j.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
    text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
    u = j.get("usageMetadata", {})
    return text, u.get("promptTokenCount", 0), u.get("candidatesTokenCount", 0) + u.get("thoughtsTokenCount", 0)


def _mock(model, system, messages, temperature, max_tokens, json_mode, timeout):
    from .llm_mock import mock_complete
    text = mock_complete(system, messages)
    return text, len(system) // 4, len(text) // 4


_PROVIDERS = {"anthropic": _anthropic, "openai": _openai, "google": _google, "mock": _mock}


# ---------------------------------------------------------------- the one door
class LLM:
    def __init__(self, provider=None):
        """provider: force one provider (e.g. the morning audit uses a different one). Default: PROVIDER in .env."""
        self.usd_inr = S.get("usd_to_inr", 96)
        self.forced = provider if provider and S.api_key(provider) else None

    def call(self, purpose, tier, system, messages, json_mode=False, temperature=0.0,
             timeout=30, max_tokens=1200) -> LLMResult:
        """purpose: understand | reply | check | note | audit. tier: fast | detailed."""
        # Attempts in order: main model, its 'backup' model (config models.<provider>.backup), then the
        # fallback provider. A busy/overloaded answer (429/5xx, timeout) is retried once on the same model.
        attempts = []
        for provider in [self.forced or S.provider] + ([S.fallback_provider] if S.fallback_provider else []):
            for model in (S.model(provider, tier), S.raw["models"][provider].get("backup")):
                if model and (provider, model) not in attempts:
                    attempts.append((provider, model))
        last = LLMResult(False, purpose=purpose, error="no provider tried")
        for provider, model in attempts:
            for attempt in range(2):
                t0 = time.time()
                try:
                    text, tin, tout = _PROVIDERS[provider](model, system, normalise_messages(messages),
                                                           temperature, max_tokens, json_mode, timeout)
                    p_in, p_out = S.price(model)
                    cost = (tin * p_in + tout * p_out) / 1_000_000 * self.usd_inr
                    res = LLMResult(True, text, provider, model, purpose, tin, tout,
                                    int((time.time() - t0) * 1000), round(cost, 4))
                    if json_mode:
                        res.data = parse_json(text) or {}
                        if not res.data:
                            res.ok = False
                            res.error = "no JSON in answer"
                    return res
                except Exception as e:  # network, timeout, HTTP error — retry if busy, else next attempt
                    last = LLMResult(False, "", provider, model, purpose, ms=int((time.time() - t0) * 1000),
                                     error=f"{type(e).__name__}: {str(e)[:200]}")
                    status = getattr(getattr(e, "response", None), "status_code", None)
                    busy = status in (429, 500, 502, 503, 504) or isinstance(e, httpx.TimeoutException)
                    if not busy or attempt:
                        break
                    time.sleep(1)
        return last


# ---------------------------------------------------------------- embeddings (for RAG, AI-C05)
def embed(texts):
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
            r = httpx.post("https://api.openai.com/v1/embeddings",
                           headers={"Authorization": f"Bearer {S.api_key('openai')}"},
                           json={"model": model, "input": texts}, timeout=60)
            r.raise_for_status()
            return [d["embedding"] for d in r.json()["data"]]
        if prov == "google":
            r = httpx.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:batchEmbedContents",
                headers={"x-goog-api-key": S.api_key("google")},
                json={"requests": [{"model": f"models/{model}", "content": {"parts": [{"text": t}]}}
                                   for t in texts]}, timeout=60)
            r.raise_for_status()
            return [e["values"] for e in r.json()["embeddings"]]
    except Exception:
        return None
    return None
