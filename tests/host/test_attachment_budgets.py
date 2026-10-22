"""``fin.host.attachment.attachment_budgets`` — the finite attachment budget contract.

Basis: ``specs/thread-attachment-source-read.md`` — every deployment
configures a finite per-attachment page cap and a finite per-run visual-call
budget, a missing/non-positive/unbounded limit fails config validation, and an
over-budget run fails typed before any page or region is silently truncated.
The closed outward code list has no entry for a config failure.

What this file pins:

* a missing limit is a failure, never a default. Blank or whitespace-only
  counts as missing; a configured crop headroom of zero is a mis-deployment
  because question-specific crops are part of the product path, while a run
  request may legitimately ask for zero crops.
* a configured value is canonical unsigned ASCII decimal only. ``int()``
  silently accepts ``"+10"``, ``"1_000"`` and full-width ``"１０"``, so a
  confusable or locale-shifted digit must never widen a bound.
* the page cap is admitted before the vision budget, and both are checked
  before any page is dropped: no case may observe a partial plan, a truncated
  page list, or a success return for an over-budget run.
* the budget is per RUN across eligible attachments, not per file, and crops
  share that one budget.
* the run budget must price one scan per admissible page, the crop headroom
  and exactly one reserved full-page transport retry, so a total equal to
  pages plus crops is a mis-deployment that aborts the boot.
* a spent-call ledger charges every outbound attempt, failed ones included,
  allows that one retry per run rather than per page, and refuses the call
  past the allowance with the budget code.
* ``send_full_page`` is the one charge→inspect→retry-only-if-reserve-free
  ladder both seams share: it raises the port's typed errors, and a spent
  reserve with run-total headroom left exits the provider failure — the
  budget code stays reserved for run-total/crop overflow.
* the module holds integers only — no provider or model identifier, since
  hardcoding one is out of contract.
* the composition root reads the four bounds before anything is published or
  bound: ``serve()`` aborts on a missing, non-positive, blank, non-numeric or
  unbounded key, and a boot that cannot state a bound never enters uvicorn.

The values below are test fixtures, not the deployment defaults; the measured
defaults are recorded with the gold-set evidence.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Mapping

import pytest

from fin.host.application.lifetime import RuntimeHandle
from fin.host.attachment.attachment_budgets import (
    ATTACHMENT_PAGE_LIMIT_EXCEEDED,
    ATTACHMENT_VISION_BUDGET_EXCEEDED,
    AttachmentBudgetConfigError,
    AttachmentBudgetError,
    AttachmentBudgets,
    AttachmentPageLimitExceeded,
    AttachmentVisionBudgetExceeded,
    FullPageSend,
    VisionCallLedger,
    from_environ,
    preflight_run,
    send_full_page,
    validate,
)
from fin.host.attachment.attachment_vision import (
    VisionCallFailedError,
    VisionCallResult,
    VisionEmptyTextError,
    VisionErrorClass,
    VisionTarget,
    VisionUsage,
)

# ----- the four configured keys -----

BYTES_KEY = "FIN_ATTACHMENT_MAX_BYTES"
PAGES_KEY = "FIN_ATTACHMENT_MAX_PDF_PAGES"
VISION_KEY = "FIN_ATTACHMENT_VISION_CALLS_PER_RUN"
CROP_KEY = "FIN_ATTACHMENT_VISION_CROP_CALLS_PER_RUN"
ALL_KEYS = (BYTES_KEY, PAGES_KEY, VISION_KEY, CROP_KEY)
FIELD_NAMES = (
    "max_upload_bytes",
    "max_pdf_pages",
    "max_vision_calls_per_run",
    "max_vision_crop_calls_per_run",
)

FIELD_FOR_KEY = {
    BYTES_KEY: "max_upload_bytes",
    PAGES_KEY: "max_pdf_pages",
    VISION_KEY: "max_vision_calls_per_run",
    CROP_KEY: "max_vision_crop_calls_per_run",
}

# One valid, finite, deliberately small configuration.
# ``max_vision_calls_per_run`` sits exactly at ``pages + crop + 1``, so the
# accepted side of the relationship (the total must also reserve one
# full-page retry for the run) is what every non-relationship case runs
# against.
MAX_BYTES = 1_048_576
MAX_PAGES = 8
MAX_VISION_CALLS = 17
MAX_CROP_CALLS = 8
VALID_ENVIRON: dict[str, str] = {
    BYTES_KEY: str(MAX_BYTES),
    PAGES_KEY: str(MAX_PAGES),
    VISION_KEY: str(MAX_VISION_CALLS),
    CROP_KEY: str(MAX_CROP_CALLS),
}


def _environ(replacements: Mapping[str, str | None] | None = None) -> dict[str, str]:
    """The valid environ with ``replacements`` applied; ``None`` drops a key."""
    environ = dict(VALID_ENVIRON)
    for key, value in (replacements or {}).items():
        assert key in ALL_KEYS, f"not a budget key: {key}"
        if value is None:
            del environ[key]
        else:
            environ[key] = value
    return environ


def _load(replacements: Mapping[str, str | None] | None = None) -> AttachmentBudgets:
    """``from_environ`` for one environ built from the valid baseline."""
    return from_environ(environ=_environ(replacements))


def _budgets(
    max_upload_bytes: Any = MAX_BYTES,
    max_pdf_pages: Any = MAX_PAGES,
    max_vision_calls_per_run: Any = MAX_VISION_CALLS,
    max_vision_crop_calls_per_run: Any = MAX_CROP_CALLS,
) -> AttachmentBudgets:
    """A programmatic config, for classes ``from_environ`` cannot express."""
    return AttachmentBudgets(
        max_upload_bytes=max_upload_bytes,
        max_pdf_pages=max_pdf_pages,
        max_vision_calls_per_run=max_vision_calls_per_run,
        max_vision_crop_calls_per_run=max_vision_crop_calls_per_run,
    )


def _with_field(bad_value: Any, position: int) -> AttachmentBudgets:
    """The valid config with one field replaced, by construction position."""
    values: list[Any] = [MAX_BYTES, MAX_PAGES, MAX_VISION_CALLS, MAX_CROP_CALLS]
    values[position] = bad_value
    return _budgets(*values)


# ----- A: normal path, one authority per key -----


def test_valid_finite_config_reads_each_field_from_its_own_env_key() -> None:
    """Four distinct values land in four distinct fields.

    Distinct fixtures so a swapped key mapping cannot pass: a byte cap must
    never become a page cap.
    """
    budgets = _load()
    assert budgets.max_upload_bytes == MAX_BYTES
    assert budgets.max_pdf_pages == MAX_PAGES
    assert budgets.max_vision_calls_per_run == MAX_VISION_CALLS
    assert budgets.max_vision_crop_calls_per_run == MAX_CROP_CALLS


def test_budgets_is_a_frozen_dataclass_with_exactly_four_configured_fields() -> None:
    """The finite-number holder has no fifth knob and no lifecycle state.

    Field names are the contract the upload door and preparation consume, so
    they are asserted as a set rather than by position.
    """
    assert dataclasses.is_dataclass(AttachmentBudgets)
    names = {field.name for field in dataclasses.fields(AttachmentBudgets)}
    assert names == set(FIELD_NAMES)


def test_wire_code_constants_are_the_named_closed_list_strings() -> None:
    """The two outward codes this module owns are the spec's literals.

    A renamed constant would silently emit an off-list wire code.
    """
    assert ATTACHMENT_PAGE_LIMIT_EXCEEDED == "attachment_page_limit_exceeded"
    assert ATTACHMENT_VISION_BUDGET_EXCEEDED == "attachment_vision_budget_exceeded"


def test_limit_errors_carry_their_named_wire_code() -> None:
    """Each typed limit error maps to exactly one outward code.

    The door turns ``exc.code`` into the response body (host precedent
    ``workspace_http`` / ``export.py:61``); two errors sharing a code, or one
    code swapped, would tell the operator the wrong thing about which bound
    was hit.
    """
    assert issubclass(AttachmentPageLimitExceeded, AttachmentBudgetError)
    assert issubclass(AttachmentVisionBudgetExceeded, AttachmentBudgetError)
    assert AttachmentPageLimitExceeded.code == ATTACHMENT_PAGE_LIMIT_EXCEEDED
    assert AttachmentVisionBudgetExceeded.code == ATTACHMENT_VISION_BUDGET_EXCEEDED
    assert AttachmentPageLimitExceeded.code != AttachmentVisionBudgetExceeded.code


def test_config_validation_failure_exposes_no_wire_code() -> None:
    """No outward code covers a failed startup config, so this stays internal.

    A config error therefore stays an internal typed exception: if it ever
    grew a ``code``, an operator mistake would surface as a product exit.
    """
    assert issubclass(AttachmentBudgetConfigError, AttachmentBudgetError)
    assert getattr(AttachmentBudgetError, "code", None) is None
    assert getattr(AttachmentBudgetConfigError, "code", None) is None
    with pytest.raises(AttachmentBudgetConfigError) as excinfo:
        _load({PAGES_KEY: None})
    assert getattr(excinfo.value, "code", None) is None


def test_preflight_accepts_a_run_at_the_page_and_budget_boundaries() -> None:
    """Exactly at the cap is admitted; the cap is not "cap minus one".

    One attachment at ``max_pdf_pages`` plus the full crop headroom is the
    equality point of the page cap, and two attachments summing to the
    reserved total with no crops reaches the run-budget boundary from the
    other side. One
    call past that same total is refused, so the acceptance is a real bound
    and not an unchecked pass-through.
    """
    budgets = _load()
    preflight_run(budgets, [MAX_PAGES], MAX_CROP_CALLS)
    preflight_run(budgets, [MAX_PAGES, MAX_PAGES], 1)
    validate(budgets)
    with pytest.raises(AttachmentVisionBudgetExceeded):
        preflight_run(budgets, [MAX_PAGES, MAX_PAGES], 2)


# ----- B: each key missing on its own -----


@pytest.mark.parametrize("missing_key", ALL_KEYS, ids=ALL_KEYS)
def test_each_env_key_missing_on_its_own_fails_config_validation(
    missing_key: str,
) -> None:
    """No default may back a deployment limit: a missing limit is a failure.

    Absent is a startup failure even for the crop headroom key, because a
    silently zeroed headroom would drop question-specific reads.
    """
    with pytest.raises(AttachmentBudgetConfigError):
        _load({missing_key: None})


# ----- C: blank and whitespace are missing, not zero -----


@pytest.mark.parametrize("key", ALL_KEYS, ids=ALL_KEYS)
@pytest.mark.parametrize("blank", ["", "   ", "\t", "\n"])
def test_blank_and_whitespace_only_values_count_as_missing(
    key: str, blank: str
) -> None:
    """Mirror of ``jev_config._trimmed``: trim, then empty is absent.

    Reading ``""`` as ``0`` would turn a typo into a silently disabled
    capability rather than a named config failure.
    """
    with pytest.raises(AttachmentBudgetConfigError):
        _load({key: blank})


# ----- D: non-positive values, and the -0 adjudication -----


@pytest.mark.parametrize("key", ALL_KEYS, ids=ALL_KEYS)
@pytest.mark.parametrize("value", ["0", "-1", "-1048576"])
def test_zero_and_negative_values_fail_config_validation(key: str, value: str) -> None:
    """A zero or negative bound admits no work; it is a mis-deployment.

    Rejected as non-positive for every field, including the byte cap, where
    zero would make every upload impossible, and the crop headroom, where
    zero means the deployment cannot answer a follow-up crop question at
    all.
    """
    with pytest.raises(AttachmentBudgetConfigError):
        _load({key: value})


def test_negative_zero_is_rejected_as_an_invalid_value() -> None:
    """Adjudication: ``"-0"`` is an invalid value, never ``0`` or missing.

    Python's ``int("-0")`` is ``0``, so a lenient parse would file it as
    "non-positive" and a sign-aware parse as "malformed". Either way it must
    fail, and which of the two it fails as is not a promise the module makes;
    this pins that it cannot be accepted.
    """
    with pytest.raises(AttachmentBudgetConfigError):
        _load({PAGES_KEY: "-0"})


def test_config_error_names_the_offending_key_and_rejected_value() -> None:
    """The operator must find the bad key without a debugger.

    Repo precedent ``attachment_store._require_version`` names the concept
    and shows the got-value; here that means the env key always, plus the
    raw text whenever a value was present and unusable.
    """
    with pytest.raises(AttachmentBudgetConfigError) as invalid:
        _load({PAGES_KEY: "-0"})
    assert PAGES_KEY in str(invalid.value)
    assert "-0" in str(invalid.value)
    with pytest.raises(AttachmentBudgetConfigError) as absent:
        _load({VISION_KEY: None})
    assert VISION_KEY in str(absent.value)


# ----- F: unbounded and non-numeric input classes -----


@pytest.mark.parametrize("key", ALL_KEYS)
@pytest.mark.parametrize(
    "value",
    [
        pytest.param("abc", id="not-a-number"),
        pytest.param("unlimited", id="the-unbounded-word-the-contract-rejects"),
        pytest.param("inf", id="infinity-spelled"),
        pytest.param("Infinity", id="infinity-spelled-capitalised"),
        pytest.param("-inf", id="negative-infinity-spelled"),
        pytest.param("nan", id="a-nan-spelled"),
        pytest.param("1.5", id="a-fraction-of-a-page-or-byte"),
        pytest.param("1e999", id="scientific-notation-int-rejects-outright"),
        pytest.param("True", id="a-boolean-word-capitalised"),
        pytest.param("true", id="a-boolean-word-lowercase"),
        # int() accepts the sign; the canonical form here is unsigned.
        pytest.param("+10", id="plus-prefixed"),
        # int() accepts the PEP 515 underscore; rejected.
        pytest.param("1_000", id="underscore-separated"),
        # int() and isdigit() both accept these.
        pytest.param("１０", id="full-width-digits"),
        pytest.param("0x10", id="a-base-prefixed-literal"),
        pytest.param(str(sys.maxsize + 1), id="one-past-the-hosts-finite-bound"),
    ],
)
def test_non_canonical_or_unbounded_values_fail_config_validation(
    key: str, value: str
) -> None:
    """The adjudication table for every class ``int()`` would swallow (F).

    Rule pinned: after trimming, the text must be one or more ASCII decimal
    digits — no sign, no base prefix, no separator, no non-ASCII digit — and
    the resulting integer must be positive and at or below ``sys.maxsize``.
    Every key carries the rule, so the table is run against all four. The
    failure must name both the key and the rejected text, because an operator
    reading a boot log has only that line to find the setting.
    """
    with pytest.raises(AttachmentBudgetConfigError) as excinfo:
        _load({key: value})
    message = str(excinfo.value)
    assert key in message
    assert value in message


@pytest.mark.parametrize(
    ("key", "raw", "expected"),
    [
        (BYTES_KEY, " 10 ", 10),
        (PAGES_KEY, "\t8\n", 8),
        (VISION_KEY, " 18 ", 18),
        (CROP_KEY, "8 ", 8),
    ],
    ids=["bytes", "pages", "vision_calls", "crop_calls"],
)
def test_surrounding_whitespace_is_trimmed_then_accepted(
    key: str, raw: str, expected: int
) -> None:
    """``" 10 "`` carries the same intent as ``"10"`` and must not fail.

    Trimming is ``jev_config._trimmed``'s behaviour, so an
    ``export FIN_ATTACHMENT_MAX_BYTES=" 8388608 "`` in a unit file is not a
    startup failure; interior whitespace stays invalid under the
    ASCII-digits rule above.
    """
    assert getattr(_load({key: raw}), FIELD_FOR_KEY[key]) == expected


@pytest.mark.parametrize("position", range(4), ids=ALL_KEYS)
def test_programmatically_supplied_bool_field_fails_validation(position: int) -> None:
    """``True`` is an ``int`` in Python, and it is not a bound here.

    Same precedent as ``attachment_store._require_version`` (line 260):
    reject ``isinstance(x, bool)`` explicitly, so a boolean handed to a
    budget field cannot silently mean one byte or one page.
    """
    with pytest.raises(AttachmentBudgetConfigError):
        validate(_with_field(True, position))


@pytest.mark.parametrize(
    "bad_value",
    [None, "8", 8.0, float("inf"), 0, -8, sys.maxsize + 1, 2**200],
    ids=[
        "none",
        "numeric_string",
        "float_whole",
        "float_infinity",
        "zero",
        "negative",
        "above_sys_maxsize",
        "absurdly_large",
    ],
)
def test_validate_rejects_non_int_or_out_of_range_fields(bad_value: Any) -> None:
    """``validate`` guards a hand-built object, not only the env door.

    Preparation or a composition root may build the config directly, so
    ``from_environ`` cannot be the only place a bound is enforced; every
    field position is checked with the same bad value.
    """
    for position in range(4):
        with pytest.raises(AttachmentBudgetConfigError):
            validate(_with_field(bad_value, position))


# ----- E: exact-boundary acceptance for the byte bound and page count -----


def test_byte_bound_at_the_finite_ceiling_is_accepted_and_one_over_is_not() -> None:
    """The byte cap itself is bounded: ``sys.maxsize`` is admissible.

    ``sys.maxsize + 1`` is a Python int but not a finite host bound, which is
    exactly the unbounded shape the spec rejects; the largest representable
    bound must still deploy, or an operator on a bigger device could not
    express it.
    """
    assert _load({BYTES_KEY: str(sys.maxsize)}).max_upload_bytes == sys.maxsize
    with pytest.raises(AttachmentBudgetConfigError):
        _load({BYTES_KEY: str(sys.maxsize + 1)})


def test_page_count_exactly_at_cap_is_admitted_and_one_over_is_rejected() -> None:
    """``max_pdf_pages`` is inclusive, and the failure is the page code.

    A one-page-over run must fail with ``attachment_page_limit_exceeded``
    rather than preparing the pages that did fit: silent truncation is the
    failure this contract exists to prevent.
    """
    budgets = _load()
    preflight_run(budgets, [MAX_PAGES], 0)
    with pytest.raises(AttachmentPageLimitExceeded) as excinfo:
        preflight_run(budgets, [MAX_PAGES + 1], 0)
    assert excinfo.value.code == ATTACHMENT_PAGE_LIMIT_EXCEEDED


# ----- G: the static page/call relationship -----


@pytest.mark.parametrize(
    ("pages", "crop", "vision", "admissible"),
    [
        (8, 8, 17, True),
        (8, 8, 18, True),
        (8, 1, 10, True),
        (40, 20, 61, True),
        (8, 8, 16, False),
        (8, 8, 15, False),
        (8, 1, 9, False),
        (8, 1, 8, False),
        (40, 20, 60, False),
        (40, 20, 59, False),
    ],
    ids=[
        "reserve_is_the_minimum",
        "one_spare_call",
        "single_crop_reserve",
        "large_reserve",
        "equality_is_no_longer_enough",
        "one_short",
        "reserve_short_by_one",
        "one_crop_unbudgeted",
        "large_equality",
        "large_one_short",
    ],
)
def test_run_budget_must_cover_pages_plus_crop_headroom_plus_one_retry(
    pages: int, crop: int, vision: int, admissible: bool
) -> None:
    """The deployment is self-consistent before any run exists.

    One full-page scan per admitted page, the crop headroom, and exactly one
    reserved full-page transport retry is the minimum a run budget must
    cover, and it must hold before a run starts; an
    under-budgeted config could only ever fail mid-run, which is what this
    check prevents. A total that equals pages plus crops spends the last call
    on the run's own work and leaves nothing for a retried scan, so it is a
    mis-deployment too. The inadmissible rows are probed with a trivially
    small run request, so the relationship is the only thing that can fail —
    ``preflight_run`` must validate it before starting work too.
    """
    budgets = _budgets(
        max_pdf_pages=pages,
        max_vision_calls_per_run=vision,
        max_vision_crop_calls_per_run=crop,
    )
    if admissible:
        validate(budgets)
        preflight_run(budgets, [pages], crop)
    else:
        with pytest.raises(AttachmentBudgetConfigError):
            validate(budgets)
        with pytest.raises(AttachmentBudgetConfigError):
            preflight_run(budgets, [1], 0)


def test_the_deployed_policy_allowance_reserves_a_retry_and_the_old_floor_does_not():
    """``89 = 80 + 8 + 1`` is the deployment target; the exact-sum ``88`` is not.

    The totals are read as both a config and an environ, because the boot door
    is ``from_environ`` and a value that only passes the dataclass check would
    still deploy. The refusal is a config error with no outward code: an
    operator mistake is not a product exit a client may retry.
    """
    reserve = _budgets(
        max_pdf_pages=80, max_vision_calls_per_run=89, max_vision_crop_calls_per_run=8
    )
    floor = _budgets(
        max_pdf_pages=80, max_vision_calls_per_run=88, max_vision_crop_calls_per_run=8
    )
    validate(reserve)
    assert _load(
        {PAGES_KEY: "80", VISION_KEY: "89", CROP_KEY: "8"}
    ).max_vision_calls_per_run == 89
    with pytest.raises(AttachmentBudgetConfigError) as excinfo:
        validate(floor)
    assert "88" in str(excinfo.value) and "89" in str(excinfo.value)
    assert getattr(excinfo.value, "code", None) is None
    with pytest.raises(AttachmentBudgetConfigError):
        _load({PAGES_KEY: "80", VISION_KEY: "88", CROP_KEY: "8"})


# ----- H, I, J: preflight ordering and per-run accounting -----


def test_page_cap_is_reported_before_the_vision_budget_when_both_are_violated() -> None:
    """Page admission happens before any scan, so its code wins.

    A run whose attachments both exceed the per-attachment page cap and blow
    the run budget must be described as the page failure it is first:
    budgeting calls for pages that cannot be admitted at all is not the
    decision the operator needs.
    """
    budgets = _load()
    admitted = [MAX_PAGES + 1, MAX_PAGES * 3]
    with pytest.raises(AttachmentPageLimitExceeded) as excinfo:
        preflight_run(budgets, admitted, MAX_CROP_CALLS)
    assert not isinstance(excinfo.value, AttachmentVisionBudgetExceeded)
    assert excinfo.value.code == ATTACHMENT_PAGE_LIMIT_EXCEEDED


def test_over_budget_run_fails_before_any_page_is_skipped() -> None:
    """The budget failure is a failure, not a shortened plan (spec:57).

    Both attachments individually sit exactly at the page cap, and the run
    sums one call past the budget: the only acceptable outcome is the typed
    budget error, with the caller's admitted page list untouched — proving
    nothing was dropped on the way out and no partial page set was returned
    as a success.
    """
    budgets = _load()
    admitted = [MAX_PAGES, MAX_PAGES]
    with pytest.raises(AttachmentVisionBudgetExceeded) as excinfo:
        preflight_run(budgets, admitted, 2)
    assert admitted == [MAX_PAGES, MAX_PAGES]
    assert not isinstance(excinfo.value, AttachmentPageLimitExceeded)
    assert excinfo.value.code == ATTACHMENT_VISION_BUDGET_EXCEEDED


def test_run_budget_is_accounted_across_every_eligible_attachment() -> None:
    """Under the page cap per file is not enough; the run sums (spec:57).

    Three eligible attachments of five pages each fit the eight-page cap
    individually, and with the crop headroom the run needs 23 of 17 calls.
    The same pages admitted for a single attachment stay admissible, which
    is the per-file versus per-run distinction.
    """
    budgets = _load()
    preflight_run(budgets, [5], MAX_CROP_CALLS)
    with pytest.raises(AttachmentVisionBudgetExceeded):
        preflight_run(budgets, [5, 5, 5], MAX_CROP_CALLS)


def test_crop_calls_consume_the_same_run_budget_as_full_page_scans() -> None:
    """Crops are headroom inside one budget, not a second counter (spec:57).

    Ten page scans plus eight crop calls is exactly one over the 17-call
    budget, while eight crops would fit the eight-crop sub-cap on its own: a
    parallel counter would wrongly admit this run. Seven crops is the
    accepted neighbour.
    """
    budgets = _load()
    preflight_run(budgets, [5, 5], MAX_CROP_CALLS - 1)
    with pytest.raises(AttachmentVisionBudgetExceeded):
        preflight_run(budgets, [5, 5], MAX_CROP_CALLS)


# ----- K: the trivial empty run -----


def test_empty_admitted_run_passes_and_still_enforces_the_run_budget() -> None:
    """No eligible page is a legitimate run, not an unchecked one.

    A turn that makes no attachment eligible is the common path, so an empty
    list must validate; it must not short-circuit the remaining checks, so a
    crop request past the whole run budget still fails. 89 requested crops over
    the deployed bounds is past the crop headroom of 8 before it is ever a run
    total, so the crop-bound refusal is the determined answer and is asserted as
    such, message included.
    """
    budgets = _load()
    preflight_run(budgets, [], MAX_CROP_CALLS)
    with pytest.raises(AttachmentVisionBudgetExceeded) as excinfo:
        preflight_run(budgets, (), MAX_VISION_CALLS + 1)
    assert f"headroom is {budgets.max_vision_crop_calls_per_run}" in str(excinfo.value)


# ----- L: immutability and load independence -----


def test_assigning_a_budget_field_raises() -> None:
    """A budget must not be renegotiated mid-run.

    Silent mid-run widening would make the recorded page and call counts
    disagree with the deployed bound.
    """
    budgets = _load()
    with pytest.raises(dataclasses.FrozenInstanceError):
        budgets.max_pdf_pages = 999  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        budgets.max_vision_calls_per_run = 999  # type: ignore[misc]
    assert budgets.max_pdf_pages == MAX_PAGES
    assert budgets.max_vision_calls_per_run == MAX_VISION_CALLS


def test_loading_the_same_environ_twice_yields_equal_independent_objects() -> None:
    """No shared mutable state behind the loader.

    Two loads must agree (idempotent config), and editing the source mapping
    after a load must not reach into the object already handed to
    preparation.
    """
    environ = _environ()
    first = from_environ(environ=environ)
    second = from_environ(environ=environ)
    assert first == second
    assert first is not second
    environ[PAGES_KEY] = "2"
    environ[VISION_KEY] = "64"
    assert first.max_pdf_pages == MAX_PAGES
    assert first.max_vision_calls_per_run == MAX_VISION_CALLS
    assert from_environ(environ=environ).max_pdf_pages == 2


def test_preflight_is_repeatable_and_never_mutates_the_deployed_bounds() -> None:
    """Preflight checks a request; it does not spend it.

    The same admissible request passes twice and the over-budget one still
    fails afterwards, so the check cannot have quietly relaxed a bound or
    kept a per-process counter that a later run depends on.
    """
    budgets = _load()
    preflight_run(budgets, [MAX_PAGES], MAX_CROP_CALLS)
    preflight_run(budgets, [MAX_PAGES], MAX_CROP_CALLS)
    with pytest.raises(AttachmentVisionBudgetExceeded):
        preflight_run(budgets, [MAX_PAGES, MAX_PAGES], 2)
    validate(budgets)
    assert budgets == _load()


# ----- M: preflight input classes -----


@pytest.mark.parametrize(
    "bad_pages",
    [
        None,
        4,
        "4",
        [4.0],
        ["4"],
        [None],
        [True],
        [-1],
        [[1]],
    ],
    ids=[
        "none_request",
        "bare_int_not_a_list",
        "string_not_a_list",
        "float_entry",
        "string_entry",
        "none_entry",
        "bool_entry",
        "negative_entry",
        "nested_list_entry",
    ],
)
def test_malformed_admitted_page_request_fails_typed_not_type_error(
    bad_pages: Any,
) -> None:
    """A malformed run request is a named failure, never a ``TypeError``.

    The page list is built by orchestration code, so a ``None`` entry or a
    float must surface as the module's own typed error rather than a leak a
    caller could mistake for an unrelated crash: no outward code covers it,
    and it is not a limit violation.
    """
    budgets = _load()
    with pytest.raises(AttachmentBudgetConfigError):
        preflight_run(budgets, bad_pages, 0)


@pytest.mark.parametrize(
    "bad_crop",
    [None, "4", 4.0, True, -1],
    ids=["none", "string", "float", "bool", "negative"],
)
def test_malformed_requested_crop_count_fails_typed_not_type_error(
    bad_crop: Any,
) -> None:
    """The same rule for the crop count, which is a real call-budget input."""
    budgets = _load()
    with pytest.raises(AttachmentBudgetConfigError):
        preflight_run(budgets, [1], bad_crop)


@pytest.mark.parametrize(
    ("pages", "crops"),
    [
        ([1], MAX_CROP_CALLS + 1),
        ([1], MAX_VISION_CALLS - 1),
        ([MAX_PAGES - 1], MAX_CROP_CALLS + 1),
    ],
    ids=["one_over", "almost_twice_the_headroom", "spends_the_whole_run_budget"],
)
def test_crop_request_over_the_configured_headroom_fails_even_inside_the_run_budget(
    pages: list[int],
    crops: int,
) -> None:
    """The fourth bound is a deployment price, so a run total with room left
    still cannot buy more crops than were priced.
    """
    budgets = _load()
    assert sum(pages) + crops <= MAX_VISION_CALLS  # the run total is not what fails
    with pytest.raises(AttachmentVisionBudgetExceeded):
        preflight_run(budgets, pages, crops)


def test_crop_request_at_exactly_the_configured_headroom_is_admitted() -> None:
    """The bound is inclusive, like the page cap."""
    budgets = _load()
    preflight_run(budgets, [1], MAX_CROP_CALLS)


# ----- O: the run's spent-call ledger -----


def _ledger(
    pages: Any = MAX_PAGES,
    vision: Any = MAX_VISION_CALLS,
    crop: Any = MAX_CROP_CALLS,
) -> VisionCallLedger:
    """A ledger over one small deployment."""
    return VisionCallLedger(
        _budgets(
            max_pdf_pages=pages,
            max_vision_calls_per_run=vision,
            max_vision_crop_calls_per_run=crop,
        )
    )


def test_ledger_starts_unspent_and_accounts_every_attempt_to_one_run_total() -> None:
    """A fresh run has spent nothing, and both call kinds share one counter.

    Four full-page scans (one of them the retry) and two crops against a
    ``3 + 2 + 1`` allowance is seven of eight calls: the spent views must add
    up to the same total the run budget is compared against, or a persisted
    run record would report a demand the bound did not enforce.
    """
    ledger = _ledger(pages=3, vision=8, crop=2)
    assert ledger.calls_spent == 0
    assert ledger.full_page_calls_spent == 0
    assert ledger.crop_calls_spent == 0
    assert ledger.retries_spent == 0
    assert ledger.remaining_calls == 8
    for _ in range(3):
        ledger.charge_full_page_attempt()
    for _ in range(2):
        ledger.charge_crop_attempt()
    ledger.charge_full_page_attempt(retry=True)
    ledger.charge_full_page_attempt()
    assert ledger.full_page_calls_spent == 5
    assert ledger.crop_calls_spent == 2
    assert ledger.retries_spent == 1
    assert ledger.calls_spent == ledger.full_page_calls_spent + ledger.crop_calls_spent
    assert ledger.calls_spent == 7
    assert ledger.remaining_calls == 1


def test_every_call_the_policy_allowance_admits_is_charged_and_one_more_is_refused():
    """89 admits 80 pages, 8 crops and the one reserved retry — and nothing else.

    This is the deployment target walked call by call: the 89th charge is
    admitted, the 90th is refused with the outward budget code, and the page
    list of the run is unaffected by which side of the line it lands on.
    """
    ledger = _ledger(pages=80, vision=89, crop=8)
    for _ in range(80):
        ledger.charge_full_page_attempt()
    for _ in range(8):
        ledger.charge_crop_attempt()
    ledger.charge_full_page_attempt(retry=True)
    assert ledger.calls_spent == 89
    assert ledger.remaining_calls == 0
    with pytest.raises(AttachmentVisionBudgetExceeded) as excinfo:
        ledger.charge_full_page_attempt()
    assert excinfo.value.code == ATTACHMENT_VISION_BUDGET_EXCEEDED
    assert ledger.calls_spent == 89
    with pytest.raises(AttachmentVisionBudgetExceeded):
        ledger.charge_crop_attempt()
    assert ledger.calls_spent == 89


def test_a_failed_attempt_consumes_budget_so_retries_cannot_run_the_tab() -> None:
    """Attempts are paid for by the run, whether or not the provider answered.

    A transport failure after the call went out is the same spend as a
    success: the ledger is charged before the request goes out and has no
    refund, so a run that keeps failing cannot spend more than the deployed
    allowance. One page, one crop and the reserve is ``1 + 1 + 1 = 3``.
    """
    ledger = _ledger(pages=1, vision=3, crop=1)

    def outbound(*, retry: bool) -> None:
        """Stand-in for the provider call: it always fails after being charged."""
        ledger.charge_full_page_attempt(retry=retry)
        raise RuntimeError("transport closed")

    failures = 0
    for attempt in (1, 2):
        with pytest.raises(RuntimeError):
            outbound(retry=attempt == 2)
        failures += 1
        assert ledger.calls_spent == attempt, f"attempt {attempt} was not charged"
    assert failures == 2
    assert ledger.retries_spent == 1
    # Only the one call the allowance still covers is left, and it is not a retry.
    ledger.charge_full_page_attempt()
    assert ledger.calls_spent == 3
    with pytest.raises(AttachmentVisionBudgetExceeded):
        ledger.charge_full_page_attempt()


def test_the_retry_reserve_is_one_per_run_not_one_per_page() -> None:
    """The single reserve belongs to the run, so a second page gets nothing.

    Three pages, one crop and one retry fit ``3 + 1 + 1 = 5``. The retry after
    the first page is admitted; the retry asked for the second page is refused
    even though the page cap, the crop sub-cap and the arithmetic all still
    allow a call — only the consumed reserve can produce that answer.
    """
    ledger = _ledger(pages=3, vision=5, crop=1)
    ledger.charge_full_page_attempt()
    ledger.charge_full_page_attempt(retry=True)
    ledger.charge_full_page_attempt()
    assert ledger.remaining_calls == 2
    with pytest.raises(AttachmentVisionBudgetExceeded) as excinfo:
        ledger.charge_full_page_attempt(retry=True)
    assert excinfo.value.code == ATTACHMENT_VISION_BUDGET_EXCEEDED
    assert ledger.retries_spent == 1
    assert ledger.calls_spent == 3
    # A plain (non-retry) attempt is still admissible under the run total.
    ledger.charge_full_page_attempt()


def test_the_crop_sub_cap_holds_even_while_the_run_total_has_room() -> None:
    """The eighth crop is a deployment price, not a discount on the total.

    Twelve calls is well over ``2 + 8 + 1``, so the only bound the ninth crop
    can be refused by is its own headroom.
    """
    ledger = _ledger(pages=2, vision=12, crop=8)
    for _ in range(8):
        ledger.charge_crop_attempt()
    assert ledger.remaining_calls == 4
    with pytest.raises(AttachmentVisionBudgetExceeded) as excinfo:
        ledger.charge_crop_attempt()
    assert excinfo.value.code == ATTACHMENT_VISION_BUDGET_EXCEEDED
    assert ledger.crop_calls_spent == 8
    # Pages and the reserve are untouched by the crop refusal.
    ledger.charge_full_page_attempt()
    ledger.charge_full_page_attempt(retry=True)


def test_ledger_rejects_a_deployment_that_leaves_no_room_for_the_retry() -> None:
    """An unreserved config cannot spend, not just cannot boot.

    The ledger is the enforcement half of the same rule, so a caller that
    builds it directly cannot bypass the boot check.
    """
    for floor in (88, 87):
        with pytest.raises(AttachmentBudgetConfigError):
            VisionCallLedger(
                _budgets(
                    max_pdf_pages=80, max_vision_calls_per_run=floor,
                    max_vision_crop_calls_per_run=8,
                )
            )


def test_ledger_accounts_are_per_object_and_live_only_in_this_process() -> None:
    """No shared counter behind a ledger: two runs, two tabulations.

    A module-level accumulator would let one run spend another's budget and
    would survive a refusal in an unrelated test. This also pins the declared
    durability boundary — the counts are in memory and a caller must persist
    them somewhere durable to keep them across processes.
    """
    budgets = _budgets(
        max_pdf_pages=8, max_vision_calls_per_run=17, max_vision_crop_calls_per_run=8
    )
    first = VisionCallLedger(budgets)
    second = VisionCallLedger(budgets)
    first.charge_full_page_attempt()
    assert first.calls_spent == 1
    assert second.calls_spent == 0
    assert second.remaining_calls == 17
    assert first.budgets is budgets


def test_concurrent_attempts_cannot_double_spend_the_last_call() -> None:
    """Two pages scanned at once still draw on one run budget.

    Every thread races for the same final call; exactly one may take it. A
    check-then-add that is not atomic would let the run spend past the bound
    it exists to enforce, and the ledger would report more calls than were
    allowed.
    """
    import threading

    ledger = _ledger(pages=8, vision=17, crop=8)
    attempts = 60
    admitted: list[int] = []
    start = threading.Barrier(attempts)
    lock = threading.Lock()

    def race() -> None:
        start.wait()
        try:
            ledger.charge_full_page_attempt()
        except AttachmentVisionBudgetExceeded:
            return
        with lock:
            admitted.append(1)

    threads = [threading.Thread(target=race) for _ in range(attempts)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert ledger.calls_spent == 17
    assert len(admitted) == 17


@pytest.mark.parametrize(
    "bad_retry",
    [1, 0, "yes", None, 1.0, [True]],
    ids=["int_one", "int_zero", "string", "none", "float", "list"],
)
def test_a_malformed_retry_ask_is_a_typed_caller_error_and_spends_nothing(
    bad_retry: Any,
) -> None:
    """A truthy-looking flag is a caller bug, not a silently consumed retry.

    ``1`` would read as ``True`` and ``0`` as ``False``: a run that meant a
    first scan would have spent its one reserve instead, so the ask must be
    refused and the budget left whole.
    """
    ledger = _ledger()
    with pytest.raises(AttachmentBudgetConfigError):
        ledger.charge_full_page_attempt(retry=bad_retry)
    assert ledger.calls_spent == 0
    assert ledger.retries_spent == 0


# ----- P: send_full_page, the one charge→inspect→retry ladder -----

_TARGET = VisionTarget(attachment_id="a", version="1", page=1, crop=None)


class _ScriptedPort:
    """``VisionPort`` witness: a queued outcome per call, every call kept.

    A string answers with that text; an exception instance is raised. The
    recorded call dicts let a case assert the exact outbound shape.
    """

    def __init__(self, outcomes: list[Any]) -> None:
        self.calls: list[dict[str, Any]] = []
        self._outcomes = list(outcomes)

    @property
    def sent_pages(self) -> list[int]:
        return [call["target"].page for call in self.calls]

    def inspect(
        self, *, image: bytes, target: Any, media_type: str, question: str
    ) -> VisionCallResult:
        self.calls.append(
            {
                "image": image,
                "target": target,
                "media_type": media_type,
                "question": question,
            }
        )
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return VisionCallResult(
            text=outcome,
            target=target,
            model_id="fake-vision-1",
            media_type=media_type,
            usage=VisionUsage(1, 1, 2),
            cost=None,
        )


def _transport_failure() -> VisionCallFailedError:
    return VisionCallFailedError("dropped", error_class=VisionErrorClass.TRANSPORT)


def _permanent_failure() -> VisionCallFailedError:
    return VisionCallFailedError("refused", error_class=VisionErrorClass.PERMANENT)


def _send(
    ledger: VisionCallLedger, port: _ScriptedPort
) -> FullPageSend:
    return send_full_page(
        port,
        ledger,
        image=b"png-bytes",
        target=_TARGET,
        media_type="image/png",
        question="what does this page show?",
    )


def test_send_full_page_answers_and_charges_exactly_one_attempt() -> None:
    """The clean path: one charge, one send, the answer and no retry fact."""
    ledger = _ledger(pages=3, vision=5, crop=1)
    port = _ScriptedPort(["one chart"])

    send = _send(ledger, port)

    assert send.result.text == "one chart"
    assert send.retry_used is False
    assert ledger.calls_spent == 1
    assert ledger.retries_spent == 0
    (call,) = port.calls
    assert call["image"] == b"png-bytes"
    assert call["target"] is _TARGET
    assert call["media_type"] == "image/png"
    assert call["question"] == "what does this page show?"


def test_send_full_page_never_retries_a_permanent_refusal() -> None:
    """A failure the adapter did not classify as transport would answer the
    same way twice, paid for twice: it exits as raised, one send, one
    charge, the reserve untouched."""
    ledger = _ledger(pages=3, vision=5, crop=1)
    port = _ScriptedPort([_permanent_failure()])

    with pytest.raises(VisionCallFailedError) as excinfo:
        _send(ledger, port)

    assert excinfo.value.retryable is False
    assert len(port.calls) == 1
    assert ledger.calls_spent == 1
    assert ledger.retries_spent == 0


def test_send_full_page_spends_the_run_reserve_once_on_a_transport_failure() -> None:
    """Retryable transport failure, reserve free: exactly one more attempt,
    charged before it leaves, and the outcome reports the reserve was used
    so each seam can record the retried-away attempt in its own envelope."""
    ledger = _ledger(pages=3, vision=5, crop=1)
    port = _ScriptedPort([_transport_failure(), "one chart"])

    send = _send(ledger, port)

    assert send.result.text == "one chart"
    assert send.retry_used is True
    assert len(port.calls) == 2
    assert ledger.calls_spent == 2
    assert ledger.retries_spent == 1


def test_send_full_page_a_second_failure_is_final_and_never_retries_twice() -> None:
    """The reserve buys one attempt, not a ladder: after the retry fails the
    original fault type propagates, there is no third send, and the run's
    reserve is gone whatever the total looks like."""
    ledger = _ledger(pages=3, vision=5, crop=1)
    port = _ScriptedPort([_transport_failure(), _transport_failure()])

    with pytest.raises(VisionCallFailedError):
        _send(ledger, port)

    assert len(port.calls) == 2
    assert ledger.calls_spent == 2
    assert ledger.retries_spent == 1


def test_send_full_page_a_spent_reserve_exits_the_provider_failure() -> None:
    """The adjudicated exit (spec: an overflow code is only for run-total or
    crop-cap refusal): with the reserve gone and a run total still several
    calls from its bound, a retryable failure re-raises the port's own
    error — the request that failed is the provider's dropped call, not the
    operator's cap — and nothing further is sent."""
    ledger = _ledger(pages=8, vision=17, crop=8)
    ledger.charge_full_page_attempt()
    ledger.charge_full_page_attempt(retry=True)
    port = _ScriptedPort([_transport_failure()])

    with pytest.raises(VisionCallFailedError) as excinfo:
        _send(ledger, port)

    assert excinfo.value.retryable is True
    assert not isinstance(excinfo.value, AttachmentVisionBudgetExceeded)
    assert len(port.calls) == 1
    assert ledger.remaining_calls == 17 - 3


