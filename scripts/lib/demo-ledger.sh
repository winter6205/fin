#!/usr/bin/env bash
# Disposable synthetic SQL ledger used only by the local ZEUS demo.

DEMO_LEDGER_DATABASE="fin_business_demo_test"
DEMO_LEDGER_IMAGE="postgres:16-alpine"
DEMO_LEDGER_LABEL_KEY="com.fin.demo.ledger"
DEMO_LEDGER_LABEL_VALUE="fin-demo-business-ledger-v1"

demo_ledger_container_name() {
  printf 'fin-demo-business-ledger-%s' "${UID:-$(id -u)}"
}

demo_ledger_owned_label() {
  local container="$1"
  docker inspect --format "{{ index .Config.Labels \"$DEMO_LEDGER_LABEL_KEY\" }}" \
    "$container" 2>/dev/null
}

demo_ledger_find_container() {
  local container="$1"
  docker ps -aq --filter "name=^/$container$"
}

demo_ledger_remove_owned() {
  local container="$1" matches="" label=""
  if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
    # EXIT: stop remains idempotent when Docker is unavailable; dev-down handles its own daemon diagnostic.
    return 0
  fi
  if ! matches="$(demo_ledger_find_container "$container")"; then
    printf 'fin demo: could not list containers while stopping the SQL ledger\n' >&2
    return 1
  fi
  if [[ -z "$matches" ]]; then
    # EXIT: an empty exact-name query proves there is no owned container to remove.
    return 0
  fi
  if ! label="$(demo_ledger_owned_label "$container")"; then
    printf 'fin demo: could not inspect the SQL ledger container\n' >&2
    return 1
  fi
  if [[ "$label" != "$DEMO_LEDGER_LABEL_VALUE" ]]; then
    printf 'fin demo: refusing to remove a container without the demo ledger ownership label\n' >&2
    return 1
  fi
  if ! docker rm --force "$container" >/dev/null 2>&1; then
    printf 'fin demo: could not remove the owned SQL ledger container\n' >&2
    return 1
  fi
}

demo_ledger_stop() {
  demo_ledger_remove_owned "$(demo_ledger_container_name)"
}

demo_ledger_exec_sql() {
  local container="$1"
  docker exec -i "$container" psql -X -A -t -v ON_ERROR_STOP=1 \
    -U postgres -d "$DEMO_LEDGER_DATABASE" -f -
}

demo_ledger_prepare_container() {
  local container="$1" matches="" label=""
  if ! matches="$(demo_ledger_find_container "$container")"; then
    printf 'fin demo: could not check for a previous SQL ledger container\n' >&2
    return 1
  fi
  if [[ -z "$matches" ]]; then
    # EXIT: no exact-name match means a new isolated container can be created.
    return 0
  fi
  if ! label="$(demo_ledger_owned_label "$container")"; then
    printf 'fin demo: could not inspect the previous SQL ledger container\n' >&2
    return 1
  fi
  if [[ "$label" != "$DEMO_LEDGER_LABEL_VALUE" ]]; then
    printf 'fin demo: a container already uses the SQL ledger name without the expected ownership label\n' >&2
    return 1
  fi
  if ! docker rm --force "$container" >/dev/null 2>&1; then
    printf 'fin demo: could not replace the previous SQL ledger container\n' >&2
    return 1
  fi
}

demo_ledger_run_container() {
  local container="$1"
  if ! docker run --rm --detach \
    --publish '127.0.0.1::5432' \
    --env "POSTGRES_DB=$DEMO_LEDGER_DATABASE" \
    --env POSTGRES_HOST_AUTH_METHOD=trust \
    --label "$DEMO_LEDGER_LABEL_KEY=$DEMO_LEDGER_LABEL_VALUE" \
    --name "$container" "$DEMO_LEDGER_IMAGE" >/dev/null 2>&1; then
    printf 'fin demo: could not start the disposable SQL ledger container\n' >&2
    return 1
  fi
}

demo_ledger_wait_ready() {
  local container="$1" waited=0
  while (( waited < 30 )); do
    if docker exec "$container" pg_isready -U postgres -d "$DEMO_LEDGER_DATABASE" \
      >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
    waited=$((waited + 1))
  done
  printf 'fin demo: the disposable SQL ledger did not become ready\n' >&2
  return 1
}

demo_ledger_verify_database() {
  local container="$1" database=""
  if ! database="$(docker exec "$container" psql -X -A -t -U postgres \
    -d "$DEMO_LEDGER_DATABASE" -c 'SELECT current_database()' 2>/dev/null)"; then
    printf 'fin demo: could not verify the SQL ledger database identity\n' >&2
    return 1
  fi
  if [[ "$database" != "$DEMO_LEDGER_DATABASE" ]]; then
    printf 'fin demo: the SQL ledger database identity check failed\n' >&2
    return 1
  fi
}

