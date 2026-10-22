#!/usr/bin/env bash
# Run the ZEUS browser acceptance only against caller-provided _test databases.
set -euo pipefail
set +x

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FIN_COMMAND="$REPO_ROOT/scripts/fin"
DB_CHECK="$REPO_ROOT/scripts/demo_acceptance_db_check.py"
RELEASE_CLAIM="$REPO_ROOT/scripts/demo_acceptance_release_instance.py"
DEMO_URL="http://127.0.0.1:8000"
ACCEPTANCE_HOME=""
START_ATTEMPTED=0
PARALLEL_WORKERS=""
FORCE_SERIAL=0

fail() {
  printf 'demo browser acceptance: %s\n' "$*" >&2
  exit 1
}

usage() {
  printf 'Usage: scripts/demo-browser-acceptance.sh [--parallel=N | --serial]\n' >&2
  printf '  --parallel=N  independent check groups in N concurrent sessions (1-6)\n' >&2
  printf '  --serial      run every check in one session (debug path)\n' >&2
  printf 'Default: --parallel=%s (FIN_DEMO_ACCEPTANCE_PARALLEL)\n' \
    "${FIN_DEMO_ACCEPTANCE_PARALLEL:-5}" >&2
}

for argument in "$@"; do
  case "$argument" in
    --parallel=[1-6]) PARALLEL_WORKERS="${argument#--parallel=}" ;;
    --serial) FORCE_SERIAL=1 ;;
    *)
      usage
      exit 2
      ;;
  esac
done
if [[ "$FORCE_SERIAL" == "1" && -n "$PARALLEL_WORKERS" ]]; then
  printf 'demo browser acceptance: --serial and --parallel are mutually exclusive\n' >&2
  exit 2
fi
if [[ "$FORCE_SERIAL" != "1" && -z "$PARALLEL_WORKERS" ]]; then
  PARALLEL_WORKERS="${FIN_DEMO_ACCEPTANCE_PARALLEL:-5}"
fi
if [[ "$FORCE_SERIAL" != "1" && ! "$PARALLEL_WORKERS" =~ ^[1-6]$ ]]; then
  printf 'demo browser acceptance: parallel worker count must be 1-6\n' >&2
  exit 2
fi

required_settings=(
  FIN_CHECKPOINT_URL
  GRAPHRAG_MEMORY_DB_URL
  FIN_API_KEY
  GRAPHRAG_MEMORY_EMBED_API_KEY
  GRAPHRAG_MEMORY_ES_URL
  GRAPHRAG_MEMORY_NEO4J_URI
  GRAPHRAG_MEMORY_EMBED_DIMENSIONS
  GRAPHRAG_MEMORY_EMBED_MODEL
)
for setting in "${required_settings[@]}"; do
  if [[ -z "${!setting:-}" ]]; then
    fail "missing caller-provided setting $setting; no database or service was touched"
  fi
done

for tool in uv node curl jq flock docker; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    fail "required command $tool is unavailable; no database or service was touched"
  fi
done
browser_cli="${PLAYWRIGHT_CLI:-playwright-cli}"
if ! command -v "$browser_cli" >/dev/null 2>&1; then
  fail "Playwright CLI is unavailable; no database or service was touched"
fi
# Node's spawn of a bare command name can fail with EINVAL on this host's
# WSL-mounted pnpm shim even though bash executes it fine. Hand the runner
# the resolved absolute path instead of the name.
PLAYWRIGHT_CLI="$(type -P "$browser_cli" || true)"
if [[ -z "$PLAYWRIGHT_CLI" ]]; then
  fail "Playwright CLI is not an executable on PATH; no database or service was touched"
fi
export PLAYWRIGHT_CLI
if ! docker compose version >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
  fail "Docker is unavailable; no database or service was touched"
fi

if [[ ! -x "$FIN_COMMAND" || ! -f "$DB_CHECK" || ! -f "$RELEASE_CLAIM" ]]; then
  fail "the fin checkout is missing its demo acceptance entrypoints"
fi

# A new temporary tree keeps both FIN_HOME and IKNOW_MEMORY_HOME out of the
# operator homes without inspecting either default-home configuration.
ACCEPTANCE_HOME="$(mktemp -d /tmp/fin-demo-browser-acceptance.XXXXXX)" ||
  fail "could not create an isolated temporary home"
ACCEPTANCE_HOME="$(cd "$ACCEPTANCE_HOME" && pwd -P)"
FIN_HOME="$ACCEPTANCE_HOME/fin"
FIN_SETTINGS_FILE="$FIN_HOME/settings.yaml"
IKNOW_MEMORY_HOME="$FIN_HOME/demo/iknow-memory"
if [[ "$IKNOW_MEMORY_HOME" == "${HOME:-}/.iknow-memory" || "$IKNOW_MEMORY_HOME" == "${HOME:-}/.iknow-memory/"* ]]; then
  fail "temporary IKNOW_MEMORY_HOME is not isolated"