def test_send_full_page_run_total_exhaustion_on_the_retry_charge_keeps_the_budget_exit() -> None:
    """The other side of the same line: the reserve is still free, but the
    run total has no call left for the retry, so the charge refuses the send
    with the budget code — a real overflow, before anything goes out."""
    ledger = _ledger(pages=2, vision=4, crop=1)
    ledger.charge_full_page_attempt()
    ledger.charge_full_page_attempt()
    ledger.charge_crop_attempt()  # 3 of 4 spent, reserve untouched
    port = _ScriptedPort([_transport_failure()])

    with pytest.raises(AttachmentVisionBudgetExceeded):
        _send(ledger, port)

    assert len(port.calls) == 1  # the first attempt left; the retry did not
    assert ledger.retries_spent == 0
    assert ledger.calls_spent == 4


def test_send_full_page_empty_output_is_never_retried() -> None:
    """Malformed or empty provider output is not a transport fault: the
    port's own error propagates after one send and one charge, and the
    reserve stays whole for a real dropped request."""
    ledger = _ledger(pages=3, vision=5, crop=1)
    port = _ScriptedPort([VisionEmptyTextError("nothing usable")])

    with pytest.raises(VisionEmptyTextError):
        _send(ledger, port)

    assert len(port.calls) == 1
    assert ledger.calls_spent == 1
    assert ledger.retries_spent == 0


