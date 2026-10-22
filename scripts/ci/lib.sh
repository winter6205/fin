# Shared by scripts/ci/pytest-public-lane.sh and
# scripts/ci/assert-fixture-lane-fails.sh. Sourced, never executed: resolving
# the repository root and reading the denylist in two places meant a fix could
# land in one copy only.

ci_repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)" || {
  echo "error: cannot resolve the repository root from ${BASH_SOURCE[0]}" >&2
  exit 1
}
cd "$ci_repo_root" || {
  echo "error: cannot enter $ci_repo_root" >&2
  exit 1
}

CI_DENYLIST="scripts/ci/fixture-dependent-tests.txt"
CI_DENYLIST_ENTRIES=()

ci_read_denylist() {
  # mapfile over a process substitution never sees grep's status, so a missing
  # or truncated file would silently yield an empty list and widen the lane.
  [ -s "$CI_DENYLIST" ] || {
    echo "error: $CI_DENYLIST is missing or empty" >&2
    exit 1
  }
  mapfile -t CI_DENYLIST_ENTRIES < <(grep -v -e '^[[:space:]]*#' -e '^[[:space:]]*$' "$CI_DENYLIST")
  # A truncation sentinel, not a count pin: a restored bundle legitimately
  # shrinks the list, and scripts/ci/assert-fixture-lane-fails.sh is what
  # decides each entry.
  [ "${#CI_DENYLIST_ENTRIES[@]}" -ge 10 ] || {
    echo "error: $CI_DENYLIST yielded ${#CI_DENYLIST_ENTRIES[@]} entries; a truncated list would silently widen the lane" >&2
    exit 1
  }
}
