#!/usr/bin/env bash
# Copy the production compose template into ~/.fin/compose/.
# Does not run docker compose.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
src="$REPO_ROOT/compose/docker-compose.production.yml"
dest="${HOME}/.fin/compose/docker-compose.production.yml"

if [[ ! -f "$src" ]]; then
  printf 'install-production-compose: missing %s\n' "$src" >&2
  exit 1
fi

mkdir -p "${HOME}/.fin/compose"
cp "$src" "$dest"
