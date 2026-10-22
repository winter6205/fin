#!/usr/bin/env bash
# Guards scripts/ci/fixture-dependent-tests.txt. Absence of the maintainer
# bundle must stay a loud failure, so the exclusion list is checked against
# the suite instead of being trusted as written.
#
# `set -e` is deliberately absent: a non-zero pytest status is the result
# directions 2 and 3 are reading, and -e would abort at the first one before
# any of them ran.
set -uo pipefail

source "$(dirname -- "$0")/lib.sh"
ci_read_denylist

lane="scripts/ci/pytest-public-lane.sh"

fail() {
  echo "::error::$1" >&2
  printf '%s\n' "$2" >&2
  exit 1
}

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# 1. The lane refuses a checkout that already has the bundle.
guard_output="$(FIN_PRIVATE_FIXTURES="$work/no-such-bundle" bash "$lane" 2>&1)"
guard_status=$?
if [ "$guard_status" -eq 0 ]; then
  fail "the lane exited 0 with FIN_PRIVATE_FIXTURES set; it must refuse" "$guard_output"
fi
if ! grep -q 'FIN_PRIVATE_FIXTURES is set' <<<"$guard_output"; then
  fail "the lane exited $guard_status for a reason other than the bundle guard" "$guard_output"
fi

# 2. Every excluded file fails on its own, and every one of those failures is
# a fixture-absence cause. Each file gets a fresh process: a sibling import
# that already read the vault makes a later file collect, so one combined run
# would report fewer failures than there are entries.
cause_classes='PrivateFixture(Missing|Invalid)Error|data/corpus|data/upstream|scripts/g4_pipeline/results|len\(\[\]\)|assert \[\]|assert 0 ==|assert 0 >='
checked=0
for file in "${CI_DENYLIST_ENTRIES[@]}"; do
  [ -f "$file" ] || fail "$file is listed in $CI_DENYLIST but does not exist; a deleted path is not proof of exclusion" ""
  uv run --no-sync pytest -q --tb=line -rfE "$file" > "$work/single.log" 2>&1
  single_status=$?
  if [ "$single_status" -eq 0 ]; then
    fail "$file is excluded but passes on its own without the bundle; drop it from $CI_DENYLIST or fix its isolation" \
      "$(tail -5 "$work/single.log")"
  fi
  # pytest exits 4 for a path it cannot find and 5 when it collects nothing,
  # so a non-zero status alone also covers a file that never ran.
  if ! grep -qE '^(FAILED|ERROR) ' "$work/single.log"; then
    fail "$file exited $single_status without reporting a FAILED or ERROR line; it did not fail, it never ran" \
      "$(tail -5 "$work/single.log")"
  fi
  if ! grep -qE "$cause_classes" "$work/single.log"; then
    fail "$file fails for a cause outside the classes $CI_DENYLIST declares; it belongs in the lane" \
      "$(tail -5 "$work/single.log")"
  fi
  checked=$((checked + 1))
done

# 3. Nothing outside the exclusion list is blocked at collection.
uv run --no-sync pytest -q --co --continue-on-collection-errors --tb=no -rfE \
  > "$work/collect.log" 2>&1
collect_status=$?
if [ "$collect_status" -eq 0 ]; then
  fail "the collection-only run exited 0; without the bundle something must fail to collect" \
    "$(tail -5 "$work/collect.log")"
fi
collected="$(sed -n 's/^\([0-9][0-9]*\(\/[0-9][0-9]*\)\?\) tests collected.*/\1/p' "$work/collect.log" | head -1)"
collected="${collected%%/*}"
if [ "${collected:-0}" -eq 0 ]; then
  fail "the collection-only run collected no tests; it never exercised the suite" \
    "$(tail -5 "$work/collect.log")"
fi
grep -E '^ERROR ' "$work/collect.log" \
  | awk '{print $2}' | sed 's/::.*//' | sort -u > "$work/blocked_set"
if [ ! -s "$work/blocked_set" ] \
  && grep -qE 'Interrupted|INTERNALERROR|usage:|no tests ran|no tests collected' "$work/collect.log"; then
  fail "the collection-only run aborted before reporting any collection error" \
    "$(tail -5 "$work/collect.log")"
fi
comm -23 "$work/blocked_set" <(printf '%s\n' "${CI_DENYLIST_ENTRIES[@]}" | sort -u) > "$work/unlisted"
if [ -s "$work/unlisted" ]; then
  fail "these files fail to collect without the bundle but are not in $CI_DENYLIST" \
    "$(cat "$work/unlisted")"
fi

echo "lane guard refuses a bundled checkout; $checked of ${#CI_DENYLIST_ENTRIES[@]} excluded files fail loudly on a fixture cause; no unlisted file is collection-blocked"
