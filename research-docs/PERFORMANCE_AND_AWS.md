# Ms Tanya — response time and AWS sizing

Why a reply takes about 20 seconds today, where that time goes, how to bring it to **5–10 seconds**, and what AWS server the project needs, including how the size grows when you add lanes.

**Summary**

- A normal reply makes **three AI calls one after another** (understand → reply → check), plus one embedding call. Together they take about 12–13 s. Queue waiting, failed attempts that were retried and network setup take it to about 20 s.
- The biggest single waste: the *understand* call writes about **850 tokens** to answer what are basically yes/no questions.
- Fixes 1 to 6 below (no compliance change needed) should bring a normal reply to about **6–9 s**. Fix 7 (skipping the AI check on low-risk replies) gets it to about **5–7 s**, but needs sign-off.
- **AWS:** one `t4g.medium` (2 vCPU, 4 GB) is enough for about 100 active users with 16 lanes. Go to `t4g.large` (8 GB) at 32 lanes.

---

## 1. Where the 20 seconds go

### 1.1 The steps of one turn

Every customer message runs these steps in order ([tanya/turn.py](tanya/turn.py)). Each AI step waits for the one before it.

| Step | What happens | AI call | Measured time |
|---|---|---|---|
| Queue wait | Message waits in Redis for a free turn worker | – | 0 s to **13 s or more** under load |
| load, gate | Memory record, masking, mode checks | – | < 0.1 s |
| **understand** | Labels the message (16 labels, facts, mood) | fast model | **3.5–5.7 s** |
| decide | Priority table picks one action | – | < 0.01 s |
| retrieve | Knowledge search (keyword search plus one embedding call) | OpenAI embedding | ~0.3–0.6 s |
| **compose** | Tanya's reply | reply model | **3.0–5.6 s** |
| **guard** | Regex net, then the AI compliance check | fast model | **2.6–3.4 s** |
| after | Counters, brief, trace, save | – | < 0.1 s |

Measured from `data/events.jsonl` on 30-Sep-2026:

| Turn | understand | reply | check | AI total |
|---|---|---|---|---|
| ANSWER_PRICE | 3.6 s (813 tokens out) | 5.6 s | 3.4 s | **12.5 s** |
| ANSWER | 5.7 s (896 tokens out) | 5.1 s | 2.6 s | **13.4 s** |
| ANSWER_SMALL_TALK | 4.6 s (812 tokens out) | 5.0 s | 2.9 s | **12.5 s** |
| ANSWER (bad case) | 20.0 s (timeout) | 32.9 s | 20.1 s (timeout) | **73 s** |

### 1.2 Why it is slow

1. **Three AI calls run one after another.** The check needs the finished reply, so these calls cannot simply be run at the same time.
2. **The understand call writes far too much.** The prompt ([tanya/understand.py](tanya/understand.py)) asks for all 16 labels, each with `on`, `evidence` and `confidence`, even when they are all false. That is about 850 output tokens. Output tokens are the slowest part of any AI call, so most of the 3.5–5.7 s comes from writing labels that are switched off.
3. **Failed attempts and retries are hidden.** [tanya/llm.py](tanya/llm.py) works through a fallback chain:
   - main model
   - wait 1 s, then retry the main model
   - backup model
   - fallback provider

   The trace records only how long the **last** attempt took. So failed attempts cost time but never appear in the numbers. Examples:
   - Until 1-Oct-2026 every Claude call was rejected (the API key was not tied to a workspace), and each turn fell back to OpenAI.
   - Claude Sonnet 5.5 still rejects every "detailed" reply because the code sends `temperature`.
4. **The timeouts are long.** They are set to 20 s / 40 s / 20 s in [config.json](config.json). One slow provider can use the whole limit, and after a timeout the same model is tried a second time. That is how the 73 s turn happened.
5. **A new HTTPS connection for every call.** Each `httpx.post` opens a new TLS connection, costing about 0.1–0.3 s on each of the 3–4 calls per turn.
6. **Customers queue behind each other.** In server mode, `run_linux.sh` starts **one** turn worker for all 8 lanes. Turns run one at a time across all users. If two customers write together, the second waits for the first turn to finish before theirs starts.

---

## 2. How to reach 5–10 seconds

Ordered by benefit. The time saved assumes a normal turn on Claude Haiku 4.5.

