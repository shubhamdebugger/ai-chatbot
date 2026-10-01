# Observability in ai-chatbot

There is no third-party observability stack (no Prometheus, OpenTelemetry, Langfuse, Sentry or Datadog), no `/metrics` endpoint and no structured logging. Observability is hand-rolled: a per-call usage record, a per-turn trace, a Redis cost ledger, a spend alert, Redis error streams, `/health` and a dev console.

## 1. Per-LLM-call usage

Defined in `tanya/llm.py:21-33` (`LLMResult`) and collected in `tanya/turn.py:38-41` (`_usage`, called at `turn.py:101`, `229`, `265`).

**Fields:** purpose, provider, model, `in` tokens, `out` tokens, `ms`, `cost_inr`, `ok`, `error`.

**Token counts** come straight from each provider's response:

| Provider | Input | Output |
|---|---|---|
| Anthropic | `usage.input_tokens` | `usage.output_tokens` |
| OpenAI | `usage.prompt_tokens` | `usage.completion_tokens` |
| Google | `promptTokenCount` | `candidatesTokenCount + thoughtsTokenCount` |
| Mock | `len(system)//4` | `len(text)//4` |

**Latency:** `ms = int((time.time() - t0) * 1000)`. The clock starts just before each provider attempt, so retries and fallbacks are each timed separately. Failed attempts are recorded too, with `error = "<ExcType>: <msg[:200]>"`. A call that returns without valid JSON in `json_mode` is `ok=False` with `error="no JSON in answer"`.

**Retries:** 429, 500, 502, 503, 504 or an httpx timeout is retried once after 1 second. After that the call moves to the backup model, then the fallback provider.

**Cost:**

```
cost_inr = (tokens_in * price_in + tokens_out * price_out) / 1_000_000 * usd_to_inr   # rounded to 4 decimals
```

- Prices come from `config.json` `prices_usd_per_million_tokens`, via `S.price(model)` (`settings.py:70-72`). An unknown model gives `[0, 0]`, so its cost is 0.
- `usd_to_inr` is `S.get("usd_to_inr", 96)`.
- Cached-token pricing is not modelled.

## 2. Turn cost and daily spend ledger

- **Turn cost:** `turn.py:313` — `round(sum(c["cost_inr"] for c in st["llm_calls"]), 4)`, stored as `trace["cost_inr"]`.
- **Daily ledger (company-wide, not per user):** `turn.py:314` adds the turn cost to the day's total.
  - Redis: `INCRBYFLOAT tanya:ledger:{YYYY-MM-DD}` plus a 3-day `EXPIRE` (`memory_store.py:168-175`). `ledger_get` reads the same key, defaulting to 0.0.
  - Dev mode: `db["ledger"][day]` in the file store (`memory_store.py:78-86`).
  - The day key is the IST calendar day (`timeutil.py:39`).
  - The ledger is not copied to MySQL (`redis.md:234`).
- **Displayed as:** `trace["spend_today_inr"] = round(today_total, 2)` (`turn.py:334`).

## 3. Spend ceiling and alert

- **Ceiling gate** (`policy.py:50-52`): if `ledger_get(now) >= daily_ai_spend_ceiling_inr` (₹200), the turn is blocked with `GATE_FIXED, "FX-05", "R01"`.
- **Alert** (`policy.py:56-58`): `spend_alert = ledger_get >= ceiling * spend_alert_percent / 100`, with 80% configured. Stored as `trace["spend_alert"]`.

## 4. Per-turn trace

Built at `turn.py:316-336` (AI-C15).

**Contents:**
- Identity and timing: `at`, `user_id`, `kind`, `session`, `trial_day`
- Gate and input checks: `gate`, `masked`, `injection`
- Understanding: `language`, `mood`, `labels_on`, `understand_failed`, `new_facts`
- Decision: `action`, `reason`, `addon`, `addon_detail`, `tier`
- Retrieval and guard: `knowledge`, `golden`, `guard`, `bubbles`
- Lead state: `temperature`, `suppressed`
- Counters: `education_used`, `education_limit` (default 30), `failed_this_session`, `profile_q_this_session`
- Usage: `llm_calls`, `cost_inr`, `spend_today_inr`, `spend_alert`
- Misc: `content_version`, `notes`, `cold_start`

