#!/usr/bin/env bash
# Ms Tanya — start on Linux / server. RUN_MODE in .env decides dev or server.
set -e
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv
source .venv/bin/activate
pip install -q -r requirements.txt
[ -f .env ] || cp .env.example .env
if grep -q "^RUN_MODE=server" .env; then
  # one turn worker per lane (customers are not queued behind each other; order kept per customer),
  # plus the reconciler that recovers messages whose CRM webhook was lost. Production: one systemd unit each.
  for p in $(seq 0 $(( $(python -c "from tanya.settings import S; print(S.get('stream_partitions', 8))") - 1 ))); do
    python -m tanya.workers turn "$p-$p" &
  done
  python -m tanya.workers persist & python -m tanya.workers loader & python -m tanya.workers sessions &
  python -m tanya.workers reconcile &
fi
python app.py