| # | Change | Where | Time saved | Needs approval |
|---|---|---|---|---|
| 1 | **Understand returns only the labels that are on.** Output drops from ~850 to ~100–150 tokens. Also lower `max_tokens` to about 400. | `tanya/understand.py` (prompt and `normalise`), mock in `tanya/llm_mock.py` | **3–4 s** | No |
| 2 | **One turn worker per lane, or per 2 lanes**, instead of one worker for everything | `run_linux.sh`, systemd | removes queue waiting (0–13 s or more) | No |
| 3 | **Tighter timeouts** (understand 8 s, reply 15 s, check 8 s), and **no second attempt on the same model after a timeout**: go straight to the fallback | `config.json`, `tanya/llm.py` | worst case drops from ~73 s to ~15–20 s | No |
| 4 | **Fix Claude Sonnet 5.5**: don't send `temperature` to models that reject it. Today every "detailed" reply fails and falls back to OpenAI. | `tanya/llm.py` (`_anthropic`) | 0.3–1 s on detailed turns, and replies stay on Claude | No |
| 5 | **Reuse one HTTP connection** (`httpx.Client`) for all AI and embedding calls | `tanya/llm.py` | 0.3–1 s | No |
| 6 | **Remember query embeddings**, and start the embedding while understand is still running | `tanya/llm.py`, `tanya/knowledge.py`, `tanya/turn.py` | 0.3–0.6 s | No |
| 7 | **Skip the AI check for low-risk actions** (small talk, greeting, light boundary); the regex SEBI net still runs | `tanya/turn.py` (`n_guard`) | ~1.5–3 s on those turns | **Yes — compliance (Tushar)** |
| 8 | **Record the full turn time** (`turn_ms`) and every failed attempt in the trace | `tanya/graph.py`, `tanya/turn.py`, `tanya/llm.py` | 0 s, but makes the real number visible | No |

### Expected result

| | understand | embedding | reply | check | other | **Total** |
|---|---|---|---|---|---|---|
| Today | 3.5–5.7 s | 0.3–0.6 s | 3.0–5.6 s | 2.6–3.4 s | queue, retries, connections | **12–20 s or more** |
| After fixes 1–6 | 1.0–1.5 s | 0–0.3 s (overlaps understand) | 2.5–4 s | 1.0–1.5 s | ~0.3 s | **~6–9 s** |
| After fixes 1–7 | 1.0–1.5 s | 0–0.3 s | 2.5–4 s | 0 s on low-risk turns | ~0.3 s | **~5–7 s** |

Fixed-line turns (handover, purchase, call booking, limits) make only the understand call. After fix 1 they come back in about **1.5–2 s**.

### Further options (bigger changes)

- **Stream the reply to the user.** The total stays the same, but the first words appear sooner. This is harder here because the reply is JSON and has to pass the guard before anything is shown.
- **Prompt caching.** On Anthropic it only works above a minimum prompt length, so it would only help on Claude Sonnet replies, which have longer prompts.
- **Rules for obvious messages.** Plain greetings or "ok"/"thanks" could skip the understand call and use simple keyword rules, as the mock provider already does.

### How to check the result

After the changes, send 20–30 test messages of different kinds and read `turn_ms` and `llm_calls[].ms` from the traces. Target: a median of 7 s or less, with 95% of replies under 10 s.

---

## 3. AWS specification

### 3.1 What runs on the server (server mode)

| Process | Count | Memory each | Notes |
|---|---|---|---|
| Gateway (`python app.py`, uvicorn) | 1 | ~70 MB | CRM webhook; needs HTTPS in front |
| Turn workers (`tanya.workers turn`) | 1 per lane (or per 2 lanes) | **~67 MB (measured)**, plan for ~80 MB | Each handles one turn at a time; spends most of its time waiting on AI calls |
| Persist worker | 1 | ~70 MB | Redis → MySQL `orch_*` tables |
| Loader worker | 1 | ~70 MB | Loads new customers from the CRM |
| Sessions worker | 1 | ~70 MB | Closes idle sessions, writes the session note |
| Redis | 1 | ~100–200 MB for ~100 users | Memory records, streams, flags, spend ledger. Turn on AOF so data survives a restart |
| Qdrant | 1 | ~200–300 MB | **Optional.** Only 15 knowledge chunks today; without it the code compares vectors in memory |
| MySQL | 0 or 1 | ~400–600 MB if it runs here | The persister writes into the **CRM's MySQL**. If that runs elsewhere, nothing is needed here |
| OS, nginx/Caddy | – | ~500 MB | – |

The workload mostly waits on network replies, so CPU use is low (well under 1 vCPU on average for ~100 users). **Memory decides the instance size.**

### 3.2 Recommended minimum