# ----- the deployed bounds, bound to their measurement evidence -----
PROJECT_ROOT = Path(__file__).resolve().parents[2]
CORPUS_DISTRIBUTION_PATH = (
    PROJECT_ROOT / "data" / "corpus" / "g4_upload_distribution.json"
)
GOLD_MANIFEST_PATH = (
    PROJECT_ROOT / "data" / "corpus" / "g4_attachment_gold" / "manifest.json"
)
CORPUS_PDF_DIR = PROJECT_ROOT / "data" / "corpus" / "pdf"


def _deployed_environ() -> dict[str, str]:
    """The four bounds as ``settings.example.yaml`` ships them.

    The numbers live in the template and in the two measured data files only;
    repeating them here would let a stale test value bless a config that no
    longer matches what was measured.
    """
    from fin.host.operator_settings import shipped_attachment_bounds

    deployed = shipped_attachment_bounds(repo_root=PROJECT_ROOT)
    unconfigured = sorted(set(ALL_KEYS) - set(deployed))
    assert unconfigured == [], f"settings.example.yaml must configure {unconfigured}"
    return deployed


def test_deployed_bounds_are_literals_with_no_shell_fallback() -> None:
    """A bound with no default may not be templated as ``${VAR:-…}``.

    All four are required, so a shell fallback in the template would quietly
    restore the guessed number this module exists to reject.
    """
    environ = _deployed_environ()
    assert sorted(environ) == sorted(ALL_KEYS)
    assert [key for key, value in environ.items() if "$" in value] == []
    from_environ(environ=environ)


