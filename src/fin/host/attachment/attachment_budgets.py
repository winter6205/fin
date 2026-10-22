"""The finite resource bounds a deployment configures for thread attachments.

No value here has a default: a guessed page cap is the unbounded shape this
module exists to reject, so a missing key fails validation instead of quietly
disabling a capability. A mis-deployment is not a product exit a client may
retry, so config failures have no outward wire code.

Budgets are per run across one turn's eligible attachments, not per file. Each
admitted page costs one full-page scan and every crop draws on the same
counter, so an over-budget run is refused whole rather than answering from a
partially skipped document.

Two doors read the same numbers. :func:`preflight_run` prices a plan before any
work starts and stays pure, while :class:`VisionCallLedger` is where a run
actually spends: every outbound attempt is charged when it goes out, so a scan
that failed at the transport still consumed its call, and the run keeps exactly
one reserved full-page retry for the whole run rather than one per page.

:func:`send_full_page` is the one charge → inspect → retry-only-if-reserve-free
ladder both full-page seams share, so the same spent event can never answer
with two different codes: a run whose reserve is gone exits the provider's own
failure, and :class:`AttachmentVisionBudgetExceeded` stays reserved for run
total or crop headroom overflow.
"""

from __future__ import annotations

import dataclasses
import sys
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from fin.host.attachment.attachment_vision import (
    VisionCallFailedError,
    VisionCallResult,
    VisionPort,
    VisionTarget,
)

__all__ = [
    "ATTACHMENT_PAGE_LIMIT_EXCEEDED",
    "ATTACHMENT_VISION_BUDGET_EXCEEDED",
    "BOUND_ENV_KEYS",
    "AttachmentBudgetConfigError",
    "AttachmentBudgetError",
    "AttachmentBudgets",
    "AttachmentPageLimitExceeded",
    "AttachmentVisionBudgetExceeded",
    "FullPageSend",
    "RETRY_RESERVE_CALLS",
    "VisionCallLedger",
    "from_environ",
    "preflight_run",
    "required_run_calls",
    "send_full_page",
    "validate",
]


ATTACHMENT_PAGE_LIMIT_EXCEEDED: Final[str] = "attachment_page_limit_exceeded"
ATTACHMENT_VISION_BUDGET_EXCEEDED: Final[str] = "attachment_vision_budget_exceeded"

# Full-page transport retries one run may make, priced inside its own budget.
# A total that only covers pages plus crops would boot fine and then have
# nothing left the first time a provider drops a request, so the reserve is
# part of what a deployment must configure rather than a runtime wish.
RETRY_RESERVE_CALLS: Final[int] = 1

# ``(env key, field name)``, in field order. No aliases, no defaults.
# Public because the composition root's lane gate reads these key names to
# decide whether the operator declared any bound; owning the spelling here
# keeps that gate from drifting from what :func:`from_environ` reads.
BOUND_ENV_KEYS: Final[tuple[tuple[str, str], ...]] = (
    ("FIN_ATTACHMENT_MAX_BYTES", "max_upload_bytes"),
    ("FIN_ATTACHMENT_MAX_PDF_PAGES", "max_pdf_pages"),
    ("FIN_ATTACHMENT_VISION_CALLS_PER_RUN", "max_vision_calls_per_run"),
    ("FIN_ATTACHMENT_VISION_CROP_CALLS_PER_RUN", "max_vision_crop_calls_per_run"),
)

# Private alias: existing internal readers keep their import name.
_ENV_BOUNDS: Final[tuple[tuple[str, str], ...]] = BOUND_ENV_KEYS


class AttachmentBudgetError(Exception):
    """Base for budget failures.

    Deliberately declares no ``code``: only the two limit violations below
    are outward, and the door reads ``exc.code`` from those alone.
    """


class AttachmentBudgetConfigError(AttachmentBudgetError):
    """A deployed bound, or a run request, is unusable.

    Internal by contract: no outward code covers "the operator misconfigured
    this", so it surfaces as an unhandled failure and never as a product exit.
    """


class AttachmentPageLimitExceeded(AttachmentBudgetError):
    """An attachment needs more pages than the deployment admits at all."""

    code = ATTACHMENT_PAGE_LIMIT_EXCEEDED


class AttachmentVisionBudgetExceeded(AttachmentBudgetError):
    """The run's page scans plus crop calls pass the per-run budget."""

    code = ATTACHMENT_VISION_BUDGET_EXCEEDED


