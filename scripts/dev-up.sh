#!/usr/bin/env bash
# One-shot local 工作台: infra + iknow-memory on PATH + SPA + fin-serve.
# After this process is up, open http://127.0.0.1:8000 and send.
#
# Usage:
#   bash scripts/dev-up.sh
#   bash scripts/dev-up.sh --check    # preflight only
#   FIN_IKNOW_ROOT=/path/to/iknow-memory bash scripts/dev-up.sh
#   FIN_IKNOW_STACK=dev fin up   # isolated demo stack (PG 5434 / ES 9201 / Neo4j 7688)
#
# Prefer the `fin` entrypoint (see scripts/fin) over calling this directly:
# `fin up` resolves the checkout from the current directory.
#
# Boot order is load-bearing: both compose stacks reach HEALTHY before
# `fin-serve` starts. The knowledge-MCP probe runs ONCE inside `serve()`
# and its snapshot (缺 / 坏 / 可达) gates conversation entry for the whole
# process lifetime, so booting serve first would publish 坏 against infra
# that is merely not up yet.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# shellcheck source=scripts/lib/readiness.sh
source "$REPO_ROOT/scripts/lib/readiness.sh"
# shellcheck source=scripts/lib/fin-root.sh
source "$REPO_ROOT/scripts/lib/fin-root.sh"
# shellcheck source=scripts/lib/attachment-bounds.sh
source "$REPO_ROOT/scripts/lib/attachment-bounds.sh"

CHECK_ONLY=0
DEMO_STACK_ONLY=0
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  if [[ "$#" -gt 1 ]]; then
    printf 'dev-up: usage: dev-up.sh [--check | --demo-stack-only]\n' >&2
    exit 2
  fi
  case "${1:-}" in
  "") ;;
  --check) CHECK_ONLY=1 ;;
  --demo-stack-only) DEMO_STACK_ONLY=1 ;;
  *)
    printf 'dev-up: usage: dev-up.sh [--check | --demo-stack-only]\n' >&2
    exit 2
    ;;
  esac
fi

# Seconds the readiness reporter waits for `GET /api/status` to answer.
# Generous: a cold iknow handshake can exceed one attempt. The product
# probe allows 10s per handshake attempt.
READY_TIMEOUT_SECONDS="${FIN_READY_TIMEOUT_SECONDS:-90}"
CHECKPOINT_PORT=5433

die() {
  printf 'dev-up: %s\n' "$*" >&2
  exit 1
}

log() { printf 'dev-up: %s\n' "$*"; }
warn() { printf 'dev-up: WARN: %s\n' "$*" >&2; }

# Worktree source must win over the shared venv's editable install, which
# may point at another checkout.
fin_host_python() {
  if [[ -x "$REPO_ROOT/.venv/bin/python" ]]; then
    PYTHONPATH="$REPO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}" \
      "$REPO_ROOT/.venv/bin/python" "$@"
    return
  fi
  PYTHONPATH="$REPO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}" \
    uv run --no-sync python "$@"
}

resolve_state_dir() {
  local dir
  if ! dir="$(fin_host_python -c '
from fin.host.fin_home import resolve_fin_home, run_dir
print(run_dir(resolve_fin_home()))
')"; then
    die "could not resolve FIN_HOME"
  fi
  [[ -n "$dir" ]] || die "could not resolve FIN_HOME"
  printf '%s\n' "$dir"
}

# Pid, log, and ready live under the resolved home, not the checkout.
# Written by the readiness reporter below, read by `fin down`. It is the
# only place `fin up`'s verdict survives: the foreground `exec` hands the
# exit code to fin-serve, so the scriptable answer lives here.
STATE_DIR="$(resolve_state_dir)"
SERVE_PID_FILE="$STATE_DIR/serve.pid"
SERVE_LOG_FILE="$STATE_DIR/serve.log"
VERDICT_FILE="$STATE_DIR/ready"

# A new log is mode 0600. An existing log keeps the mode it already has.
publish_runtime_files() {
  mkdir -p "$STATE_DIR"
  if [[ ! -e "$SERVE_LOG_FILE" ]]; then
    (umask 077 && : >"$SERVE_LOG_FILE")
  fi
  printf '%s\n' "$$" >"$SERVE_PID_FILE"
}