def test_deployed_vision_total_is_the_policy_allowance_with_its_reserve() -> None:
    """The shipped template prices the retry, and the value is the stated one.

    ``89 = 80 + 8 + 1`` is a policy allowance for a deployment target, so it is
    pinned both as the named number and as the relationship the validator
    enforces: a template that drifts back to the exact sum boots fine and then
    cannot retry a failed scan.
    """
    budgets = from_environ(environ=_deployed_environ())
    assert budgets.max_pdf_pages == 80
    assert budgets.max_vision_crop_calls_per_run == 8
    assert budgets.max_vision_calls_per_run == 89
    assert budgets.max_vision_calls_per_run == (
        budgets.max_pdf_pages + budgets.max_vision_crop_calls_per_run + 1
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _assert_describes_committed_bytes(
    path: Path, declared_bytes: int, declared_sha256: str
) -> None:
    """Pin a recorded size to the file it was recorded from.

    The bounds were chosen over recorded sizes and page counts, so a guard that
    reads only the JSON would bless a cap that admits a listing rather than the
    corpus. A matching digest makes the recorded page count a statement about
    these exact bytes.
    """
    assert path.is_file(), f"{path} is not committed"
    assert path.stat().st_size == declared_bytes, f"{path} size drifted from the record"
    assert _sha256(path) == declared_sha256, f"{path} content drifted from the record"


def test_corpus_distribution_records_describe_the_committed_files() -> None:
    """Every measured filing the byte and page caps were chosen from is here.

    A filing whose bytes changed since the distribution was written would leave
    the cap decision describing a corpus that no longer exists.
    """
    measured = json.loads(CORPUS_DISTRIBUTION_PATH.read_text())["files"]
    assert len(measured) > 0
    for entry in measured:
        _assert_describes_committed_bytes(
            CORPUS_PDF_DIR / entry["filename"],
            entry["bytes_on_disk"],
            entry["sha256"],
        )


def test_deployed_upload_cap_admits_the_measured_corpus() -> None:
    """The bound ships to admit every filing it was priced over.

    The artifact is the measurement producer's own output, so this row is
    also proof the producer still runs: its deployed-cap value comes from
    ``from_environ`` over the shipped template, which is the same seam the
    upload door reads.
    """
    findings = json.loads(CORPUS_DISTRIBUTION_PATH.read_text())["cap_findings"]
    deployed = from_environ(environ=_deployed_environ()).max_upload_bytes
    assert findings["deployed_attachment_cap_bytes"] == deployed
    assert findings["deployed_attachment_cap_source"] == (
        "settings.example.yaml:attachment.maxBytes"
    )
    assert findings["files_over_deployed_attachment_cap"] == []
    # The retired constant is historical: the filings it rejected are the
    # reason the deployed bound is larger.
    assert findings["route_constant_bytes"] < deployed
    assert findings["count_over_route_constant"] > 0


def test_gold_manifest_describes_the_committed_fixtures() -> None:
    """The four gold cases the caps must fit are the bytes on disk.

    The manifest is also the file that states each case's page count, which is
    what the single-run preflight below charges against the vision budget.
    """
    cases = json.loads(GOLD_MANIFEST_PATH.read_text())["cases"]
    assert len(cases) == 4
    for case in cases:
        _assert_describes_committed_bytes(
            PROJECT_ROOT / case["path"], case["bytes"], case["sha256"]
        )


INK_SCREEN_PATH = PROJECT_ROOT / "data" / "corpus" / "g4_ink_screen.json"


def test_ink_screen_artifact_is_what_the_gold_manifest_cites() -> None:
    """The "no native chart in this corpus" gap is one artifact's numbers.

    That gap is the reason a layout model's figure recall cannot be scored on
    the committed filings and the reason the spec's full-page scan rule is not
    optional, so it is evidence rather than commentary. Every count and fraction
    its sentence states is read out of ``data/corpus/g4_ink_screen.json``, which
    ``scripts/g4_ink_screen.py`` writes: a re-screen that moves a number has to
    carry the sentence that quotes it along with it.
    """
    screen = json.loads(INK_SCREEN_PATH.read_text())
    summary = screen["summary"]
    gap = next(
        entry
        for entry in json.loads(GOLD_MANIFEST_PATH.read_text())["known_gaps"]
        if entry.startswith("NO NATIVE CHART")
    )
    for figure in (
        summary["pages_rendered"],
        summary["pages_with_material_ink"],
        summary["pages_with_any_solid_block"],
        summary["max_solid_fraction"]["5"],
        summary["max_solid_fraction"]["9"],
    ):
        assert str(figure) in gap, f"{figure} is no longer stated by known_gaps[0]"
    assert {page["pdf"] for page in screen["pages"]} == {
        path.name for path in CORPUS_PDF_DIR.glob("*.pdf")
    }, "the screen does not cover the committed corpus"


def test_deployed_bounds_admit_every_file_in_the_measured_distribution() -> None:
    """No real filing in the measured corpus is rejected on size or pages.

    The bound was selected from this distribution, so a cap that excludes one
    of its own samples is either a stale value or a changed corpus. The files
    themselves are checked by
    ``test_corpus_distribution_records_describe_the_committed_files``.
    """
    budgets = from_environ(environ=_deployed_environ())
    measured = json.loads(CORPUS_DISTRIBUTION_PATH.read_text())["files"]
    assert [
        f["filename"] for f in measured if f["bytes_on_disk"] > budgets.max_upload_bytes
    ] == []
    assert [
        f["filename"]
        for f in measured
        if f["page_count_pdfinfo"] > budgets.max_pdf_pages
    ] == []


def test_deployed_bounds_prepare_every_gold_case_in_one_run() -> None:
    """All four committed gold cases fit one preflight, pages plus crops.

    This is the acceptance clause "every gold file fits" checked through the
    same door preparation uses, so a cap that fits each file alone but not the
    set cannot pass.
    """
    budgets = from_environ(environ=_deployed_environ())
    cases = json.loads(GOLD_MANIFEST_PATH.read_text())["cases"]
    pages = [case["page_count"] for case in cases]
    assert [
        c["filename"] for c in cases if c["bytes"] > budgets.max_upload_bytes
    ] == []
    preflight_run(budgets, pages, budgets.max_vision_crop_calls_per_run)


# ----- N: the composition root reads the four bounds before it serves -----


def _stage_environ(
    monkeypatch: pytest.MonkeyPatch, deployment: Mapping[str, str | None]
) -> None:
    """``os.environ`` carrying exactly the four keys as ``deployment`` has them.

    Every key is cleared first, so a shell-exported operator variable
    cannot supply a bound a case means to omit. The
    composition root's per-process cache goes back to unread with it:
    ``http`` reads the four bounds once and keeps them, so a case that deploys
    an environ has to pin the cache too or it answers from whatever the
    previous boot saw, and the abort table below would pass or fail by file
    order instead of by its own input. ``monkeypatch.setattr`` restores the
    previous value at teardown, so these cases neither read nor leak a bound.
    """
    import fin.host.application.composition as composition_module
    import fin.host.http as http_module

    monkeypatch.setattr(
        composition_module, "_http_attachment_budgets_singleton", None, raising=False
    )
    for key in ALL_KEYS:
        monkeypatch.delenv(key, raising=False)
        value = deployment.get(key)
        if value is not None:
            monkeypatch.setenv(key, value)


def _stage_serve(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    deployment: Mapping[str, str | None],
) -> tuple[list[str], list[str]]:
    """Stage ``fin.host.http.serve()`` against one deployment env.

    ``apply_operator_settings``, the knowledge-MCP boot probe, the graph
    compile and the four workers are stubbed and ``uvicorn.run`` only
    records that it was entered, so a bound is the only thing in this
    boot that can abort. Nothing here touches a network, a model, or a
    database.

    Returns the recorders: ``served`` (uvicorn entries) and ``published``
    (probe entries).
    """
    import fin.host.application.composition as composition_module
    import fin.host.http as http_module
    import uvicorn

    _stage_environ(monkeypatch, deployment)
    served: list[str] = []
    published: list[str] = []
    # The boot refuses an undeclared durable checkpointer before it opens
    # anything, so this deployment has to state one. The compile seam
    # below is the only thing that would connect to it.
    monkeypatch.setenv("FIN_CHECKPOINT_URL", "postgresql://placeholder")
    monkeypatch.setattr(composition_module, "apply_operator_settings", lambda: None)
    monkeypatch.setattr(
        composition_module, "publish_knowledge_mcp", lambda: published.append("probe")
    )
    # The boot seam is the closeable runtime handle, not the graph-only
    # compile it delegates to, so that is what has to be stubbed here.
    monkeypatch.setattr(
        composition_module,
        "_compile_http_runtime",
        lambda **_kwargs: RuntimeHandle(None),
    )
    # The remaining workers are boot collaborators, not what a bound test
    # exercises: stubbed so a staged boot never acquires a real thread, a
    # real service or a real store. The same declared checkpoint URL would
    # otherwise send every one of them to Postgres.
    monkeypatch.setattr(composition_module, "start_application_ingest_worker", lambda: None)
    monkeypatch.setattr(composition_module, "stop_ingest_worker", lambda: None)
    monkeypatch.setattr(composition_module, "default_attachment_service", lambda: object())
    monkeypatch.setattr(composition_module, "default_thread_store", lambda: object())
    monkeypatch.setattr(composition_module, "get_workspace_root", lambda: tmp_path)
    monkeypatch.setattr(composition_module, "start_application_attachment_prep_worker", lambda _s: None)
    monkeypatch.setattr(composition_module, "stop_attachment_prep_worker", lambda: None)
    monkeypatch.setattr(composition_module, "start_application_attachment_resume_worker", lambda **_kw: None)
    monkeypatch.setattr(composition_module, "stop_attachment_resume_worker", lambda: None)
    monkeypatch.setattr(
        composition_module, "start_application_invalidation_drain_worker", lambda **_kw: None
    )
    monkeypatch.setattr(composition_module, "stop_invalidation_drain_worker", lambda: None)
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: served.append("uvicorn"))
    return served, published


