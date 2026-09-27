"""TDD: production wiring of the previous_records provider and journals.

Context: a shipped boot built IngestEndpoint without previous_records,
without a path-backed PushJournal, and without a path-backed
InvalidationLedger. The replacement stale-block closure no-oped and push
resume was within-process only.

Contract asserted here:
  * the service boots with all three wired, over a visible data dir;
  * every successful ingest persists its pushed records, so a replacement
    upload closes the superseded version's blocks through the fake
    invalidation port at the accept door;
  * a same-version re-ingest triggers no invalidation;
  * new store instances over the same paths (a restart) still have the
    records the replacement closure needs;
  * catalog semantics are unchanged: the on_ingested hook keeps the
    catalog_record_failed warning path exactly.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import fin.host.workspace_http as werkplace_http
from fin.corpus.ingest_endpoint import IngestEndpoint
from fin.corpus.ingest_state import InMemoryIngestStateStore, IngestState
from fin.corpus.metadata_store import (
    InMemoryMetadataStore,
    MetadataPointerNotFoundError,
)
from fin.corpus.object_store import InMemoryObjectStore
from fin.corpus.pushed_records import PushedRecordsStore
from fin.host.workspace_http import (
    _build_default_ingest_service,
    _ingest_components,
)
from fin.retrieve.fake_push_port import FakePushPort

_V1_TEXT = (
    "Quarterly results overview.\n\n"
    "Revenue grew across every segment this quarter.\n\n"
    "The company reaffirmed its full-year guidance."
).encode("utf-8")

_V2_TEXT = (
    "Quarterly results overview, revised.\n\n"
    "Revenue declined in two segments this quarter.\n\n"
    "The company withdrew its full-year guidance."
).encode("utf-8")


def _journal_env(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(
        "FIN_CORPUS_PUSHED_RECORDS_PATH", str(tmp_path / "pushed_records.jsonl")
    )
    monkeypatch.setenv(
        "FIN_CORPUS_PUSH_JOURNAL_PATH", str(tmp_path / "push_journal.jsonl")
    )
    monkeypatch.setenv(
        "FIN_CORPUS_INVALIDATION_LEDGER_PATH",
        str(tmp_path / "invalidation_ledger.jsonl"),
    )


def _fresh_durable() -> tuple[Any, Any, Any]:
    """A private accept-time stack for one test.

    ``preflight_ingest_storage()`` caches one process-wide snapshot; the
    scenarios below move documents through pointer/job states, so they
    must not inherit another test's durable state.
    """
    return InMemoryObjectStore(), InMemoryMetadataStore(), InMemoryIngestStateStore()


def _submit(service: IngestEndpoint, data: bytes, document_id: str = "DOC-1") -> Any:
    return service.submit_durable(
        data, document_id=document_id, declared_format="txt"
    )


def _advance(states: InMemoryIngestStateStore, document_id: str) -> None:
    for to_state in (
        IngestState.FETCHING,
        IngestState.EXTRACTING,
        IngestState.CATALOGING,
        IngestState.READY,
    ):
        states.transition(document_id, to_state)


def _pointer_sha_of(pointers: Any, document_id: str) -> str | None:
    try:
        return pointers.get_pointer(document_id).pointer.sha256
    except MetadataPointerNotFoundError:
        return None


def _wired(
    service: IngestEndpoint,
    fake: FakePushPort,
    *,
    durable: tuple[Any, Any, Any] | None = None,
) -> IngestEndpoint:
    """The production wiring with the contract fake swapped in for the
    retrieval faces. Journals, provider, hook, and durable stores stay
    exactly as the build function wired them (or are handed in for a
    restart scenario)."""
    objects, pointers, states = durable or (
        service._object_store,
        service._metadata_store,
        service._state_store,
    )
    kwargs: dict[str, Any] = dict(
        push_port=fake,
        invalidation_port=fake,
        journal=service._journal,
        invalidation_ledger=service._invalidation_ledger,
        previous_records=service._previous_records,
        on_ingested=service._on_ingested,
        object_store=objects,
        metadata_store=pointers,
        state_store=states,
    )
    store = getattr(service, "_pushed_records_store", None)
    if store is not None:
        kwargs["pushed_records_store"] = store
    return type(service)(**kwargs)


# ----- component paths ----------------------------------------------------


def test_components_default_to_the_visible_catalog_dir(
    monkeypatch, tmp_path
) -> None:
    for name in (
        "FIN_CORPUS_PUSH_JOURNAL_PATH",
        "FIN_CORPUS_INVALIDATION_LEDGER_PATH",
        "FIN_CORPUS_PUSHED_RECORDS_PATH",
    ):
        monkeypatch.delenv(name, raising=False)
    catalog = tmp_path / "catalog.json"

    components = _ingest_components(catalog_path=catalog)

    assert components["push_journal_path"] == tmp_path / "push_journal.jsonl"
    assert components["invalidation_ledger_path"] == (
        tmp_path / "invalidation_ledger.jsonl"
    )
    assert components["pushed_records_path"] == tmp_path / "pushed_records.jsonl"


def test_components_honor_configured_paths(monkeypatch, tmp_path) -> None:
    journal = tmp_path / "j.jsonl"
    ledger = tmp_path / "l.jsonl"
    records = tmp_path / "r.jsonl"
    monkeypatch.setenv("FIN_CORPUS_PUSH_JOURNAL_PATH", str(journal))
    monkeypatch.setenv("FIN_CORPUS_INVALIDATION_LEDGER_PATH", str(ledger))
    monkeypatch.setenv("FIN_CORPUS_PUSHED_RECORDS_PATH", str(records))

    components = _ingest_components(catalog_path=tmp_path / "catalog.json")

    assert components["push_journal_path"] == journal
    assert components["invalidation_ledger_path"] == ledger
    assert components["pushed_records_path"] == records


# ----- service build -------------------------------------------------------


def test_booting_the_service_writes_nothing(tmp_path, monkeypatch) -> None:
    _journal_env(monkeypatch, tmp_path)

    _build_default_ingest_service()

    assert list(tmp_path.iterdir()) == []


def test_wired_service_exposes_the_three_seams(tmp_path, monkeypatch) -> None:
    _journal_env(monkeypatch, tmp_path)

    service = _build_default_ingest_service()

    assert isinstance(service, IngestEndpoint)
    assert service._journal is not None
    assert service._journal.path == tmp_path / "push_journal.jsonl"
    assert service._invalidation_ledger is not None
    assert service._previous_records is not None
    assert service._previous_records() == []


# ----- records persist on every successful ingest --------------------------


def test_provider_reads_the_durable_store_after_ingest(
    tmp_path, monkeypatch
) -> None:
    _journal_env(monkeypatch, tmp_path)
    service = _build_default_ingest_service()
    wired = _wired(service, FakePushPort(), durable=_fresh_durable())

    result = wired.ingest(
        _V1_TEXT, document_id="DOC-1", declared_format="txt"
    )

    assert result.records
    assert wired._previous_records() == list(result.records)
    for record in wired._previous_records():
        locator = record.metadata["locator"]
        assert {"document_id", "version", "unit"} <= set(locator)
        assert locator["document_id"] == "DOC-1"
        assert locator["version"] == result.version
    # the durable file replays the same set
    store = PushedRecordsStore(path=tmp_path / "pushed_records.jsonl")
    assert store.all_records() == list(result.records)


# ----- replacement closure through the wiring ------------------------------


def test_replacement_upload_closes_superseded_blocks_at_the_accept_door(
    tmp_path, monkeypatch
) -> None:
    _journal_env(monkeypatch, tmp_path)
    service = _build_default_ingest_service()
    fake = FakePushPort()
    wired = _wired(service, fake, durable=_fresh_durable())

    first = _submit(wired, _V1_TEXT)
    result = wired.ingest(_V1_TEXT, document_id="DOC-1", declared_format="txt")
    assert result.invalidation is None  # nothing stale against an empty store
    _advance(wired._state_store, "DOC-1")

    second = _submit(wired, _V2_TEXT)

    # the accept door closed every block the replaced version pushed
    closed = fake.invalidation_records()
    assert sorted(row["block_id"] for row in closed) == sorted(
        record.block_id for record in result.records
    )
    assert {row["reason"] for row in closed} == {"unit_version_changed"}
    # and the closure settled through the wired ledger, on disk
    settled = [
        json.loads(line)
        for line in (tmp_path / "invalidation_ledger.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    assert settled and {event["event"] for event in settled} == {"settled"}
    # reopen semantics untouched: one job, READY -> PENDING with the
    # supersede audit row, pointer moved to exactly one logical version
    assert second.job_id == first.job_id
    assert second.state is IngestState.PENDING
    assert wired._state_store.get_job("DOC-1").state is IngestState.PENDING
    superseded = [
        row
        for row in wired._state_store.list_audit("DOC-1")
        if row.action == "job_superseded"
    ]
    assert [(row.from_state, row.to_state) for row in superseded] == [
        (IngestState.READY, IngestState.PENDING)
    ]
    assert _pointer_sha_of(wired._metadata_store, "DOC-1") == second.version


def test_ready_generation_record_is_not_lost_by_the_closure(
    tmp_path, monkeypatch
) -> None:
    # The wired closure must not rewrite the audit trail: the ready
    # generation's transitions stay, the reopen adds one row.
    _journal_env(monkeypatch, tmp_path)
    service = _build_default_ingest_service()
    fake = FakePushPort()
    wired = _wired(service, fake, durable=_fresh_durable())

    _submit(wired, _V1_TEXT)
    wired.ingest(_V1_TEXT, document_id="DOC-1", declared_format="txt")
    _advance(wired._state_store, "DOC-1")

    _submit(wired, _V2_TEXT)

    actions = [row.action for row in wired._state_store.list_audit("DOC-1")]
    assert actions.count("job_superseded") == 1
    assert len(actions) > 1  # the reopen added a row; it replaced none
    # the document still names exactly one logical version after reopen
    assert _pointer_sha_of(wired._metadata_store, "DOC-1") == hashlib.sha256(
        _V2_TEXT
    ).hexdigest()


def test_same_version_resubmit_and_reingest_trigger_no_invalidation(
    tmp_path, monkeypatch
) -> None:
    # A re-push of an unchanged pair must not trigger invalidation.
    _journal_env(monkeypatch, tmp_path)
    service = _build_default_ingest_service()
    fake = FakePushPort()
    wired = _wired(service, fake, durable=_fresh_durable())

    first = _submit(wired, _V1_TEXT)
    pushed = wired.ingest(_V1_TEXT, document_id="DOC-1", declared_format="txt")
    _advance(wired._state_store, "DOC-1")

    joined = _submit(wired, _V1_TEXT)  # duplicate: joins, no supersede
    again = wired.ingest(_V1_TEXT, document_id="DOC-1", declared_format="txt")

    assert joined.job_id == first.job_id
    assert joined.state is IngestState.READY
    assert again.version == pushed.version
    assert fake.invalidation_records() == ()
    assert not (tmp_path / "invalidation_ledger.jsonl").exists()
    # the store keeps one generation per key — no duplicate blocks
    store = PushedRecordsStore(path=tmp_path / "pushed_records.jsonl")
    assert store.all_records() == list(again.records)


# ----- restart ---------------------------------------------------------------


def test_replacement_closure_survives_a_restart(tmp_path, monkeypatch) -> None:
    _journal_env(monkeypatch, tmp_path)
    # the durable pointer/job stack and the retrieval backend outlive the
    # process in production; one fresh stack stands in for both boots
    durable = _fresh_durable()
    first_boot = _build_default_ingest_service()
    fake = FakePushPort()
    first_wired = _wired(first_boot, fake, durable=durable)

    _submit(first_wired, _V1_TEXT)
    pushed = first_wired.ingest(
        _V1_TEXT, document_id="DOC-1", declared_format="txt"
    )
    _advance(first_wired._state_store, "DOC-1")

    # process restart: fresh service — fresh path-backed store, journal,
    # and ledger over the same paths
    restarted = _build_default_ingest_service()
    assert restarted._previous_records()  # replayed, not re-pushed
    restarted_wired = _wired(restarted, fake, durable=durable)

    submission = _submit(restarted_wired, _V2_TEXT)

    assert submission.state is IngestState.PENDING
    closed = fake.invalidation_records()
    assert sorted(row["block_id"] for row in closed) == sorted(
        record.block_id for record in pushed.records
    )
    assert {row["reason"] for row in closed} == {"unit_version_changed"}


# ----- catalog semantics through the new hook --------------------------------


def test_catalog_warning_semantics_survive_the_new_hook(
    tmp_path, monkeypatch
) -> None:
    _journal_env(monkeypatch, tmp_path)
    service = _build_default_ingest_service()
    wired = _wired(service, FakePushPort(), durable=_fresh_durable())

    def boom(*rows, **kwargs):
        raise OSError("catalog disk full")

    monkeypatch.setattr(werkplace_http, "record_filings", boom)
    werkplace_http._catalog_hook_slot.__dict__.clear()

    result = wired.ingest(
        _V1_TEXT, document_id="DOC-9", declared_format="txt"
    )

    warning = getattr(werkplace_http._catalog_hook_slot, "warning", None)
    assert warning is not None
    assert warning.startswith("catalog_record_failed")
    # the push stands and the records persist despite the catalog failure
    assert wired._previous_records() == list(result.records)

    werkplace_http._catalog_hook_slot.__dict__.clear()
