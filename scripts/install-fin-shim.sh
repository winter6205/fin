#!/usr/bin/env bash
# Install the `fin` entrypoint onto PATH so a bare `fin` starts the
# local 工作台 from any directory.
#
# The repo already ships the dispatcher (scripts/fin) and the lifecycle
# scripts (dev-up.sh / dev-down.sh). What is missing is a stable name on
# PATH: without one the operator types `bash scripts/dev-up.sh` from the
# checkout root and nothing at all from anywhere else.
#
# The installed file is a tiny shim, not a copy, so it cannot drift from
# scripts/fin. It runs NO discovery of its own: it hands the launcher the
# checkout it was installed from as a fallback (--checkout-for-shim) and
# lets scripts/fin do the single walk-up from $PWD first, so `fin` in a
# worktree drives that worktree and `fin` from anywhere else drives the
# install-time checkout. When the walk-up finds nothing, scripts/fin
# prints `fin: not inside a fin checkout; using <path>` on stderr — the
# announcement that a bare `fin` is about to act on a tree the operator
# did not name.
#
# The shim used to carry its own `find_checkout` whose markers had
# already diverged from scripts/fin's; that copy is gone on purpose. A
# shim is a delegate, never a second source of truth.
#
# Idempotent: re-running rewrites the same file. Remove with
# `--uninstall`.
#
# Usage:
#   bash scripts/install-fin-shim.sh
#   bash scripts/install-fin-shim.sh --uninstall
#   BIN_DIR=/somewhere/on/PATH bash scripts/install-fin-shim.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN_DIR="${BIN_DIR:-$HOME/.local/bin}"
SHIM="$BIN_DIR/fin"
MARKER="# fin entrypoint (scripts/install-fin-shim.sh)"

log() { printf 'install-fin-shim: %s\n' "$*"; }
die() {
  printf 'install-fin-shim: %s\n' "$*" >&2
  exit 1
}

if [ "${1:-}" = "--uninstall" ]; then
  if [ -f "$SHIM" ] && grep -qF "$MARKER" "$SHIM"; then
    rm -f "$SHIM"
    log "removed $SHIM"
  else
    log "nothing to remove at $SHIM (not our shim)"
  fi
  exit 0
fi

# The SAME two markers scripts/fin's walk-up and its --checkout-for-shim
# fallback require, so a shim can never be installed pointing at a
# directory the launcher would refuse to treat as a checkout.
[ -f "$REPO_ROOT/scripts/dev-up.sh" ] || die "scripts/dev-up.sh not found under $REPO_ROOT"
[ -f "$REPO_ROOT/scripts/fin" ] || die "scripts/fin not found under $REPO_ROOT"
[ -f "$REPO_ROOT/src/fin/host/http.py" ] ||
  die "src/fin/host/http.py not found under $REPO_ROOT (both markers are required: scripts/dev-up.sh and src/fin/host/http.py)"
[ -d "$BIN_DIR" ] || mkdir -p "$BIN_DIR"

# Refuse to clobber a foreign `fin`: the operator may already have one.
if [ -e "$SHIM" ] && ! grep -qF "$MARKER" "$SHIM"; then
  die "$SHIM exists and is not this installer's shim; move it aside first"
fi

# The generated shim is a pure delegate: no walk-up, no cd. Both live in
# scripts/fin so there is exactly one implementation of each.
cat >"$SHIM" <<EOF
#!/usr/bin/env bash
$MARKER
set -euo pipefail

# The checkout this shim was installed from. Used only when \$PWD has no
# checkout at or above it; scripts/fin prints which one it took.
DEFAULT_CHECKOUT="$REPO_ROOT"

exec bash "\$DEFAULT_CHECKOUT/scripts/fin" --checkout-for-shim "\$DEFAULT_CHECKOUT" "\$@"
EOF
chmod +x "$SHIM"

log "installed $SHIM -> $REPO_ROOT/scripts/fin"

case ":$PATH:" in
*":$BIN_DIR:"*) log "$BIN_DIR is already on PATH" ;;
*)
  log "WARNING: $BIN_DIR is not on PATH in this shell."
  log "  add it, e.g. to ~/.bashrc:  export PATH=\"$BIN_DIR:\$PATH\""
  ;;
esac

log "try:  fin --help"
