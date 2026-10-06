# Handover: Running `tanya_ai` (Ms Tanya Code Frame v1.0)

## 1. What the project is
- **Ms Tanya**: the AI chat layer for TG Lite trial users, built in the "AI Phase 3 Backbone" shape.
- Python + FastAPI app. In dev mode it serves a **developer console** at `http://localhost:8000` where you chat with Tanya and see what she understood, decided and checked.
- **Mock mode** (no API key) gives rule-based answers, good for testing the rules.
- Core rules from the README: code decides the action, the AI only writes words; fixed lines are never AI-written; prices, plans and FAQs come only from `content/`; every reply passes the SEBI guard.
*
## 2. What I verified (in my sandbox, before guiding you)
| Check | Result |
|---|---|
| App starts (`python app.py`) | Works, dev console returns 200 at `/` |
| Rule tests (`python tests/run_tests.py`) | 34 passed |
| One test fails only if `REDIS_URL` is set and Redis isn't running | Optional test (`redis_store_and_streams`); blank `REDIS_URL=` in `.env` fixes it |
| Dev console URL | `http://localhost:8000/` (the `/dev/` path returns 404, which is normal) |

## 3. Where you are now (your machine)
- [x] Zip extracted to `C:\Users\USER\Downloads\tanya_ai_code_frame_v1.0\tanya_ai`
- [x] Opened the **inner** `tanya_ai` folder in Antigravity IDE
- [x] Python **3.11.15** confirmed (meets 3.11+ requirement)
- [x] Virtual environment created: `python -m venv .venv`
- [x] Virtual environment activated (prompt shows `(.venv)`)
- [x] Packages installed: `pip install -r requirements.txt` (pip upgrade notice is harmless)
- [x] `.env` created: `copy .env.example .env`
- [ ] **Not done, optional:** set `REDIS_URL=` to blank inside `.env` (only needed for the test command)
- [ ] **Next:** start the app

## 4. Next steps (in order)
1. In the Antigravity terminal (prompt starts with `(.venv)`), run:
   ```
   python app.py
   ```
2. Wait for: `Uvicorn running on http://127.0.0.1:8000`
3. Open **http://localhost:8000** in your browser.
4. Try a few messages in the console, for example:
   - `Hello`
   - `Stop-loss kya hota hai?`
   - `Kal Nifty upar jayega kya?` (Tanya should refuse trade predictions)
5. To stop the app: click the terminal and press **Ctrl + C**.

## 5. Optional, later
| Goal | What to do |
|---|---|
| Run the rule tests | Set `REDIS_URL=` blank in `.env`, save, then `python tests/run_tests.py` (expect 34 passed) |
| Real AI answers | In `.env`: `PROVIDER=anthropic` (or `openai` / `google`) and paste the key after `ANTHROPIC_API_KEY=`, then restart the app |
| Live voice and trick-question tests | `python tests/run_golden.py` (needs an API key) |
| Production (server) mode | Needs Redis, MySQL and the CRM; skip unless you are wiring the real system |

## 6. Troubleshooting
| Problem | Fix |
|---|---|
| `python` not recognized | Reinstall Python 3.11+, tick "Add python.exe to PATH" |
| `(.venv)` missing from the prompt | Run `.venv\Scripts\Activate.ps1` again |
| "Running scripts is disabled" on activate | Run `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`, then activate again |
| Port 8000 already in use | In `.env` set `PORT=8001`, restart, and open `http://localhost:8001` |
| `langgraph` problems | In `.env` set `USE_LANGGRAPH=0` |
| Typed a `.env` line into the terminal by mistake | Harmless; those lines go inside the `.env` file, not the terminal |

## 7. Key files to know
| Path | Purpose |
|---|---|
| `app.py` | Starts the server |
| `.env` | Your settings (never share or commit it) |
| `tanya/turn.py`, `tanya/decider.py` | How one chat turn works and how the action is chosen |
| `tanya/guard_output.py` | SEBI safety checks on replies |
| `content/` | All approved prices, plans, FAQs, lessons and fixed lines |
| `docs/JUNIOR_TASKS.md` | What is done and what is left in the build |
| `docs/Ms_Tanya_Code_Frame_Spec_v1.0.pdf` | The spec (the contract) |
