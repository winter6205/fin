#!/usr/bin/env bash
# One-shot local 工作台: infra + iknow-memory on PATH + SPA + fin-serve.
# After this process is up, open http://127.0.0.1:8000 and send.
#
# Usage:
#   bash scripts/dev-up.sh
#   bash scripts/dev-up.sh --check    # preflight only
#   FIN_IKNOW_ROOT=/path/to/iknow-memory bash scripts/dev-up.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

CHECK_ONLY=0
if [[ "${1:-}" == "--check" ]]; then
  CHECK_ONLY=1
fi

die() {
  printf 'dev-up: %s\n' "$*" >&2
  exit 1
}

log() { printf 'dev-up: %s\n' "$*"; }

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

resolve_iknow_root() {
  if [[ -n "${FIN_IKNOW_ROOT:-}" ]]; then
    printf '%s\n' "$FIN_IKNOW_ROOT"
    return
  fi
  local sibling
  sibling="$(cd "$REPO_ROOT/.." && pwd)/iknow-memory"
  printf '%s\n' "$sibling"
}

IKNOW_ROOT="$(resolve_iknow_root)"
[[ -d "$IKNOW_ROOT" ]] || die "iknow-memory not found at $IKNOW_ROOT (set FIN_IKNOW_ROOT)"
[[ -f "$IKNOW_ROOT/package.json" ]] || die "$IKNOW_ROOT is not an iknow-memory checkout"

command -v docker >/dev/null || die "docker is required"
docker compose version >/dev/null 2>&1 || die "docker compose plugin is required"
command -v node >/dev/null || die "node is required"
command -v uv >/dev/null || die "uv is required"

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
  log "fin Postgres (:5433)"
  docker compose -f "$REPO_ROOT/docker-compose.yml" up -d --wait
  log "iknow-memory infra (Postgres :5432, Neo4j, Elasticsearch) — first boot can take a few minutes"
  # compose pins pull_policy: build for neo4j/es. That re-hits docker.io /
  # docker.elastic.co on every up; behind Clash fake-ip (198.18.0.0/15) the
  # metadata fetch often times out even when local images already exist.
  local compose_args=( -f "$IKNOW_ROOT/docker-compose.yml" up -d --wait )
  if iknow_images_present; then
    log "reusing local iknow neo4j/es images (--no-build)"
    compose_args+=( --no-build )
  else
    log "building iknow neo4j/es images (needs registry access)"
  fi
  if ! docker compose "${compose_args[@]}"; then
    die "iknow-memory compose failed. Registry timeout (docker-auth.elastic.co / 198.18.x) usually means Clash fake-ip blocking Docker — fix DNS/proxy or mirror, then retry. If images already exist: docker compose -f $IKNOW_ROOT/docker-compose.yml up -d --wait --no-build"
  fi
}

print_ready() {
  log "open http://127.0.0.1:8000"
  if [[ -z "${FIN_API_KEY:-}" ]]; then
    log "WARN: FIN_API_KEY is empty — UI will load; model calls will 401 until .env has a key"
  fi
  if [[ -z "${GRAPHRAG_MEMORY_EMBED_API_KEY:-}" && -z "${GRAPHRAG_MEMORY_VECTOR_API_KEY:-}" ]]; then
    log "WARN: no iknow embed key — 工作台+MCP 可达; ingest/retrieve 向量臂需要 GRAPHRAG_MEMORY_EMBED_API_KEY in $IKNOW_ROOT/.env.local"
  fi
}

if [[ "$CHECK_ONLY" -eq 1 ]]; then
  install_iknow_shim
  command -v iknow-memory >/dev/null || die "iknow-memory shim not on PATH"
  log "check ok"
  log "  fin:    $REPO_ROOT"
  log "  iknow:  $IKNOW_ROOT"
  log "  shim:   $(command -v iknow-memory)"
  log "  next:   bash scripts/dev-up.sh"
  exit 0
fi

install_iknow_shim
ensure_iknow_build
ensure_spa
start_compose
print_ready
exec uv run fin-serve
