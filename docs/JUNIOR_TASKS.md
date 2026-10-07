# Ms Tanya — What the frame gives you, and what you finish

**Status legend:** ✅ DONE in the frame (tested) · 🟡 PARTIAL (frame ready, plumbing or content left) · ⬜ TODO (not in the frame)
**Rule:** do not rewrite a ✅ module. Change it only through the Spec (Spec → Python → Node) and keep `tests/run_tests.py` green.

## 1. Build Flow v3.2 activities against the frame

| # | Activity | Owner | Frame | What is left (plumbing / tuning) | Proof |
|---|---|---|---|---|---|
| S0.1 | Prove the CRM doors with real calls | CODER D | ⬜ | Real api.php samples (send, get conversation, get user, notes, department) and webhook payloads → fill every `TO CONFIRM` in `crm_adapter.py` | Saved request/response files (inputs A1–A4) |
| S0.2 | orch_ tables | CODER D | ✅ | Run `db/orch_tables.sql` on staging, then live; create the orch_ login | `SHOW TABLES` screenshot |
| S0.3 | Project skeleton and settings | JUNIOR C | ✅ | Put the repo in Git (input E5); `.env` per environment | Repo link |
| S0.4 | Minimum content pack | Tushar | 🟡 | Replace placeholders: `content/pricing.csv` (C1), `profile_to_plan.csv` (C2), `kb/*.md` (C3, C4), `sebi_texts.json` + FX-17/18 (D1) | Files approved |
| S0.5 | Test sets and harness | JUNIOR B | 🟡 | Rule tests ✅ (36). Grow `tests/trick_questions.txt` from 40 to 100 (D3); add simulations S1–S15 to `run_golden.py` | Golden report |
| S1.1 | AI Gateway — webhook intake (AI-C01) | JUNIOR A | 🟡 | Python reference ✅. Port to the small **Node** service word for word from `gateway.py` + `guard_input.py` (Tushar's decision) | Same test events give same queue entries |
| S1.2 | Redis Streams and workers | JUNIOR A | ✅ | Deploy Redis (AOF on), run workers as services | Worker logs; `tanya:errors` empty |
| S1.3 | CRM adapter | JUNIOR C + CODER D | 🟡 | `SupportBoardAdapter` — confirm names and shapes (A1–A4); keep NO delete function | Post a reply on staging; read it in the CRM thread |
| S1.4 | Routing gate — Mode Policy (AI-C02) | JUNIOR A | ✅ | — | `run_tests.py` |
| S1.5 | App polling and app-open event | JUNIOR C + app dev | 🟡 | App backend calls `/events/app` on open with header `X-App-Secret` = `APP_EVENTS_SECRET` (endpoint off while it is empty); polling back-off 1 s → 2 s → 15 s idle | App-open greeting on a real phone |
| S2.1 | Knowledge base loading (AI-C05) | JUNIOR B | ✅ | Real files (C3, C4); choose `EMBEDDING_PROVIDER`; sync chunks to `orch_kb_chunks` | Search returns the right lesson |
| S2.2 | Input guard (AI-C23) | JUNIOR A | ✅ | — | `run_tests.py` |
| S2.3 | Understand call | JUNIOR B | ✅ | Tune on 2,000 real chats (F1) | Label accuracy sheet |
| S2.4 | Ms Tanya generator | JUNIOR B | ✅ | Tune with approved golden examples (Personality Guide) | `run_golden.py` voice tests pass |
| S2.5 | SEBI guard — three layers (AI-C14) | JUNIOR A | ✅ | Run 100 trick questions on the chosen model — all refused | Golden report: 0 failures |
| S2.6 | Cost ledger, trace, live status | JUNIOR C | 🟡 | Ledger ✅, trace ✅. Live status (thinking/typing) to the app (B7) | Status visible in app |
| S2.7 | Model bake-off | JUNIOR C | ⬜ | Same golden run with Anthropic / OpenAI / Google keys; compare quality, speed, ₹ | Bake-off table |
| S3.1 | Customer Picture loader (AI-C25) | JUNIOR C + CODER D | 🟡 | `load_from_crm()` — map real field names (A5) and agree the agent-note format (A4) | Amit-style known facts loaded |
| S3.2 | Persona, known facts, last promise | JUNIOR B | ✅ | — | Dev console |
| S3.3 | Facts and state writer | JUNIOR B | ✅ | — | orch_lead_facts rows |
| S3.4 | Next-Action decider v1 | JUNIOR A | ✅ | — | `run_tests.py` |
| S3.5 | Hand-offs — day and night | JUNIOR A | 🟡 | Logic ✅. Real `hand_to_human` (department / agent, A8); confirm calling window (open decision) | Staff sees the hand-off in the CRM |
| S4.1 | Lead temperature | JUNIOR C | ✅ | Tune weekly from call outcomes | — |
| S4.2 | Lead Brief and callbacks | JUNIOR C | 🟡 | Brief ✅, card ✅. Write the brief as Tanya's protected CRM note (A4); agent outcome codes | Brief visible in CRM, not deletable by agents |
| S4.3 | Contradictions — shadow mode | JUNIOR B | ✅ | Hidden from agents until 8 of 10 reviewed items prove real (`contradictions_visible_to_agents`) | Review sheet |
| S4.4 | Purchase path and journey events | JUNIOR A | 🟡 | Purchase hand-off ✅. `plan_bought` event must cancel sales nudges (B2) | Test purchase on staging |
| S5.1 | Reliability | JUNIOR A | 🟡 | Dedupe ✅, reclaim ✅, dead letter ✅, version check ✅. Add catch-up poller (every 60 s via api.php) and the nightly Redis ⇄ MySQL audit | Kill a worker mid-turn — nothing lost |
| S5.2 | Trial-day nudges | JUNIOR A | ⬜ | Needs push API (B6) and trial length (G1) | Nudge received |
| S5.3 | Decider v2, past-message lookup | JUNIOR B | 🟡 | Past lookup in Redis window ✅. Objection library, commitment ladder | New decider tests |
| S5.4 | Guards and alerts | JUNIOR C | 🟡 | Alerts land in `orch_alerts` ✅. Send them to Telegram/email (E7) | Alert received |
| S5.5 | Load and failure test | JUNIOR C | ⬜ | After integration, with real polling traffic | Report |
| S5.6 | Full test run and final model | JUNIOR B | ⬜ | `run_tests.py` + `run_golden.py` on the chosen model | Both green |
| S5.7 | Pilot readiness | JUNIOR C | ⬜ | Pilot groups (`orch_pilot_groups`), daily review sample | Checklist |
| S5.9 | Morning compliance audit | JUNIOR B | 🟡 | Dev version ✅ (reads events). Switch source to MySQL; use provider batch API; 9 AM report to Tushar and compliance (D5) | Morning report |

## 2. Remaining effort — estimate, once the P0 inputs are in hand

| Person | Main remaining work | Working days |
|---|---|---|
| CODER D | S0.1 proofs, S0.2 tables, adapter confirmation with JUNIOR C, loader field mapping | 2 |
| JUNIOR A | Node Gateway port, Redis + workers on the server, catch-up poller, nightly audit, hand-off plumbing | 3–4 |
| JUNIOR B | Bake-off runs, trick set to 100, tuning on real chats, morning audit on MySQL | 3–4 |
| JUNIOR C | CRM adapter + loader, app-open / status / alerts, load test, pilot readiness | 4 |

Working in parallel: **about 5–6 working days** to a closed pilot instead of 10 — only if inputs A1–A7, B1–B5, C1–C3, D1, E1–E5 arrive on Day 0. Every missing input moves its line by the days it is late.

## 3. Daily proof to Tushar

1. `run_tests.bat` — all PASS (screenshot).
2. `run_golden.py` report once a provider key is set — failures listed with the exact reply.
3. One dev-console conversation per day on the persona of the day (S1–S15).