@pytest.mark.parametrize("key", ALL_KEYS, ids=ALL_KEYS)
@pytest.mark.parametrize(
    "bad_value",
    [
        pytest.param(None, id="missing"),
        pytest.param("", id="blank"),
        pytest.param("   ", id="whitespace-only"),
        pytest.param("0", id="zero"),
        pytest.param("-1", id="negative"),
        pytest.param("abc", id="not-a-number"),
        pytest.param("unlimited", id="the-unbounded-word"),
        pytest.param(str(sys.maxsize + 1), id="one-past-the-finite-bound"),
    ],
)
def test_serve_aborts_boot_on_an_unusable_bound_and_never_serves(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, key: str, bad_value: str | None
) -> None:
    """Startup aborts for every bound and every unusable class.

    This is the boot-wiring half of the contract: ``serve()`` reads the four
    bounds through :func:`fin.host.attachment.attachment_budgets.from_environ`, so a
    deployment that cannot state a finite positive bound fails there instead
    of opening the upload door with no cap. The recorded lists are the actual
    promise — an exception alone would also come from a boot that had already
    bound the port — and the probe list also pins the ordering, since a bound
    that was read after publication would let a mis-deployed server announce
    itself reachable first. The abort names the offending key, which is the
    only line an operator gets in a boot log.
    """
    import fin.host.http as http_module

    served, published = _stage_serve(
        monkeypatch, tmp_path, {**VALID_ENVIRON, key: bad_value}
    )

    with pytest.raises(AttachmentBudgetConfigError) as excinfo:
        http_module.serve()

    assert served == []
    assert published == []
    assert key in str(excinfo.value)


