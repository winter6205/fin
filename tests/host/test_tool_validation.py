"""P3 T2/T3 — pre-effect validation, safe feedback, bounded correction.

The canonical contract rejects bad input before any domain operation runs.
Feedback names a field path and the expected shape/constraint and never
carries a submitted value, raw kwargs, exception text, or stack trace. One
logical call earns at most one correction; a second invalid response is the
typed terminal outcome.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from fin.host.tools.contracts import UnknownToolContract
from fin.host.tools.validation import (
    CorrectionTracker,
    ToolValidationOutcome,
    validate_tool_arguments,
)

SECRET = "SUBMITTED-SECRET-9f3a"

_FORBIDDEN_FRAGMENTS = (
    SECRET,
    "Traceback",
    "ValidationError",
    "ValueError",
    "input_value",
    "kwargs",
)


def _assert_safe(feedback: str | None) -> None:
    assert feedback, "a rejected call must carry feedback"
    for fragment in _FORBIDDEN_FRAGMENTS:
        assert fragment not in feedback, (fragment, feedback)


# ----- valid input ----------------------------------------------------------


def test_valid_arguments_pass_unchanged_with_declared_defaults() -> None:
    result = validate_tool_arguments("retrieve", {"query": "net sales"})
    assert result.ok
    assert result.feedback is None
    assert result.args is not None
    assert result.args["query"] == "net sales"
    assert result.args["limit"] == 8
    assert result.args["tool_call_id"] == ""
    assert result.args["state"] is None
    assert result.args["filters"] is None


def test_valid_nested_arguments_are_normalized_to_plain_data() -> None:
    submitted = {
        "document_id": "AAPL-2023Q3",
        "version": "v1",
        "unit": 1,
    }
    result = validate_tool_arguments(
        "read_source", {"source_ref": submitted, "position": 0}
    )
    assert result.ok
    assert result.args is not None
    assert result.args["source_ref"] == submitted
    assert result.args["position"] == 0


def test_valid_filter_and_sql_window_pass() -> None:
    result = validate_tool_arguments(
        "retrieve",
        {
            "query": "sales",
            "filters": {"company": "AAPL"},
            "sql_window_start": "2024-01-01",
            "sql_window_end": "2024-12-31",
        },
    )
    assert result.ok, result.feedback


# ----- rejected input -------------------------------------------------------


def test_missing_required_field_names_the_field() -> None:
    result = validate_tool_arguments("retrieve", {})
    assert not result.ok
    assert "query" in (result.feedback or "")
    _assert_safe(result.feedback)


def test_extra_field_is_refused_without_echoing_its_value() -> None:
    result = validate_tool_arguments(
        "retrieve", {"query": "sales", "bogus": SECRET}
    )
    assert not result.ok
    assert "bogus" in (result.feedback or "")
    _assert_safe(result.feedback)


def test_wrong_type_is_refused_without_echoing_the_value() -> None:
    result = validate_tool_arguments("retrieve", {"query": 12345})
    assert not result.ok
    assert "query" in (result.feedback or "")
    assert "12345" not in (result.feedback or "")
    _assert_safe(result.feedback)


def test_empty_prohibited_locator_value_is_refused() -> None:
    result = validate_tool_arguments(
        "read_source",
        {"source_ref": {"document_id": "", "version": "v", "unit": 1}},
    )
    assert not result.ok
    assert "source_ref" in (result.feedback or "")
    _assert_safe(result.feedback)


def test_empty_plotted_target_name_is_refused() -> None:
    result = validate_tool_arguments(
        "read_visual_detail",
        {
            "attachment_id": "a",
            "page": 1,
            "chart": "",
            "series": "s",
            "category": "c",
        },
    )
    assert not result.ok
    assert "chart" in (result.feedback or "")
    _assert_safe(result.feedback)


def test_invalid_enum_in_nested_locator_is_refused() -> None:
    result = validate_tool_arguments(
        "read_source",
        {
            "source_ref": {
                "kind": "document",
                "attachment_id": SECRET,
                "version": "1",
                "unit": 1,
            }
        },
    )
    assert not result.ok
    assert "source_ref" in (result.feedback or "")
    _assert_safe(result.feedback)


def test_negative_and_zero_bounds_are_refused() -> None:
    for unit in (-3, 0):
        result = validate_tool_arguments(
            "read_source",
            {"source_ref": {"document_id": "d", "version": "v", "unit": unit}},
        )
        assert not result.ok, unit
        assert "source_ref" in (result.feedback or "")
        _assert_safe(result.feedback)


def test_at_declared_bound_passes_and_beyond_is_refused() -> None:
    at_bound = validate_tool_arguments(
        "read_source",
        {"source_ref": {"document_id": "d", "version": "v", "unit": 1}},
    )
    assert at_bound.ok, at_bound.feedback

    # The visual lane's existing 120-character target bound.
    valid = validate_tool_arguments(
        "read_visual_detail",
        {
            "attachment_id": "a",
            "page": 1,
            "chart": "c" * 120,
            "series": "s",
            "category": "c",
        },
    )
    assert valid.ok, valid.feedback

    too_long = validate_tool_arguments(
        "read_visual_detail",
        {
            "attachment_id": "a",
            "page": 1,
            "chart": SECRET + "c" * 120,
            "series": "s",
            "category": "c",
        },
    )
    assert not too_long.ok
    assert "chart" in (too_long.feedback or "")
    assert "120" in (too_long.feedback or "")
    _assert_safe(too_long.feedback)


def test_attachment_version_keeps_the_declared_pattern() -> None:
    for version in ("01", -1, True):
        result = validate_tool_arguments(
            "read_source",
            {
                "source_ref": {
                    "kind": "attachment",
                    "attachment_id": "a",
                    "version": version,
                    "unit": 1,
                }
            },
        )
        assert not result.ok, version
        assert "source_ref" in (result.feedback or "")
        _assert_safe(result.feedback)

    for version in ("0", "12", 0, 7):
        result = validate_tool_arguments(
            "read_source",
            {
                "source_ref": {
                    "kind": "attachment",
                    "attachment_id": "a",
                    "version": version,
                    "unit": 1,
                }
            },
        )
        assert result.ok, (version, result.feedback)


def test_sql_window_must_be_both_bounds_and_a_valid_day() -> None:
    for window in (
        {"sql_window_start": "2024-01-01"},
        {"sql_window_end": "2024-12-31"},
        {"sql_window_start": "01/01/2024", "sql_window_end": "2024-12-31"},
        {"sql_window_start": "2024-12-31", "sql_window_end": "2024-01-01"},
    ):
        result = validate_tool_arguments("retrieve", {"query": "x", **window})
        assert not result.ok, window
        assert "sql_window" in (result.feedback or "")
        _assert_safe(result.feedback)


def test_metadata_filter_contract_is_delegated_to_the_domain_validator() -> None:
    for filters in (
        {"not_a_key": "v"},
        {"company": 7},
        {"company": "*"},
        {"company": "x" * 257},
    ):
        result = validate_tool_arguments(
            "retrieve", {"query": "x", "filters": filters}
        )
        assert not result.ok, filters
        assert "filters" in (result.feedback or "")
        _assert_safe(result.feedback)

    # A whitespace-only value is omitted by the domain normalizer, not refused.
    result = validate_tool_arguments(
        "retrieve", {"query": "x", "filters": {"company": "   "}}
    )
    assert result.ok, result.feedback


def test_visual_scope_shape_is_enforced() -> None:
    for scope in ([0.0, 0.0, 1.0], [0.9, 0.1, 0.1, 0.9], [-0.1, 0.0, 1.0, 1.0]):
        result = validate_tool_arguments(
            "read_visual_detail",
            {
                "attachment_id": "a",
                "page": 1,
                "chart": "c",
                "series": "s",
                "category": "c",
                "scope": scope,
            },
        )
        assert not result.ok, scope
        assert "scope" in (result.feedback or "")
        _assert_safe(result.feedback)


def test_position_is_a_recorded_domain_check_not_a_schema_bound() -> None:
    # The current model-facing schema has no minimum on ``position``; the
    # domain rejects a negative cursor with the typed invalid_position dict.
    result = validate_tool_arguments(
        "read_source",
        {
            "source_ref": {"document_id": "d", "version": "v", "unit": 1},
            "position": -1,
        },
    )
    assert result.ok


def test_unknown_tool_name_is_a_wiring_error_not_feedback() -> None:
    with pytest.raises(UnknownToolContract):
        validate_tool_arguments("no_such_tool", {})


# ----- bounded correction ---------------------------------------------------


def test_one_correction_then_terminal_exhaustion() -> None:
    tracker = CorrectionTracker("thread-1", "run-1")
    first = tracker.begin_model_attempt(
        [{"name": "retrieve", "id": "call-1", "args": {}}]
    )
    logical = first["call-1"]

    rejected = tracker.record_validation(
        "retrieve", {"query": 1}, tool_call_id="call-1"
    )
    assert rejected.outcome == "correction_offered"
    assert rejected.schema_rejected is True
    assert rejected.correction_offered is True
    assert rejected.logical_call_id == logical
    _assert_safe(rejected.feedback)

    # The corrected model attempt carries a NEW provider id; it is still the
    # SAME logical call.
    second = tracker.begin_model_attempt(
        [{"name": "retrieve", "id": "call-2", "args": {}}]
    )
    assert second["call-2"] == logical
    assert tracker.logical_call_for("call-2") == logical

    exhausted = tracker.record_validation(
        "retrieve", {"query": 2}, tool_call_id="call-2"
    )
    assert exhausted.outcome == "tool_validation_exhausted"
    assert exhausted.schema_rejected is True
    assert exhausted.correction_offered is False
    assert exhausted.feedback is None
    assert exhausted.logical_call_id == logical


def test_provider_tool_call_id_alone_is_not_logical_identity() -> None:
    tracker = CorrectionTracker("thread-1", "run-1")
    tracker.begin_model_attempt([{"name": "read_source", "id": "a", "args": {}}])
    tracker.record_validation(
        "read_source",
        {"source_ref": {"document_id": "", "version": "v", "unit": 1}},
        tool_call_id="a",
    )
    # One model turn emits the corrected attempt plus an unrelated sibling
    # call of the same tool. Only the first is the correction.
    tracker.begin_model_attempt(
        [
            {"name": "read_source", "id": "b", "args": {}},
            {"name": "read_source", "id": "c", "args": {}},
        ]
    )

    assert tracker.logical_call_for("a") == tracker.logical_call_for("b")
    assert tracker.logical_call_for("c") != tracker.logical_call_for("b")


def test_a_valid_correction_is_accepted_on_the_same_logical_call() -> None:
    tracker = CorrectionTracker("thread-1", "run-1")
    tracker.begin_model_attempt([{"name": "retrieve", "id": "call-1", "args": {}}])
    tracker.record_validation(
        "retrieve", {"query": ""}, tool_call_id="call-1"
    )
    tracker.begin_model_attempt([{"name": "retrieve", "id": "call-2", "args": {}}])
    accepted = tracker.record_validation(
        "retrieve", {"query": "sales"}, tool_call_id="call-2"
    )
    assert accepted.outcome == "valid_input_accepted"
    assert accepted.accepted is True
    assert accepted.normalized_args is not None
    assert accepted.normalized_args["query"] == "sales"


def test_a_different_tool_name_is_not_correlated() -> None:
    tracker = CorrectionTracker("thread-1", "run-1")
    tracker.begin_model_attempt([{"name": "retrieve", "id": "call-1", "args": {}}])
    tracker.record_validation("retrieve", {"query": 1}, tool_call_id="call-1")
    tracker.begin_model_attempt([{"name": "list_files", "id": "call-2", "args": {}}])
    assert tracker.logical_call_for("call-2") != tracker.logical_call_for("call-1")


def test_an_exhausted_call_never_absorbs_a_later_attempt() -> None:
    """A NEW invalid call after an exhausted one gets its own correction."""
    tracker = CorrectionTracker("thread-1", "run-1")
    tracker.begin_model_attempt([{"name": "retrieve", "id": "c1", "args": {}}])
    tracker.record_validation("retrieve", {"query": 1}, tool_call_id="c1")
    tracker.begin_model_attempt([{"name": "retrieve", "id": "c2", "args": {}}])
    terminal = tracker.record_validation(
        "retrieve", {"query": 2}, tool_call_id="c2"
    )
    assert terminal.outcome == "tool_validation_exhausted"

    tracker.begin_model_attempt([{"name": "retrieve", "id": "c3", "args": {}}])
    third = tracker.record_validation(
        "retrieve", {"query": 3}, tool_call_id="c3"
    )
    # The exhausted call is terminal and stays that way: the later attempt
    # is a NEW logical call with its own bounded correction.
    assert third.outcome == "correction_offered"
    assert third.logical_call_id != terminal.logical_call_id
    _assert_safe(third.feedback)


def test_an_accepted_call_never_absorbs_a_later_attempt() -> None:
    tracker = CorrectionTracker("thread-1", "run-1")
    tracker.begin_model_attempt([{"name": "retrieve", "id": "c1", "args": {}}])
    tracker.record_validation("retrieve", {"query": 1}, tool_call_id="c1")
    tracker.begin_model_attempt([{"name": "retrieve", "id": "c2", "args": {}}])
    accepted = tracker.record_validation(
        "retrieve", {"query": "sales"}, tool_call_id="c2"
    )
    assert accepted.accepted is True

    tracker.begin_model_attempt([{"name": "retrieve", "id": "c3", "args": {}}])
    third = tracker.record_validation(
        "retrieve", {"query": 3}, tool_call_id="c3"
    )
    assert third.outcome == "correction_offered"
    assert third.logical_call_id != accepted.logical_call_id


def test_a_new_turn_resets_the_correction_budget() -> None:
    """A second question on one thread does not inherit the exhaustion."""
    tracker = CorrectionTracker("thread-1", "run-1")
    tracker.begin_model_attempt(
        [{"name": "retrieve", "id": "q1-a1", "args": {}}], turn_id="turn-1"
    )
    tracker.record_validation("retrieve", {"query": 1}, tool_call_id="q1-a1")
    tracker.begin_model_attempt(
        [{"name": "retrieve", "id": "q1-a2", "args": {}}], turn_id="turn-1"
    )
    exhausted = tracker.record_validation(
        "retrieve", {"query": 2}, tool_call_id="q1-a2"
    )
    assert exhausted.outcome == "tool_validation_exhausted"

    tracker.begin_model_attempt(
        [{"name": "retrieve", "id": "q2-a1", "args": {}}], turn_id="turn-2"
    )
    first_of_turn_two = tracker.record_validation(
        "retrieve", {"query": 3}, tool_call_id="q2-a1"
    )
    assert first_of_turn_two.outcome == "correction_offered"
    # A different logical call, so the ledger and the metrics never key two
    # turns' facts under one id.
    assert first_of_turn_two.logical_call_id != exhausted.logical_call_id
    _assert_safe(first_of_turn_two.feedback)


def test_a_hydrated_tracker_keeps_the_one_correction_bound() -> None:
    """A takeover or an eviction resumes the same budget, not a new one."""
    tracker = CorrectionTracker("thread-1", "run-1")
    tracker.hydrate(
        [
            _fact("retrieve", "retrieve#1", "attempt-1", "schema_rejected", 1),
            _fact("retrieve", "retrieve#1", "attempt-1", "correction_offered", 1),
        ]
    )

    # The corrected attempt (a NEW provider id) still correlates to the
    # call that already spent its correction.
    bindings = tracker.begin_model_attempt(
        [{"name": "retrieve", "id": "attempt-2", "args": {}}], turn_id="turn-1"
    )
    assert bindings["attempt-2"] == "retrieve#1"
    terminal = tracker.record_validation(
        "retrieve", {"query": 2}, tool_call_id="attempt-2"
    )
    assert terminal.outcome == "tool_validation_exhausted"
    assert terminal.feedback is None


def test_a_hydrated_call_that_was_already_exhausted_stays_terminal() -> None:
    tracker = CorrectionTracker("thread-1", "run-1")
    tracker.hydrate(
        [
            _fact("retrieve", "retrieve#1", "attempt-1", "schema_rejected", 1),
            _fact("retrieve", "retrieve#1", "attempt-1", "correction_offered", 1),
            _fact("retrieve", "retrieve#1", "attempt-2", "schema_rejected", 2),
            _fact(
                "retrieve", "retrieve#1", "attempt-2", "correction_exhausted", 2
            ),
        ]
    )

    tracker.begin_model_attempt(
        [{"name": "retrieve", "id": "attempt-3", "args": {}}], turn_id="turn-1"
    )
    assert tracker.logical_call_for("attempt-1") == "retrieve#1"
    assert tracker.logical_call_for("attempt-2") == "retrieve#1"
    # A NEW attempt of the same tool is a new logical call: the exhausted
    # one is terminal and cannot answer for it.
    assert tracker.logical_call_for("attempt-3") != "retrieve#1"


def test_hydration_continues_the_logical_call_numbering() -> None:
    tracker = CorrectionTracker("thread-1", "run-1")
    tracker.hydrate(
        [_fact("retrieve", "retrieve#7", "attempt-7", "schema_rejected", 1)]
    )
    bindings = tracker.begin_model_attempt(
        [{"name": "retrieve", "id": "attempt-8", "args": {}}], turn_id="turn-1"
    )
    assert bindings["attempt-8"] == "retrieve#8"


def test_execution_attempts_count_dispatches_not_model_attempts() -> None:
    tracker = CorrectionTracker("thread-1", "run-1")
    tracker.begin_model_attempt([{"name": "retrieve", "id": "c1", "args": {}}])
    outcome = tracker.record_validation(
        "retrieve", {"query": "sales"}, tool_call_id="c1"
    )
    assert tracker.next_execution_attempt(outcome.logical_call_id) == 1
    assert tracker.next_execution_attempt(outcome.logical_call_id) == 2
    assert outcome.model_attempt == 1


def test_hydration_restores_the_execution_attempt_counter() -> None:
    """A takeover dispatches past the attempts the previous owner committed."""
    tracker = CorrectionTracker("thread-1", "run-1")
    tracker.hydrate(
        [
            _fact(
                "retrieve",
                "retrieve#1",
                "attempt-1",
                "valid_input_accepted",
                1,
                execution_attempt=None,
            ),
            _fact(
                "retrieve",
                "retrieve#1",
                "attempt-1",
                "execution_started",
                1,
                execution_attempt=1,
            ),
            _fact(
                "retrieve",
                "retrieve#1",
                "attempt-1",
                "succeeded",
                1,
                execution_attempt=1,
            ),
            _fact(
                "retrieve",
                "retrieve#1",
                "attempt-2",
                "execution_started",
                2,
                execution_attempt=2,
            ),
            _fact(
                "retrieve",
                "retrieve#1",
                "attempt-2",
                "succeeded",
                2,
                execution_attempt=2,
            ),
        ]
    )

    assert tracker.next_execution_attempt("retrieve#1") == 3


def test_hydration_without_committed_execution_attempts_starts_at_one() -> None:
    tracker = CorrectionTracker("thread-1", "run-1")
    tracker.hydrate(
        [
            _fact("retrieve", "retrieve#1", "attempt-1", "schema_rejected", 1),
            _fact("retrieve", "retrieve#1", "attempt-1", "correction_offered", 1),
        ]
    )

    assert tracker.next_execution_attempt("retrieve#1") == 1


def test_hydrated_execution_attempt_counters_are_independent() -> None:
    tracker = CorrectionTracker("thread-1", "run-1")
    tracker.hydrate(
        [
            _fact(
                "retrieve",
                "retrieve#1",
                "attempt-1",
                "succeeded",
                1,
                execution_attempt=2,
            ),
            _fact(
                "retrieve",
                "retrieve#2",
                "attempt-2",
                "succeeded",
                2,
                execution_attempt=5,
            ),
        ]
    )

    assert tracker.next_execution_attempt("retrieve#1") == 3
    assert tracker.next_execution_attempt("retrieve#2") == 6
    # Allocating on one logical call never advances another.
    assert tracker.next_execution_attempt("retrieve#1") == 4


def test_trackers_are_isolated_per_run() -> None:
    first = CorrectionTracker("thread-1", "run-1")
    second = CorrectionTracker("thread-1", "run-2")
    first.begin_model_attempt([{"name": "retrieve", "id": "x", "args": {}}])
    assert second.logical_call_for("x") is None


def test_correction_tracker_is_thread_safe() -> None:
    tracker = CorrectionTracker("thread-1", "run-1")
    names = [
        "list_files",
        "retrieve",
        "get_xbrl_fact",
        "compare_facts",
        "redline",
        "extract_tables",
        "three_year_statement",
        "read_source",
    ]

    def attempt(index: int) -> str:
        name = names[index]
        bindings = tracker.begin_model_attempt(
            [{"name": name, "id": f"call-{index}", "args": {}}]
        )
        logical = bindings[f"call-{index}"]
        outcome: ToolValidationOutcome = tracker.record_validation(
            name,
            _valid_args(name),
            tool_call_id=f"call-{index}",
        )
        assert outcome.outcome == "valid_input_accepted", outcome
        return logical

    with ThreadPoolExecutor(max_workers=len(names)) as pool:
        logical_ids = list(pool.map(attempt, range(len(names))))

    assert len(set(logical_ids)) == len(names)


class _Fact:
    """One committed ledger fact, in the shape the tracker rebuilds from."""

    def __init__(
        self,
        tool_name: str,
        logical_call_id: str,
        provider_tool_call_id: str | None,
        kind: str,
        model_attempt: int,
        execution_attempt: int | None = None,
    ) -> None:
        self.tool_name = tool_name
        self.logical_call_id = logical_call_id
        self.provider_tool_call_id = provider_tool_call_id
        self.kind = kind
        self.model_attempt = model_attempt
        self.execution_attempt = execution_attempt


def _fact(
    tool_name: str,
    logical_call_id: str,
    provider_tool_call_id: str | None,
    kind: str,
    model_attempt: int,
    execution_attempt: int | None = None,
) -> _Fact:
    return _Fact(
        tool_name,
        logical_call_id,
        provider_tool_call_id,
        kind,
        model_attempt,
        execution_attempt,
    )


def _valid_args(name: str) -> dict[str, Any]:
    return {
        "list_files": {},
        "retrieve": {"query": "x"},
        "get_xbrl_fact": {"concept": "us-gaap:Revenues", "period": "2023"},
        "compare_facts": {"concept": "us-gaap:Revenues", "periods": ["2023"]},
        "redline": {
            "before_document_id": "a",
            "before_version": "v",
            "after_document_id": "b",
            "after_version": "v",
        },
        "extract_tables": {"document_id": "a", "version": "v"},
        "three_year_statement": {
            "issuer": "AAPL",
            "periods": ["2021", "2022", "2023"],
            "concepts": ["us-gaap:Revenues"],
        },
        "read_source": {
            "source_ref": {"document_id": "a", "version": "v", "unit": 1}
        },
    }[name]