**Emission:** `_ev(st, "trace", ...)` at `turn.py:337`, then `store.emit(events)`.
- Redis: `XADD tanya:persist` with `maxlen=1_000_000` (`memory_store.py:160-166`).
- Dev: appended as JSON lines to the file store's `events_path` (`memory_store.py:71-76`).

**Persistence** (`workers.py:180-188`):
- Each trace becomes one row in `orch_reply_trace` (user_id, action, reason, cost_inr, trace JSON, at).
- Each `llm_calls` entry becomes one row in `orch_ai_usage` (purpose, provider, model, tokens_in, tokens_out, ms, cost_inr, ok, error, at).
- Schema: `db/orch_tables.sql:43-56` (`orch_ai_usage`, index `k_usage_time`) and `:157-165` (`orch_reply_trace`).

## 5. Reliability signals

In `tanya/streams.py` and `tanya/workers.py`.

- **`tanya:errors` stream** (`streams.py:71`): every handler exception is logged as `"Type: msg"[:500]`, capped at 10,000 entries.
- **`tanya:dead` stream** (`streams.py:81-86`): a message goes here after `max_deliveries_before_dead_letter` (5) failed deliveries, then is acknowledged. Maxlen is 100,000.
- **Reclaim:** messages idle longer than `reclaim_idle_seconds` (60) are reclaimed; the check runs every 15 seconds (`streams.py:105`).
- **Alert events** saved to `orch_alerts` (`workers.py:171`, `209-211`): `crm_write_failed` (`workers.py:90`), `loader_failed` (`workers.py:272`), `delivery_failed`, `case_*`.

## 6. Health and display

- **`GET /health`** (`gateway.py:43-54`) returns `ok`, `run_mode`, `provider`, `fallback`, `langgraph`, `knowledge_chunks`, `vectors`, `vector_store`, `qdrant_error`, `content_version` and `killswitch`. It does not ping Redis or MySQL and measures no latency.
- **Dev console** (`static/devconsole.html:86-87`): an "AI calls and cost" table (purpose, model, in/out tokens, ms, ₹), then "Turn ₹{cost_inr} · spend today ₹{spend_today_inr}" with a warning when `spend_alert` is true.
- **Kill switch:** `POST /dev/killswitch` (`gateway.py:195-202`) sets `tanya:killswitch` to off, limited or stopped.
- `/dev/*` endpoints return 404 unless `DEV` is true (`RUN_MODE != server` or `DEV_CONSOLE=1`).

## 7. Configuration

**Env vars** (names from `.env.example`): `PROVIDER`, `FALLBACK_PROVIDER`, `AUDIT_PROVIDER`, `EMBEDDING_PROVIDER`, `RUN_MODE`, `DEV_CONSOLE`, `USE_LANGGRAPH`, `REDIS_URL`, `MYSQL_HOST`, `MYSQL_PORT`, `MYSQL_USER`, `MYSQL_PASSWORD`, `MYSQL_DB`. None of them configures a metrics or tracing backend.

**`config.json` keys:** `daily_ai_spend_ceiling_inr`, `spend_alert_percent`, `usd_to_inr`, `prices_usd_per_million_tokens`, `max_deliveries_before_dead_letter`, `reclaim_idle_seconds`, `education_questions_per_day`.

## 8. Product counters (not observability)

- `counters.education_used`: incremented at `turn.py:293`, reset daily (`memory_model.py:66-70`), limited by `education_questions_per_day` (`decider.py:76`).
- `counters.user_msgs_total`: incremented at `turn.py:64`, used at `decider.py:124` and `scoring.py:44`.
- Session counters are persisted to `orch_lead_state.counters` (`workers.py:191-199`).

## 9. Gaps

- No latency percentiles or aggregation.
- No whole-turn wall-clock timing, only per-LLM-call `ms`.
- No exported counters or histograms, no `/metrics` endpoint, no structured logging. The only `print` calls are `workers.py:342` and `morning_audit.py:86`.
- No per-user cost totals except by summing `orch_ai_usage` rows.
- Cached-token pricing is not modelled.
- `morning_audit.py` does not use the ledger.
- `docs/JUNIOR_TASKS.md` lists the nightly Redis-to-MySQL audit (S5.1) and the catch-up poller as to-do. The live status (thinking/typing) is not done (S2.6).
