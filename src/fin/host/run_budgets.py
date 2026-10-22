"""The per-stage budgets one run is bounded by.

Every value here is a row of the product's execution budget table, not a
measured runtime result. The table's own rule is that a deployment may
TIGHTEN a budget and never raise one, so the published maximum is the
ceiling these types refuse to cross.

Two executor-owned stages are budgeted, and only two:

``execute``
    One streamed graph execution. While the run has produced no event it is
    inside some call — model, tool, or blocking I/O — so the bound held
    here is the model-invocation ceiling read as a no-progress ceiling: a
    run that goes longer than the longest single permitted call without
    producing anything is not making progress. That reading is also why
    this is a per-stretch bound rather than a total: a long healthy run is
    not over budget merely for being long.
``cleanup``
    Releasing the graph iterator, which is what returns the per-thread
    ownership. The grace bounds how long that release is waited for;
    work that survives it is fenced and stays supervised, so its stage is
    reported incomplete rather than quietly reported finished.

What this module does NOT do is stop a stage that makes no progress and
never yields: the executor observes its stages between the events the
graph emits, and closing a generator that is executing is not an
operation Python offers. A caller that needs such a stage fenced belongs
to the run-recovery work, not to a clock.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Final


# Model invocation: 120 seconds per call — the largest per-call budget in
# the table, and therefore the longest a run may make no observable
# progress.
SPEC_EXECUTE_STAGE_MAX_S: Final[float] = 120.0
# Cancellation cleanup: 1 second grace, during which surviving work is
# fenced rather than released.
SPEC_CLEANUP_GRACE_MAX_S: Final[float] = 1.0

# The two stage names. Deliberately not the receipt's twelve keys: this
# module measures what the executor can see, and projecting those two into
# the receipt's fixed shape belongs to the receipt owner.
STAGE_EXECUTE: Final[str] = "execute"
STAGE_CLEANUP: Final[str] = "cleanup"


class RunBudgetConfigError(RuntimeError):
    """A budget that cannot bound, or that raises a published maximum.

    Raised at construction so an unusable bound is a startup failure,
    before any run can be admitted without one.
    """


def _require_seconds(name: str, value: object, ceiling: float) -> float:
    """One budget as a positive, finite number no larger than its ceiling."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RunBudgetConfigError(
            f"{name} must be a number of seconds, got {value!r}"
        )  # EXIT: a non-number cannot be compared against elapsed time
    seconds = float(value)
    if not math.isfinite(seconds):
        raise RunBudgetConfigError(
            f"{name} must be finite, got {value!r}"
        )  # EXIT: infinity is not a bound, and NaN compares false to
        # everything and would pass every comparison below
    if seconds <= 0:
        raise RunBudgetConfigError(
            f"{name} must be positive, got {value!r}"
        )  # EXIT: zero or negative leaves the run unbounded while looking
        # configured
    if seconds > ceiling:
        raise RunBudgetConfigError(
            f"{name} must not exceed the {ceiling}s published maximum, got {seconds}"
        )  # EXIT: a budget may be tightened by a deployment, never raised
    return seconds


@dataclass(frozen=True, slots=True)
class RunBudgets:
    """The two executor-owned ceilings of one deployment.

    Frozen because a budget renegotiated mid-run would make the recorded
    stage outcomes describe a bound nobody is held to.
    """

    execute_stage_s: float = SPEC_EXECUTE_STAGE_MAX_S
    cleanup_grace_s: float = SPEC_CLEANUP_GRACE_MAX_S

    def __post_init__(self) -> None:
        _require_seconds(
            "execute_stage_s", self.execute_stage_s, SPEC_EXECUTE_STAGE_MAX_S
        )
        _require_seconds(
            "cleanup_grace_s", self.cleanup_grace_s, SPEC_CLEANUP_GRACE_MAX_S
        )

    @property
    def execute_stage_ms(self) -> int:
        return int(self.execute_stage_s * 1000)

    @property
    def cleanup_grace_ms(self) -> int:
        return int(self.cleanup_grace_s * 1000)


@dataclass(frozen=True, slots=True)
class StageOutcome:
    """What one stage was observed to cost, against the bound it had.

    ``elapsed_ms`` is observed work time and ``complete`` says whether the
    stage's end was observed at all: an interrupted stage reports the
    lower bound that was seen, never a total. ``exceeded`` reports the
    BOUND being passed, which for ``execute`` is the no-progress ceiling
    rather than a ceiling on total run time.
    """

    stage: str
    elapsed_ms: int
    budget_ms: int
    exceeded: bool
    complete: bool

    def to_dict(self) -> dict[str, object]:
        """The same five facts as JSON-ready scalars, for an observer."""
        return {
            "stage": self.stage,
            "elapsed_ms": self.elapsed_ms,
            "budget_ms": self.budget_ms,
            "exceeded": self.exceeded,
            "complete": self.complete,
        }


