#!/usr/bin/env bash
# Readiness verdict for the local 工作台 — ONE source of truth.
#
# Both the boot-time reporter in `scripts/dev-up.sh` and `fin status`
# answer the same question ("is serve up, and is the knowledge MCP
# reachable?") and both must return the same exit code for the same
# wire state. Duplicating the classification in two files is how those
# two answers drift apart, so the classification lives here.
#
# Sourced, not executed. Callers set nothing: the port and the exit codes
# are defined here.
#
# The three states come from the product's own boot-time probe snapshot
# (`fin.host.knowledge_mcp`), surfaced by `GET /api/status`. This file
# does not re-derive reachability — it reports what the process published.
#
# Gate semantics mirror `fin.host.knowledge_mcp.knowledge_mcp_gate_closed`:
# the gate is closed only when the bound retrieve port is the product `mcp`
# port AND the state is 缺 / 坏. A fake / scripted boot publishes 缺 with
# the gate OPEN, so 缺 there is ready, not gated. Classifying by state
# alone would report a false failure on the scripted e2e lane.
#
# That equivalence is why the classification reads `retrieve_adapter_kind`
# rather than `knowledge_mcp` alone, and why the route publishes the port
# the process actually BOUND (a scripted boot's fake port) rather than the
# configured `FIN_RETRIEVE_ADAPTER` string: the two disagree whenever
# `FIN_SCRIPTED_MODEL` is set, and classifying on the configured string
# reported GATED — "create / open / send will 409" — for a process whose
# gate is open. See `fin.host.workspace_http`'s `_workspace_status_adapter`.

# The port `fin-serve` binds. NOT a knob: the product hardcodes 8000 in
# `fin.host.http.serve()` (uvicorn.run(..., port=8000)). Overriding this
# only moves the PROBE — `dev-up.sh` refuses to start when it is
# overridden so a mismatch cannot be published as fact; the test suite
# sets it to point the classifier at a stub. See README.
WORKBENCH_SERVE_PORT="${WORKBENCH_SERVE_PORT:-8000}"

READINESS_EXIT_READY=0
READINESS_EXIT_NOT_READY=2
READINESS_EXIT_GATED=3

readiness_url() {
  printf 'http://127.0.0.1:%s/api/status\n' "$WORKBENCH_SERVE_PORT"
}

# Print the status body on success; non-zero when nothing answers.
readiness_fetch() {
  curl -fsS --max-time 3 "$(readiness_url)" 2>/dev/null
}

# readiness_classify <body> <prefix>
#
# Prints the verdict to stdout (ready / gate-not-applicable) or stderr
# (gated / not answering), and returns the matching exit code. <prefix>
# is the message prefix, e.g. `fin` or `dev-up`.
readiness_classify() {
  local body="$1"
  local prefix="${2:-fin}"
  local state adapter collection model

  if [[ -z "$body" ]]; then
    printf '%s: not ready — no fin-serve on %s\n' "$prefix" "$(readiness_url)" >&2
    printf '%s:   start it with:  fin up\n' "$prefix" >&2
    return "$READINESS_EXIT_NOT_READY"
  fi

  # A 200 that is not the pinned status payload must FAIL CLOSED. Without
  # the `jq -e .` probe a body like an HTML error page from whatever else
  # holds the port made every `jq` below fail; under `set -e` in a caller
  # that meant a bare `jq: parse error` and an exit code outside the
  # documented 0/1/2/3 table, and in the launcher's `|| body=""` shape it
  # was worse — the substitutions collapsed to empty and the verdict came
  # out `ready` for a server that is not fin-serve at all. Refusing to
  # classify is the only honest answer: we cannot know the gate state.
  if ! jq -e . >/dev/null 2>&1 <<<"$body"; then
    printf '%s: not ready — %s answered 200 with a body that is not the fin\n' \
      "$prefix" "$(readiness_url)" >&2
    printf '%s:   /api/status payload (expected JSON with %s). Something else\n' \
      "$prefix" "'knowledge_mcp'" >&2
    printf '%s:   may be holding port %s — check:  ss -ltnp | grep :%s\n' \
      "$prefix" "$WORKBENCH_SERVE_PORT" "$WORKBENCH_SERVE_PORT" >&2
    return "$READINESS_EXIT_NOT_READY"
  fi

  state="$(jq -r '.knowledge_mcp // "missing"' <<<"$body")"
  adapter="$(jq -r '.retrieve_adapter_kind // "mcp"' <<<"$body")"
  collection="$(jq -r '.bound_collection_name // "unbound"' <<<"$body")"
  model="$(jq -r '.model_identity // "-"' <<<"$body")"

  if [[ "$adapter" != "mcp" ]]; then
    printf '%s: ready — http://127.0.0.1:%s (retrieve_adapter=%s, knowledge_mcp=%s; gate n/a)\n' \
      "$prefix" "$WORKBENCH_SERVE_PORT" "$adapter" "$state"
    printf '%s:   collection=%s model=%s\n' "$prefix" "$collection" "$model"
    return "$READINESS_EXIT_READY"
  fi

  if [[ "$state" == "reachable" ]]; then
    printf '%s: ready — http://127.0.0.1:%s (knowledge_mcp=reachable)\n' \
      "$prefix" "$WORKBENCH_SERVE_PORT"
    printf '%s:   collection=%s model=%s\n' "$prefix" "$collection" "$model"
    return "$READINESS_EXIT_READY"
  fi

  printf '%s: serving but GATED — knowledge_mcp=%s\n' "$prefix" "$state" >&2
  printf '%s:   create / open / send will 409 (knowledge_mcp_fault) until the\n' "$prefix" >&2
  printf '%s:   iknow-memory server answers initialize + tools/list.\n' "$prefix" >&2
  if [[ "$state" == "missing" ]]; then
    printf '%s:   state=missing — the launch command is not on PATH. Check the\n' "$prefix" >&2
    printf '%s:   iknow checkout and that node runs.\n' "$prefix" >&2
  else
    printf '%s:   state=fault — the binary exists but the handshake failed.\n' "$prefix" >&2
    printf '%s:   The probe runs ONCE per process, so restart once infra is\n' "$prefix" >&2
    printf '%s:   warm:  fin down && fin up\n' "$prefix" >&2
  fi
  return "$READINESS_EXIT_GATED"
}
