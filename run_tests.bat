@echo off
REM Ms Tanya — rule tests (no AI key needed)
cd /d %~dp0
if not exist .venv ( python -m venv .venv )
call .venv\Scripts\activate.bat
pip install -r requirements.txt >nul
python tests\run_tests.py
pause