# Prints `export KEY=...` for keys the settings file adds. Process keys
# already set are omitted, matching `apply_operator_settings`.
apply_operator_settings_env() {
  local output
  if ! output="$(fin_host_python -c '
import os
import shlex
import sys

from fin.host.operator_settings import (
    OperatorSettingsConfigError,
    apply_operator_settings,
)

bag = dict(os.environ)
try:
    apply_operator_settings(environ=bag)
except OperatorSettingsConfigError as exc:
    print(exc, file=sys.stderr)
    sys.exit(1)
for key, value in bag.items():
    if os.environ.get(key) == value:
        continue
    print(f"export {key}={shlex.quote(value)}")
')"; then
    die "could not apply settings.yaml"
  fi
  # shellcheck disable=SC2086
  eval "$output"
}

# `is_iknow_checkout` / `main_checkout_root` / `resolve_iknow_root` /
# `require_docker_daemon` live in scripts/lib/fin-root.sh — sourced above
# and shared with dev-down.sh, which used to carry a stale copy of the
# resolver that silently skipped the iknow infra from a worktree.
IKNOW_ROOT="$(resolve_iknow_root)"

command -v node >/dev/null || die "node is required"
command -v uv >/dev/null || die "uv is required"
command -v curl >/dev/null || die "curl is required"
command -v jq >/dev/null || die "jq is required"

# `fin up` must not pretend to drive a port the product does not read.
# `fin-serve` hardcodes 8000 (fin.host.http.serve), so an overridden
# WORKBENCH_SERVE_PORT would make every check below probe a port nothing
# will ever bind: the port-ownership check would pass, the reporter would
# watch the wrong port, and the bind failure would land as an opaque
# uvicorn error — exactly the failure these checks exist to prevent. The
# test suite sets it deliberately (it points the classifier at a stub),
# so refuse loudly instead of silently ignoring it.
check_serve_port_override() {
  if [[ "${WORKBENCH_SERVE_PORT}" != "8000" ]]; then
    die "WORKBENCH_SERVE_PORT=$WORKBENCH_SERVE_PORT is set, but fin-serve binds 8000 unconditionally.
  The variable moves the readiness PROBE, not the server, so up cannot honor it.
  Unset it (fin up), or point it at a running server for status checks only."
  fi
}

# Production Postgres is project ``fin`` on port 5433. An empty label
# means the container was not started by compose; project ``fin`` is ours.
# Any other project already owns the port.
# The demo uses its configured checkpoint URL and never owns this Compose
# project, so the exception requires both demo mode and the dev stack.
is_demo_dev_stack() {
  [[ "${FIN_DEMO_MODE:-0}" == "1" && "$FIN_IKNOW_STACK" == "dev" ]]
}

check_checkpoint_port_owner() {
  local owner
  if is_demo_dev_stack; then
    return 0
  fi
  # `awk ... exit` rather than `head -n1`: under `pipefail` a `head` that
  # exits early SIGPIPEs the producer and the whole pipeline returns 141,
  # which under `set -e` would abort boot on the diagnostic path.
  owner="$(docker ps --filter "publish=$CHECKPOINT_PORT" --format '{{.Label "com.docker.compose.project"}}' 2>/dev/null | awk '{print; exit}')"
  [[ -n "$owner" ]] || return 0
  [[ "$owner" == "fin" ]] && return 0
  die "port $CHECKPOINT_PORT is already published by compose project '$owner' (production project is 'fin')."
}

# fin-serve hardcodes 0.0.0.0:8000, so two trees cannot serve at once.
# Name the situation instead of surfacing an opaque bind error.
check_serve_port_free() {
  if ! curl -fsS --max-time 2 "$(readiness_url)" >/dev/null 2>&1; then
    return 0
  fi
  die "port $WORKBENCH_SERVE_PORT already answers GET /api/status — a fin-serve is up.
  Stop it first:  fin down
  (or: kill the process holding port $WORKBENCH_SERVE_PORT, then re-run)"
}

# Project ~/.fin/settings.yaml into this process before the shell defaults.
# `serve()` applies the same file again; keys already exported here win.
# iknow reads ~/.iknow-memory/setting.json itself; this script does not.
apply_operator_settings_env

