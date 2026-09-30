# Ms Tanya — Code Frame v1.0

The AI layer for TG Lite's trial users — chat now, voice next — built in the shape of the **AI Phase 3 Backbone**.
The intelligence is written and tested; what remains is **plumbing** (CRM, servers, content) and **tuning**.
Read with: `Ms_Tanya_Code_Frame_Spec_v1.0.pdf` (the contract), `Ms_Tanya_AI_Architecture_v3.2.pdf` (the why),
`Ms_Tanya_Build_Flow_v3.2.pdf` (the order), and `docs/JUNIOR_TASKS.md` (who finishes what).

## 1. Rules that never change

1. **Spec → Python → Node.** Every change goes into the Spec first, then the Python, then the Node Gateway.
2. **The live chat path never touches MySQL.** Redis is Tanya's working memory; MySQL (orch_ tables) is written behind her.
3. **Code decides, the AI writes.** The decider (`tanya/decider.py`) picks the action; the AI only writes words for it.
4. **Fixed lines are never written by the AI** (`content/fixed_lines.json`).
5. **Only approved content**: prices, plans, FAQs and lessons come from `content/` — the AI never invents them.
6. **Every reply passes the SEBI guard** (three layers) before it reaches him.

## 2. Folder map (Backbone component in brackets)

| Path | What it is |
|---|---|
| `tanya/gateway.py` | Webhook intake, app events, dev console **(AI-C01 Gateway — Python reference; Node port pending)** |
| `tanya/policy.py` | BOT/HUMAN mode, kill switch, spend ceiling, calling hours **(AI-C02, AI-C20, AI-C19)** |
| `tanya/graph.py`, `tanya/turn.py` | One turn as 8 steps — LangGraph or plain loop **(AI-C03 Orchestrator)** |
| `tanya/understand.py` | The understand call — labels with evidence **(AI-C03)** |
| `tanya/decider.py` | Priority table → one action **(AI-C03 / AI-C09)** |
| `tanya/prompts.py` | Persona, known facts, action, knowledge, golden examples **(AI-C03 / AI-C17)** |
| `tanya/knowledge.py` | RAG: chunks, keyword + vector (hybrid) search **(AI-C05)** |
| `tanya/guard_input.py` | Masking of private numbers, injection check **(AI-C23)** |
| `tanya/guard_output.py` | SEBI net, ₹ check, emoji rule, AI check **(AI-C14)** |
| `tanya/memory_model.py`, `tanya/memory_store.py` | Customer Memory rules; File (dev) and Redis (prod) stores **(AI-C25)** |
| `tanya/scoring.py`, `tanya/brief.py` | Temperature; Lead Brief; Agent Short-Call Card **(AI-C06/07/09)** |
| `tanya/handoff.py` | Cases, callbacks, honest times **(AI-C18)** |
| `tanya/llm.py`, `tanya/llm_mock.py` | Anthropic / OpenAI / Google / mock, cost in ₹, fallback, embeddings **(AI-C21)** |
| `tanya/streams.py` | Redis Streams: 8 lanes, consumer groups, reclaim, dead letter |
| `tanya/workers.py` | Turn worker, persister (→ MySQL), loader, session closer |
| `tanya/crm_adapter.py` | The only code that talks to the PHP CRM (Support Board api.php) |
| `tanya/morning_audit.py` | Morning compliance audit by a second AI **(AI-C14 / AI-C22)** |
| `db/orch_tables.sql` | The 16 orch_ tables for the CRM MySQL |
| `content/` | Everything Tanya may say: prices, plans, fixed lines, SEBI texts, golden examples, value cards, knowledge |
| `config.json` | The configuration table — every number with its owner |
| `tests/run_tests.py` | 34 rule tests (+2 with Redis/MySQL) — no AI key needed |
| `tests/run_golden.py` | Live tests: voice tests and trick trade questions — needs a key |
| `static/devconsole.html` | Chat with Tanya and see what she understood, decided and checked |

## 3. Run it on a Windows PC (dev console)

1. Install **Python 3.11 or newer** from python.org — tick **"Add python.exe to PATH"**.
2. Double-click **`run_windows.bat`**. The first run installs everything (2–3 minutes) and opens `http://localhost:8000`.
3. Without a key it runs in **mock mode** (rule-based answers) — good for testing the rules.
4. For real answers: open `.env` in Notepad, set `PROVIDER=anthropic` (or `openai` / `google`) and paste the key
   after `ANTHROPIC_API_KEY=`. Save, close the black window, double-click `run_windows.bat` again.
5. Rule tests: double-click **`run_tests.bat`** — all must say PASS.

## 4. Run it on the server (production shape)

1. `.env`: `RUN_MODE=server`, `REDIS_URL`, `MYSQL_*`, `CRM_MODE=supportboard`, `CRM_*`, provider key.
2. Create the tables once: `mysql crm_db < db/orch_tables.sql` (staging first, DBA approval).
3. Start: `./run_linux.sh` — gateway + turn worker (lanes 0–7) + persister + loader + session closer.
4. Point the CRM's Webhook setting to `https://<server>/webhook/crm` with the shared secret.
5. Morning audit every day (cron): `python -m tanya.morning_audit`.

## 5. What is done and what is left

See **`docs/JUNIOR_TASKS.md`** — every Build Flow activity (S0.1–S5.9) with its status in this frame,
what is left, the owner and the proof.
