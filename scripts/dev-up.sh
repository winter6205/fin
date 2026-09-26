#!/usr/bin/env bash
# One-shot local 工作台: infra + iknow-memory on PATH + SPA + fin-serve.
# After this process is up, open http://127.0.0.1:8000 and send.
#
# Usage:
#   bash scripts/dev-up.sh
#   bash scripts/dev-up.sh --check    # preflight only
#   FIN_IKNOW_ROOT=/path/to/iknow-memory bash scripts/dev-up.sh
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

CHECK_ONLY=0
if [[ "${1:-}" == "--check" ]]; then
  CHECK_ONLY=1
fi

# Seconds the readiness reporter waits for `GET /api/status` to answer.
# Generous: a cold iknow handshake can exceed the product probe's own 2s
# budget.
READY_TIMEOUT_SECONDS="${FIN_READY_TIMEOUT_SECONDS:-90}"
CHECKPOINT_PORT=5433
STATE_DIR="$REPO_ROOT/.fin"
SERVE_PID_FILE="$STATE_DIR/serve.pid"
SERVE_LOG_FILE="$STATE_DIR/serve.log"
# Written by the readiness reporter below, read by `fin down` (and by an
# operator). It is the only place `fin up`'s verdict survives: the
# foreground `exec` hands the exit code to fin-serve, so the scriptable
# answer to "did this boot gate?" lives here rather than in a status code.
VERDICT_FILE="$STATE_DIR/ready"

die() {
  printf 'dev-up: %s\n' "$*" >&2
  exit 1
}

log() { printf 'dev-up: %s\n' "$*"; }
warn() { printf 'dev-up: WARN: %s\n' "$*" >&2; }

