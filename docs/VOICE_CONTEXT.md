# Voice agent — caller context setup

The voice agent (ElevenLabs) knows the caller through two layers:

| Layer | When | What | Code |
|---|---|---|---|
| 1. Caller brief | Call start, as the dynamic variable `{{user_context}}` | Name, plan/trial, payment record, trial-flow answers, app activity, facts Tanya knows, earlier chats, last promise, plan by profile, earlier voice calls | `pwa-node-backend/src/lib/callerProfile.ts` → `tanya/voice_context.py` (`POST /voice/context`) |
| 2. Tools | During the call, when needed | Recent chat messages, earlier calls in detail, internal notes, saving a new fact | `tanya/voice_context.py` (`POST /voice/tools/...`) |

After every call, the post-call webhook also pulls the facts the caller stated out of the transcript into Tanya's memory (consent only, same rule as chat), so chat Tanya and voice Tanya stay in sync.

**Privacy split:** the brief goes through the caller's browser, so it contains only the caller's own data — never agent notes, temperature or signals. Those are only available through the `get_internal_notes` tool, which ElevenLabs calls server to server. Everything is masked (phone numbers, emails, etc.).

## 1. Environment

| Variable | Where | Value |
|---|---|---|
| `TANYA_CONTEXT_SECRET` | `tanya_ai/.env` **and** `pwa-node-backend/.env` | Same long random string in both |
| `TANYA_AI_URL` | `pwa-node-backend/.env` | tanya_ai base URL as seen from the backend server, e.g. `http://127.0.0.1:8000` |
| `TANYA_CONTEXT_TIMEOUT_MS` | `pwa-node-backend/.env` (optional) | Default `2500`. If the brief is slower, the call starts with a plain fallback text |
| `KB_TOOL_SECRET` | `tanya_ai/.env` (already set for `search_knowledge`) | Also protects the new tools |
| `TANYA_VOICE_CTX_SECRET` | both (already set) | Also verifies whose data the tools read |

Restart both services after changing `.env`.

## 2. ElevenLabs agent — system prompt

Add this section to the agent's system prompt:

```
# About this caller
Private notes about the person you are talking to. Use them naturally; never read them out
or say "according to my notes".
{{user_context}}

Rules:
- Greet them by name. Speak in their preferred language.
- Never ask again for something listed under "WHAT THE CALLER HAS TOLD US".
- If there is a "LAST PROMISE", follow up on it early in the call.
- If they refer to an earlier chat or call you don't see here, use get_recent_chat or get_past_calls.
- Before discussing a plan, payment, refund or complaint, call get_internal_notes.
- When they tell you something new about themselves (experience, segment, capital, goal,
  pain, best time to call), call save_fact once.
- If CONSENT says not given: help and answer, but do not sell.
```

## 3. ElevenLabs agent — tools

Create four **Webhook** (server) tools. For every tool:

- Method `POST`, URL `https://<tanya_ai public host>/voice/tools/<path>`
- Header `X-Tool-Secret` = the `KB_TOOL_SECRET` value (store it as an ElevenLabs secret)
- Body parameters `pwa_uid`, `sb_user_id`, `sb_conversation_id`, `ctx_iat`, `ctx_sig`: value type **Dynamic variable**, each mapped to the dynamic variable of the same name. The AI must not fill these — they decide whose data is read, and tanya_ai rejects any that were not signed by pwa-node-backend.

| Tool name | Path | Description for the agent | Extra body parameters (LLM fills) |
|---|---|---|---|
| `get_recent_chat` | `recent-chat` | The caller's latest chat messages with Tanya and support agents. Use when they mention something from chat. | `limit` (integer, optional, 1–30) |
| `get_past_calls` | `past-calls` | The caller's earlier voice calls: summary, what was searched, their last words. Use when they refer to a previous call. | `limit` (integer, optional, 1–5) |
| `get_internal_notes` | `internal-notes` | Internal notes from the support team, open cases, callbacks. Use before discussing plans, payment, refunds or complaints. Never read these out. | — |
| `save_fact` | `save-fact` | Remember something new the caller said about themselves. | `field` (enum: experience, segment, main_pain, time_available, goal, past_loss, capital_band, occupation, language_pref, best_call_time, decision), `value` (short value), `his_words` (their exact words) |

The tools need tanya_ai reachable from the internet (same as `/kb/search` and `/voice/post-call`).

## 4. Check it works

