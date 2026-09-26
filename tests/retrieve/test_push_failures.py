"""Push and protocol failures return named, distinguishable errors
(plan T16, SC21, §B2 第 5 条 / §B3).

Default-suite tests: the retrieval service is the T14 contract fake
(`FakePushPort`) plus small scripted ports for the two failure shapes —
no service process, no network, no large corpus.

Covered, one test per T16 acceptance clause:

* an **unreachable** service and a **protocol** violation return
  mutually distinguishable named failures (distinct stable codes,
  neither is an instance of the other);
* on either failure path nothing is recorded as a successful write,
  no local artifact counts as completion, and the transport is called
  exactly once (no silent retry);
* an **empty block batch** is invalid input (§B2 第 5 条): a named
  failure that does **not** set `degraded`, kept apart from a
  legitimate "nothing new to write" success.
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

import pytest

from fin.corpus.chunking import split_unit
from fin.corpus.parsers._common import build_unit
from fin.corpus.push import PushRecord, unit_push_records
from fin.retrieve.fake_push_port import FakePushPort
from fin.retrieve.push_batching import PushJournal, plan_batches, push_in_batches
from fin.retrieve.push_port import (
    PUSH_ATTEMPTS_PER_BATCH,
    PushBatchFailedError,
    PushBatchOverflowError,
    PushBatchSizeInvalidError,
    PushEmptyBatchError,
    PushEmptyDocumentError,
    PushError,
    PushMetadataIncompleteError,
    PushOutcome,
    PushProtocolError,
    PushServiceUnavailableError,
    UnknownPushedBlockError,
    push_records,
)

DOCUMENT_ID = "TEST-2025Q3"
VERSION = "c" * 64


def _sentences(n: int, start: int = 0) -> str:
    return "".join(
        f"Sentence {i:04d} reports that operating revenue grew in the "
        "quarter and the gross margin improved again. "
        for i in range(start, start + n)
    )


def _records(units: int = 2, sentences: int = 30) -> tuple[PushRecord, ...]:
    out: list[PushRecord] = []
    for unit in range(1, units + 1):
        chunking = split_unit(
            build_unit(
                fmt="txt",
                source_sha256=VERSION,
                document_id=DOCUMENT_ID,
                version=VERSION,
                unit=unit,
                text=_sentences(sentences, start=unit * 1000),
            ),
            doc_type="narrative",
        )
        out.extend(unit_push_records(chunking))
    return tuple(out)


class UnreachablePushPort:
    """A `PushPort` whose transport never reaches the service.

    A refused connection is the transport-level shape of SC21's
    「检索服务不可用」; the adapter must turn it into a named failure
    rather than let an `OSError` escape untyped.
    """

    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error or ConnectionRefusedError("connection refused")
        self.calls = 0

    def push(self, records: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
        self.calls += 1
        raise self.error


class AcknowledgementLosingPushPort:
    """A `PushPort` that stores for real, then answers off-contract.

    The nastiest shape of SC21's 「协议失败」: the service did write, but
    its answer cannot be trusted, so fin must not book the batch as a
    success on the strength of an envelope it just rejected.
    """

    def __init__(self, store: FakePushPort) -> None:
        self.store = store
        self.calls = 0

    def push(self, records: Sequence[Mapping[str, Any]]) -> Any:
        self.calls += 1
        response = self.store.push(records)
        return {**response, "block_ids": response["block_ids"][:-1]}


class MalformedResponsePushPort:
    """A `PushPort` that answers, but off-contract.

    The service was reached and stored nothing the adapter can trust, so
    the response envelope — not the connection — is what violates the
    contract (SC21's 「协议失败」).
    """

    def __init__(self, response: Any) -> None:
        self.response = response
        self.calls = 0

    def push(self, records: Sequence[Mapping[str, Any]]) -> Any:
        self.calls += 1
        return self.response


# ---------------------------------------------------------------------------
# SC21: unreachable service and protocol failure are distinguishable
# ---------------------------------------------------------------------------


def test_unreachable_service_is_a_named_failure() -> None:
    port = UnreachablePushPort()

    with pytest.raises(PushServiceUnavailableError) as excinfo:
        push_records(port, _records())

    assert excinfo.value.code == "push_service_unavailable"
    assert isinstance(excinfo.value, PushError)
    assert isinstance(excinfo.value.__cause__, ConnectionRefusedError)


@pytest.mark.parametrize(
    "response",
    [
        None,
        [],
        "ok",
        {"written": 1, "duplicates": 0},
        {"block_ids": ["b0"], "duplicates": 0},
        {"block_ids": ["b0"], "written": 1},
        {"block_ids": "b0", "written": 1, "duplicates": 0},
        {"block_ids": [1], "written": 1, "duplicates": 0},
        {"block_ids": ["b0", "b1"], "written": 2, "duplicates": 0},
        {"block_ids": ["b0"], "written": "one", "duplicates": 0},
        {"block_ids": ["b0"], "written": -1, "duplicates": 2},
        {"block_ids": ["b0"], "written": 0, "duplicates": 0},
    ],
)
def test_off_contract_response_is_a_named_protocol_failure(response: Any) -> None:
    port = MalformedResponsePushPort(response)

    with pytest.raises(PushProtocolError) as excinfo:
        push_records(port, _records()[:1])

    assert excinfo.value.code == "push_protocol_error"
    assert isinstance(excinfo.value, PushError)


def test_the_two_failures_are_mutually_distinguishable() -> None:
    unavailable = UnreachablePushPort()
    protocol = MalformedResponsePushPort({"block_ids": [], "written": 0})
    records = _records()[:1]

    with pytest.raises(PushError) as unavailable_info:
        push_records(unavailable, records)
    with pytest.raises(PushError) as protocol_info:
        push_records(protocol, records)

    assert unavailable_info.value.code != protocol_info.value.code
    assert not isinstance(unavailable_info.value, PushProtocolError)
    assert not isinstance(protocol_info.value, PushServiceUnavailableError)


def test_the_contract_fake_answers_on_contract() -> None:
    """The distinguishing checks above must not fire on the real shape."""
    port = FakePushPort()
    records = _records()

    outcome = push_records(port, records)

    assert outcome.written == port.block_total()
    assert len(outcome.block_ids) == len(records)


# ---------------------------------------------------------------------------
# SC21: no success recorded, no local completion, no silent retry
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "port_factory",
    [
        lambda: UnreachablePushPort(),
        lambda: MalformedResponsePushPort({"block_ids": [], "written": 0}),
    ],
    ids=["unavailable", "protocol"],
)
def test_failure_path_does_not_retry_silently(port_factory) -> None:
    port = port_factory()

    with pytest.raises(PushError):
        push_records(port, _records()[:1])

    assert PUSH_ATTEMPTS_PER_BATCH == 1
    assert port.calls == PUSH_ATTEMPTS_PER_BATCH


@pytest.mark.parametrize(
    "port_factory",
    [
        lambda: UnreachablePushPort(),
        lambda: MalformedResponsePushPort({"block_ids": [], "written": 0}),
    ],
    ids=["unavailable", "protocol"],
)
def test_failure_path_records_no_write_and_no_completion(
    port_factory, tmp_path
) -> None:
    journal_path = tmp_path / "push-journal.jsonl"
    journal = PushJournal(path=journal_path)
    records = _records()
    port = port_factory()

    with pytest.raises(PushError) as excinfo:
        push_in_batches(port, records, journal=journal, batch_size=3)

    # not recorded as a successful write, by either side of the seam
    assert journal.completed_keys() == ()
    assert [failure.code for failure in journal.failures()] == [excinfo.value.code]
    # the only local artifact is the failure log; it marks nothing done
    assert [path.name for path in tmp_path.iterdir()] == [journal_path.name]
    events = [
        json.loads(line)
        for line in journal_path.read_text(encoding="utf-8").splitlines()
        if line
    ]
    assert [event["event"] for event in events] == ["failed"]
    assert events[0]["code"] == excinfo.value.code


def test_local_journal_does_not_let_a_failed_run_count_as_complete(
    tmp_path,
) -> None:
    """The on-disk journal is a failure log, not a completion receipt: a
    resumed run re-pushes every batch (SC21「不降级为本地落盘即算完成」)."""
    journal_path = tmp_path / "push-journal.jsonl"
    records = _records()
    planned = len(plan_batches(records, batch_size=3))

    with pytest.raises(PushServiceUnavailableError):
        push_in_batches(
            UnreachablePushPort(),
            records,
            journal=PushJournal(path=journal_path),
            batch_size=3,
        )

    reloaded = PushJournal(path=journal_path)
    assert reloaded.completed_keys() == ()
    assert len(reloaded.failures()) == 1

    store = FakePushPort()
    outcome = push_in_batches(store, records, journal=reloaded, batch_size=3)

    assert outcome.batches_skipped == 0
    assert outcome.batches_pushed == planned
    assert store.block_total() == len({r.idempotency_key for r in records})


@pytest.mark.parametrize(
    "port_factory",
    [
        lambda: UnreachablePushPort(),
        lambda: MalformedResponsePushPort({"block_ids": [], "written": 0}),
    ],
    ids=["unavailable", "protocol"],
)
def test_batch_driver_does_not_retry_a_failed_batch_silently(
    port_factory,
) -> None:
    """The driver stops at the first failing batch: no hidden second
    attempt, and no push of the batches behind it."""
    port = port_factory()

    with pytest.raises(PushError):
        push_in_batches(port, _records(), journal=PushJournal(), batch_size=3)

    assert port.calls == PUSH_ATTEMPTS_PER_BATCH


def test_a_stored_batch_with_an_off_contract_ack_is_not_counted_as_written(
    tmp_path,
) -> None:
    """SC21「不把写入记为成功」at its hardest: the service stored the
    records but answered off-contract, so fin books nothing and replays
    the batch, which the store's idempotency turns into duplicates
    rather than a second write."""
    store = FakePushPort()
    port = AcknowledgementLosingPushPort(store)
    journal = PushJournal(path=tmp_path / "push-journal.jsonl")
    records = _records()
    expected = len({record.idempotency_key for record in records})

    with pytest.raises(PushProtocolError):
        push_in_batches(port, records, journal=journal, batch_size=3)

    # the service really wrote, yet fin recorded no successful batch
    assert store.block_total() > 0
    assert journal.completed_keys() == ()
    assert [failure.code for failure in journal.failures()] == [
        "push_protocol_error"
    ]

    replay = push_in_batches(store, records, journal=journal, batch_size=3)

    assert replay.batches_skipped == 0
    assert replay.written < expected
    assert replay.duplicates > 0
    assert replay.written + replay.duplicates == len(records)
    assert store.block_total() == expected


def test_unavailable_service_keeps_its_code_through_the_batch_driver(
    tmp_path,
) -> None:
    """The batch driver must not flatten SC21's two failures into the
    generic `push_batch_failed` wrapper."""
    journal = PushJournal()

    with pytest.raises(PushServiceUnavailableError) as excinfo:
        push_in_batches(
            UnreachablePushPort(),
            _records(),
            journal=journal,
            batch_size=3,
        )

    assert not isinstance(excinfo.value, PushBatchFailedError)
    assert journal.failures()[0].code == "push_service_unavailable"


# ---------------------------------------------------------------------------
# §B2 第 5 条: an empty block batch is invalid input, not an empty result
# ---------------------------------------------------------------------------


def test_empty_batch_push_is_a_named_failure_before_the_transport() -> None:
    port = UnreachablePushPort()

    with pytest.raises(PushEmptyBatchError) as excinfo:
        push_records(port, [])

    assert excinfo.value.code == "push_empty_batch"
    assert isinstance(excinfo.value, PushError)
    assert port.calls == 0


def test_the_contract_fake_itself_refuses_an_empty_batch() -> None:
    port = FakePushPort()

    with pytest.raises(PushEmptyBatchError):
        port.push([])

    assert port.block_total() == 0
    assert port.rejected_batches() == 1


def test_empty_batch_through_the_batch_driver_is_a_named_failure() -> None:
    journal = PushJournal()

    with pytest.raises(PushEmptyBatchError) as excinfo:
        push_in_batches(FakePushPort(), (), journal=journal, batch_size=3)

    assert excinfo.value.code == "push_empty_batch"
    assert journal.completed_keys() == ()


def test_empty_batch_failure_does_not_set_degraded() -> None:
    with pytest.raises(PushEmptyBatchError) as excinfo:
        push_records(FakePushPort(), [])

    assert excinfo.value.degraded is False
    assert excinfo.value.degraded_reasons == ()


@pytest.mark.parametrize(
    ("error_class", "degraded"),
    [
        (PushEmptyBatchError, False),
        (PushEmptyDocumentError, False),
        (PushMetadataIncompleteError, False),
        (PushBatchOverflowError, False),
        (PushBatchSizeInvalidError, False),
        (UnknownPushedBlockError, False),
        (PushServiceUnavailableError, True),
        (PushProtocolError, True),
        (PushBatchFailedError, True),
    ],
)
def test_degraded_classification_splits_invalid_input_from_capability_failure(
    error_class: type[PushError], degraded: bool
) -> None:
    """§B2 第 5 条: invalid input is not a capability failure, so it never
    sets `degraded`; §B3 rule 2: an attempted-and-failed capability does.
    The field names are verbatim `degraded` / `degraded_reasons`."""
    error = error_class("scripted")

    assert error.degraded is degraded
    assert bool(error.degraded_reasons) is degraded


def test_a_push_with_nothing_new_to_write_is_a_success_not_a_failure() -> None:
    """The other half of the §B2 第 5 条 split: a non-empty batch whose
    records are all already stored is a legitimate zero-write success,
    while an empty batch is a typed failure — the two are not merged."""
    port = FakePushPort()
    records = _records()
    push_records(port, records)

    replay = push_records(port, records)

    assert replay.written == 0
    assert replay.duplicates == len(records)
    assert len(replay.block_ids) == len(records)


# ---------------------------------------------------------------------------
# T4 handoff: the ``content_identity`` the service stored survives the seam
# ---------------------------------------------------------------------------

_REF_A = '{"document_id":"T-2025Q3","version":"c","unit":1}'
_REF_B = '{"document_id":"T-2025Q3","version":"c","unit":2}'
_IDENTITY_A = "1" * 64
_IDENTITY_B = "2" * 64


def _wire(source_ref: str, body: str) -> dict[str, Any]:
    """The smallest envelope the push shim forwards unchanged."""
    return {
        "source_ref": source_ref,
        "body": body,
        "chunks": [{"index": 0, "content": body}],
        "metadata": {"fmt": "txt"},
    }


class _AnsweringPushPort:
    """A `PushPort` that answers one fixed response dict."""

    def __init__(self, response: Mapping[str, Any]) -> None:
        self.response = response
        self.calls = 0

    def push(self, records: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
        self.calls += 1
        return self.response


def test_outcome_carries_a_single_document_content_identity() -> None:
    port = _AnsweringPushPort(
        {
            "block_ids": ["b0"],
            "written": 1,
            "duplicates": 0,
            "content_identity": _IDENTITY_A,
        }
    )

    outcome = push_records(port, [_wire(_REF_A, "alpha")])

    assert outcome.content_identities == {_REF_A: _IDENTITY_A}


def test_outcome_carries_batch_content_identities_in_send_order() -> None:
    port = _AnsweringPushPort(
        {
            "block_ids": ["b0", "b1"],
            "written": 2,
            "duplicates": 0,
            "content_identities": [_IDENTITY_A, _IDENTITY_B],
        }
    )

    outcome = push_records(
        port, [_wire(_REF_A, "alpha"), _wire(_REF_B, "beta")]
    )

    assert outcome.content_identities == {_REF_A: _IDENTITY_A, _REF_B: _IDENTITY_B}


def test_outcome_content_identities_are_empty_for_an_identity_less_response() -> None:
    """A response that predates the identity field is still on-contract for
    the write; the caller then simply has no identity to hand on."""
    port = _AnsweringPushPort(
        {"block_ids": ["b0"], "written": 1, "duplicates": 0}
    )

    assert push_records(port, [_wire(_REF_A, "alpha")]).content_identities == {}


def test_push_outcome_without_the_identity_field_still_constructs() -> None:
    """Call sites written before the identity handoff keep working: the new
    field defaults to empty rather than becoming a required argument."""
    outcome = PushOutcome(block_ids=("b0",), written=1, duplicates=0)

    assert outcome.content_identities == {}


@pytest.mark.parametrize(
    "identity_fields",
    [
        pytest.param(
            {"content_identity": _IDENTITY_A}, id="scalar-for-a-two-doc-batch"
        ),
        pytest.param({"content_identity": 7}, id="scalar-not-a-string"),
        pytest.param(
            {"content_identities": "abc"}, id="identities-not-a-sequence"
        ),
        pytest.param({"content_identities": [_IDENTITY_A]}, id="too-few"),
        pytest.param(
            {"content_identities": [_IDENTITY_A, _IDENTITY_B, _IDENTITY_A]},
            id="too-many",
        ),
        pytest.param(
            {"content_identities": [_IDENTITY_A, None]}, id="stray-non-string"
        ),
        pytest.param(
            {"content_identities": {_REF_A: _IDENTITY_A, _REF_B: 9}},
            id="keyed-with-a-non-string-identity",
        ),
        pytest.param(
            {
                "content_identity": _IDENTITY_A,
                "content_identities": [_IDENTITY_A, _IDENTITY_B],
            },
            id="both-forms-at-once",
        ),
    ],
)
def test_off_contract_identity_fields_are_a_named_protocol_failure(
    identity_fields: Mapping[str, Any],
) -> None:
    """An identity fin cannot attribute to exactly one pushed 资料 is
    refused by name — publishing a guessed pair would send a wrong value
    to ``submit_index_job``."""
    port = MalformedResponsePushPort(
        {
            "block_ids": ["b0", "b1"],
            "written": 2,
            "duplicates": 0,
            **identity_fields,
        }
    )

    with pytest.raises(PushProtocolError) as excinfo:
        push_records(port, [_wire(_REF_A, "alpha"), _wire(_REF_B, "beta")])

    assert excinfo.value.code == "push_protocol_error"
    assert isinstance(excinfo.value, PushError)