@dataclass(frozen=True, slots=True)
class AttachmentBudgets:
    """The four finite bounds of one deployment.

    Frozen because a budget renegotiated mid-run would make the recorded
    page and call counts disagree with the bound that was deployed.
    """

    max_upload_bytes: int
    max_pdf_pages: int
    max_vision_calls_per_run: int
    max_vision_crop_calls_per_run: int


def _trimmed(raw: str | None) -> str:
    """An environ value as text; blank is absent, never a zero bound."""
    return "" if raw is None else raw.strip()


def _require_bound(name: str, value: object) -> int:
    """One bound under every door: a positive host-sized ``int``, never a bool."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise AttachmentBudgetConfigError(
            f"{name} must be an int, got {value!r}"
        )  # EXIT: a non-integer bound cannot be compared against work
    if not 1 <= value <= sys.maxsize:
        raise AttachmentBudgetConfigError(
            f"{name} must be between 1 and {sys.maxsize}, got {value!r}"
        )  # EXIT: zero admits nothing and past maxsize is unbounded
    return value


def _parse_bound(key: str, raw: str | None) -> int:
    """Read one configured bound as canonical text, then as an integer.

    ``int()`` would accept ``"+10"``, ``"1_000"``, and full-width ``"１０"``;
    each is a confusable spelling of a limit, so only unsigned ASCII decimal
    digits are accepted (``isdigit()`` alone also admits the full-width ones).
    """
    text = _trimmed(raw)
    if not text:
        raise AttachmentBudgetConfigError(
            f"{key} must be set to a positive integer, got {raw!r}"
        )  # EXIT: a missing limit is a startup failure, never a default
    if not text.isascii() or not text.isdigit():
        raise AttachmentBudgetConfigError(
            f"{key} must be an unsigned ASCII decimal integer, got {raw!r}"
        )  # EXIT: signs, separators and non-ASCII digits are not a bound
    return _require_bound(key, int(text))


def required_run_calls(budgets: AttachmentBudgets) -> int:
    """The least a run budget must cover: every page, every crop, one retry.

    Single source for the relationship, so the boot check and the spend check
    cannot drift apart on what a run is priced at.
    """
    return (
        budgets.max_pdf_pages
        + budgets.max_vision_crop_calls_per_run
        + RETRY_RESERVE_CALLS
    )


def validate(budgets: AttachmentBudgets) -> None:
    """Check a deployed config, however it was built.

    Also enforces the self-consistency a run cannot recover from: one scan per
    admitted page, the crop headroom, and the one reserved full-page retry
    must all fit inside the run budget. A total exactly equal to pages plus
    crops is not enough — it spends the last call on the run's own work and
    leaves a dropped request unretryable.
    """
    for field in dataclasses.fields(budgets):
        _require_bound(field.name, getattr(budgets, field.name))
    minimum = required_run_calls(budgets)
    if budgets.max_vision_calls_per_run < minimum:
        raise AttachmentBudgetConfigError(
            f"max_vision_calls_per_run must cover every admissible page, the "
            f"crop headroom and {RETRY_RESERVE_CALLS} reserved full-page "
            f"retry calls, needs {minimum}, got "
            f"{budgets.max_vision_calls_per_run}"
        )  # EXIT: such a config could only ever fail mid-run


def from_environ(*, environ: Mapping[str, str]) -> AttachmentBudgets:
    """Read and validate the four bounds; every one of them is required.

    Keyword-only so a composition root cannot pass the wrong mapping by
    position, matching ``load_jev_config``.
    """
    values: dict[str, int] = {}
    for key, field_name in _ENV_BOUNDS:
        values[field_name] = _parse_bound(key, environ.get(key))
    budgets = AttachmentBudgets(**values)
    validate(budgets)
    return budgets


def _page_counts(admitted_pages: Sequence[int]) -> tuple[int, ...]:
    """The run's per-attachment page counts as a tuple, or a typed failure.

    The list is built by orchestration code, so a ``None`` entry or a float is
    a caller bug named as one rather than a ``TypeError`` that looks like an
    unrelated crash.
    """
    if not isinstance(admitted_pages, Sequence):
        raise AttachmentBudgetConfigError(
            f"admitted_pages must be a sequence of ints, got {admitted_pages!r}"
        )  # EXIT: a non-sequence request describes no run at all
    counts: list[int] = []
    for pages in admitted_pages:
        if isinstance(pages, bool) or not isinstance(pages, int) or pages < 0:
            raise AttachmentBudgetConfigError(
                f"admitted_pages entries must be non-negative ints, got {pages!r}"
            )  # EXIT: an unusable entry cannot be priced, so nothing is admissible
        counts.append(pages)
    return tuple(counts)


def _crop_count(requested_crop_calls: int) -> int:
    """The run's crop request as an integer; zero crops is a valid ask."""
    if (
        isinstance(requested_crop_calls, bool)
        or not isinstance(requested_crop_calls, int)
        or requested_crop_calls < 0
    ):
        raise AttachmentBudgetConfigError(
            "requested_crop_calls must be a non-negative int, "
            f"got {requested_crop_calls!r}"
        )  # EXIT: an unusable count cannot be charged to the budget
    return requested_crop_calls