demo_ledger_load_fixtures() {
  local container="$1" fixture_dir="$2"
  if ! demo_ledger_exec_sql "$container" <"$fixture_dir/001_schema.sql" >/dev/null 2>&1; then
    printf 'fin demo: could not load the synthetic SQL ledger schema\n' >&2
    return 1
  fi
  if ! demo_ledger_exec_sql "$container" <"$fixture_dir/002_seed.sql" >/dev/null 2>&1; then
    printf 'fin demo: could not seed the synthetic SQL ledger\n' >&2
    return 1
  fi
}

demo_ledger_create_reader() {
  local container="$1"
  if ! demo_ledger_exec_sql "$container" >/dev/null 2>&1 <<SQL
CREATE ROLE fin_sql_reader LOGIN;
GRANT CONNECT ON DATABASE $DEMO_LEDGER_DATABASE TO fin_sql_reader;
GRANT USAGE ON SCHEMA ledger_ro TO fin_sql_reader;
GRANT SELECT ON ledger_ro.contract_expiry TO fin_sql_reader;
ALTER ROLE fin_sql_reader SET default_transaction_read_only = on;
ALTER ROLE fin_sql_reader SET statement_timeout = '3s';
ALTER ROLE fin_sql_reader SET lock_timeout = '1s';
ALTER ROLE fin_sql_reader SET idle_in_transaction_session_timeout = '10s';
SQL
  then
    printf 'fin demo: could not create the read-only SQL ledger role\n' >&2
    return 1
  fi
}

demo_ledger_verify_reader() {
  local container="$1" readonly=""
  if ! readonly="$(docker exec "$container" psql -X -A -t -U fin_sql_reader \
    -d "$DEMO_LEDGER_DATABASE" -c 'SHOW default_transaction_read_only' 2>/dev/null)"; then
    printf 'fin demo: could not verify the SQL ledger reader role\n' >&2
    return 1
  fi
  if [[ "$readonly" != "on" ]]; then
    printf 'fin demo: the SQL ledger reader is not read-only\n' >&2
    return 1
  fi
}

demo_ledger_start() {
  local repo_root="$1" container="$(demo_ledger_container_name)"
  local fixture_dir="$repo_root/tests/retrieve/fixtures/ledger"
  if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
    printf 'fin demo: Docker is required for the isolated SQL ledger\n' >&2
    return 1
  fi
  if ! demo_ledger_prepare_container "$container"; then return 1; fi
  if [[ ! -r "$fixture_dir/001_schema.sql" || ! -r "$fixture_dir/002_seed.sql" ]]; then
    printf 'fin demo: the synthetic SQL ledger fixtures are missing\n' >&2
    return 1
  fi
  if ! demo_ledger_run_container "$container"; then return 1; fi
  if ! demo_ledger_wait_ready "$container"; then return 1; fi
  if ! demo_ledger_verify_database "$container"; then return 1; fi
  if ! demo_ledger_load_fixtures "$container" "$fixture_dir"; then return 1; fi
  if ! demo_ledger_create_reader "$container"; then return 1; fi
  if ! demo_ledger_verify_reader "$container"; then return 1; fi
  printf 'fin demo: isolated synthetic SQL ledger is ready\n'
}

demo_ledger_install_uv_wrapper() {
  local wrapper_dir="$1"
  if ! mkdir -p "$wrapper_dir"; then
    printf 'fin demo: could not prepare the SQL ledger launcher directory\n' >&2
    return 1
  fi
  if ! cat >"$wrapper_dir/uv" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
set +x
if [[ "${1:-}" == "run" && "${2:-}" == "fin-serve" ]]; then
  binding="$(docker port "$FIN_DEMO_LEDGER_CONTAINER" 5432/tcp 2>/dev/null)" || {
    printf 'fin demo: the SQL ledger loopback port is unavailable\n' >&2
    exit 1
  }
  if [[ ! "$binding" =~ ^127\.0\.0\.1:([0-9]{1,5})$ ]]; then
    printf 'fin demo: the SQL ledger is not bound to a valid loopback port\n' >&2
    exit 1
  fi
  port="${BASH_REMATCH[1]}"
  if (( 10#$port < 1 || 10#$port > 65535 )); then
    printf 'fin demo: the SQL ledger is not bound to a valid loopback port\n' >&2
    exit 1
  fi
  ledger_url="postgresql://fin_sql_reader@127.0.0.1:${port}/fin_business_demo_test"
  export FIN_BUSINESS_LEDGER_URL="$ledger_url"
  unset ledger_url binding
fi
exec "$FIN_DEMO_REAL_UV" "$@"
EOF
  then
    printf 'fin demo: could not create the SQL ledger launcher\n' >&2
    return 1
  fi
  if ! chmod 700 "$wrapper_dir/uv"; then
    printf 'fin demo: could not set permissions on the SQL ledger launcher\n' >&2
    return 1
  fi
}