| Item | Value |
|---|---|
| Instance | **EC2 `t4g.medium`**: 2 vCPU (ARM Graviton), 4 GB RAM. `t3.medium` is the x86 alternative, about 20% more expensive |
| Region | `ap-south-1` (Mumbai), close to users and to IST operations |
| Disk | 30 GB gp3 |
| Swap | 2 GB swap file as a safety margin |
| Network | Elastic IP and a domain name; HTTPS through Caddy or nginx with Let's Encrypt (the CRM webhook needs HTTPS) |
| Security group | Port 443 open; port 22 from the office IP only; Redis 6379, Qdrant 6333 and MySQL 3306 **never** open (localhost only) |
| Process manager | systemd services that restart workers automatically. `run_linux.sh` starts them with `&`, so a crashed worker stays dead and its lanes stop being answered |
| Monitoring | CloudWatch alarm on memory above 85%; watch the queue length (`XLEN tanya:in:*`); billing alerts for AI providers |
| Rough cost | `t4g.medium` on demand is about US$25–35 a month, plus ~US$3 for the disk. Check the AWS pricing calculator for current prices |

### 3.3 Sizing by number of lanes

How lanes work:
- A **lane** is one Redis stream (`tanya:in:N`). Each customer always uses the same lane, which keeps their messages in order.
- **Replies in progress at once = number of turn workers**, which can never be more than the number of lanes.
- Today `stream_partitions` is **8**, and `run_linux.sh` runs **1** worker for all 8 lanes.

Memory below = turn workers × ~80 MB, plus a fixed base of ~1.2 GB (gateway, 3 other workers, Redis, Qdrant, OS, proxy; no MySQL on the box).

| Lanes (`stream_partitions`) | Turn workers | Memory needed | Instance | Throughput at 13 s/turn (today) | Throughput at 7 s/turn (after fixes) | Active users supported* |
|---|---|---|---|---|---|---|
| 8 (today, with 1 worker) | 1 | ~1.3 GB | `t4g.small` (2 GB) | ~4.6 msg/min | ~8.5 msg/min | ~5 |
| 8 | 8 | ~1.9 GB | `t4g.medium` (4 GB) | ~37 msg/min | ~68 msg/min | ~25–45 |
| **16** | 16 | ~2.5 GB | **`t4g.medium` (4 GB)** | ~74 msg/min | ~137 msg/min | **~50–100** |
| **32** | 32 | ~3.8 GB | **`t4g.large` (8 GB)** | ~148 msg/min | ~274 msg/min | **~100–200** |
| 64 | 64 | ~6.3 GB | `t4g.xlarge` (16 GB, 4 vCPU), or 2 × `t4g.large` with 32 lanes each | ~295 msg/min | ~550 msg/min | ~200–400 |

\* Assumes each active user sends about one message a minute, with about 30% headroom so bursts do not build a queue. If MySQL runs on the same box, add about 0.5 GB, which means the next size up for 16 lanes and above.

**Recommendation:** go to **16 lanes and 16 workers on `t4g.medium`** now. Move to **32 lanes on `t4g.large`** when the system regularly has more than about 100 active users at the same time.

### 3.4 Changing the number of lanes

1. Change `stream_partitions` in [config.json](config.json), for example 8 → 16.
2. **Do it while the queues are empty** (`XLEN tanya:in:*` is 0). The lane for each customer is worked out from their ID, so changing the count moves customers to different lanes.
3. Start one worker per lane, for example `python -m tanya.workers turn 5-5`, or one worker per pair of lanes (`turn 4-5`). Each worker gets its own consumer name automatically, taken from its first lane.
4. Restart the gateway as well, so new messages go into the new lanes.

### 3.5 Limits outside AWS

More lanes only help if these limits allow it:

| Limit | Today | What it means at scale |
|---|---|---|
| **Daily AI spend ceiling** | `daily_ai_spend_ceiling_inr` = **₹200** (owner Open, E2) | A full turn on Haiku costs about **₹0.8–1.2** (estimate from config prices); fixed-line turns cost much less. ₹200 covers only about 170–250 replies a day **for all users together**. After that everyone gets FX-05. 100 users × 10 messages a day needs about ₹800–1,200 a day. |
| **Anthropic rate limits** | Depend on the account's tier | Each normal turn makes 3 Claude requests. At 32 lanes and 7 s turns that is about 800 requests a minute plus tokens. Check the account's limit for requests and tokens per minute. Calls refused with 429 are retried, which makes replies slower again. |
| **OpenAI embeddings and fallback** | `text-embedding-3-small`; GPT-6 as fallback | One embedding per knowledge search. The fallback takes the full load whenever Claude fails, so its rate limits must cover that too. |

### 3.6 Growing beyond one server

For several hundred active users, or when you need high availability:
1. Move Redis to **ElastiCache** and MySQL to **RDS**.
2. Run two or more app servers behind an **ALB**, splitting the lanes between servers. For example, server A runs lanes 0–31 and server B runs lanes 32–63.

The design keeps each customer in one lane, so this split does not change the code.
