#!/usr/bin/env bash
# Ms Tanya — start on Linux / server. RUN_MODE in .env decides dev or server.
set -e
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv
source .venv/bin/activate
pip install -q -r requirements.txt
[ -f .env ] || cp .env.example .env
if grep -q "^RUN_MODE=server" .env; then
  python -m tanya.workers turn 0-7 & python -m tanya.workers persist & python -m tanya.workers loader & python -m tanya.workers sessions &
fi
python app.py
