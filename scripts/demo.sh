#!/usr/bin/env bash
# Start or stop the isolated local ZEUS demo.
set -euo pipefail
# Runtime settings include URLs and credentials. Do not let inherited bash
# tracing place expanded values in terminal output or the startup log.
set +x

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$REPO_ROOT/scripts/lib/readiness.sh"
source "$REPO_ROOT/scripts/lib/fin-root.sh"
source "$REPO_ROOT/scripts/lib/demo-ledger.sh"

ACTION="${1:-}"
BASE_HOME="${2:-}"
if [[ "$#" -ne 2 || ( "$ACTION" != "start" && "$ACTION" != "stop" ) || -z "$BASE_HOME" ]]; then
  printf 'fin demo: internal usage error\n' >&2
  exit 2
fi

DEMO_HOME="$BASE_HOME/demo"
DEMO_IKNOW_HOME="$DEMO_HOME/iknow-memory"
SERVE_PID=""
DEMO_URL="http://127.0.0.1:$WORKBENCH_SERVE_PORT"

log() { printf 'fin demo: %s\n' "$*"; }

demo_environment() {
  export FIN_SETTINGS_FILE="${FIN_SETTINGS_FILE:-$BASE_HOME/settings.yaml}"
  export FIN_HOME="$DEMO_HOME"
  export IKNOW_MEMORY_HOME="$DEMO_IKNOW_HOME"
  export FIN_DEMO_MODE=1
  export FIN_IKNOW_STACK=dev
  export FIN_IKNOW_ROOT="$IKNOW_ROOT"
}

fin_host_python() {
  if [[ -x "$REPO_ROOT/.venv/bin/python" ]]; then
    PYTHONPATH="$REPO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}" \
      "$REPO_ROOT/.venv/bin/python" "$@"
    return
  fi
  PYTHONPATH="$REPO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}" \
    uv run --no-sync python "$@"
}

require_checkpoint_setting() {
  # EXIT: the script returns success only when the normal env/YAML projection has a non-empty checkpoint URL.
  if fin_host_python "$REPO_ROOT/scripts/demo_checkpoint_preflight.py" >/dev/null 2>&1; then
    return 0
  fi
  printf 'fin demo: missing FIN_CHECKPOINT_URL or invalid FIN_SETTINGS_FILE; configure the process environment or checkpoint.url in FIN_SETTINGS_FILE\n' >&2
  return 1
}

cleanup_demo() {
  local cleanup_status=0
  if [[ -n "$SERVE_PID" ]] && kill -0 "$SERVE_PID" 2>/dev/null; then
    # EXIT: the serve process may exit between the liveness check and either operation.
    kill "$SERVE_PID" 2>/dev/null || true
    wait "$SERVE_PID" 2>/dev/null || true
    SERVE_PID=""
  fi
  if ! bash "$REPO_ROOT/scripts/dev-down.sh" --demo >/dev/null 2>&1; then
    cleanup_status=1
  fi
  if ! demo_ledger_stop; then
    cleanup_status=1
  fi
  return "$cleanup_status"
}

fail_stage() {
  local stage="$1" status="${2:-1}"
  printf 'fin demo: stage %s failed (exit %s)\n' "$stage" "$status" >&2
  if ! cleanup_demo; then
    printf 'fin demo: cleanup also failed; inspect the demo service and dev stack\n' >&2
  fi
  exit 1
}

stop_demo() {
  local status=0
  if ! bash "$REPO_ROOT/scripts/dev-down.sh" --demo; then
    printf 'fin demo: stage stop demo failed\n' >&2
    status=1
  fi
  if ! demo_ledger_stop; then
    status=1
  fi
  if [[ "$status" -eq 0 ]]; then
    log "stopped"
  fi
  return "$status"
}

stop_fin_bench() {
  local output id
  local -a containers=()
  if ! output="$(docker ps -q --filter label=com.docker.compose.project=fin-bench)"; then
    return 1
  fi
  while IFS= read -r id; do
    [[ -n "$id" ]] && containers+=("$id")
  done <<<"$output"
  if (("${#containers[@]}" == 0)); then
    log "fin-bench has no running containers"
    return 0
  fi
  log "stopping fin-bench containers"
  docker stop "${containers[@]}"
}

read_demo_setting() {
  local setting="$1" value
  if [[ -n "${!setting:-}" ]]; then
    # EXIT: an explicit process setting wins; only absent values fall through to the isolated SettingStore.
    printf '%s' "${!setting}"
    return 0
  fi
  # `iknow-memory-config get` intentionally redacts database credentials and
  # secret values, so its display output cannot be used as runtime config.
  # Read through the paired repo's SettingStore while IKNOW_MEMORY_HOME is
  # pinned to the isolated demo directory. The value stays in this process
  # substitution and is never printed or logged.
  if ! value="$(FIN_DEMO_SETTING_KEY="$setting" node --input-type=module -e '
import { pathToFileURL } from "node:url";
const storeModule = pathToFileURL(
  `${process.env.FIN_IKNOW_ROOT}/dist/config/setting-store.js`
);
const { createSettingStore } = await import(storeModule.href);
const key = process.env.FIN_DEMO_SETTING_KEY;
const value = (await createSettingStore().read())[key];
if (typeof value !== "string" || value.trim() === "") process.exit(3);
process.stdout.write(value);
' 2>/dev/null)"; then
    return 1
  fi
  [[ -n "$value" ]] || return 1
  printf '%s' "$value"
}

