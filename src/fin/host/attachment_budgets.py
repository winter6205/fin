"""The finite resource bounds a deployment configures for thread attachments.

No value here has a default: a guessed page cap is the unbounded shape this
module exists to reject, so a missing key fails validation instead of quietly
disabling a capability. A mis-deployment is not a product exit a client may
retry, so config failures have no outward wire code.

Budgets are per run across one turn's eligible attachments, not per file. Each
admitted page costs one full-page scan and every crop draws on the same
counter, so an over-budget run is refused whole rather than answering from a
partially skipped document.
"""

from __future__ import annotations

import dataclasses
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

__all__ = [
    "ATTACHMENT_PAGE_LIMIT_EXCEEDED",
    "ATTACHMENT_VISION_BUDGET_EXCEEDED",
    "AttachmentBudgetConfigError",
    "AttachmentBudgetError",
    "AttachmentBudgets",
    "AttachmentPageLimitExceeded",
    "AttachmentVisionBudgetExceeded",
    "from_environ",
    "preflight_run",
    "validate",
]


ATTACHMENT_PAGE_LIMIT_EXCEEDED: Final[str] = "attachment_page_limit_exceeded"
ATTACHMENT_VISION_BUDGET_EXCEEDED: Final[str] = "attachment_vision_budget_exceeded"

# ``(env key, field name)``, in field order. No aliases, no defaults.
_ENV_BOUNDS: Final[tuple[tuple[str, str], ...]] = (
    ("FIN_ATTACHMENT_MAX_BYTES", "max_upload_bytes"),
    ("FIN_ATTACHMENT_MAX_PDF_PAGES", "max_pdf_pages"),
    ("FIN_ATTACHMENT_VISION_CALLS_PER_RUN", "max_vision_calls_per_run"),
    ("FIN_ATTACHMENT_VISION_CROP_CALLS_PER_RUN", "max_vision_crop_calls_per_run"),
)


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


def validate(budgets: AttachmentBudgets) -> None:
    """Check a deployed config, however it was built.

    Also enforces the self-consistency a run cannot recover from: one scan per
    admitted page plus the crop headroom must fit inside the run budget,
    equality being the minimum.
    """
    for field in dataclasses.fields(budgets):
        _require_bound(field.name, getattr(budgets, field.name))
    minimum = budgets.max_pdf_pages + budgets.max_vision_crop_calls_per_run
    if budgets.max_vision_calls_per_run < minimum:
        raise AttachmentBudgetConfigError(
            "max_vision_calls_per_run must cover every admissible page plus "
            f"the crop headroom, needs {minimum}, got "
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
    gets the same answer.
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