fi
export FIN_HOME FIN_SETTINGS_FILE IKNOW_MEMORY_HOME
# Do not let libpq fall back to the operator's service or password files.
export PGSERVICEFILE="$ACCEPTANCE_HOME/pg_service.conf"
export PGPASSFILE="$ACCEPTANCE_HOME/pgpass"
unset PGSERVICE
export WORKBENCH_SERVE_PORT=8000
export FIN_DEMO_MODE=1 FIN_IKNOW_STACK=dev
export FIN_DEMO_SKIP_BROWSER=1
export FIN_DEMO_SUPPRESS_START_LOG=1
export FIN_DEMO_EMBED_DIMENSIONS="$GRAPHRAG_MEMORY_EMBED_DIMENSIONS"
export FIN_DEMO_EMBED_GENERATION="$GRAPHRAG_MEMORY_EMBED_MODEL"
export FIN_RETRIEVE_ADAPTER=mcp
unset FIN_SCRIPTED_MODEL FIN_SCRIPTED_SCENARIO FIN_DEMO_ACCEPTANCE_READY
# The demo browser flow validates only its checkpoint and document-index
# databases. Keep optional SQL retrieval and ingestion metadata connections
# unavailable so inherited URLs cannot route queries outside those targets.
unset FIN_BUSINESS_LEDGER_URL
unset FIN_INGEST_STATE_DSN FIN_METADATA_DSN FIN_METADATA_URL

cleanup() {
  local status="$?"
  local stopped=1
  trap - EXIT INT TERM HUP
  if [[ "$START_ATTEMPTED" == "1" ]]; then
    if ! env \
      -u FIN_CHECKPOINT_URL \
      -u GRAPHRAG_MEMORY_DB_URL \
      -u FIN_BUSINESS_LEDGER_URL \
      -u FIN_INGEST_STATE_DSN \
      -u FIN_METADATA_DSN \
      -u FIN_METADATA_URL \
      -u GRAPHRAG_MEMORY_ES_URL \
      -u GRAPHRAG_MEMORY_NEO4J_URI \
      -u GRAPHRAG_MEMORY_EMBED_API_KEY \
      -u GRAPHRAG_MEMORY_VECTOR_API_KEY \
      -u PGPASSWORD \
      -u FIN_API_KEY \
      "$FIN_COMMAND" demo --stop >/dev/null 2>&1; then
      printf 'demo browser acceptance: stopping the isolated demo stack failed\n' >&2
      stopped=0
      if [[ "$status" == "0" ]]; then status=1; fi
    fi
  fi
  if [[ -n "$ACCEPTANCE_HOME" && "$stopped" == "1" ]]; then
    rm -rf -- "$ACCEPTANCE_HOME" 2>/dev/null || {
      printf 'demo browser acceptance: temporary home cleanup failed\n' >&2
      if [[ "$status" == "0" ]]; then status=1; fi
    }
  elif [[ -n "$ACCEPTANCE_HOME" ]]; then
    printf 'demo browser acceptance: isolated home retained for recovery at %s\n' \
      "$ACCEPTANCE_HOME" >&2
  fi
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

# Refuse a service already occupying the fixed browser target before demo
# seeding can reset any data.
if curl --silent --show-error --max-time 2 "$DEMO_URL/api/status" >/dev/null 2>&1; then
  fail "loopback port 8000 already serves a workspace; no database or service was touched"
fi

if ! uv run --no-sync python "$DB_CHECK"; then
  fail "PostgreSQL target verification failed; no demo seed or service startup was attempted"
fi

# The temporary home above is new on every run, so the instance claim a
# previous run left in the demo checkpoint database would refuse to serve
# it. Release that claim before the demo starts; the claim is the only row
# this step touches.
if ! uv run --no-sync python "$RELEASE_CLAIM"; then
  fail "the demo instance claim could not be released; no demo seed or service startup was attempted"
fi

START_ATTEMPTED=1
if ! "$FIN_COMMAND" demo >/dev/null 2>&1; then
  fail "fin demo could not start; its diagnostic output was suppressed to keep credentials out of logs"
fi

printf 'demo browser acceptance: checking %s\n' "$DEMO_URL"
browser_status=0
(
  unset FIN_CHECKPOINT_URL
  unset FIN_BUSINESS_LEDGER_URL FIN_INGEST_STATE_DSN FIN_METADATA_DSN FIN_METADATA_URL
  unset GRAPHRAG_MEMORY_DB_URL GRAPHRAG_MEMORY_ES_URL GRAPHRAG_MEMORY_NEO4J_URI
  unset GRAPHRAG_MEMORY_EMBED_API_KEY GRAPHRAG_MEMORY_VECTOR_API_KEY
  unset PGPASSWORD
  unset FIN_API_KEY FIN_JEV_API_KEY FIN_OBJECT_STORE_ACCESS_KEY FIN_OBJECT_STORE_SECRET_KEY
  unset OPENAI_API_KEY ANTHROPIC_API_KEY
  unset FIN_BASE_URL FIN_MODEL
  export FIN_DEMO_ACCEPTANCE_READY=1
  export FIN_DEMO_E2E_BASE_URL="$DEMO_URL"
  browser_args=(--base-url="$DEMO_URL")
  if [[ "$FORCE_SERIAL" == "1" ]]; then
    browser_args+=(--serial)
  else
    browser_args+=(--parallel="$PARALLEL_WORKERS")
  fi
  node "$REPO_ROOT/scripts/demo-browser-acceptance.mjs" "${browser_args[@]}"
) || browser_status=$?
if [[ "$browser_status" -ne 0 ]]; then
  exit "$browser_status"
fi
printf 'demo browser acceptance: passed\n'
