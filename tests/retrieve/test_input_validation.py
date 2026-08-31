"""The invalid-input / legal-empty dichotomy (T21, SC42 / SC43, §B2 第 5 条).

Four invalid-input classes each answer with a named typed error, are
counted as an input-validation failure, and leave ``degraded`` unset:

* empty document — the corpus parse path's ``empty_source`` / ``no_units``
  and the push seam's ``push_empty_document``;
* empty query — the retrieve port;
* empty block batch — the push seam (T16), folded into one classification;
* conflicting filters — mutually exclusive conditions, proven from the
  bindings fin holds.

Two legal-empty classes answer with a success and an empty list, do not
raise, and leave ``degraded`` unset:

* a valid query with no hits;
* an unknown (syntactically fine) filter value.

Every case runs through ``observe_input_outcome`` so the two sides are
compared by one judgement rather than by two unrelated shapes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fin.corpus.errors import (
    EmptySourceParseError,
    MalformedSourceParseError,
    NoUnitsParseError,
)
from fin.corpus.extracted import Locator
from fin.retrieve.degradation import (
    DEGRADED_FIELD,
    DEGRADED_REASONS_FIELD,
    NOT_DEGRADED,
)
from fin.retrieve.fake_port import FakeRetrievePort
from fin.retrieve.fake_push_port import FakePushPort
from fin.retrieve.input_validation import (
    INPUT_CLASS_CONFLICTING_FILTERS,
    INPUT_CLASS_EMPTY_BATCH,
    INPUT_CLASS_EMPTY_DOCUMENT,
    INPUT_CLASS_EMPTY_QUERY,
    INPUT_VALIDATION_CLASSES,
    INPUT_VALIDATION_LOG_ENV,
    OUTCOME_INVALID_INPUT,
    OUTCOME_LEGAL_EMPTY,
    ConflictingFiltersError,
    EmptyQueryError,
    InputValidationLedger,
    InputValidationLedgerCorruptError,
    InputValidationLedgerUnwritableError,
    InvalidInputMarkedDegradedError,
    UnclassifiedInputFailureError,
    classify_input_failure,
    classify_input_failure_code,
    codes_for_input_class,
    degradation_signal_of_failure,
    detect_filter_conflict,
    input_validation_codes,
    input_validation_ledger_from_env,
    is_empty_result,
    observe_input_outcome,
    record_input_failure,
)
from fin.retrieve.mcp_adapter import MCPRetrieveAdapter
from fin.retrieve.port import Chunk
from fin.retrieve.push_port import (
    PushEmptyBatchError,
    PushEmptyDocumentError,
    push_records,
)


# ----- fixtures / helpers -----

AAPL_CIK = "0000320193"
MSFT_CIK = "0000789019"

MANIFEST_DOCS: dict[str, dict[str, object]] = {
    "DOC-2023Q1": {
        "cik": AAPL_CIK,
        "ticker": "AAPL",
        "form": "10-Q",
        "filing_date": "2023-02-03",
    },
    "DOC-2023Q2": {
        "cik": MSFT_CIK,
        "ticker": "MSFT",
        "form": "10-Q",
        "filing_date": "2023-04-25",
    },
}


def _locator(document_id: str = "DOC-2023Q1") -> Locator:
    return Locator(document_id=document_id, version="v-" + "a" * 60, page=1)


def _chunk(text: str = "revenue rose") -> Chunk:
    return Chunk(text=text, source_ref=_locator(), score=1.0)


def _port() -> FakeRetrievePort:
    """A fake port wired with bindings, so conflicts are provable."""
    return FakeRetrievePort(
        hits_by_query={"revenue": [_chunk()]},
        known_document_ids={"DOC-2023Q1", "DOC-2023Q2"},
        manifest_docs=MANIFEST_DOCS,
    )


def _wire_record(block_id: str = "b-1", text: str = "revenue rose") -> dict:
    return {
        "block_id": block_id,
        "source_ref": '{"document_id":"DOC-2023Q1"}',
        "content_id": "c-" + block_id,
        "idempotency_key": ['{"document_id":"DOC-2023Q1"}', "c-" + block_id],
        "text": text,
        "parent_block_id": "p-1",
        "parent_text": "revenue rose sharply",
        "metadata": {
            "fmt": "pdf",
            "doc_type": "narrative",
            "section": "MD&A",
            "source_sha256": "s" * 64,
            "parse_rule_version": "parse-1",
            "params_table_version": "params-1",
        },
    }


class _PushRecordLike:
    """The minimal ``PushRecord`` surface ``push_records`` consumes."""

    def __init__(self, wire: dict) -> None:
        self._wire = wire

    def to_wire(self) -> dict:
        return dict(self._wire)


# ===== invalid input: four classes =====


def test_empty_document_is_a_named_failure_counted_and_not_degraded() -> None:
    """Class 1 (parse side): a document with no parseable content is
    invalid input, not a unit that simply produced nothing."""
    ledger = InputValidationLedger()

    outcome = observe_input_outcome(
        lambda: (_ for _ in ()).throw(
            EmptySourceParseError("source bytes are empty")
        ),
        ledger=ledger,
    )

    assert outcome.kind == OUTCOME_INVALID_INPUT
    assert outcome.input_class == INPUT_CLASS_EMPTY_DOCUMENT
    assert outcome.code == "empty_source"
    assert outcome.degraded is False
    assert ledger.count_by_class() == {INPUT_CLASS_EMPTY_DOCUMENT: 1}


def test_empty_document_on_the_push_seam_is_the_same_class() -> None:
    """Class 1 (push side): a pushed record with no text is refused by
    name, and lands in the same class as the parse-side codes — one
    dichotomy, not two."""
    port = FakePushPort()
    ledger = InputValidationLedger()

    outcome = observe_input_outcome(
        lambda: port.push([_wire_record(text="   ")]),
        ledger=ledger,
    )

    assert outcome.kind == OUTCOME_INVALID_INPUT
    assert outcome.input_class == INPUT_CLASS_EMPTY_DOCUMENT
    assert outcome.code == "push_empty_document"
    assert outcome.degraded is False
    assert port.written_total() == 0, "a refused record must not be written"
    assert ledger.count() == 1


def test_empty_query_is_a_named_failure_counted_and_not_degraded() -> None:
    """Class 2: an empty query is invalid input, NOT ``{"chunks": []}``."""
    port = _port()

    for empty_query in ("", "   ", "\t\n", "\n"):
        ledger = InputValidationLedger()
        outcome = observe_input_outcome(
            lambda q=empty_query: port.retrieve(q), ledger=ledger
        )

        assert outcome.kind == OUTCOME_INVALID_INPUT, empty_query
        assert outcome.input_class == INPUT_CLASS_EMPTY_QUERY
        assert outcome.code == "empty_query"
        assert outcome.degraded is False
        assert ledger.count_by_code() == {"empty_query": 1}


def test_empty_block_batch_is_a_named_failure_counted_and_not_degraded() -> None:
    """Class 3: T16's ``push_empty_batch``, now inside the shared
    classification and count."""
    ledger = InputValidationLedger()

    outcome = observe_input_outcome(
        lambda: push_records(
            FakePushPort(), [], input_validation_ledger=None
        ),
        ledger=ledger,
    )

    assert outcome.kind == OUTCOME_INVALID_INPUT
    assert outcome.input_class == INPUT_CLASS_EMPTY_BATCH
    assert outcome.code == "push_empty_batch"
    assert outcome.degraded is False
    assert ledger.count() == 1


def test_conflicting_filters_are_a_named_failure_counted_and_not_degraded() -> None:
    """Class 4: AAPL's ticker with MSFT's cik — no document can satisfy
    both, so it is invalid input rather than an empty hit list."""
    port = _port()
    ledger = InputValidationLedger()

    outcome = observe_input_outcome(
        lambda: port.retrieve("revenue", ticker="AAPL", cik=MSFT_CIK),
        ledger=ledger,
    )

    assert outcome.kind == OUTCOME_INVALID_INPUT
    assert outcome.input_class == INPUT_CLASS_CONFLICTING_FILTERS
    assert outcome.code == "conflicting_filters"
    assert outcome.degraded is False
    assert ledger.count_by_class() == {INPUT_CLASS_CONFLICTING_FILTERS: 1}


def test_document_id_bound_metadata_conflict_is_named() -> None:
    """The other provable exclusion: a known ``document_id`` pins one
    row, so a filter naming a different value for that row conflicts."""
    port = _port()
    ledger = InputValidationLedger()

    outcome = observe_input_outcome(
        lambda: port.retrieve(
            "revenue", document_id="DOC-2023Q1", cik=MSFT_CIK
        ),
        ledger=ledger,
    )

    assert outcome.kind == OUTCOME_INVALID_INPUT
    assert outcome.input_class == INPUT_CLASS_CONFLICTING_FILTERS


def test_every_invalid_input_class_is_covered_by_the_four_cases() -> None:
    """SC42's count over the four classes: none of them is missing a
    named code, and no fifth class sneaked into the vocabulary."""
    assert set(INPUT_VALIDATION_CLASSES) == {
        INPUT_CLASS_EMPTY_DOCUMENT,
        INPUT_CLASS_EMPTY_QUERY,
        INPUT_CLASS_EMPTY_BATCH,
        INPUT_CLASS_CONFLICTING_FILTERS,
    }
    for input_class in INPUT_VALIDATION_CLASSES:
        assert codes_for_input_class(input_class), input_class


@pytest.mark.parametrize("code", ["push_empty_batch", "push_empty_document"])
def test_push_codes_in_the_registry_match_the_error_classes(code: str) -> None:
    """The registry spells the two push codes as strings (the push module
    imports this one); this pins them against the classes so the pair
    cannot drift apart unnoticed."""
    published = {
        PushEmptyBatchError.code,
        PushEmptyDocumentError.code,
    }
    assert code in published
    assert code in input_validation_codes()


# ===== legal empty results: two classes =====


def test_valid_query_without_hits_is_a_legal_empty_result() -> None:
    """Class 1: the input was fine and the capability ran — an empty
    list, no exception, no ``degraded``."""
    port = _port()
    ledger = InputValidationLedger()

    outcome = observe_input_outcome(
        lambda: port.retrieve("nothing matches this"), ledger=ledger
    )

    assert outcome.kind == OUTCOME_LEGAL_EMPTY
    assert outcome.result == {"chunks": []}
    assert outcome.degraded is False
    assert ledger.count() == 0, "a legal empty result is not a failure"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("cik", "9999999999"),
        ("ticker", "ZZZZ"),
        ("form", "10-K"),
        ("filing_date", "1999-01-01"),
        ("document_id", "DOC-9999Q9"),
    ],
)
def test_unknown_filter_value_is_a_legal_empty_result(
    field: str, value: str
) -> None:
    """Class 2: syntactically valid, simply matches nothing (SC43). Not a
    conflict, not counted, not degraded."""
    port = _port()
    ledger = InputValidationLedger()

    outcome = observe_input_outcome(
        lambda: port.retrieve("revenue", **{field: value}), ledger=ledger
    )

    assert outcome.kind == OUTCOME_LEGAL_EMPTY
    assert outcome.degraded is False
    assert ledger.count() == 0


def test_a_matching_filter_still_returns_hits() -> None:
    """The control: the filter machinery did not become a blanket refusal."""
    port = _port()
    ledger = InputValidationLedger()

    outcome = observe_input_outcome(
        lambda: port.retrieve("revenue", ticker="AAPL", cik=AAPL_CIK),
        ledger=ledger,
    )

    assert outcome.kind == "hits"
    assert ledger.count() == 0


def test_without_bindings_a_conflicting_looking_pair_is_not_invented() -> None:
    """No ``manifest_docs`` means fin holds no binding to contradict, so
    the pair is not a *proven* conflict — the receiving side names it.
    Pinned so the boundary is deliberate rather than accidental."""
    port = FakeRetrievePort(hits_by_query={"revenue": [_chunk()]})
    ledger = InputValidationLedger()

    outcome = observe_input_outcome(
        lambda: port.retrieve("revenue", ticker="AAPL", cik=MSFT_CIK),
        ledger=ledger,
    )

    assert outcome.kind != OUTCOME_INVALID_INPUT
    assert ledger.count() == 0


# ===== the two sides are not conflated =====


def test_invalid_input_and_legal_empty_land_on_different_kinds() -> None:
    """SC42 / SC43's 不混同, as one direct comparison: the same judgement
    over the same port, one call per side of the split."""
    port = _port()
    ledger = InputValidationLedger()

    invalid = observe_input_outcome(lambda: port.retrieve(""), ledger=ledger)
    legal = observe_input_outcome(
        lambda: port.retrieve("nothing matches this"), ledger=ledger
    )

    assert invalid.kind == OUTCOME_INVALID_INPUT
    assert legal.kind == OUTCOME_LEGAL_EMPTY
    assert invalid.kind != legal.kind
    assert invalid.counted is True and legal.counted is False
    assert ledger.count() == 1, "only the invalid side is counted"
    assert invalid.degraded is False and legal.degraded is False


def test_conflicting_and_unknown_filters_land_on_different_kinds() -> None:
    """SC43's own pair: a conflict is a failure, an unknown value is an
    empty result, and they differ on the same port with the same query."""
    port = _port()
    ledger = InputValidationLedger()

    conflicting = observe_input_outcome(
        lambda: port.retrieve("revenue", ticker="AAPL", cik=MSFT_CIK),
        ledger=ledger,
    )
    unknown = observe_input_outcome(
        lambda: port.retrieve("revenue", ticker="ZZZZ"), ledger=ledger
    )

    assert conflicting.kind == OUTCOME_INVALID_INPUT
    assert unknown.kind == OUTCOME_LEGAL_EMPTY
    assert ledger.count_by_class() == {INPUT_CLASS_CONFLICTING_FILTERS: 1}


def test_an_empty_result_list_is_never_read_off_an_error_envelope() -> None:
    """The conflation this ticket forbids, at the shape level: a named
    failure envelope is not an empty result even though it has no hits."""
    assert is_empty_result({"chunks": []}) is True
    assert is_empty_result({"error": "empty_query"}) is False
    assert is_empty_result({"error": "empty_query", "chunks": []}) is False
    assert is_empty_result({"chunks": [_chunk()]}) is False


def test_error_envelope_reuses_strict_input_failure_validation() -> None:
    """An invalid-input envelope cannot claim a capability degradation."""
    ledger = InputValidationLedger()

    with pytest.raises(InvalidInputMarkedDegradedError):
        observe_input_outcome(
            lambda: {
                "error": "empty_query",
                DEGRADED_FIELD: True,
                DEGRADED_REASONS_FIELD: ["retrieve:attempt_failed"],
            },
            ledger=ledger,
        )

    assert ledger.count() == 0


# ===== the count itself =====


def test_the_count_survives_the_producing_process(tmp_path: Path) -> None:
    """A persisted ledger replays, so ``count() > 0`` is assertable later."""
    path = tmp_path / "input-validation.jsonl"
    first = InputValidationLedger(path=path)
    record_input_failure(EmptyQueryError("blank"), ledger=first)
    record_input_failure(PushEmptyBatchError("no records"), ledger=first)

    reloaded = InputValidationLedger(path=path)

    assert reloaded.count() == 2
    assert reloaded.count_by_class() == {
        INPUT_CLASS_EMPTY_QUERY: 1,
        INPUT_CLASS_EMPTY_BATCH: 1,
    }


def test_an_unwritable_ledger_is_a_named_failure(tmp_path: Path) -> None:
    """Counting in memory while the operator's log stays empty would be a
    count nobody can read — refused by name instead."""
    blocked = tmp_path / "file"
    blocked.write_text("not a directory")
    ledger = InputValidationLedger(path=blocked / "log.jsonl")

    with pytest.raises(InputValidationLedgerUnwritableError):
        record_input_failure(EmptyQueryError("blank"), ledger=ledger)

    assert ledger.count() == 0, "a failed append must not leave a count"


def test_a_corrupt_ledger_line_is_a_named_failure(tmp_path: Path) -> None:
    """Skipping an unreplayable line would silently lower the count."""
    path = tmp_path / "input-validation.jsonl"
    path.write_text("{not json}\n")

    with pytest.raises(InputValidationLedgerCorruptError):
        InputValidationLedger(path=path)


def test_the_ledger_path_comes_from_the_environment(
    tmp_path: Path,
) -> None:
    path = tmp_path / "log.jsonl"

    assert input_validation_ledger_from_env({}).path is None
    assert input_validation_ledger_from_env(
        {INPUT_VALIDATION_LOG_ENV: "  "}
    ).path is None
    assert input_validation_ledger_from_env(
        {INPUT_VALIDATION_LOG_ENV: str(path)}
    ).path == path


def test_an_unclassified_failure_is_not_counted() -> None:
    """A count that accepts anything stops being evidence for SC42."""
    ledger = InputValidationLedger()

    with pytest.raises(UnclassifiedInputFailureError):
        record_input_failure(
            MalformedSourceParseError("corrupt zip"), ledger=ledger
        )

    assert ledger.count() == 0


def test_an_unclassified_exception_is_re_raised_by_the_observer() -> None:
    """The seam names failures; it never absorbs one it cannot classify."""
    ledger = InputValidationLedger()

    with pytest.raises(MalformedSourceParseError):
        observe_input_outcome(
            lambda: (_ for _ in ()).throw(
                MalformedSourceParseError("corrupt zip")
            ),
            ledger=ledger,
        )

    assert ledger.count() == 0


# ===== 入参非法不是能力失败 =====


@pytest.mark.parametrize(
    "exc",
    [
        pytest.param(EmptyQueryError("blank"), id="empty_query"),
        pytest.param(
            ConflictingFiltersError("ticker vs cik"), id="conflicting"
        ),
        pytest.param(PushEmptyBatchError("no records"), id="empty_batch"),
        pytest.param(
            PushEmptyDocumentError("no text"), id="empty_document_push"
        ),
        pytest.param(
            EmptySourceParseError("0 bytes"), id="empty_document_parse"
        ),
        pytest.param(NoUnitsParseError("whitespace only"), id="no_units"),
    ],
)
def test_no_invalid_input_failure_sets_degraded(exc: Exception) -> None:
    """The rule 不可变通: read through T20's authority, every one of the
    four classes reports the undegraded signal."""
    assert classify_input_failure(exc) in INPUT_VALIDATION_CLASSES
    assert degradation_signal_of_failure(exc) == NOT_DEGRADED
    assert degradation_signal_of_failure(exc).degraded is False
    assert degradation_signal_of_failure(exc).degraded_reasons == ()


def test_a_failure_claiming_degraded_is_refused_at_the_count() -> None:
    """Enforced where the count is taken, not only in a test: an invalid
    input that declares the capability flag never becomes a number."""

    class _MislabelledEmptyQueryError(EmptyQueryError):
        degraded = True
        degraded_reasons = ("retrieve:attempt_failed",)

    ledger = InputValidationLedger()

    with pytest.raises(InvalidInputMarkedDegradedError):
        record_input_failure(
            _MislabelledEmptyQueryError("blank"), ledger=ledger
        )

    assert ledger.count() == 0


def test_unknown_locator_is_not_reclassified_by_this_ticket() -> None:
    """``unknown_locator`` (SC43's third clause) keeps its own semantics:
    it is the ``read_file`` miss code, not an input-validation class, so
    this ticket's registry must not adopt it and change how the citation
    gate reads it."""
    assert classify_input_failure_code("unknown_locator") is None
    assert "unknown_locator" not in input_validation_codes()


def test_the_field_names_are_the_verbatim_ones() -> None:
    """§B3's two names, used逐字 by this module's reuse of T20."""
    assert DEGRADED_FIELD == "degraded"
    assert DEGRADED_REASONS_FIELD == "degraded_reasons"


# ===== conflict detection is proof, not guesswork =====


def test_detect_filter_conflict_needs_a_binding() -> None:
    assert (
        detect_filter_conflict(
            ticker="AAPL", cik=MSFT_CIK, manifest_docs=None
        )
        is None
    )


def test_detect_filter_conflict_ignores_unknown_values() -> None:
    """An unknown ticker binds nothing, so it cannot contradict a cik."""
    assert (
        detect_filter_conflict(
            ticker="ZZZZ", cik=MSFT_CIK, manifest_docs=MANIFEST_DOCS
        )
        is None
    )


def test_detect_filter_conflict_names_both_sides() -> None:
    message = detect_filter_conflict(
        ticker="AAPL", cik=MSFT_CIK, manifest_docs=MANIFEST_DOCS
    )

    assert message is not None
    assert "AAPL" in message and MSFT_CIK in message


def test_consistent_filters_are_not_a_conflict() -> None:
    assert (
        detect_filter_conflict(
            ticker="AAPL",
            cik=AAPL_CIK,
            form="10-Q",
            document_id="DOC-2023Q1",
            manifest_docs=MANIFEST_DOCS,
        )
        is None
    )


# ===== every port implementation answers the same dichotomy =====


def test_the_mcp_adapter_refuses_an_empty_query_before_the_round_trip() -> None:
    """The product adapter names it too, and still does not pay for a
    round trip: the scripted transport raises if it is reached."""
    adapter = MCPRetrieveAdapter(command=None)

    with pytest.raises(EmptyQueryError):
        adapter.retrieve("   ")

    assert adapter.input_validation_failures.count_by_code() == {
        "empty_query": 1
    }


def test_a_port_built_without_a_ledger_still_counts() -> None:
    """A seam without a configured log is quiet, never blind."""
    port = FakeRetrievePort()

    with pytest.raises(EmptyQueryError):
        port.retrieve("")

    assert port.input_validation_failures.count() == 1