def test_serve_aborts_boot_on_a_total_that_leaves_no_retry_reserve(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A run budget at the old exact-sum floor is a mis-deployment, not a boot.

    Every single bound is finite and positive here, so only the relationship
    between them can abort: this is the proof the reserve is enforced where a
    server actually starts, not just in a helper nobody calls.
    """
    import fin.host.http as http_module

    served, published = _stage_serve(
        monkeypatch,
        tmp_path,
        {**VALID_ENVIRON, VISION_KEY: str(MAX_PAGES + MAX_CROP_CALLS)},
    )

    with pytest.raises(AttachmentBudgetConfigError) as excinfo:
        http_module.serve()

    assert served == []
    assert published == []
    assert "max_vision_calls_per_run" in str(excinfo.value)


def test_serve_boots_and_serves_when_all_four_bounds_are_finite(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The counter-case that keeps every abort row above honest.

    A gate that raised for any env would satisfy the table beside it while no
    deployment could start, so the same door is walked from the accepted side:
    one finite bound set gets the probe, then uvicorn, exactly once.
    """
    import fin.host.http as http_module

    served, published = _stage_serve(monkeypatch, tmp_path, VALID_ENVIRON)

    http_module.serve()

    assert published == ["probe"]
    assert served == ["uvicorn"]


def test_get_attachment_budgets_reads_the_deployed_values_from_the_environ(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dependency hands the door the deployed numbers, not its own.

    Four deliberately different values are read out of ``os.environ`` in field
    order, so a composition root that baked a constant cap, or crossed two
    keys, is caught here rather than serving a bound nobody deployed.
    """
    import fin.host.http as http_module

    _stage_environ(
        monkeypatch,
        {BYTES_KEY: "2097152", PAGES_KEY: "4", VISION_KEY: "12", CROP_KEY: "4"},
    )

    budgets = http_module.get_attachment_budgets()

    assert budgets == AttachmentBudgets(
        max_upload_bytes=2_097_152,
        max_pdf_pages=4,
        max_vision_calls_per_run=12,
        max_vision_crop_calls_per_run=4,
    )
