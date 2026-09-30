@echo off
REM Ms Tanya — start on a Windows PC (dev console at http://localhost:8000)
cd /d %~dp0
where python >nul 2>nul
if errorlevel 1 (
  echo Python is not installed. Install Python 3.11 or newer from https://www.python.org/downloads/
  echo During install, tick "Add python.exe to PATH". Then double-click this file again.
  pause
  exit /b
)
if not exist .venv (
  echo First run: creating the Python environment...
  python -m venv .venv
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip >nul
pip install -r requirements.txt
if not exist .env copy .env.example .env >nul
echo.
echo Opening http://localhost:8000 in your browser...
start "" cmd /c "timeout /t 4 >nul & start http://localhost:8000"
python app.py
pause
