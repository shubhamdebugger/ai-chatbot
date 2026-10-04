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
4. After the call, the CRM note appears as before, and new facts show in Tanya's memory (`/dev/state/<sb_user_id>` with the dev console on).

## Limits today

- `get_recent_chat` and `get_internal_notes` read the CRM through `api.php` only when `CRM_MODE=supportboard`. With `CRM_MODE=console` (local default) chat comes from Tanya's memory and agent notes are empty. The CRM function names are still marked `TO CONFIRM` in `crm_adapter.py`.
- Facts and chat history exist only for users Tanya already has a memory record for (keyed by CRM user id). A first-time caller gets the app profile and earlier calls only.
- The brief is capped at 3,500 characters.