def preflight_run(
    budgets: AttachmentBudgets,
    admitted_pages: Sequence[int],
    requested_crop_calls: int,
) -> None:
    """Refuse a run that does not fit, before a single page is skipped.

    ``admitted_pages`` is one entry per eligible attachment, so the page cap
    is per attachment while the vision budget is the run's own total. Crops are
    charged to that total and are separately capped by their own headroom.
    Nothing is spent and nothing is mutated, so the same request asked twice
    gets the same answer; the spend belongs to :class:`VisionCallLedger`.
    """
    validate(budgets)
    counts = _page_counts(admitted_pages)
    crops = _crop_count(requested_crop_calls)
    for pages in counts:
        if pages > budgets.max_pdf_pages:
            raise AttachmentPageLimitExceeded(
                f"attachment needs {pages} pages, cap is "
                f"{budgets.max_pdf_pages}"
            )  # EXIT: page admission precedes any scan, so it is the honest code
    if crops > budgets.max_vision_crop_calls_per_run:
        raise AttachmentVisionBudgetExceeded(
            f"run asks {crops} crop calls, headroom is "
            f"{budgets.max_vision_crop_calls_per_run}"
        )  # EXIT: a bound of its own, so a run total with room left stays refused
    needed = sum(counts) + crops
    if needed > budgets.max_vision_calls_per_run:
        raise AttachmentVisionBudgetExceeded(
            f"run needs {needed} visual calls, budget is "
            f"{budgets.max_vision_calls_per_run}"
        )  # EXIT: the run is refused whole, never truncated to fit