export FIN_RETRIEVE_ADAPTER="${FIN_RETRIEVE_ADAPTER:-mcp}"
export FIN_BASE_URL="${FIN_BASE_URL:-https://api.minimaxi.com/v1}"

# Which iknow-memory stack `start_compose` brings up. `production` is the
# default and stays the default: the installed template under the iknow
# home, project iknow-memory — the stack `fin down` must never stop.
# `dev` is the checkout's own compose, project iknow-memory-dev, which
# publishes PG 5434 / ES 9201 / Neo4j 7688 for the demo seed.
#
# An environment variable rather than a flag because `fin up` execs
# dev-up.sh with NO arguments, so a flag here would be unreachable from the
# entrypoint this script's own header recommends. ~/.fin/settings.yaml
# cannot supply it either: operator_settings projects a closed field map,
# so an unknown key is rejected rather than exported. Validated before the
# builds below, so a typo costs a second instead of a multi-minute build.
FIN_IKNOW_STACK="${FIN_IKNOW_STACK:-production}"
case "$FIN_IKNOW_STACK" in
production | dev) ;;
*)
  die "FIN_IKNOW_STACK must be production or dev, got: ${FIN_IKNOW_STACK}"
  ;;
esac

if [[ "$DEMO_STACK_ONLY" -eq 1 ]] &&
  [[ "${FIN_DEMO_MODE:-0}" != "1" || "$FIN_IKNOW_STACK" != "dev" ]]; then
  die "--demo-stack-only requires FIN_DEMO_MODE=1 and FIN_IKNOW_STACK=dev"
fi

# serve() reads all four attachment bounds before it binds. The template is
# the source when neither the process nor settings.yaml supplied one.
export_attachment_bounds dev-up

install_iknow_shim() {
  local bin_dir
  bin_dir="$(mktemp -d "${TMPDIR:-/tmp}/fin-dev-bin.XXXXXX")"
  cat >"$bin_dir/iknow-memory" <<EOF
#!/usr/bin/env bash
exec node "$IKNOW_ROOT/bin/iknow-memory.js" "\$@"
EOF
  chmod +x "$bin_dir/iknow-memory"
  export PATH="$bin_dir:$PATH"
}

ensure_iknow_build() {
  if [[ ! -f "$IKNOW_ROOT/dist/index.js" ]]; then
    log "building iknow-memory (dist/index.js missing)"
    (cd "$IKNOW_ROOT" && npm run build)
  fi
  [[ -f "$IKNOW_ROOT/dist/index.js" ]] || die "iknow-memory build did not produce dist/index.js"
}

ensure_spa() {
  if [[ ! -f "$REPO_ROOT/web/dist/index.html" ]]; then
    log "building web SPA"
    (cd "$REPO_ROOT/web" && npm install && npm run build)
  fi
  [[ -f "$REPO_ROOT/web/dist/index.html" ]] || die "web/dist/index.html missing after build"
}

# Names of the three containers that make a stack usable. Both stacks
# publish them as `<project>-<service>`, so the selected compose project is
# the only thing that differs — passing it in keeps the probe from reporting
# the production stack as "already running" while the dev stack is the one
# being selected.
iknow_infra_running() {
  local project="$1" names name
  names="$(docker ps --filter status=running --format '{{.Names}}')"
  for name in "${project}-postgres" "${project}-neo4j" "${project}-es"; do
    if ! printf '%s\n' "$names" | grep -qx "$name"; then
      return 1
    fi
  done
  return 0
}

# stdout is the effective config; discard it. A non-zero exit dies with stderr.
preflight_iknow_config() {
  local err status
  err="$(node "$IKNOW_ROOT/bin/iknow-memory-config.js" show 2>&1 >/dev/null)" && status=0 || status=$?
  if [[ "$status" -ne 0 ]]; then
    die "iknow-memory-config show failed: ${err}"
  fi
}

start_fin_postgres() {
  if is_demo_dev_stack; then
    log "fin Postgres Compose skipped in demo mode"
    return 0
  fi
  local compose_file="${HOME}/.fin/compose/docker-compose.production.yml"
  if [[ ! -f "$compose_file" ]]; then
    die "fin compose template missing: ${compose_file}"
  fi
  log "fin Postgres via ${compose_file}"
  check_checkpoint_port_owner
  docker compose -p fin -f "$compose_file" up -d --wait
}