class StageLedger:
    """The executor's clock for the two stages it owns.

    One instance per run. ``begin_execute`` opens the streamed stage,
    ``observe_progress`` marks an event the graph produced, and
    ``record_cleanup`` closes the release stage with the time it actually
    took — the grace is a bound on how long that release is WAITED for,
    not a reason to abandon it, because ownership must not be released
    while work still holds it.
    """

    __slots__ = (
        "_budgets",
        "_execute_started",
        "_execute_silent_since",
        "_execute_elapsed_s",
        "_execute_exceeded",
        "_execute_interrupted",
        "_cleanup_elapsed_s",
    )

    def __init__(self, budgets: RunBudgets | None = None) -> None:
        self._budgets = budgets if budgets is not None else RunBudgets()
        self._execute_started: float | None = None
        self._execute_silent_since: float | None = None
        self._execute_elapsed_s = 0.0
        self._execute_exceeded = False
        self._execute_interrupted = False
        self._cleanup_elapsed_s: float | None = None

    @property
    def budgets(self) -> RunBudgets:
        return self._budgets

    def begin_execute(self) -> None:
        now = time.perf_counter()
        self._execute_started = now
        self._execute_silent_since = now

    def observe_progress(self) -> None:
        """The graph produced an event: the no-progress clock starts over.

        Time spent in the CALLER between events is deliberately not
        counted — a slow SSE reader is backpressure on the wire, not work
        the run is failing to do.
        """
        if self._execute_silent_since is None:
            return
        now = time.perf_counter()
        self._account_silence(now)
        self._execute_silent_since = now

    def _account_silence(self, now: float) -> None:
        since = self._execute_silent_since
        if since is None:
            return
        self._execute_elapsed_s += now - since
        if now - since > self._budgets.execute_stage_s:
            self._execute_exceeded = True

    def execute_without_progress_s(self) -> float:
        """How long the run has gone without producing an event."""
        since = self._execute_silent_since
        if since is None:
            return 0.0
        return time.perf_counter() - since

    def execute_exceeded(self) -> bool:
        """Whether the no-progress bound has been passed, INCLUDING right
        now.

        The executor calls this after each pull from the graph, and the
        silence it must judge is exactly the one the pull just spent — it
        has not been accounted by any other call yet.
        """
        if (
            self._execute_silent_since is not None
            and self.execute_without_progress_s() > self._budgets.execute_stage_s
        ):
            self._execute_exceeded = True
        return self._execute_exceeded

    def finish_execute(self, *, interrupted: bool = False) -> None:
        """Close the streamed stage; ``interrupted`` records that the stage
        was ended by the deadline rather than by the run finishing."""
        if self._execute_started is None:
            return
        now = time.perf_counter()
        self._account_silence(now)
        self._execute_interrupted = interrupted
        self._execute_started = None
        self._execute_silent_since = None

    def record_cleanup(self, elapsed_s: float) -> None:
        self._cleanup_elapsed_s = max(0.0, float(elapsed_s))

    def outcomes(self) -> tuple[StageOutcome, ...]:
        """Both stages as observed, whether or not they were reached."""
        budgets = self._budgets
        cleanup_ms = (
            0
            if self._cleanup_elapsed_s is None
            else int(self._cleanup_elapsed_s * 1000)
        )
        cleanup_exceeded = (
            self._cleanup_elapsed_s is not None
            and self._cleanup_elapsed_s > budgets.cleanup_grace_s
        )
        return (
            StageOutcome(
                stage=STAGE_EXECUTE,
                elapsed_ms=int(self._execute_elapsed_s * 1000),
                budget_ms=budgets.execute_stage_ms,
                exceeded=self._execute_exceeded,
                complete=self._execute_started is None
                and not self._execute_interrupted,
            ),
            StageOutcome(
                stage=STAGE_CLEANUP,
                elapsed_ms=cleanup_ms,
                budget_ms=budgets.cleanup_grace_ms,
                exceeded=cleanup_exceeded,
                complete=self._cleanup_elapsed_s is not None
                and not cleanup_exceeded,
            ),
        )