require_demo_setting() {
  local setting="$1" value
  if ! value="$(read_demo_setting "$setting")" || [[ -z "$value" ]]; then
    printf 'fin demo: missing %s; set it in the process environment or the isolated demo setting at %s/setting.json\n' \
      "$setting" "$DEMO_IKNOW_HOME" >&2
    return 1
  fi
  printf -v "$setting" '%s' "$value"
  export "$setting"
}

resolve_embed_setting() {
  local public_name="$1" config_name="$2" value
  if [[ -n "${!public_name:-}" ]]; then
    value="${!public_name}"
  else
    value="$(read_demo_setting "$config_name")" || {
      printf 'fin demo: missing %s; set %s or provision it in %s/setting.json\n' \
        "$config_name" "$public_name" "$DEMO_IKNOW_HOME" >&2
      return 1
    }
  fi
  [[ -n "$value" ]] || {
    printf 'fin demo: missing %s; set %s or provision it in %s/setting.json\n' \
      "$config_name" "$public_name" "$DEMO_IKNOW_HOME" >&2
    return 1
  }
  printf -v "$config_name" '%s' "$value"
  export "$config_name"
  case "$config_name" in
  GRAPHRAG_MEMORY_EMBED_DIMENSIONS)
    FIN_DEMO_EMBED_DIMENSIONS="$value"
    export FIN_DEMO_EMBED_DIMENSIONS
    ;;
  GRAPHRAG_MEMORY_EMBED_MODEL)
    FIN_DEMO_EMBED_GENERATION="$value"
    export FIN_DEMO_EMBED_GENERATION
    ;;
  esac
}

start_demo_stack() {
  bash "$REPO_ROOT/scripts/dev-up.sh" --demo-stack-only
}

seed_demo() {
  # EXIT: the existing seed chain reuses succeeded jobs and makes a ready demo a zero-embed no-op.
  uv run --no-sync python "$REPO_ROOT/scripts/seed_demo.py" \
    --yes \
    --embed-dimensions "$FIN_DEMO_EMBED_DIMENSIONS" \
    --embed-generation "$FIN_DEMO_EMBED_GENERATION" \
    --iknow-root "$IKNOW_ROOT"
}

start_demo_serve() {
  local timeout_seconds="${FIN_DEMO_START_TIMEOUT_SECONDS:-300}" waited=0 body="" verdict=0
  local published_pid=""
  local real_uv="" wrapper_dir="$DEMO_HOME/run/demo-bin"
  local startup_log="$DEMO_HOME/run/demo-start.log"
  if [[ "${FIN_DEMO_SUPPRESS_START_LOG:-0}" == "1" ]]; then
    startup_log=/dev/null
  fi
  if [[ ! "$timeout_seconds" =~ ^[1-9][0-9]*$ ]]; then
    printf 'fin demo: FIN_DEMO_START_TIMEOUT_SECONDS must be a positive integer\n' >&2
    return 1
  fi
  real_uv="$(command -v uv)" || {
    printf 'fin demo: uv is required to start fin-serve\n' >&2
    return 1
  }
  if ! demo_ledger_install_uv_wrapper "$wrapper_dir"; then
    return 1
  fi
  if ! mkdir -p "$DEMO_HOME/run"; then
    printf 'fin demo: could not prepare the service runtime directory\n' >&2
    return 1
  fi
  # ``9>&-`` closes the demo operation lock in the service child. The lock
  # serializes start and stop operations; a server that kept holding it would
  # make every later ``fin demo --stop`` report "another demo start or stop is
  # in progress", so the demo could never be stopped.
  PATH="$wrapper_dir:$PATH" \
    FIN_DEMO_REAL_UV="$real_uv" \
    FIN_DEMO_LEDGER_CONTAINER="$(demo_ledger_container_name)" \
    nohup bash "$REPO_ROOT/scripts/dev-up.sh" \
    >>"$startup_log" 2>&1 </dev/null 9>&- &
  SERVE_PID=$!

  while (( waited < timeout_seconds )); do
    published_pid=""
    if [[ -f "$DEMO_HOME/run/serve.pid" ]]; then
      # EXIT: an unreadable or partial pid file means the owner has not published its pid yet.
      IFS= read -r published_pid <"$DEMO_HOME/run/serve.pid" || published_pid=""
    fi
    # Do not accept a reachable server that was already using port 8000.
    # dev-up publishes this pid only after its port-ownership checks pass.
    if [[ "$published_pid" == "$SERVE_PID" ]]; then
      # EXIT: an unavailable status response is treated as startup-in-progress until the timeout.
      body="$(readiness_fetch)" || body=""
      if [[ -n "$body" ]]; then
        readiness_classify "$body" "fin demo" || verdict=$?
        if [[ "$verdict" -eq "$READINESS_EXIT_READY" ]]; then
          SERVE_PID=""
          return 0
        fi
        if [[ "$verdict" -eq "$READINESS_EXIT_GATED" || "$verdict" -eq "$READINESS_EXIT_NOT_READY" ]]; then
          return 1
        fi
      fi
    fi
    if ! kill -0 "$SERVE_PID" 2>/dev/null; then
      # EXIT: the process has already exited; collect its status without masking the named startup failure.
      wait "$SERVE_PID" 2>/dev/null || true
      SERVE_PID=""
      if [[ "$startup_log" == "/dev/null" ]]; then
        printf 'fin demo: fin-serve exited before readiness; startup output was suppressed\n' >&2
      else
        printf 'fin demo: fin-serve exited before readiness; inspect %s\n' "$startup_log" >&2
      fi
      return 1
    fi
    sleep 1
    waited=$((waited + 1))
  done
  if [[ "$startup_log" == "/dev/null" ]]; then
    printf 'fin demo: fin-serve did not answer within %ss; startup output was suppressed\n' \
      "$timeout_seconds" >&2
  else
    printf 'fin demo: fin-serve did not answer within %ss; inspect %s\n' \
      "$timeout_seconds" "$startup_log" >&2
  fi
  return 1
}