start_compose() {
  local compose_file
  start_fin_postgres
  if [[ "$FIN_IKNOW_STACK" == "dev" ]]; then
    start_dev_compose
    return 0
  fi
  if iknow_infra_running iknow-memory; then
    log "iknow-memory infra already running (postgres, neo4j, elasticsearch)"
    return 0
  fi
  compose_file="${HOME}/.iknow-memory/compose/docker-compose.production.yml"
  if [[ ! -f "$compose_file" ]]; then
    die "iknow compose template missing: ${compose_file}"
  fi
  log "iknow-memory infra via ${compose_file}"
  if ! docker compose -p iknow-memory -f "$compose_file" up -d --wait; then
    die "iknow-memory compose failed: docker compose -p iknow-memory -f ${compose_file} up -d --wait"
  fi
}

# FIN_IKNOW_STACK=dev: the isolated stack the demo seed targets (PG 5434,
# ES 9201, Neo4j 7688). It is the iknow checkout's own compose — the very
# file `fin down` already stops — so the selection changes which stack
# comes up and nothing about how it stops. The production branch above is
# the default and is untouched.
start_dev_compose() {
  local compose_file="${IKNOW_ROOT}/docker-compose.yml"
  if iknow_infra_running iknow-memory-dev; then
    log "iknow-memory dev stack already running (postgres, neo4j, elasticsearch)"
    return 0
  fi
  if [[ ! -f "$compose_file" ]]; then
    die "iknow dev compose missing: ${compose_file}"
  fi
  log "iknow-memory dev stack via ${compose_file}"
  if ! docker compose -p iknow-memory-dev -f "$compose_file" up -d --wait; then
    die "iknow-memory dev compose failed: docker compose -p iknow-memory-dev -f ${compose_file} up -d --wait"
  fi
}

# Wait for the status route, then classify with the SAME verdict `fin
# status` uses (scripts/lib/readiness.sh). Runs DETACHED so the parent can
# keep the foreground / Ctrl+C semantics. Stops as soon as the serve
# process dies, so a boot failure reports immediately instead of idling
# out the full timeout.
#
# The verdict is also written to $VERDICT_FILE. `fin up` ends in an `exec`
# of fin-serve, so this process can never turn its own verdict into the
# script's exit code — the file is what makes the reporter's answer
# durable for `fin down` and for an operator reading the state dir.
report_readiness() {
  local serve_pid="${1:-}"
  local deadline=$((SECONDS + READY_TIMEOUT_SECONDS))
  local body=""
  while ((SECONDS < deadline)); do
    if [[ -n "$serve_pid" ]] && ! kill -0 "$serve_pid" 2>/dev/null; then
      printf 'dev-up: NOT READY — fin-serve exited before it answered.\n' >&2
      printf 'dev-up:   the cause is in the output above (a bad settings.yaml or an\n' >&2
      printf 'dev-up:   unreachable FIN_CHECKPOINT_URL are the usual ones).\n' >&2
      write_verdict "$READINESS_EXIT_NOT_READY" "not ready"
      return "$READINESS_EXIT_NOT_READY"
    fi
    body="$(readiness_fetch)" && break
    sleep 1
  done

  if [[ -z "$body" ]]; then
    printf 'dev-up: NOT READY — %s did not answer within %ss.\n' \
      "$(readiness_url)" "$READY_TIMEOUT_SECONDS" >&2
    printf 'dev-up:   a boot failure (bad settings.yaml, missing FIN_CHECKPOINT_URL) stops\n' >&2
    printf 'dev-up:   the process before it binds; see the output above.\n' >&2
    write_verdict "$READINESS_EXIT_NOT_READY" "not ready"
    return "$READINESS_EXIT_NOT_READY"
  fi

  # `|| verdict=$?` rather than a bare call followed by `$?`: under
  # `set -e` a non-zero return aborts the reporter subshell on the spot, so
  # the GATED path (3) printed its explanation and then died before the
  # verdict could be recorded — the one case the file exists for. Observed
  # on a real cold boot, not inferred.
  local verdict=0
  readiness_classify "$body" dev-up || verdict=$?
  case "$verdict" in
  "$READINESS_EXIT_READY") write_verdict "$verdict" "ready" ;;
  "$READINESS_EXIT_GATED") write_verdict "$verdict" "gated" ;;
  *) write_verdict "$verdict" "not ready" ;;
  esac
  return "$verdict"
}