class VisionCallLedger:
    """One run's spent visual calls, charged as the attempts go out.

    Preflight prices a plan; this is what makes the price real. A charge is
    taken before the provider request leaves, so an attempt that fails at the
    transport is still spent — a run of failures is bounded by the same
    deployed allowance as a run of successes, and there is deliberately no
    refund path. Full-page transport retries draw on
    :data:`RETRY_RESERVE_CALLS`, which belongs to the run: one page using the
    reserve leaves every later page in that run with none.

    Durability boundary: the counters live in this object, in this process,
    for the lifetime of one run. Nothing here is written to disk or to the
    attachment store, so a restart forgets them. The durable preparation run
    record owns persistence and must copy :attr:`calls_spent`,
    :attr:`full_page_calls_spent`, :attr:`crop_calls_spent` and
    :attr:`retries_spent` when it writes the run's page and call counts; until
    it does, the enforcement is per process.

    Page scans can be prepared concurrently, so the check and the spend are
    one atomic step under a lock: a check-then-add would let two attempts each
    see room for the last call. Per-attachment page admission is not this
    object's bound to hold — ``max_pdf_pages`` prices one attachment, and
    :func:`preflight_run` checks it before a run starts.
    """

    __slots__ = ("_budgets", "_calls", "_crops", "_full_pages", "_lock", "_retries")

    def __init__(self, budgets: AttachmentBudgets) -> None:
        validate(budgets)
        self._budgets = budgets
        self._calls = 0
        self._full_pages = 0
        self._crops = 0
        self._retries = 0
        self._lock = threading.Lock()

    @property
    def budgets(self) -> AttachmentBudgets:
        """The deployed bounds this ledger spends against, unchanged."""
        return self._budgets

    @property
    def calls_spent(self) -> int:
        """Every visual attempt charged so far, failures included."""
        with self._lock:
            return self._calls

    @property
    def full_page_calls_spent(self) -> int:
        """Full-page attempts, including the one made as a retry."""
        with self._lock:
            return self._full_pages

    @property
    def crop_calls_spent(self) -> int:
        """Crop attempts charged so far, failures included."""
        with self._lock:
            return self._crops

    @property
    def retries_spent(self) -> int:
        """How much of the run's retry reserve is used."""
        with self._lock:
            return self._retries

    @property
    def remaining_calls(self) -> int:
        """Run budget left for further attempts, regardless of kind."""
        with self._lock:
            return self._budgets.max_vision_calls_per_run - self._calls

    def _spend_one(self, kind: str) -> None:
        """Charge one call; the caller must hold the lock."""
        budget = self._budgets.max_vision_calls_per_run
        if self._calls + 1 > budget:
            raise AttachmentVisionBudgetExceeded(
                f"run has spent {self._calls} of {budget} visual calls and "
                f"cannot start another {kind}"
            )  # EXIT: the allowance is exhausted, so nothing further goes out
        self._calls += 1

    def charge_full_page_attempt(self, *, retry: bool = False) -> None:
        """Charge one outbound full-page scan, retried or not.

        Call it before the request goes out. ``retry`` asks for the run's one
        reserved transport retry, so a second retry is refused even while the
        run total still has room — the reserve is the bound that fails there.
        """
        if not isinstance(retry, bool):
            raise AttachmentBudgetConfigError(
                f"retry must be a bool, got {retry!r}"
            )  # EXIT: a truthy-looking flag would silently spend the reserve
        with self._lock:
            if retry and self._retries >= RETRY_RESERVE_CALLS:
                raise AttachmentVisionBudgetExceeded(
                    f"run already used its {RETRY_RESERVE_CALLS} reserved "
                    "full-page retry"
                )  # EXIT: a per-page reserve would multiply the allowance
            self._spend_one("full-page scan")
            self._full_pages += 1
            if retry:
                self._retries += 1

    def charge_crop_attempt(self) -> None:
        """Charge one outbound crop inspection against both of its bounds.

        The crop sub-cap is checked first: a run total with room left must not
        buy more crops than the deployment priced.
        """
        with self._lock:
            headroom = self._budgets.max_vision_crop_calls_per_run
            if self._crops + 1 > headroom:
                raise AttachmentVisionBudgetExceeded(
                    f"run has spent {self._crops} of {headroom} crop calls"
                )  # EXIT: a bound of its own, independent of the run total
            self._spend_one("crop scan")
            self._crops += 1


@dataclass(frozen=True, slots=True)
class FullPageSend:
    """One finished full-page ladder: the answer, and whether the reserve went.

    ``retry_used`` is the fact a caller needs to record the retried-away
    failure attempt (no provider numbers) in its own observation envelope;
    the attempt that answered arrives in ``result``.
    """

    result: VisionCallResult
    retry_used: bool


def send_full_page(
    port: VisionPort,
    ledger: VisionCallLedger,
    *,
    image: bytes,
    target: VisionTarget,
    media_type: str,
    question: str,
) -> FullPageSend:
    """The one charge → inspect → retry-only-if-reserve-free ladder.

    Every outbound attempt is charged before it leaves, so a failed scan
    keeps its spend, and only a failure the port adapter classified as
    retryable transport may buy the run's single reserved retry — never a
    second one. The port's typed errors raise unchanged: each seam owns
    turning them into its own codes.

    A spent reserve is a provider failure exit, not a budget exit: when the
    reserve is gone the first failure re-raises as-is, because the request
    the run failed to make is the provider's dropped call while the operator
    cap refused nothing. ``AttachmentVisionBudgetExceeded`` still surfaces
    when the retry's own charge finds the run total spent — that is a real
    overflow, and nothing further is sent.
    """
    ledger.charge_full_page_attempt()
    try:
        result = port.inspect(
            image=bytes(image),
            target=target,
            media_type=media_type,
            question=question,
        )
    except VisionCallFailedError as failure:
        if not failure.retryable:
            # EXIT: a failure the adapter did not classify as transport
            # would answer the same way twice, paid for twice.
            raise
        if ledger.retries_spent >= RETRY_RESERVE_CALLS:
            # EXIT: the run's one reserved retry is spent and the run total
            # still has calls; the honest exit is the provider's dropped
            # request, never a budget code.
            raise
        ledger.charge_full_page_attempt(retry=True)
        # EXIT: a second failure, of any shape, propagates — the reserve is
        # now gone for the whole run and this page stays unanswered.
        result = port.inspect(
            image=bytes(image),
            target=target,
            media_type=media_type,
            question=question,
        )
        return FullPageSend(result=result, retry_used=True)
    return FullPageSend(result=result, retry_used=False)