open_demo_browser() {
  local opener
  for opener in xdg-open wslview open; do
    # EXIT: a successful opener ends the chain; a missing or failing opener falls through to the next platform command.
    command -v "$opener" >/dev/null 2>&1 || continue
    if "$opener" "$DEMO_URL" >/dev/null 2>&1; then
      return 0
    fi
  done
  printf 'fin demo: no browser opener succeeded (tried xdg-open, wslview, and open)\n' >&2
  return 1
}

if ! command -v flock >/dev/null 2>&1; then
  printf 'fin demo: flock is required to coordinate the shared demo stack\n' >&2
  exit 1
fi
LOCK_PATH="${TMPDIR:-/tmp}/fin-demo-${UID:-$(id -u)}.lock"
if ! exec 9>"$LOCK_PATH"; then
  printf 'fin demo: could not open the demo operation lock\n' >&2
  exit 1
fi
if ! flock -n 9; then
  printf 'fin demo: another demo start or stop is in progress\n' >&2
  exit 1
fi

IKNOW_ROOT="$(resolve_iknow_root)"
demo_environment

if [[ "$ACTION" == "stop" ]]; then
  stop_demo
  exit $?
fi

if ! is_iknow_checkout "$IKNOW_ROOT"; then
  printf 'fin demo: missing iknow-memory checkout at %s; set FIN_IKNOW_ROOT\n' "$IKNOW_ROOT" >&2
  exit 1
fi

# Read every required value from the isolated demo environment before any
# service or stack is stopped or started. A preflight failure must leave the
# running development environment untouched.
if ! require_demo_setting GRAPHRAG_MEMORY_DB_URL; then
  exit 1
fi
if ! require_demo_setting GRAPHRAG_MEMORY_ES_URL; then
  exit 1
fi
if ! require_demo_setting GRAPHRAG_MEMORY_NEO4J_URI; then
  exit 1
fi
if ! resolve_embed_setting FIN_DEMO_EMBED_DIMENSIONS GRAPHRAG_MEMORY_EMBED_DIMENSIONS; then
  exit 1
fi
if ! resolve_embed_setting FIN_DEMO_EMBED_GENERATION GRAPHRAG_MEMORY_EMBED_MODEL; then
  exit 1
fi
if ! require_checkpoint_setting; then
  exit 1
fi

if ! mkdir -p "$DEMO_HOME/run" "$DEMO_IKNOW_HOME"; then
  printf 'fin demo: stage prepare demo directory failed\n' >&2
  exit 1
fi

trap 'fail_stage interrupted 130' HUP INT TERM

if ! stop_demo; then
  fail_stage "stop previous demo"
fi
if ! require_docker_daemon "fin demo"; then
  fail_stage "stop fin-bench"
fi
if ! stop_fin_bench; then
  fail_stage "stop fin-bench"
fi
if ! start_demo_stack; then
  fail_stage "start dev stack"
fi
if ! seed_demo; then
  fail_stage "seed ZEUS demo data"
fi
if ! demo_ledger_start "$REPO_ROOT"; then
  fail_stage "start synthetic SQL ledger"
fi
if ! start_demo_serve; then
  fail_stage "start fin service"
fi

log "available at $DEMO_URL"
if [[ "${FIN_DEMO_SKIP_BROWSER:-0}" == "1" ]]; then
  log "browser opening skipped"
  exit 0
fi
if ! open_demo_browser; then
  fail_stage "open browser"
fi
log "browser opened"