write_verdict() {
  printf '%s %s\n' "$1" "$2" >"$VERDICT_FILE" 2>/dev/null || true
}

print_warnings() {
  if [[ -z "${FIN_API_KEY:-}" ]]; then
    warn "FIN_API_KEY is empty — UI will load; model calls will 401 until ~/.fin/settings.yaml llm.apiKey is set"
  fi
  if [[ -z "${GRAPHRAG_MEMORY_EMBED_API_KEY:-}" && -z "${GRAPHRAG_MEMORY_VECTOR_API_KEY:-}" ]]; then
    warn "no iknow embed key — 工作台+MCP 可达; ingest/retrieve 向量臂需要 GRAPHRAG_MEMORY_EMBED_API_KEY in ~/.iknow-memory/setting.json"
  fi
}

# Sourced callers load the exports and function definitions only.
# Config show and the container check stay on the executed boot path.
if [[ "${BASH_SOURCE[0]}" != "$0" ]]; then
  return 0
fi

if [[ "$CHECK_ONLY" -eq 1 ]]; then
  require_docker_daemon dev-up
  check_serve_port_override
  is_iknow_checkout "$IKNOW_ROOT" || die "iknow-memory checkout not found at $IKNOW_ROOT (set FIN_IKNOW_ROOT)"
  install_iknow_shim
  command -v iknow-memory >/dev/null || die "iknow-memory shim not on PATH"
  log "check ok"
  log "  fin:    $REPO_ROOT"
  log "  iknow:  $IKNOW_ROOT"
  log "  stack:  $FIN_IKNOW_STACK"
  log "  shim:   $(command -v iknow-memory)"
  log "  next:   fin up"
  exit 0
fi

# A worktree may start the demo stack because every Compose call in that
# path has an explicit stable project identity (fin, iknow-memory-dev).
if [[ -f "$REPO_ROOT/.git" ]]; then
  if [[ "${FIN_DEMO_MODE:-0}" != "1" || "$FIN_IKNOW_STACK" != "dev" ]]; then
    die "this checkout is a linked worktree; docker compose up is refused so it cannot create a project volume"
  fi
fi

is_iknow_checkout "$IKNOW_ROOT" || die "iknow-memory checkout not found at $IKNOW_ROOT (set FIN_IKNOW_ROOT)"

# Preflight BEFORE any build. Both port checks are cheap, and a conflict
# is unrecoverable: making the operator sit through a multi-minute SPA /
# image build only to be refused is the failure this ordering avoids.
require_docker_daemon dev-up
if [[ "$DEMO_STACK_ONLY" -eq 0 ]]; then
  check_serve_port_override
  check_serve_port_free
fi
check_checkpoint_port_owner

install_iknow_shim
preflight_iknow_config
ensure_iknow_build
if [[ "$DEMO_STACK_ONLY" -eq 0 ]]; then
  ensure_spa
fi
start_compose
if [[ "$DEMO_STACK_ONLY" -eq 1 ]]; then
  log "demo dev stack is ready"
  exit 0
fi
print_warnings

# Publish this shell's pid (preserved across the `exec` below) so
# `fin down` and the port check can name this workspace.
publish_runtime_files

log "starting fin-serve on :$WORKBENCH_SERVE_PORT — readiness verdict follows"
# `exec` below preserves $$, so the reporter can watch the serve process
# and report a boot failure within a second instead of idling out the
# full timeout.
report_readiness "$$" &
# Tee serve's output to $SERVE_LOG_FILE, which is what `fin logs` tails.
# The process substitution keeps `exec` (and therefore $$, the foreground
# Ctrl+C semantics, and the reporter's liveness watch) intact while the
# log file grows — a plain `| tee` would fork and break all three.
exec uv run fin-serve > >(tee -a "$SERVE_LOG_FILE") 2>&1
