"""Content invalidation against the contract fake (plan T18, SC24, §B4).

Default-suite tests: the retrieval service is the T14 contract fake
(``FakePushPort``), extended here with the §B4 invalidation semantics
「关闭时间有效区间使其不再被任何查询时刻命中、不物理删除」 so the SC24
assertions have something to stand on. No service process, no network,
no large corpus.

Covered, one test per T18 acceptance clause:

* a new ``version`` for the same ``{document_id, unit}`` makes fin issue
  an invalidation for that unit's old blocks;
* a rule / params-table bump that changed the block text does the same;
* after the invalidation the old block is hit at **no** query time;
* repeating the same invalidation request yields the same result
  (idempotent) and the same store state;
* a failing invalidation returns by name, leaves no half-done state and
  leaves an enumerable record;
* the invalidation records are queryable, in-process and across
  processes.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

import pytest

from fin.corpus.chunking import split_unit
from fin.corpus.invalidation import (
    REASON_PARSE_RULE_VERSION_CHANGED,
    REASON_UNIT_VERSION_CHANGED,
    plan_invalidations,
    request_key,
)
from fin.corpus.parsers._common import build_unit
from fin.corpus.push import unit_push_records
from fin.retrieve.fake_push_port import FakePushPort
from fin.retrieve.invalidation_port import (
    DEGRADED_REASON_INVALIDATION_ATTEMPT_FAILED,
    INVALIDATION_ATTEMPTS_PER_REQUEST,
    InvalidationEmptyBatchError,
    InvalidationError,
    InvalidationFailedError,
    InvalidationLedger,
    InvalidationLedgerCorruptError,
    InvalidationLedgerUnwritableError,
    InvalidationProtocolError,
    InvalidationServiceUnavailableError,
    InvalidationUnknownTargetError,
    invalidate_targets,
)
from fin.retrieve.push_port import push_records

DOCUMENT_ID = "TEST-2023Q1"
VERSION_A = "a" * 64
VERSION_B = "b" * 64

# Query instants the SC24「任意查询时刻」assertion is spot-checked at, on
# top of the interval-emptiness claim the fake answers directly.
QUERY_TIMES = (-(10**9), -1, 0, 1, 7, 10**6, 10**18)


def _sentences(n: int, start: int = 0, topic: str = "operating revenue") -> str:
    return "".join(
        f"Sentence {i:03d} reports that {topic} grew in the quarter "
        "and the gross margin improved again. "
        for i in range(start, start + n)
    )


def _records(*, unit: int = 1, version: str = VERSION_A, text: str | None = None):
    chunking = split_unit(
        build_unit(
            fmt="txt",
            source_sha256=version,
            document_id=DOCUMENT_ID,
            version=version,
            unit=unit,
            text=text if text is not None else _sentences(40),
        ),
        doc_type="narrative",
    )
    return unit_push_records(chunking)


class _ScriptedPort:
    """An invalidation port that answers with whatever it is given."""

    def __init__(self, response: Any = None, *, raises: BaseException | None = None):
        self._response = response
        self._raises = raises
        self.calls = 0

    def invalidate(self, requests: Sequence[Mapping[str, Any]]) -> Any:
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        return self._response


# ---------------------------------------------------------------------------
# SC24: the trigger reaches the service, and the old block stops hitting
# ---------------------------------------------------------------------------


def test_a_new_unit_version_makes_fin_invalidate_the_old_blocks() -> None:
    port = FakePushPort()
    previous = _records(version=VERSION_A)
    push_records(port, previous)
    current = _records(version=VERSION_B)
    push_records(port, current)

    targets = plan_invalidations(previous, current)
    outcome = invalidate_targets(port, targets, ledger=InvalidationLedger())

    assert {target.reason for target in targets} == {REASON_UNIT_VERSION_CHANGED}
    assert outcome.closed == len(targets)
    assert set(outcome.block_ids) == {record.block_id for record in previous}


def test_a_rule_version_bump_changing_block_text_invalidates_the_old_blocks() -> None:
    port = FakePushPort()
    previous = _records()
    push_records(port, previous)
    current = _records(text=_sentences(40, topic="net interest income"))
    push_records(port, current)

    class _Signal:
        parse_changed = True
        params_changed = False

    targets = plan_invalidations(previous, current, version_signal=_Signal())
    outcome = invalidate_targets(port, targets, ledger=InvalidationLedger())

    assert {target.reason for target in targets} == {
        REASON_PARSE_RULE_VERSION_CHANGED
    }
    assert outcome.closed == len(targets)
    for record in previous:
        assert port.is_hittable_at_any_time(record.block_id) is False


def test_invalidated_blocks_are_hit_at_no_query_time() -> None:
    port = FakePushPort()
    previous = _records(version=VERSION_A)
    push_records(port, previous)
    current = _records(version=VERSION_B)
    push_records(port, current)

    for record in previous:
        assert port.is_hittable_at_any_time(record.block_id) is True

    invalidate_targets(
        port, plan_invalidations(previous, current), ledger=InvalidationLedger()
    )

    stale_ids = {record.block_id for record in previous}
    for record in previous:
        assert port.is_hittable_at_any_time(record.block_id) is False
        for at in QUERY_TIMES:
            assert port.count_hits(record.block_id, at=at) == 0
    for at in QUERY_TIMES:
        assert stale_ids.isdisjoint({row["block_id"] for row in port.hits(at=at)})

    # the surviving version keeps hitting
    for record in current:
        assert port.is_hittable_at_any_time(record.block_id) is True
    assert {row["block_id"] for row in port.hits(at=0)} == {
        record.block_id for record in current
    }


def test_invalidation_does_not_physically_delete_the_old_block() -> None:
    port = FakePushPort()
    previous = _records(version=VERSION_A)
    push_records(port, previous)
    current = _records(version=VERSION_B)
    push_records(port, current)
    total_before = port.block_total()

    invalidate_targets(
        port, plan_invalidations(previous, current), ledger=InvalidationLedger()
    )

    assert port.block_total() == total_before
    for record in previous:
        assert port.physically_present(record.block_id) is True
        row = port.recall(record.block_id)
        assert row["text"].encode("utf-8") == record.text.encode("utf-8")


# ---------------------------------------------------------------------------
# SC24: idempotency
# ---------------------------------------------------------------------------


def test_repeating_the_same_invalidation_request_is_idempotent() -> None:
    port = FakePushPort()
    previous = _records(version=VERSION_A)
    push_records(port, previous)
    current = _records(version=VERSION_B)
    push_records(port, current)
    targets = plan_invalidations(previous, current)
    ledger = InvalidationLedger()

    first = invalidate_targets(port, targets, ledger=ledger)
    state_after_first = port.invalidation_state()
    repeats = [invalidate_targets(port, targets, ledger=ledger) for _ in range(2)]

    for repeat in repeats:
        assert repeat.block_ids == first.block_ids
        assert repeat.closed_at == first.closed_at
        assert repeat.closed + repeat.already_closed == len(targets)
        assert repeat.already_closed == len(targets)
    assert port.invalidation_state() == state_after_first
    assert port.block_total() == len(previous) + len(current)
    # one distinct request, three appended trace entries: idempotent
    # result, no silent overwrite of the trail
    assert ledger.settled_requests() == (request_key(targets),)
    assert ledger.count() == 3


def test_reordering_the_targets_is_the_same_request() -> None:
    port = FakePushPort()
    previous = _records(version=VERSION_A)
    push_records(port, previous)
    current = _records(version=VERSION_B)
    push_records(port, current)
    targets = plan_invalidations(previous, current)
    ledger = InvalidationLedger()

    invalidate_targets(port, targets, ledger=ledger)
    invalidate_targets(port, tuple(reversed(targets)), ledger=ledger)

    assert ledger.settled_requests() == (request_key(targets),)


# ---------------------------------------------------------------------------
# SC24: the records are queryable
# ---------------------------------------------------------------------------


def test_invalidation_records_are_queryable() -> None:
    port = FakePushPort()
    previous = _records(version=VERSION_A)
    push_records(port, previous)
    current = _records(version=VERSION_B)
    push_records(port, current)
    targets = plan_invalidations(previous, current)
    ledger = InvalidationLedger()

    invalidate_targets(port, targets, ledger=ledger)

    records = ledger.records()
    assert len(records) == 1
    record = records[0]
    assert record.request_key == request_key(targets)
    assert record.code == "invalidation_settled"
    assert record.reasons == (REASON_UNIT_VERSION_CHANGED,)
    assert set(record.block_ids) == {target.block_id for target in targets}
    assert record.source_refs == (targets[0].source_ref,)
    assert ledger.records_for_source_ref(targets[0].source_ref) == records
    assert ledger.records_for_source_ref("no-such-source-ref") == ()
    assert ledger.failures() == ()
    # the service keeps its own queryable closure record (§B4)
    assert {row["block_id"] for row in port.invalidation_records()} == {
        target.block_id for target in targets
    }


def test_invalidation_records_survive_the_process(tmp_path) -> None:
    log = tmp_path / "invalidations.jsonl"
    port = FakePushPort()
    previous = _records(version=VERSION_A)
    push_records(port, previous)
    current = _records(version=VERSION_B)
    push_records(port, current)
    targets = plan_invalidations(previous, current)

    invalidate_targets(port, targets, ledger=InvalidationLedger(path=log))

    reloaded = InvalidationLedger(path=log)
    assert reloaded.count() == 1
    assert reloaded.settled_requests() == (request_key(targets),)
    assert set(reloaded.records()[0].block_ids) == {
        target.block_id for target in targets
    }


def test_a_corrupt_ledger_line_fails_by_name(tmp_path) -> None:
    log = tmp_path / "invalidations.jsonl"
    log.write_text("{not json at all\n", encoding="utf-8")

    with pytest.raises(InvalidationLedgerCorruptError) as excinfo:
        InvalidationLedger(path=log)

    assert excinfo.value.code == "invalidation_ledger_corrupt"


def test_an_unwritable_ledger_fails_by_name_instead_of_miscounting(
    tmp_path,
) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory\n", encoding="utf-8")
    ledger = InvalidationLedger(path=blocker / "invalidations.jsonl")
    port = FakePushPort()
    previous = _records(version=VERSION_A)
    push_records(port, previous)
    current = _records(version=VERSION_B)
    push_records(port, current)

    with pytest.raises(InvalidationLedgerUnwritableError) as excinfo:
        invalidate_targets(
            port, plan_invalidations(previous, current), ledger=ledger
        )

    assert excinfo.value.code == "invalidation_ledger_unwritable"
    assert ledger.count() == 0


# ---------------------------------------------------------------------------
# SC24: failures are named, silent nothing, half-done nothing
# ---------------------------------------------------------------------------


def test_an_unreachable_service_is_a_named_failure() -> None:
    port = _ScriptedPort(raises=ConnectionRefusedError("connection refused"))
    previous = _records(version=VERSION_A)
    targets = plan_invalidations(previous, _records(version=VERSION_B))
    ledger = InvalidationLedger()

    with pytest.raises(InvalidationServiceUnavailableError) as excinfo:
        invalidate_targets(port, targets, ledger=ledger)

    assert excinfo.value.code == "invalidation_service_unavailable"
    assert isinstance(excinfo.value, InvalidationError)
    assert isinstance(excinfo.value.__cause__, ConnectionRefusedError)
    assert excinfo.value.degraded is True
    assert excinfo.value.degraded_reasons == (
        DEGRADED_REASON_INVALIDATION_ATTEMPT_FAILED,
    )
    failures = ledger.failures()
    assert [failure.code for failure in failures] == [
        "invalidation_service_unavailable"
    ]
    assert ledger.settled_requests() == ()
    assert port.calls == INVALIDATION_ATTEMPTS_PER_REQUEST == 1


@pytest.mark.parametrize(
    "response",
    [
        None,
        [],
        "ok",
        {"closed": 1, "already_closed": 0, "closed_at": {}},
        {"block_ids": [], "already_closed": 0, "closed_at": {}},
        {"block_ids": [], "closed": 0, "closed_at": {}},
        {"block_ids": [], "closed": 0, "already_closed": 0},
        {"block_ids": "b1", "closed": 1, "already_closed": 0, "closed_at": {}},
        {"block_ids": [1], "closed": 1, "already_closed": 0, "closed_at": {}},
        {"block_ids": [], "closed": 0, "already_closed": 0, "closed_at": {}},
        {"block_ids": ["x"], "closed": -1, "already_closed": 2, "closed_at": {}},
        {"block_ids": ["x"], "closed": True, "already_closed": 0, "closed_at": {}},
        {"block_ids": ["x"], "closed": 0, "already_closed": 0, "closed_at": {}},
        {"block_ids": ["x"], "closed": 1, "already_closed": 0, "closed_at": []},
        {
            "block_ids": ["x"],
            "closed": 1,
            "already_closed": 0,
            "closed_at": {"y": 0},
        },
        {
            "block_ids": ["x"],
            "closed": 1,
            "already_closed": 0,
            "closed_at": {"x": "soon"},
        },
    ],
)
def test_an_off_contract_response_is_a_named_protocol_failure(response) -> None:
    port = _ScriptedPort(response)
    previous = _records(version=VERSION_A)[:1]
    targets = plan_invalidations(previous, _records(version=VERSION_B))
    ledger = InvalidationLedger()

    with pytest.raises(InvalidationProtocolError) as excinfo:
        invalidate_targets(port, targets, ledger=ledger)

    assert excinfo.value.code == "invalidation_protocol_error"
    assert excinfo.value.degraded is True
    assert [failure.code for failure in ledger.failures()] == [
        "invalidation_protocol_error"
    ]
    assert ledger.settled_requests() == ()


def test_an_untyped_transport_exception_is_wrapped_not_swallowed() -> None:
    port = _ScriptedPort(raises=RuntimeError("wire broke mid-frame"))
    targets = plan_invalidations(
        _records(version=VERSION_A), _records(version=VERSION_B)
    )
    ledger = InvalidationLedger()

    with pytest.raises(InvalidationFailedError) as excinfo:
        invalidate_targets(port, targets, ledger=ledger)

    assert excinfo.value.code == "invalidation_failed"
    assert isinstance(excinfo.value.__cause__, RuntimeError)
    assert [failure.code for failure in ledger.failures()] == ["invalidation_failed"]


def test_an_empty_target_batch_is_invalid_input_not_a_zero_write_success() -> None:
    port = FakePushPort()

    with pytest.raises(InvalidationEmptyBatchError) as excinfo:
        invalidate_targets(port, (), ledger=InvalidationLedger())
    assert excinfo.value.code == "invalidation_empty_batch"
    # §B2 第 5 条: caller error, not a capability failure
    assert excinfo.value.degraded is False
    assert excinfo.value.degraded_reasons == ()

    # the receiving side refuses independently of the driver
    with pytest.raises(InvalidationEmptyBatchError):
        port.invalidate([])


def test_an_unknown_target_refuses_the_whole_batch_leaving_nothing_closed() -> None:
    port = FakePushPort()
    previous = _records(version=VERSION_A)
    push_records(port, previous)
    current = _records(version=VERSION_B)
    push_records(port, current)
    targets = plan_invalidations(previous, current)
    stranger = targets[0].to_wire()
    stranger["content_id"] = "0" * 64

    with pytest.raises(InvalidationUnknownTargetError) as excinfo:
        port.invalidate([*(t.to_wire() for t in targets), stranger])

    assert excinfo.value.code == "invalidation_unknown_target"
    assert excinfo.value.degraded is False
    # no half-done state: not one interval was closed
    assert port.invalidation_records() == ()
    for record in previous:
        assert port.is_hittable_at_any_time(record.block_id) is True
    assert port.refused_invalidations() == 1


def test_a_failed_invalidation_leaves_the_store_untouched() -> None:
    port = FakePushPort()
    previous = _records(version=VERSION_A)
    push_records(port, previous)
    current = _records(version=VERSION_B)
    push_records(port, current)
    targets = plan_invalidations(previous, current)
    ledger = InvalidationLedger()
    unknown = targets[0].with_content_id("0" * 64)

    with pytest.raises(InvalidationUnknownTargetError):
        invalidate_targets(port, (*targets, unknown), ledger=ledger)

    assert port.invalidation_state() == {}
    for record in [*previous, *current]:
        assert port.is_hittable_at_any_time(record.block_id) is True
    assert [failure.code for failure in ledger.failures()] == [
        "invalidation_unknown_target"
    ]
    assert ledger.settled_requests() == ()

    # the retry after the caller fixed its target list settles cleanly
    outcome = invalidate_targets(port, targets, ledger=ledger)
    assert outcome.closed == len(targets)
    assert ledger.settled_requests() == (request_key(targets),)


def test_the_invalidation_error_codes_are_pairwise_distinct() -> None:
    errors = (
        InvalidationError,
        InvalidationServiceUnavailableError,
        InvalidationProtocolError,
        InvalidationEmptyBatchError,
        InvalidationUnknownTargetError,
        InvalidationFailedError,
        InvalidationLedgerUnwritableError,
        InvalidationLedgerCorruptError,
    )
    codes = [error.code for error in errors]

    assert len(set(codes)) == len(codes)
    assert all(issubclass(error, InvalidationError) for error in errors)


@pytest.mark.parametrize(
    ("error", "degraded"),
    [
        (InvalidationError, False),
        (InvalidationEmptyBatchError, False),
        (InvalidationUnknownTargetError, False),
        (InvalidationLedgerUnwritableError, False),
        (InvalidationLedgerCorruptError, False),
        (InvalidationServiceUnavailableError, True),
        (InvalidationProtocolError, True),
        (InvalidationFailedError, True),
    ],
)
def test_degraded_classification_follows_b3(error, degraded: bool) -> None:
    assert error.degraded is degraded
    assert bool(error.degraded_reasons) is degraded
