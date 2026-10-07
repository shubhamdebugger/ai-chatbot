# Ms Tanya — Redis, Qdrant and guarantee fix (FX-20)

What was set up, checked and changed in `ai-chatbot` on the `production-redis-setup` branch: where conversations are stored, how the knowledge search works, the new fixed reply for guarantee questions, the problems found while testing, and the research done for the next tasks.

**Summary**

- Conversations are now stored in **Redis**. This needed a config change only (`RUN_MODE=server`), no code change.
- Knowledge search now uses **OpenAI embeddings in Qdrant**, in a new collection `tanya_kb_openai` (15 chunks). Qdrant is shared with the `tglevels_demo_ragchatbot` project, which was approved.
- Questions about a money guarantee now get a **fixed line (FX-20) at the gate**, before any AI call, so they cost ₹0.
- Seven problems were found while testing (section 5). None of them are fixed yet.
- A daily message limit with a polite wind-down was researched (section 6.3). It is not built yet.

---

## 1. Where conversations are stored

### 1.1 How the store is chosen

`make_store()` in [tanya/memory_store.py](../tanya/memory_store.py) (line 200) picks the store from `RUN_MODE`:

| `RUN_MODE` | Store | Where the conversation goes |
|---|---|---|
| `dev` (old setting) | `FileStore` | `data/memory.json`, plus events in `data/events.jsonl` |
| `server` | `RedisStore` | Redis key `tanya:rec:{user_id}` |

With `RUN_MODE=dev`, Redis was running but never used for chats.

### 1.2 The change

Config only, in `.env` and `.env.example`:

```
RUN_MODE=server
REDIS_URL=redis://localhost:6379/0
```

Local Redis runs in Docker:

```
docker run -d --name tanya-redis -p 6379:6379 --restart unless-stopped redis:7
```

### 1.3 What is stored

- One record per user under `tanya:rec:{user_id}`: profile, facts, session, counters, mode and the `messages` list.
- Every message is added as a new entry, including repeated questions. The last 300 messages are kept.
- Each turn reads the record from Redis, writes a fresh reply, and saves the record back. Nothing is read from MySQL during a chat.
- MySQL gets a copy only in server mode, written within seconds by the `persist` worker into `orch_inbox` and `orch_replies`.

### 1.4 How it was checked

```
docker exec tanya-redis redis-cli keys "tanya:*"
docker exec tanya-redis redis-cli get tanya:rec:U1001 | ConvertFrom-Json | Select-Object -ExpandProperty messages | Select-Object -Last 4 n, role, text
```

Messages sent in the dev console appeared in the record.

---

## 2. Knowledge search — Qdrant and embeddings

### 2.1 What was found

| Item | Value |
|---|---|
| Qdrant | `localhost:6333`, version 1.18.2, shared with `tglevels_demo_ragchatbot` |
| Collection `knowledge_vectors` | Belongs to the other project |
| Collection `tanya_kb` | 15 points, vector size **384** (a small local model). OpenAI vectors are 1536, so they cannot go in it. |

### 2.2 The change

A new collection for OpenAI embeddings. In `.env` and `.env.example`:

```
EMBEDDING_PROVIDER=openai
QDRANT_URL=http://localhost:6333
QDRANT_COLLECTION=tanya_kb_openai
KNOWLEDGE_BACKEND=qdrant
```

The old `tanya_kb` collection was left untouched.

Checked: `tanya_kb_openai` has 15 points with status green, and `KnowledgeIndex().has_vectors` returns `True`.

### 2.3 How the search works

In [tanya/knowledge.py](../tanya/knowledge.py):

1. **Chunks.** Each lesson or FAQ file in `content/kb` is cut into chunks (`_split`, lines 88–101). An answer of 900 characters or less stays whole; a longer one is split at blank lines. Each chunk is searched by its title, question forms and text.
2. **Words.** Text is lower-cased and split into words, filler words are dropped (`the`, `is`, `hai`, `kya`), and Hinglish words are mapped to English (`nuksaan` → `loss`, `sl` → `stop-loss`) (`tokens()`, lines 52–60).
3. **Vectors.** At start-up each chunk is sent to OpenAI and its vector is stored in Qdrant. Each customer question is turned into a vector the same way.
4. **Score.** Keyword score (BM25) and meaning score (cosine similarity) are blended 50/50. The top chunks go into Tanya's prompt, and she answers only from them.

Customer conversations are **not** chunked or embedded. They stay in Redis.

---

