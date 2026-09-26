#!/usr/bin/env bash
# The four `FIN_ATTACHMENT_*` bounds, resolved from ONE source: the keys as
# `.env.example` ships them.
#
# `serve()` reads all four before it binds a port and refuses to boot without
# them, so every launcher has to supply them. A literal copy per launcher was
# how they drifted: the cap in `dev-up.sh`, in `cursor-start.sh`, in
# `run-scripted-e2e.mjs` and in `.env.example` had to agree by hand, and the
# host only checks that a value is a positive integer, never that it is the
# measured one. Reading the template instead keeps `.env.example` authoritative.
#
# Sourced, not executed. Callers set `REPO_ROOT` to the checkout they act for,
# then call `export_attachment_bounds` with a message prefix.

ATTACHMENT_BOUND_KEYS=(
  FIN_ATTACHMENT_MAX_BYTES
  FIN_ATTACHMENT_MAX_PDF_PAGES
  FIN_ATTACHMENT_VISION_CALLS_PER_RUN
  FIN_ATTACHMENT_VISION_CROP_CALLS_PER_RUN
)

# Value of $1 in `$REPO_ROOT/.env.example`, or empty. Only bare or
# `export`-prefixed `KEY=value` lines count; a value's surrounding quotes are
# stripped. No shell expansion of the template: it is data, not code.
attachment_bound_from_example() {
  local key="$1"
  [[ -f "$REPO_ROOT/.env.example" ]] || return 0
  sed -nE "s/^[[:space:]]*(export[[:space:]]+)?${key}[[:space:]]*=[[:space:]]*(.*)[[:space:]]*\$/\2/p" \
    "$REPO_ROOT/.env.example" | tail -n 1 | sed -E "s/^([\"'])(.*)\1\$/\2/"
}

# Fill unset bounds from the template. An already-exported value wins, so an
# operator's `.env` or an injected process variable is never overridden. A key
# neither set nor in the template is a hard failure: booting anyway would
# trade a clear message for `serve()`'s later config refusal.
export_attachment_bounds() {
  local prefix="${1:-launcher}" key value missing=0
  for key in "${ATTACHMENT_BOUND_KEYS[@]}"; do
    [[ -n "${!key:-}" ]] && continue
    value="$(attachment_bound_from_example "$key")"
    if [[ -z "$value" ]]; then
      printf '%s: %s is unset and .env.example does not define it.\n' \
        "$prefix" "$key" >&2
      printf '%s: set it, or restore the four bounds in .env.example.\n' \
        "$prefix" >&2
      missing=1
      continue
    fi
    export "$key=$value"
  done
  if [[ "$missing" -ne 0 ]]; then
    exit 1
  fi
}
