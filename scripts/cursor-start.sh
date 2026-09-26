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

# --------------------------------------------------------------------
# Git attribution (every boot: overwrite if the platform rewrote identity/hooks)
# --------------------------------------------------------------------
log "git attribution"
bash "$REPO_ROOT/scripts/cursor-git-attribution.sh"

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

# --------------------------------------------------------------------
# LLM boot defaults (official MiniMax Chat Completions)
# --------------------------------------------------------------------
# Process env / Cloud secrets win. These exports only fill gaps so a
# pod that injects FIN_API_KEY still hits official chat
# instead of localhost:20128. Never assign the key here.
log "llm env defaults"
export FIN_BASE_URL="${FIN_BASE_URL:-https://api.minimaxi.com/v1}"
export FIN_MODEL="${FIN_MODEL:-MiniMax-M3}"
export FIN_CHECKPOINT_URL="${FIN_CHECKPOINT_URL:-postgresql://fin:fin@localhost:5432/fin?sslmode=disable}"

# --------------------------------------------------------------------
# Attachment boot bounds
# --------------------------------------------------------------------
# serve() reads all four bounds before it binds a port, and this boot loads
# no .env file, so a pod that injected none would not come up at all. The
# template is the source; an injected process variable still wins.
# shellcheck source=scripts/lib/attachment-bounds.sh
source "$REPO_ROOT/scripts/lib/attachment-bounds.sh"
log "attachment bounds defaults"
export_attachment_bounds cursor-start

log "ready"