## 3. Guarantee questions — fixed line FX-20

### 3.1 The problem

The customer wrote *"gurrantee hai ki mere paise double hojaaenge"*. The AI wrote a correct denial, but the code net in [tanya/guard_output.py](../tanya/guard_output.py) blocked it:

```
P4 Sure-shot claim | P5 Accuracy or certainty claim:
"Markets uncertain hote hain, aur 'sure-shot' ya '100%' ka wada warning sign hai."
```

The net checks each sentence alone. This sentence warns against "100%" promises but has no denial word such as "nahi", so it was treated as a promise. The customer got the generic FX-12 line instead.

### 3.2 The change

A fixed line that is always the same and needs no AI call:

> *"Nahi, paise double hone ki guarantee koi SEBI-registered research provider nahi de sakta. Markets uncertain hote hain, aur 'sure-shot' ya '100%' ka wada warning sign hai."*

| File | Change |
|---|---|
| [content/fixed_lines.json](../content/fixed_lines.json) | New line FX-20 |
| [tanya/policy.py](../tanya/policy.py) | In `gate()`, after the injection check and before the spend ceiling: a guarantee word **and** a money word, with **no** grievance word → `GATE_FIXED`, FX-20, reason `R07G` |
| [tanya/turn.py](../tanya/turn.py) | Passes the masked text into `gate()` |
| [tanya/understand.py](../tanya/understand.py) | New label `asks_guarantee`, with a keyword backup |
| [tanya/decider.py](../tanya/decider.py) | Backup row `R07G` before the no-consent row (R08), for wordings the gate keywords miss |
| [tests/run_tests.py](../tests/run_tests.py) | New test `guarantee_question_gives_fixed_line` |

Keywords used at the gate:

| Group | Words |
|---|---|
| Guarantee | guarantee, gurantee, gurrantee, guaranty, pakka, sure shot, 100% |
| Money | paisa, paise, money, return, profit, double, dugna |
| Grievance (skip FX-20) | fraud, refund, cheat, dhokha, complaint |

A message with a grievance word still goes to the complaint flow (case number and team), not FX-20.

### 3.3 Result

- The test passes: FX-20 is shown and `llm_calls` is empty, so the turn costs ₹0.
- Rule tests: 35 passed, 1 failed. The failure is `persister_writes_orch_tables` (MySQL access denied for user `tg_level`), which is unrelated to this change.

### 3.4 Needs sign-off

1. The FX-20 wording (compliance text, input D2).
2. Adding row R07G to the Spec (Spec → Python → Node).

---

## 4. Guardrails — when each one triggers

| Order | Guard | Where | Triggers on | Result |
|---|---|---|---|---|
| 1 | Masking | [tanya/guard_input.py](../tanya/guard_input.py) | Phone, email, PAN, card, Aadhaar, OTP, bank number | Hidden before any AI sees it |
| 2 | Injection check | [tanya/guard_input.py](../tanya/guard_input.py) | "Ignore your instructions" and similar | FX-12 |
| 3 | Gate | [tanya/policy.py](../tanya/policy.py) | HUMAN mode or kill switch `stopped` | Silent, no AI call |
| | | | Daily AI spend ceiling reached (₹200) | FX-05 |
| | | | Money-guarantee question | FX-20 |
| 4 | Route (layer 1) | [tanya/decider.py](../tanya/decider.py) | Trade question (direction, level, strike, stock) | FX-03 refusal and a lesson |
| 5 | Code net (layer 3) | [tanya/guard_output.py](../tanya/guard_output.py) | Promise words without a denial in the same sentence; hard words (strike, target, entry level); an unapproved ₹ figure | Reply replaced by FX-12 |
| 6 | AI check (layer 2) | [tanya/guard_output.py](../tanya/guard_output.py) | Direction view, levels, return promise, unapproved price or benefit, invented human life, "tum", superlatives | Reply replaced by FX-12 |

- The AI check runs only if the code net passes. When the net blocks, the console shows `ai_check: null`.
- When a reply is replaced, the blocked text is kept in the trace and shown in orange in the dev console. The customer never sees it.
- Fixed lines are pre-approved and are not checked.

---

## 5. Problems found while testing

