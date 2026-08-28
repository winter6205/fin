#!/usr/bin/env python3
"""Print done / refuse counts for the nine gold eval cases (SC10).

Usage::

    uv run python scripts/eval_counts.py

The script shells out to the documented eval lane
(``uv run pytest -q -m eval``) and prints two integers:

    done=6
    refuse=3

Where the numbers come from: ``tests/eval/conftest.py`` wraps the
graph every gold case invokes and records that run's terminal status
(``done`` / ``refuse`` / ``step_cap``). At the end of the session it
writes one machine-readable line — the ``COUNT_LINE_PREFIX`` line
below — which this script parses. The counts are therefore OBSERVED
run outcomes, not a tally of expectations written next to the tests,
and no LLM judges anything: the lane runs ``ScriptedChatModel``
against the frozen corpus.

Exit code: pytest's, so a red eval suite still fails the command.
A missing count line exits ``2`` rather than printing zeros — the
lane not reporting is a different fact from the lane observing
nothing.
"""

from __future__ import annotations

import subprocess
import sys
from collections import Counter
from typing import Final, Iterable, Mapping, Sequence


COUNT_LINE_PREFIX: Final[str] = "eval_terminal_counts:"

# The closed terminal-status enum of one host-graph run (``fin.host
# .state.TERMINAL_STATUSES``). Anything else a run reports lands in
# ``unknown``.
TERMINAL_STATUSES: Final[tuple[str, ...]] = ("done", "refuse", "step_cap")

# The eval selection SC10 pins. Kept here as data so the counter and
# the documented command cannot drift.
DEFAULT_PYTEST_COMMAND: Final[tuple[str, ...]] = (
    "uv",
    "run",
    "pytest",
    "-q",
    "-m",
    "eval",
)

_MISSING_LINE_EXIT: Final[int] = 2


class EvalCountsError(RuntimeError):
    """The eval lane did not report a parseable count line."""


def count_terminal_statuses(statuses: Iterable[str]) -> dict[str, int]:
    """Tally observed run statuses into the printed shape.

    Always returns every key (zeros included) so the printed line has
    a fixed shape. ``unknown`` catches anything outside the terminal
    enum — a run that ended ``running`` is a real defect, and folding
    it into ``refuse`` would hide it.
    """
    counter = Counter(str(status) for status in statuses)
    counts = {name: int(counter.get(name, 0)) for name in TERMINAL_STATUSES}
    counts["unknown"] = sum(
        count for name, count in counter.items() if name not in TERMINAL_STATUSES
    )
    counts["total"] = sum(counter.values())
    return counts


def format_count_line(counts: Mapping[str, int]) -> str:
    """Render the one line the eval lane prints for this script."""
    body = " ".join(f"{key}={int(value)}" for key, value in counts.items())
    return f"{COUNT_LINE_PREFIX} {body}"


def parse_count_line(output: str) -> dict[str, int]:
    """Pull the counts out of a full pytest report.

    The last matching line wins so a nested / repeated report cannot
    make the command print a stale tally.
    """
    for line in reversed(output.splitlines()):
        stripped = line.strip()
        if not stripped.startswith(COUNT_LINE_PREFIX):
            continue
        counts: dict[str, int] = {}
        for token in stripped[len(COUNT_LINE_PREFIX) :].split():
            key, _, value = token.partition("=")
            try:
                counts[key] = int(value)
            except ValueError as exc:
                raise EvalCountsError(
                    f"malformed count token {token!r} in eval output"
                ) from exc
        if "done" not in counts or "refuse" not in counts:
            raise EvalCountsError(
                f"count line is missing done / refuse: {stripped!r}"
            )
        return counts
    raise EvalCountsError(
        f"no {COUNT_LINE_PREFIX!r} line in the eval output; the eval lane "
        "recorded no run statuses"
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the eval lane, echo its report, print the two integers."""
    extra = list(argv if argv is not None else sys.argv[1:])
    command = [*DEFAULT_PYTEST_COMMAND, *extra]
    completed = subprocess.run(command, capture_output=True, text=True)

    # The pytest report goes to stderr so stdout carries only the
    # integers a caller (or a CI step) wants to read.
    sys.stderr.write(completed.stdout)
    sys.stderr.write(completed.stderr)

    try:
        counts = parse_count_line(completed.stdout)
    except EvalCountsError as exc:
        sys.stderr.write(f"eval_counts: {exc}\n")
        return completed.returncode or _MISSING_LINE_EXIT

    print(f"done={counts['done']}")
    print(f"refuse={counts['refuse']}")

    if completed.returncode != 0:
        return completed.returncode
    # An unknown terminal status means a run ended outside the closed
    # enum; the suite may still be green, but the counts are not
    # trustworthy, so the command must not report success.
    return 1 if counts.get("unknown") else 0


if __name__ == "__main__":
    raise SystemExit(main())
