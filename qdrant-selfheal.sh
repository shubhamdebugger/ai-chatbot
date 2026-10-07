#!/usr/bin/env bash
# Qdrant start wrapper: Qdrant must never crash-loop (production fix, 05-Oct-2026).
#
# Plain English:
# - Qdrant keeps every write in a log (WAL) before applying it. If the machine, Docker or WSL stops while a write
#   is half done, the last log record is torn and Qdrant panics on EVERY start ("Can't deserialize entry, probably
#   corrupted WAL") — the container then restarts forever (seen live: exit 101, 157 restarts).
# - Tanya's knowledge collection is a copy: Tanya rebuilds it from data/kb_vectors.json whenever it is missing
#   (knowledge.py _qdrant_sync / self-heal). So a broken collection can safely be moved aside and Qdrant started
#   clean. Nothing is deleted: the broken files go to storage/quarantine/<collection>-<time> for inspection.
# - Stop signals are passed to Qdrant (like the image's own entrypoint) so `docker stop` is a clean shutdown.
#
# Use: run the qdrant image with this file mounted and as the command (see compose.qdrant.yml).
set -u
cd /qdrant
STORAGE="${QDRANT_STORAGE_DIR:-/qdrant/storage}"
LOG=/tmp/qdrant-start.log
MAX_HEALS="${QDRANT_MAX_SELF_HEALS:-3}"
QDRANT_PID=0
STOPPING=0

_term() { STOPPING=1; [ "$QDRANT_PID" -gt 0 ] && kill -TERM "$QDRANT_PID" 2>/dev/null; }
_int()  { STOPPING=1; [ "$QDRANT_PID" -gt 0 ] && kill -INT  "$QDRANT_PID" 2>/dev/null; }
trap _term TERM
trap _int INT

heals=0
while true; do
  : > "$LOG"
  ./qdrant "$@" > >(tee -a "$LOG") 2>&1 &
  QDRANT_PID=$!
  wait "$QDRANT_PID"; rc=$?
  # a trap interrupts `wait`: wait again for Qdrant to finish its clean shutdown
  while [ "$STOPPING" = 1 ] && kill -0 "$QDRANT_PID" 2>/dev/null; do wait "$QDRANT_PID"; rc=$?; done
  [ "$STOPPING" = 1 ] && exit "$rc"

  if grep -qE "corrupted WAL|Can't deserialize entry|Failed to load local shard|Can't open WAL" "$LOG"; then
    col="$(grep -oE 'Loading collection: [A-Za-z0-9_.-]+' "$LOG" | tail -1 | sed 's/^Loading collection: //')"
    if [ -n "$col" ] && [ -d "$STORAGE/collections/$col" ] && [ "$heals" -lt "$MAX_HEALS" ]; then
      heals=$((heals + 1))
      dest="$STORAGE/quarantine/$col-$(date -u +%Y%m%dT%H%M%SZ)"
      mkdir -p "$STORAGE/quarantine"
      mv "$STORAGE/collections/$col" "$dest"
      echo "[qdrant-selfheal] collection '$col' could not be loaded (corrupted WAL) — moved to $dest;" \
           "starting clean, Tanya re-creates it from her vector cache (heal $heals/$MAX_HEALS)" >&2
      continue
    fi
  fi
  echo "[qdrant-selfheal] qdrant exited rc=$rc (no self-heal applied) — container restart policy takes over" >&2
  exit "$rc"
done
