#!/usr/bin/env bash
# The four `FIN_ATTACHMENT_*` bounds, from ONE source: `settings.example.yaml`.
#
# `serve()` reads all four before it binds a port and refuses to boot without
# them, so every launcher has to supply them. A literal copy per launcher was
# how they drifted. This file asks `fin.host.operator_settings` for the four
# numbers and does not parse YAML. An already-exported process value wins.
#
# Sourced, not executed. Callers set `REPO_ROOT` to the checkout whose
# template they want, then call `export_attachment_bounds` with a prefix.

ATTACHMENT_BOUND_KEYS=(
  FIN_ATTACHMENT_MAX_BYTES
  FIN_ATTACHMENT_MAX_PDF_PAGES
  FIN_ATTACHMENT_VISION_CALLS_PER_RUN
  FIN_ATTACHMENT_VISION_CROP_CALLS_PER_RUN
)

_ATTACHMENT_BOUNDS_PKG="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

_attachment_bounds_python() {
  if [[ -x "$_ATTACHMENT_BOUNDS_PKG/.venv/bin/python" ]]; then
    "$_ATTACHMENT_BOUNDS_PKG/.venv/bin/python" "$@"
    return
  fi
  uv run python "$@"
}

# Print KEY=VALUE for the four bounds. Failure text goes to stderr of the
# caller via the command substitution in `export_attachment_bounds`.
_shipped_attachment_bound_lines() {
  local template_root="$1"
  (
    cd "$_ATTACHMENT_BOUNDS_PKG" || exit 1
    FIN_ATTACHMENT_EXAMPLE_ROOT="$template_root" _attachment_bounds_python -c '
import os
import sys
from pathlib import Path

from fin.host.operator_settings import (
    OperatorSettingsConfigError,
    shipped_attachment_bounds,
)

root = Path(os.environ["FIN_ATTACHMENT_EXAMPLE_ROOT"])
try:
    bounds = shipped_attachment_bounds(repo_root=root)
except OperatorSettingsConfigError as exc:
    print(exc, file=sys.stderr)
    sys.exit(1)
keys = (
    "FIN_ATTACHMENT_MAX_BYTES",
    "FIN_ATTACHMENT_MAX_PDF_PAGES",
    "FIN_ATTACHMENT_VISION_CALLS_PER_RUN",
    "FIN_ATTACHMENT_VISION_CROP_CALLS_PER_RUN",
)
for key in keys:
    print(f"{key}={bounds[key]}")
'
  )
}

export_attachment_bounds() {
  local prefix="${1:-launcher}" key value missing=0 output status=0
  declare -A template=()
  output="$(_shipped_attachment_bound_lines "$REPO_ROOT" 2>&1)" || status=$?
  if [[ "$status" -ne 0 ]]; then
    printf '%s: %s\n' "$prefix" "$output" >&2
    printf '%s: set it, or restore the four bounds in settings.example.yaml.\n' \
      "$prefix" >&2
    exit 1
  fi
  while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    key="${line%%=*}"
    value="${line#*=}"
    template["$key"]="$value"
  done <<<"$output"
  for key in "${ATTACHMENT_BOUND_KEYS[@]}"; do
    [[ -n "${!key:-}" ]] && continue
    value="${template[$key]:-}"
    if [[ -z "$value" ]]; then
      printf '%s: %s is unset and settings.example.yaml does not define it.\n' \
        "$prefix" "$key" >&2
      printf '%s: set it, or restore the four bounds in settings.example.yaml.\n' \
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
