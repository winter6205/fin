#!/usr/bin/env bash
# Checkout + iknow-root discovery for every `fin` entrypoint — ONE source
# of truth.
#
# `scripts/fin`, the PATH shim, `dev-up.sh` and `dev-down.sh` all have to
# answer "which fin checkout is this?" and "where is the iknow-memory
# checkout beside it?". Each answer had its own copy; the copies drifted,
# and the drift was not theoretical: `dev-down.sh` carried the pre-worktree
# version of `resolve_iknow_root`, so from a worktree it computed
# `.iknow/worktrees/iknow-memory` (does not exist), printed "not found —
# skipping", and exited 0 — reporting success while leaving the infra
# `dev-up.sh` had started still running. The README's own remedy for a cold
# boot (`fin down && fin up`) was inoperative from a worktree.
#
# Sourced, not executed. Callers set `REPO_ROOT` to the checkout they are
# acting for (they each derive it the same way: parent of their own dir)
# and then call these.

# Is $1 an iknow-memory checkout? Two markers so a directory that merely
# shares the name cannot match.
is_iknow_checkout() {
  [[ -n "${1:-}" && -f "$1/package.json" && -f "$1/bin/iknow-memory.js" ]]
}

# The MAIN checkout's root, even when REPO_ROOT is a linked worktree.
# `--git-common-dir` points at the main `.git`, whose parent is the main
# working tree; for the main checkout itself it is just `.git`.
main_checkout_root() {
  local common
  if ! common="$(git -C "$REPO_ROOT" rev-parse --git-common-dir 2>/dev/null)"; then
    return 1
  fi
  if [[ "$common" != /* ]]; then
    common="$REPO_ROOT/$common"
  fi
  (cd "$(dirname "$common")" 2>/dev/null && pwd)
}

# The iknow-memory checkout this tree should drive. NEVER fails: when no
# candidate matches it prints the first candidate so the error the caller
# prints names a path the operator can act on.
resolve_iknow_root() {
  if [[ -n "${FIN_IKNOW_ROOT:-}" ]]; then
    printf '%s\n' "$FIN_IKNOW_ROOT"
    return 0
  fi

  # Candidate order matters. The plain sibling works for a normal
  # checkout. Inside a worktree (`.iknow/worktrees/<leaf>`) the sibling of
  # REPO_ROOT is the worktrees directory, so fall back to the sibling of
  # the MAIN checkout, which is where the iknow checkout usually lives.
  local sibling main_root
  sibling="$(cd "$REPO_ROOT/.." && pwd)/iknow-memory"
  if is_iknow_checkout "$sibling"; then
    printf '%s\n' "$sibling"
    return 0
  fi

  if main_root="$(main_checkout_root)"; then
    if [[ "$main_root" != "$REPO_ROOT" ]]; then
      sibling="$(cd "$main_root/.." && pwd)/iknow-memory"
      if is_iknow_checkout "$sibling"; then
        printf '%s\n' "$sibling"
        return 0
      fi
    fi
  fi

  printf '%s\n' "$(cd "$REPO_ROOT/.." && pwd)/iknow-memory"
}

# NOTE: the checkout walk-up (`find_checkout`) deliberately does NOT live
# here. Locating a checkout has to happen BEFORE any file inside it can be
# sourced, so it belongs to the bootstrap in `scripts/fin`; the PATH shim
# no longer walks at all (it hands its install-time checkout over as a
# fallback and lets `scripts/fin` do the single walk-up). That is what
# removed the drift this module was created to fix.

# `command -v docker` proves the CLIENT is installed, not that the daemon
# answers. Without this check the first `docker compose` dies with a long
# transport error that reads like a compose bug rather than a stopped
# daemon. Shared by `up` and `down`: the stop path needs the daemon just
# as much, and swallowing its absence made `fin down` report success.
#
# $1 is the message prefix (e.g. `dev-up`). Exits 1 on a dead daemon.
require_docker_daemon() {
  local prefix="${1:-docker}"
  command -v docker >/dev/null || {
    printf '%s: docker is required\n' "$prefix" >&2
    exit 1
  }
  docker compose version >/dev/null 2>&1 || {
    printf '%s: docker compose plugin is required\n' "$prefix" >&2
    exit 1
  }
  if docker info >/dev/null 2>&1; then
    return 0
  fi
  printf '%s: docker daemon is not answering (%s failed).\n' "$prefix" 'docker info' >&2
  printf '%s:   WSL:      sudo service docker start\n' "$prefix" >&2
  printf '%s:   Desktop:  start Docker Desktop, wait for %s\n' "$prefix" "'Engine running'" >&2
  printf '%s:   then re-run this command.\n' "$prefix" >&2
  exit 1
}

# Is the docker daemon answering? The non-fatal form, for callers that
# must degrade with a named reason instead of exiting.
docker_daemon_answers() {
  command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1
}
