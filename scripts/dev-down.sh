#!/usr/bin/env bash
# Stop the local 工作台 stack: fin-serve (via its pid file) and both
# compose projects. Counterpart to scripts/dev-up.sh; `fin down` calls
# this.
#
# Usage:
#   bash scripts/dev-down.sh            # serve + both composes
#   bash scripts/dev-down.sh --keep-infra   # leave iknow infra running
#
# Stopping is intentionally explicit about what it kills. It never
# blind-pkill's: an operator's other fin checkout is exactly what the
# pid file and the port check are here to tell apart.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# shellcheck source=scripts/lib/readiness.sh
source "$REPO_ROOT/scripts/lib/readiness.sh"
# shellcheck source=scripts/lib/fin-root.sh
source "$REPO_ROOT/scripts/lib/fin-root.sh"

KEEP_INFRA=0
DEMO_ONLY=0
for arg in "$@"; do
  case "$arg" in
  --keep-infra) KEEP_INFRA=1 ;;
  --demo) DEMO_ONLY=1 ;;
  *)
    printf 'dev-down: unknown argument %s\n' "$arg" >&2
    exit 2
    ;;
  esac
done

# The probe port comes from readiness.sh (see there for why it is a probe
# port and not a knob). The stop path used to carry its own literal, which
# meant an overridden port made `down` watch the wrong socket and delete
# the pid file of a serve that was still running.
SERVE_PORT="$WORKBENCH_SERVE_PORT"

# Same run dir as dev-up. Resolve it before docker so a bad FIN_HOME
# fails before any compose call. Worktree source wins over a shared venv.
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
    printf 'dev-down: could not resolve FIN_HOME\n' >&2
    exit 1
  fi
  [[ -n "$dir" ]] || {
    printf 'dev-down: could not resolve FIN_HOME\n' >&2
    exit 1
  }
  printf '%s\n' "$dir"
}

STATE_DIR="$(resolve_state_dir)"
SERVE_PID_FILE="$STATE_DIR/serve.pid"
VERDICT_FILE="$STATE_DIR/ready"

log() { printf 'dev-down: %s\n' "$*"; }

IKNOW_ROOT="$(resolve_iknow_root)"

# The pid listener for $SERVE_PORT, or empty. `ss` needs no privileges
# for our own sockets; fall back to nothing when it is unavailable.
#
# `awk ... exit` rather than `head -n1`: under `pipefail` a `head` that
# exits early SIGPIPEs the producer and the pipeline returns 141, so the
# diagnostic that is supposed to NAME the port holder degraded silently to
# "could not identify the listener".
serve_port_listener() {
  command -v ss >/dev/null || return 0
  ss -ltnp 2>/dev/null | awk -v p=":$SERVE_PORT" '
    $4 ~ p { if (match($0, /pid=[0-9]+/)) { print substr($0, RSTART + 4, RLENGTH - 4); exit } }
  '
}

stop_serve() {
  local pid="" listener=""
  if [[ -f "$SERVE_PID_FILE" ]]; then
    pid="$(cat "$SERVE_PID_FILE" 2>/dev/null || true)"
  fi

  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
    log "stopping fin-serve (pid $pid)"
    kill "$pid" 2>/dev/null || true
  elif [[ -n "$pid" ]]; then
    log "fin-serve pid $pid is already gone (stale pid file)"
  else
    log "no fin-serve pid file at $SERVE_PID_FILE"
  fi

  if [[ "$DEMO_ONLY" -eq 1 ]] &&
    { [[ -z "$pid" ]] || ! kill -0 "$pid" 2>/dev/null; }; then
    rm -f "$SERVE_PID_FILE" "$VERDICT_FILE"
    return 0
  fi

  # Wait for the port to go quiet. Killing `uv` should take its python
  # child with it; if it does not, say so instead of guessing.
  #
  # The pid file is only removed once the port actually answers nothing:
  # removing it while serve still runs would hide the process from the
  # next `fin down`, which is how a stray serve used to survive a "success".
  local waited=0
  while (( waited < 10 )); do
    if ! curl -fsS --max-time 1 "$(readiness_url)" >/dev/null 2>&1; then
      rm -f "$SERVE_PID_FILE" "$VERDICT_FILE"
      return 0
    fi
    sleep 1
    waited=$((waited + 1))
  done

  listener="$(serve_port_listener)"
  printf 'dev-down: port %s still answers after 10s.\n' "$SERVE_PORT" >&2
  if [[ -n "$listener" ]]; then
    printf 'dev-down:   listener pid %s — %s\n' "$listener" "$(ps -o args= -p "$listener" 2>/dev/null || echo '?')" >&2
    printf 'dev-down:   stop it with: kill %s\n' "$listener" >&2
  else
    printf 'dev-down:   could not identify the listener; check: ss -ltnp | grep :%s\n' \
      "$SERVE_PORT" >&2
  fi
  return 1
}