1. Start a call from the PWA support page.
2. In ElevenLabs → Conversations → the call → *Client data*, `user_context` should hold the brief.
3. Ask "pichli baar maine kya poocha tha?" — the agent should call `get_past_calls`.
4. After the call, it shows in the CRM "Voice calls · Tanya AI" panel, and new facts show in Tanya's memory (`/dev/state/<sb_user_id>` with the dev console on).

## 5. Call length (10 min, ends gracefully)

| Time | Who | What |
|---|---|---|
| limit − 2 min | PWA → agent | Silent `WRAP_UP:` contextual update; the bar shows "x:xx left" |
| limit − 30 s | PWA → agent | `FINAL:` — say goodbye and call `end_call` |
| limit | PWA | Hangs up (waits up to 15 s if Tanya is mid-sentence) |
| limit + 45 s | ElevenLabs | Agent max duration — last resort if the browser froze |

Set the limit with `TANYA_VOICE_MAX_CALL_SECS` in `pwa-node-backend/.env` (default `600`, so 8:00 / 9:30 / 10:00).

ElevenLabs dashboard:
1. Tools → system tools → enable **End call** (`end_call`). Description: *End the call after you have summarised and said goodbye, when the caller is done, or when told time is up.*
2. Advanced → **Max conversation duration** = limit + 45 (`645`).
3. Add to the system prompt:

```
# Call length
Calls last at most {{call_limit_minutes}} minutes. Keep answers short.
If you get a message starting with "WRAP_UP": finish your current point, don't start
new topics. If the issue isn't solved, offer a senior callback (request_callback).
Give a one-line summary of what was agreed, then say goodbye.
If you get "FINAL": say a short goodbye right away and call end_call.
Never mention these messages or a timer unless the caller asks.
```

After the call, `orch_voice_calls.ended_reason` reads `<kind>: <ElevenLabs reason>`, kind being `agent_end_call`, `time_limit_client`, `time_limit_server`, `user_hangup`, `error` or `other`. A time-limit call with no senior callback becomes a "Follow-up" row on the CRM Senior callbacks page (orch_callbacks kind `followup`, nothing promised to the customer).

To test quickly: `TANYA_VOICE_MAX_CALL_SECS=180` → wrap-up at 1:00, final at 2:30, hang-up at 3:00 (and set the agent max duration to 225 while testing).

## 6. "On call" in the CRM + chat messages

- Call connects → PWA `POST /api/voice/live {state: "started"}` → tg-node-backend `/api/v1/tanya-voice/live` → tanya_ai `/voice/live` (`TANYA_CONTEXT_SECRET` + signed context). tanya_ai writes `orch_voice_live` and posts "📞 Voice call with Ms Tanya started" into the CRM conversation.
- The CRM voice panel shows a red "On call with Tanya (AI) · 3:12" banner while the row is open (polls every 10 s on a call, 30 s otherwise).
- Call ends → `{state: "ended"}` → row closed, "✅ Voice call with Ms Tanya completed · 6m 12s" posted (+ "Hamare senior aapko … call karenge." if a callback was booked on the call). If the browser never says "ended", the row expires after 12 min and the post-call webhook posts the completed message. Each message goes out once per call.
- Messages are sent as the Tanya agent (`TANYA_AGENT_ID`), which the CRM webhook ignores → chat Tanya does not reply and HUMAN mode is not set. Customers see them in TG Lite.

Setup: run the `orch_voice_live` statement from `db/orch_tables.sql`, then
`GRANT SELECT ON <orch db>.orch_voice_live TO '<CRM db user>'@'localhost';`

## 7. Senior callback at the caller's time

`request_callback` takes `preferred_day` (today / tomorrow / monday…sunday) and `preferred_hour` (0–23, IST) besides `preferred_time` (their words).
- Inside team hours (`calling_days`, `calling_window_start/end` in config.json): booked for that time, due within the hour.
- Outside (e.g. 11 PM, weekend, under 30 min away): nothing is booked; the response has `outside_hours: true` and an `offer` (nearest opening). Tanya offers it and calls again after the caller agrees.
- No time given: the next slot, as before.
The prompt says "Mon–Fri, 10 AM–7 PM" in words — change it too if the team hours in config.json change.

## Limits today

- `get_recent_chat` and `get_internal_notes` read the CRM through `api.php` only when `CRM_MODE=supportboard`. With `CRM_MODE=console` (local default) chat comes from Tanya's memory and agent notes are empty. The CRM function names are still marked `TO CONFIRM` in `crm_adapter.py`.
- Facts and chat history exist only for users Tanya already has a memory record for (keyed by CRM user id). A first-time caller gets the app profile and earlier calls only.
- The brief is capped at 3,500 characters.