load_env_file() {
  local file="$1"
  [[ -f "$file" ]] || return 0
  local line key value
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    [[ -z "$line" || "$line" == \#* ]] && continue
    [[ "$line" == export\ * ]] && line="${line#export }"
    [[ "$line" == *=* ]] || continue
    key="${line%%=*}"
    value="${line#*=}"
    key="${key%"${key##*[![:space:]]}"}"
    key="${key#"${key%%[![:space:]]*}"}"
    [[ -n "$key" ]] || continue
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    if [[ ${#value} -ge 2 ]]; then
      local q="${value:0:1}"
      if [[ "$q" == '"' || "$q" == "'" ]] && [[ "${value: -1}" == "$q" ]]; then
        value="${value:1:-1}"
      fi
    fi
    if [[ -z "${!key:-}" ]]; then
      export "$key=$value"
    fi
  done <"$file"
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

# This compose project's own name. Docker derives it from the checkout
# directory, so each worktree gets a distinct name — which is what lets
# the ownership check tell "mine" from "another tree's".
compose_project_name() {
  # The `|| true` matters under `set -e`: a compose parse failure inside
  # this substitution must degrade to "unknown owner", not abort boot.
  docker compose -f "$REPO_ROOT/docker-compose.yml" config --format json 2>/dev/null |
    jq -r '.name // empty' || true
}

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

# Refuse to start onto a port a DIFFERENT workspace already owns. Every
# worktree publishes the same 5433:5433, so a second `fin up` in a
# sibling tree otherwise dies deep inside compose with a port error that
# never names the offending workspace.
check_checkpoint_port_owner() {
  local mine owner
  mine="$(compose_project_name)"
  [[ -n "$mine" ]] || return 0
  # `awk ... exit` rather than `head -n1`: under `pipefail` a `head` that
  # exits early SIGPIPEs the producer and the whole pipeline returns 141,
  # which under `set -e` would abort boot on the diagnostic path.
  owner="$(docker ps --filter "publish=$CHECKPOINT_PORT" --format '{{.Label "com.docker.compose.project"}}' 2>/dev/null | awk '{print; exit}')"
  [[ -n "$owner" ]] || return 0
  [[ "$owner" == "$mine" ]] && return 0
  die "port $CHECKPOINT_PORT is already published by compose project '$owner' (this tree is '$mine').
  Another fin checkout is up. Either stop it:
    docker compose -p $owner down
  or work in that tree instead. The port is pinned 5433:5433 by the spec
  acceptance gate (tests/host/test_compile.py), so it is not overridable here."
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

load_env_file "$REPO_ROOT/.env"
load_env_file "$IKNOW_ROOT/.env.local"
load_env_file "$IKNOW_ROOT/.env"

export FIN_CHECKPOINT_URL="${FIN_CHECKPOINT_URL:-postgresql://fin:fin@localhost:5433/fin?sslmode=disable}"
export FIN_RETRIEVE_ADAPTER="${FIN_RETRIEVE_ADAPTER:-mcp}"
export FIN_BASE_URL="${FIN_BASE_URL:-https://api.minimaxi.com/v1}"
export FIN_MODEL="${FIN_MODEL:-MiniMax-M3}"

export GRAPHRAG_MEMORY_STORAGE="${GRAPHRAG_MEMORY_STORAGE:-pgvector}"
export GRAPHRAG_MEMORY_DB_URL="${GRAPHRAG_MEMORY_DB_URL:-postgresql://iknow_memory:iknow_memory@localhost:5432/iknow_memory}"
export GRAPHRAG_MEMORY_NEO4J_URI="${GRAPHRAG_MEMORY_NEO4J_URI:-bolt://localhost:7687}"
export GRAPHRAG_MEMORY_NEO4J_USER="${GRAPHRAG_MEMORY_NEO4J_USER:-neo4j}"
export GRAPHRAG_MEMORY_NEO4J_PASSWORD="${GRAPHRAG_MEMORY_NEO4J_PASSWORD:-iknow-memory-dev}"
export GRAPHRAG_MEMORY_ES_URL="${GRAPHRAG_MEMORY_ES_URL:-http://localhost:9200}"

if [[ -z "${ANTHROPIC_AUTH_TOKEN:-}" && -n "${FIN_API_KEY:-}" ]]; then
  export ANTHROPIC_AUTH_TOKEN="$FIN_API_KEY"
fi

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

iknow_images_present() {
  docker image inspect \
    "iknow-memory-neo4j:5.26.0-gds-2.13.2" \
    "iknow-memory-elasticsearch:8.19.20" \
    >/dev/null 2>&1
}

start_compose() {
  log "fin Postgres (:$CHECKPOINT_PORT)"
  check_checkpoint_port_owner
  docker compose -f "$REPO_ROOT/docker-compose.yml" up -d --wait
  log "iknow-memory infra (Postgres :5432, Neo4j, Elasticsearch) — first boot can take a few minutes"
  # compose pins pull_policy: build for neo4j/es. That re-hits docker.io /
  # docker.elastic.co on every up; behind Clash fake-ip (198.18.0.0/15) the
  # metadata fetch often times out even when local images already exist.
  if iknow_images_present; then
    log "reusing local iknow neo4j/es images (--no-build)"
    if ! docker compose -f "$IKNOW_ROOT/docker-compose.yml" up -d --wait --no-build; then
      die "iknow-memory compose failed with --no-build. Retry by hand: docker compose -f $IKNOW_ROOT/docker-compose.yml up -d --wait --no-build"
    fi
  else
    log "building iknow neo4j/es images (needs registry access)"
    if ! docker compose -f "$IKNOW_ROOT/docker-compose.yml" up -d --wait; then
      die "iknow-memory compose failed. Registry timeout (docker-auth.elastic.co / 198.18.x) usually means Clash fake-ip blocking Docker — fix DNS/proxy or mirror, then retry. If images already exist: docker compose -f $IKNOW_ROOT/docker-compose.yml up -d --wait --no-build"
    fi
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
      printf 'dev-up:   the cause is in the output above (a bad .env or an\n' >&2
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
    printf 'dev-up:   a boot failure (bad .env, missing FIN_CHECKPOINT_URL) stops\n' >&2
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
    warn "FIN_API_KEY is empty — UI will load; model calls will 401 until .env has a key"
  fi
  if [[ -z "${GRAPHRAG_MEMORY_EMBED_API_KEY:-}" && -z "${GRAPHRAG_MEMORY_VECTOR_API_KEY:-}" ]]; then
    warn "no iknow embed key — 工作台+MCP 可达; ingest/retrieve 向量臂需要 GRAPHRAG_MEMORY_EMBED_API_KEY in $IKNOW_ROOT/.env.local"
  fi
}

if [[ "$CHECK_ONLY" -eq 1 ]]; then
  require_docker_daemon dev-up
  check_serve_port_override
  is_iknow_checkout "$IKNOW_ROOT" || die "iknow-memory checkout not found at $IKNOW_ROOT (set FIN_IKNOW_ROOT)"
  install_iknow_shim
  command -v iknow-memory >/dev/null || die "iknow-memory shim not on PATH"
  log "check ok"
  log "  fin:    $REPO_ROOT"
  log "  iknow:  $IKNOW_ROOT"
  log "  shim:   $(command -v iknow-memory)"
  log "  next:   fin up"
  exit 0
fi

is_iknow_checkout "$IKNOW_ROOT" || die "iknow-memory checkout not found at $IKNOW_ROOT (set FIN_IKNOW_ROOT)"

# Preflight BEFORE any build. Both port checks are cheap, and a conflict
# is unrecoverable: making the operator sit through a multi-minute SPA /
# image build only to be refused is the failure this ordering avoids.
require_docker_daemon dev-up
check_serve_port_override
check_serve_port_free
check_checkpoint_port_owner

install_iknow_shim
ensure_iknow_build
ensure_spa
start_compose
mkdir -p "$STATE_DIR"
print_warnings

# Publish this shell's pid (preserved across the `exec` below) so
# `fin down` and the port check can name this workspace.
printf '%s\n' "$$" >"$SERVE_PID_FILE"

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
