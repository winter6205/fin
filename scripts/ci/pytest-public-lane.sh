#!/usr/bin/env bash
# Runs every test file the denylist does not exclude. The README documents
# `uv run --frozen` for a checkout with its own venv; CI uses the venv that
# `uv sync --locked` just created, so it syncs no further.
set -euo pipefail

source "$(dirname -- "$0")/lib.sh"

if [ -n "${FIN_PRIVATE_FIXTURES:-}" ]; then
  echo "error: FIN_PRIVATE_FIXTURES is set; the maintainer bundle does not belong in CI" >&2
  exit 1
fi

ci_read_denylist

ignore_args=()
for file in "${CI_DENYLIST_ENTRIES[@]}"; do
  ignore_args+=("--ignore=$file")
done

# No --continue-on-collection-errors: a file blocked at collection and missing
# from the exclusion list must stop the lane, not be counted alongside passes.
uv run --no-sync pytest -q "${ignore_args[@]}"
