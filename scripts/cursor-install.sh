#!/usr/bin/env bash
# Baseline setup for the Cursor Cloud Agent environment (`install` phase).
#
# With environment builds this runs once, while the snapshot is created;
# pods that boot from that snapshot never re-run it. So everything here
# must be idempotent and may only produce durable files. Anything that
# needs a live process on every boot belongs in `cursor-start.sh`.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

log() { printf '\n=== cursor-install: %s ===\n' "$*"; }

# --------------------------------------------------------------------
# PATH
# --------------------------------------------------------------------
# nvm's node only lands on PATH for shells that source ~/.bashrc, and the
# install phase is not one of them.
export NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
if [ -s "$NVM_DIR/nvm.sh" ]; then
  # shellcheck disable=SC1091
  . "$NVM_DIR/nvm.sh"
fi

mkdir -p "$HOME/.local/bin"
export PATH="$HOME/.local/bin:$PATH"

# uv and playwright-cli land in ~/.local/bin, which ~/.profile only adds
# for login shells. Agent shells are non-login, so pin it in ~/.bashrc.
BASHRC_MARKER='# cursor-install: ~/.local/bin'
if ! grep -qF "$BASHRC_MARKER" "$HOME/.bashrc" 2>/dev/null; then
  {
    printf '%s\n' "$BASHRC_MARKER"
    printf '%s\n' 'export PATH="$HOME/.local/bin:$PATH"'
  } >>"$HOME/.bashrc"
fi

log "toolchain preflight"
command -v node
node --version
command -v npm
npm --version

# --------------------------------------------------------------------
# System packages
# --------------------------------------------------------------------
# pg_isready is how both cursor-start.sh and humans check the compose
# Postgres from the host side.
log "apt packages"
if ! command -v pg_isready >/dev/null 2>&1; then
  sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends postgresql-client
fi
pg_isready --version

# --------------------------------------------------------------------
# uv + Python dependencies
# --------------------------------------------------------------------
log "uv"
if ! command -v uv >/dev/null 2>&1; then
  # Download first and run second: `bash <(curl ...)` reports the exit
  # status of bash, so a 404 would silently succeed.
  curl -fsSL --retry 3 --retry-delay 2 --retry-all-errors \
    https://astral.sh/uv/install.sh -o /tmp/uv-install.sh
  sh /tmp/uv-install.sh
  rm -f /tmp/uv-install.sh
fi
uv --version

log "python dependencies (.venv)"
uv sync --group dev
test -x "$REPO_ROOT/.venv/bin/python"

# --------------------------------------------------------------------
# Web SPA
# --------------------------------------------------------------------
# fin-serve serves web/dist same-origin; without the build only the API
# routes come up (see README).
log "web dependencies + SPA build"
(
  cd "$REPO_ROOT/web"
  npm ci
  npm run build
)
test -f "$REPO_ROOT/web/dist/index.html"

# --------------------------------------------------------------------
# Docker engine
# --------------------------------------------------------------------
log "docker engine"
if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL --retry 3 --retry-delay 2 --retry-all-errors \
    https://get.docker.com -o /tmp/get-docker.sh
  sudo sh /tmp/get-docker.sh
  rm -f /tmp/get-docker.sh
fi
# overlay2 cannot mount on this VM's root filesystem; vfs is the only
# storage driver that works here.
sudo mkdir -p /etc/docker
printf '{\n  "storage-driver": "vfs"\n}\n' | sudo tee /etc/docker/daemon.json >/dev/null
sudo groupadd -f docker
sudo usermod -aG docker "$(id -un)"
docker --version

log "prefetch compose images"
# Pulling into the snapshot keeps `cursor-start.sh` off the network. The
# daemon is only a means to that end here -- leave it exactly as it was
# found, since install may also be re-run on a live pod.
dockerd_was_running=no
if sudo docker info >/dev/null 2>&1; then
  dockerd_was_running=yes
fi

if bash "$REPO_ROOT/scripts/cursor-dockerd.sh" start; then
  sudo docker compose pull --quiet
  sudo docker image ls
else
  echo "cursor-install: dockerd unavailable; cursor-start.sh will pull on first boot" >&2
fi

if [ "$dockerd_was_running" = no ]; then
  bash "$REPO_ROOT/scripts/cursor-dockerd.sh" stop || true
fi

# --------------------------------------------------------------------
# Browser automation
# --------------------------------------------------------------------
log "playwright-cli"
npm install -g --prefix "$HOME/.local" @playwright/cli@latest
playwright-cli --version

log "chromium for playwright"
npx --yes playwright install --with-deps chromium

log "git attribution (overwrite platform identity/hooks)"
bash "$REPO_ROOT/scripts/cursor-git-attribution.sh"

log "done"
