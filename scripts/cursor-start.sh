#!/usr/bin/env bash
# Per-boot runtime init for the Cursor Cloud Agent VM (`start` phase).
#
# Runs on every pod boot, including pods restored from a build snapshot.
# Only daemons and other state that cannot survive a snapshot belong
# here; dependency installation belongs in cursor-install.sh.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

log() { printf '\n=== cursor-start: %s ===\n' "$*"; }

log "dockerd"
bash "$REPO_ROOT/scripts/cursor-dockerd.sh" start

log "postgres (docker compose)"
sudo docker compose up -d

for _ in $(seq 60); do
  if pg_isready -h 127.0.0.1 -p 5432 -U fin -d fin >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

# Print the readiness verdict either way, then fail loudly if it never
# came up -- a silently missing checkpointer DB is worse than a red start.
pg_isready -h 127.0.0.1 -p 5432 -U fin -d fin

log "ready"
