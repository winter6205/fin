"""``fin.host.attachment_budgets`` — the finite attachment budget contract.

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
* the module holds integers only — no provider or model identifier, since
  hardcoding one is out of contract.

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

from fin.host.attachment_budgets import (
    ATTACHMENT_PAGE_LIMIT_EXCEEDED,
    ATTACHMENT_VISION_BUDGET_EXCEEDED,
    AttachmentBudgetConfigError,
    AttachmentBudgetError,
    AttachmentBudgets,
    AttachmentPageLimitExceeded,
    AttachmentVisionBudgetExceeded,
    from_environ,
    preflight_run,
    validate,
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
# ``max_vision_calls_per_run`` sits exactly at ``pages + crop``, so the
# accepted side of the relationship (equality is the minimum) is what every
# non-relationship case runs against.
MAX_BYTES = 1_048_576
MAX_PAGES = 8
MAX_VISION_CALLS = 16
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
    equality point of the run budget, and two attachments summing to the
    same total with no crops reaches that boundary from the other side. One
    call past that same total is refused, so the acceptance is a real bound
    and not an unchecked pass-through.
    """
    budgets = _load()
    preflight_run(budgets, [MAX_PAGES], MAX_CROP_CALLS)
    preflight_run(budgets, [MAX_PAGES, MAX_PAGES], 0)
    validate(budgets)
    with pytest.raises(AttachmentVisionBudgetExceeded):
        preflight_run(budgets, [MAX_PAGES, MAX_PAGES], 1)


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
        (VISION_KEY, " 16 ", 16),
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
        (8, 8, 16, True),
        (8, 8, 17, True),
        (8, 1, 9, True),
        (40, 20, 60, True),
        (8, 8, 15, False),
        (8, 1, 8, False),
        (40, 20, 59, False),
    ],
    ids=[
        "equality_is_the_minimum",
        "one_spare_call",
        "minimal_headroom_equality",
        "large_equality",
        "one_short",
        "one_crop_unbudgeted",
        "large_one_short",
    ],
)
def test_run_budget_must_cover_pages_plus_crop_headroom(
    pages: int, crop: int, vision: int, admissible: bool
) -> None:
    """The deployment is self-consistent before any run exists.

    One full-page scan per admitted page plus the measured crop/retry
    headroom is the minimum a run budget must cover, and it must hold before
    a run starts; an
    under-budgeted config could only ever fail mid-run, which is what this
    check prevents. The inadmissible rows are probed with a trivially small
    run request, so the relationship is the only thing that can fail —
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
        preflight_run(budgets, admitted, 1)
    assert admitted == [MAX_PAGES, MAX_PAGES]
    assert not isinstance(excinfo.value, AttachmentPageLimitExceeded)
    assert excinfo.value.code == ATTACHMENT_VISION_BUDGET_EXCEEDED


def test_run_budget_is_accounted_across_every_eligible_attachment() -> None:
    """Under the page cap per file is not enough; the run sums (spec:57).

    Three eligible attachments of five pages each fit the eight-page cap
    individually, and with the crop headroom the run needs 23 of 16 calls.
    The same pages admitted for a single attachment stay admissible, which
    is the per-file versus per-run distinction.
    """
    budgets = _load()
    preflight_run(budgets, [5], MAX_CROP_CALLS)
    with pytest.raises(AttachmentVisionBudgetExceeded):
        preflight_run(budgets, [5, 5, 5], MAX_CROP_CALLS)


def test_crop_calls_consume_the_same_run_budget_as_full_page_scans() -> None:
    """Crops are headroom inside one budget, not a second counter (spec:57).

    Ten page scans plus seven crop calls is exactly one over the 16-call
    budget, while seven crops would fit a separate eight-crop allowance: a
    parallel counter would wrongly admit this run. Six crops is the accepted
    neighbour.
    """
    budgets = _load()
    preflight_run(budgets, [5, 5], MAX_CROP_CALLS - 2)
    with pytest.raises(AttachmentVisionBudgetExceeded):
        preflight_run(budgets, [5, 5], MAX_CROP_CALLS - 1)


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
        preflight_run(budgets, [MAX_PAGES, MAX_PAGES], 1)
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


# ----- the deployed bounds, bound to their measurement evidence -----
PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_EXAMPLE_PATH = PROJECT_ROOT / ".env.example"
CORPUS_DISTRIBUTION_PATH = (
    PROJECT_ROOT / "data" / "corpus" / "g4_upload_distribution.json"
)
GOLD_MANIFEST_PATH = (
    PROJECT_ROOT / "data" / "corpus" / "g4_attachment_gold" / "manifest.json"
)
CORPUS_PDF_DIR = PROJECT_ROOT / "data" / "corpus" / "pdf"


def _deployed_environ() -> dict[str, str]:
    """The four bounds as ``.env.example`` ships them.

    The numbers live in the template and in the two measured data files only;
    repeating them here would let a stale test value bless a config that no
    longer matches what was measured.
    """
    deployed: dict[str, str] = {}
    for raw in ENV_EXAMPLE_PATH.read_text().splitlines():
        key, separator, value = raw.partition("=")
        key = key.strip()
        if separator and not key.startswith("#") and key in ALL_KEYS:
            deployed[key] = value.strip()
    unconfigured = sorted(set(ALL_KEYS) - set(deployed))
    assert unconfigured == [], f".env.example must configure {unconfigured}"
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


def test_gold_manifest_describes_the_committed_fixtures() -> None:
    """The three gold cases the caps must fit are the bytes on disk.

    The manifest is also the file that states each case's page count, which is
    what the single-run preflight below charges against the vision budget.
    """
    cases = json.loads(GOLD_MANIFEST_PATH.read_text())["cases"]
    assert len(cases) == 3
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
    """All three committed gold cases fit one preflight, pages plus crops.

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