| # | Problem | Seen as | Suggested fix | Needs approval |
|---|---|---|---|---|
| 1 | Good warnings blocked by the code net | "100%" / "sure-shot" in a warning sentence without "nahi" → FX-12 | Add `warning`, `savdhaan`, `beware`, `avoid` to `denial_words` in [content/guard_lists.json](../content/guard_lists.json) | Yes — compliance (D3) |
| 2 | AI check blocks any mention of the trial | `ai_problems`: "Claims a trial and describes its benefit, neither of which is in the approved text" | Add an approved trial description to the content files | Yes — content |
| 3 | Distress blocks the customer's own purchase request | After "mere paise doob gae", "course lena hai" → FX-12 instead of FX-14 | A kind fixed line for "wants to buy, but distressed this session" | Yes — policy |
| 4 | Users start without consent in server mode | After **Reset user**, `consent: NO` and no known facts, because no CRM loader runs | Fall back to `content/test_users.json` when `CRM_MODE=console` | No |
| 5 | AI promises a hand-over that code did not do | "Request team tak pahuchaungi" saved as `last_promise`, but no callback created | Make promises match the actions the code takes | No |
| 6 | MySQL test fails | Access denied for user `tg_level` | Correct MySQL details | No |
| 7 | Redis test wipes the database | `redis_store_and_streams` calls `r.flushdb()` | Use a separate `REDIS_TEST_URL` | No |

---

## 6. Research

### 6.1 Response time and AWS

See [PERFORMANCE_AND_AWS.md](PERFORMANCE_AND_AWS.md).

### 6.2 Hand-over to a person

See [HANDOVER.md](HANDOVER.md). Its four known gaps are still open:

1. Callbacks never move past `requested`.
2. Purchase and call requests are not routed to a person.
3. Tanya is silenced only when staff type a reply.
4. No production path to hand a chat back to the bot, other than the 12-hour timer.

### 6.3 Daily limit with a polite wind-down (not built)

The aim: each lead has a daily limit. When the lead gets close to it, the backend raises an alert and Tanya brings the conversation to a natural close, without the customer feeling pushed away.

```
every message ──► usage for this lead today
                    │
                    ├─ below 80% ──► normal chat
                    ├─ 80%       ──► event limit_near + WIND_DOWN add-on
                    ├─ 100%      ──► event limit_reached + FX-21 close (₹0) + session ends
                    └─ after     ──► FX-22 short reply (₹0) until 00:00 IST reset
```

| Part | Plan | Where |
|---|---|---|
| Limit | `daily_limit_mode` (messages, minutes or level questions), `daily_limit_value`, `wind_down_start_percent` (80) | [config.json](../config.json) |
| Counters | `user_msgs_today`, `minutes_today`, reset at 00:00 IST | `ensure_day()` in [tanya/memory_model.py](../tanya/memory_model.py) |
| Alert | Events `limit_near` and `limit_reached` → `orch_alerts`, shown in the Lead Brief | [tanya/turn.py](../tanya/turn.py) |
| Wind-down | Add-on `WIND_DOWN`: Tanya connects what this customer discussed today and gives one practical next step on that topic | [tanya/decider.py](../tanya/decider.py), [tanya/prompts.py](../tanya/prompts.py) |
| Close | FX-21, with today's topic filled in by code from the last lesson used | [content/fixed_lines.json](../content/fixed_lines.json) |

Wording rules:

- No mention of a limit, of time, or of a "last question".
- Built from the customer's own conversation, not a generic line.
- Always leaves a way to reach the team for anything urgent.
- Tanya stays disclosed as an AI (FX-01, FX-11), as the Spec and compliance require.

Never blocked by the limit: complaints, a request for a person, distress, and app or payment problems. In the decider the limit rows come after these rows.

Decisions needed:

1. Limit by messages, minutes or level questions.
2. The number.
3. Different limits for trial and paid users.
4. Approval of the FX-21 and FX-22 wording.

---

## 7. Commits on `production-redis-setup`

| Commit | Change |
|---|---|
| `aa3724f` | `.env.example`: `RUN_MODE=server` and Qdrant settings |
| `75224f8` | `.env.example`: OpenAI embeddings, collection `tanya_kb_openai` |
| `064a3c1` | Guarantee questions: fixed line FX-20 at the gate, no AI cost |

`.env` is never committed.

---

## 8. What production still needs

1. Redis on the server with AOF on (`--appendonly yes`), so chats survive a restart.
2. `RUN_MODE=server` with the four workers running as services: `turn`, `persist`, `loader`, `sessions`.
3. MySQL with the `orch_` tables from `db/orch_tables.sql`.
4. The CRM connection: every `TO CONFIRM` in [tanya/crm_adapter.py](../tanya/crm_adapter.py).