# A failing `compose down` used to read as success: called as
# `stop_compose || status=1`, `set -e` is suspended inside the function, so
# a dead daemon or an unreachable project was swallowed and the last `log`
# made the function return 0. The daemon is checked up front, and each
# compose failure now names itself and sets the exit status.
stop_compose() {
  local rc=0
  log "stopping fin dev Postgres"
  if ! docker compose -p fin-dev -f "$REPO_ROOT/docker-compose.yml" down; then
    printf 'dev-down: stopping fin Postgres FAILED (see docker output above)\n' >&2
    rc=1
  fi
  # plans/fin-storage-identity.md P1: compose project fin is the
  # primary-checkout Postgres and this script does not stop it.
  log "primary-checkout Postgres stays up; fin down stops only the fin-dev project"
  if [[ "$KEEP_INFRA" -eq 1 ]]; then
    log "leaving iknow-memory infra running (--keep-infra)"
    return "$rc"
  fi
  if [[ ! -f "$IKNOW_ROOT/docker-compose.yml" ]]; then
    printf 'dev-down: no iknow-memory compose at %s — the infra dev-up started\n' \
      "$IKNOW_ROOT" >&2
    printf 'dev-down:   (if any) is still running. Point FIN_IKNOW_ROOT at the checkout,\n' >&2
    printf 'dev-down:   or stop it by hand:  docker compose -f <checkout>/docker-compose.yml down\n' >&2
    return 1
  fi
  log "stopping iknow-memory infra ($IKNOW_ROOT)"
  if ! docker compose -f "$IKNOW_ROOT/docker-compose.yml" down; then
    printf 'dev-down: stopping iknow-memory infra FAILED (see docker output above)\n' >&2
    return 1
  fi
  return "$rc"
}

stop_demo_project() {
  local project="$1" output id
  local -a containers=()
  if ! output="$(docker ps -q --filter "label=com.docker.compose.project=$project")"; then
    printf 'dev-down: listing %s containers FAILED\n' "$project" >&2
    return 1
  fi
  while IFS= read -r id; do
    [[ -n "$id" ]] && containers+=("$id")
  done <<<"$output"
  if (("${#containers[@]}" == 0)); then
    log "$project has no running containers"
    return 0
  fi
  log "stopping $project containers"
  if ! docker stop "${containers[@]}"; then
    printf 'dev-down: stopping %s containers FAILED\n' "$project" >&2
    return 1
  fi
  return 0
}

if [[ "$DEMO_ONLY" -eq 1 ]]; then
  status=0
  stop_serve || status=1
  if docker_daemon_answers; then
    stop_demo_project fin-dev || status=1
    stop_demo_project iknow-memory-dev || status=1
  else
    log "Docker daemon is not answering; demo containers are unavailable"
  fi
  if [[ "$status" -eq 0 ]]; then
    log "demo stopped"
  fi
  exit "$status"
fi

# The daemon gate comes first: without it a dead daemon turned every
# compose call into a swallowed failure and `fin down` exited 0 having
# stopped nothing.
require_docker_daemon dev-down

status=0
stop_serve || status=1
stop_compose || status=1
if [[ "$status" -eq 0 ]]; then
  log "stopped"
fi
exit "$status"
