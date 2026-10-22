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
  * a catalog failure during a durable run is a TYPED, AUDIT-DERIVED
    warning on poll (T6): the hook propagates, the endpoint records
    ``catalog_publication_failed``, the job stays ``cataloging``, and
    ``GET /api/corpus/ingest/{id}`` surfaces it — never a thread-local
    transport.
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
from fin.retrieve.fake_index_job_port import FakeIndexJobPort
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
    monkeypatch.setenv(
        "FIN_CORPUS_INDEX_JOB_LEDGER_PATH",
        str(tmp_path / "index_jobs.jsonl"),
    )


def _fresh_durable() -> tuple[Any, Any, Any]:
    """A private accept-time stack for one test.

    ``preflight_ingest_storage()`` caches one process-wide snapshot; the
    scenarios below move documents through pointer/job states, so they
    must not inherit another test's durable state.
    """
    return InMemoryObjectStore(), InMemoryMetadataStore(), InMemoryIngestStateStore()


def _submit(service: IngestEndpoint, data: bytes) -> Any:
    return service.submit_durable(data, declared_format="txt")


def _document_id(scope: str, data: bytes) -> str:
    """The public upload's content-derived id, written out longhand."""
    digest = hashlib.sha256(data).digest()
    preimage = (
        b"fin-corpus-document-v1\x00" + scope.encode("utf-8") + b"\x00" + digest
    )
    return "doc_" + hashlib.sha256(preimage).hexdigest()


def _seed_ready_generation(
    service: IngestEndpoint, *, document_id: str, data: bytes
) -> None:
    """Install the durable generation a replacement upload supersedes.

    The public flow derives its id from the submitted bytes, so two
    different byte strings can never name one document through the
    accept door alone. A replacement is still the settled lifecycle for
    an id an internal/legacy caller already owns, and this seeds that
    state directly so the wiring can be exercised without a second
    public mode.
    """
    version = hashlib.sha256(data).hexdigest()
    pointer = service._object_store.put(  # type: ignore[union-attr]
        f"filings/{document_id}/{version}", data, content_type="text/plain"
    )
    service._metadata_store.put_pointer(document_id, pointer)  # type: ignore[union-attr]
    service._state_store.create_job(document_id)  # type: ignore[union-attr]
    _advance(service._state_store, document_id)  # type: ignore[arg-type]


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
        corpus_scope_id=service._corpus_scope_id,
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
        "FIN_CORPUS_INDEX_JOB_LEDGER_PATH",
    ):
        monkeypatch.delenv(name, raising=False)
    catalog = tmp_path / "catalog.json"

    components = _ingest_components(catalog_path=catalog)

    assert components["push_journal_path"] == tmp_path / "push_journal.jsonl"
    assert components["invalidation_ledger_path"] == (
        tmp_path / "invalidation_ledger.jsonl"
    )
    assert components["pushed_records_path"] == tmp_path / "pushed_records.jsonl"
    # The index-job ledger joins the visible data dir: submitted job
    # ids are restart state like every other ingest journal.
    assert components["index_job_ledger_path"] == (
        tmp_path / "index_jobs.jsonl"
    )


def test_components_honor_configured_paths(monkeypatch, tmp_path) -> None:
    journal = tmp_path / "j.jsonl"
    ledger = tmp_path / "l.jsonl"
    records = tmp_path / "r.jsonl"
    index_jobs = tmp_path / "i.jsonl"
    monkeypatch.setenv("FIN_CORPUS_PUSH_JOURNAL_PATH", str(journal))
    monkeypatch.setenv("FIN_CORPUS_INVALIDATION_LEDGER_PATH", str(ledger))
    monkeypatch.setenv("FIN_CORPUS_PUSHED_RECORDS_PATH", str(records))
    monkeypatch.setenv("FIN_CORPUS_INDEX_JOB_LEDGER_PATH", str(index_jobs))

    components = _ingest_components(catalog_path=tmp_path / "catalog.json")

    assert components["push_journal_path"] == journal
    assert components["invalidation_ledger_path"] == ledger
    assert components["pushed_records_path"] == records
    assert components["index_job_ledger_path"] == index_jobs


# ----- service build -------------------------------------------------------


def test_booting_the_service_writes_nothing(tmp_path, monkeypatch) -> None:
    _journal_env(monkeypatch, tmp_path)

    _build_default_ingest_service()

    assert list(tmp_path.iterdir()) == []


def test_booting_the_index_job_tracker_writes_nothing(tmp_path, monkeypatch) -> None:
    _journal_env(monkeypatch, tmp_path)
    monkeypatch.setenv("FIN_RETRIEVE_ADAPTER", "fake")
    monkeypatch.setattr(werkplace_http, "_index_job_singleton", None)

    tracker, ledger = werkplace_http._get_index_job_components()

    assert list(tmp_path.iterdir()) == []
    # One process-wide pair: the worker's hook and the status route's
    # dependency resolve to the same tracker and the same ledger.
    assert werkplace_http.get_index_job_tracker() is tracker
    assert werkplace_http._get_index_job_components() == (tracker, ledger)
    assert ledger.path == tmp_path / "index_jobs.jsonl"


def test_index_job_port_follows_the_adapter_kind_in_both_boots(
    tmp_path, monkeypatch
) -> None:
    """Fake boot gets the contract fake; the product boot gets the MCP
    stdio adapter on the same default command config as the push port —
    selected the same way the service selects its push face, and built
    without ever reaching the service."""
    from fin.corpus.index_job import IndexJobTracker
    from fin.retrieve.mcp_index_job_port import MCPIndexJobPort

    _journal_env(monkeypatch, tmp_path)

    monkeypatch.setattr(werkplace_http, "_index_job_singleton", None)
    monkeypatch.setenv("FIN_RETRIEVE_ADAPTER", "fake")
    tracker, _ledger = werkplace_http._get_index_job_components()
    assert isinstance(tracker, IndexJobTracker)
    assert isinstance(tracker._port, FakeIndexJobPort)  # type: ignore[attr-defined]

    monkeypatch.setattr(werkplace_http, "_index_job_singleton", None)
    monkeypatch.setenv("FIN_RETRIEVE_ADAPTER", "mcp")
    tracker, _ledger = werkplace_http._get_index_job_components()
    assert isinstance(tracker._port, MCPIndexJobPort)  # type: ignore[attr-defined]


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

    replacement_doc = _document_id(wired._corpus_scope_id, _V2_TEXT)
    first = _submit(wired, _V1_TEXT)
    result = wired.ingest(
        _V1_TEXT, document_id=replacement_doc, declared_format="txt"
    )
    assert result.invalidation is None  # nothing stale against an empty store
    _seed_ready_generation(
        wired, document_id=replacement_doc, data=_V1_TEXT
    )

    second = _submit(wired, _V2_TEXT)

    # different bytes are a different public document, never a silent
    # replacement of the first upload
    assert second.job_id == replacement_doc
    assert second.job_id != first.job_id
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
    assert second.state is IngestState.PENDING
    assert wired._state_store.get_job(replacement_doc).state is (
        IngestState.PENDING
    )
    superseded = [
        row
        for row in wired._state_store.list_audit(replacement_doc)
        if row.action == "job_superseded"
    ]
    assert [(row.from_state, row.to_state) for row in superseded] == [
        (IngestState.READY, IngestState.PENDING)
    ]
    assert _pointer_sha_of(wired._metadata_store, replacement_doc) == second.version


def test_ready_generation_record_is_not_lost_by_the_closure(
    tmp_path, monkeypatch
) -> None:
    # The wired closure must not rewrite the audit trail: the ready
    # generation's transitions stay, the reopen adds one row.
    _journal_env(monkeypatch, tmp_path)
    service = _build_default_ingest_service()
    fake = FakePushPort()
    wired = _wired(service, fake, durable=_fresh_durable())

    replacement_doc = _document_id(wired._corpus_scope_id, _V2_TEXT)
    wired.ingest(_V1_TEXT, document_id=replacement_doc, declared_format="txt")
    _seed_ready_generation(
        wired, document_id=replacement_doc, data=_V1_TEXT
    )

    _submit(wired, _V2_TEXT)

    actions = [
        row.action
        for row in wired._state_store.list_audit(replacement_doc)
    ]
    assert actions.count("job_superseded") == 1
    assert len(actions) > 1  # the reopen added a row; it replaced none
    # the document still names exactly one logical version after reopen
    assert _pointer_sha_of(
        wired._metadata_store, replacement_doc
    ) == hashlib.sha256(_V2_TEXT).hexdigest()


def test_same_version_resubmit_and_reingest_trigger_no_invalidation(
    tmp_path, monkeypatch
) -> None:
    # A re-push of an unchanged pair must not trigger invalidation.
    _journal_env(monkeypatch, tmp_path)
    service = _build_default_ingest_service()
    fake = FakePushPort()
    wired = _wired(service, fake, durable=_fresh_durable())

    doc = _document_id(wired._corpus_scope_id, _V1_TEXT)
    first = _submit(wired, _V1_TEXT)
    pushed = wired.ingest(_V1_TEXT, document_id=doc, declared_format="txt")
    _advance(wired._state_store, doc)

    joined = _submit(wired, _V1_TEXT)  # duplicate: joins, no supersede
    again = wired.ingest(_V1_TEXT, document_id=doc, declared_format="txt")

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

    replacement_doc = _document_id(first_wired._corpus_scope_id, _V2_TEXT)
    pushed = first_wired.ingest(
        _V1_TEXT, document_id=replacement_doc, declared_format="txt"
    )
    _seed_ready_generation(
        first_wired, document_id=replacement_doc, data=_V1_TEXT
    )

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


# ----- catalog publication semantics through the durable run -----------------


def test_a_catalog_failure_is_a_typed_warning_derived_from_the_audit(
    tmp_path, monkeypatch
) -> None:
    """A catalog outage is durable, typed, and pollable — never swallowed.

    The hook propagates the failure, the endpoint records the typed
    ``catalog_publication_failed`` event, the job stays ``cataloging``
    (pushed and searchable, but not ready), and polling surfaces the
    warning. Nothing travels through thread-local state, so a poll
    after a restart sees the same warning.
    """
    _journal_env(monkeypatch, tmp_path)
    service = _build_default_ingest_service()
    wired = _wired(service, FakePushPort(), durable=_fresh_durable())

    def boom(*rows, **kwargs):
        raise OSError("catalog disk full")

    monkeypatch.setattr(werkplace_http, "record_filings", boom)

    submitted = _submit(wired, _V1_TEXT)
    outcome = wired.run_durable(submitted.document_id)

    # The push stands; the catalog write did not.
    assert outcome.state is IngestState.CATALOGING
    status = wired.job_status(submitted.document_id)
    assert status.state is IngestState.CATALOGING
    assert status.catalog_warning is not None
    assert status.catalog_warning.code == "catalog_publication_failed"
    assert status.catalog_warning.failure_code == "catalog_unavailable"
    assert status.catalog_warning_count == 1
    # The push landed despite the catalog failure, so the replacement
    # closure still has the records to close.
    assert wired._previous_records()


def test_the_poll_body_carries_no_raw_exception_text(
    tmp_path, monkeypatch
) -> None:
    """The route's warning body is a closed vocabulary, never a message."""
    from fin.host.workspace_http import _catalog_warning_body

    _journal_env(monkeypatch, tmp_path)
    service = _build_default_ingest_service()
    wired = _wired(service, FakePushPort(), durable=_fresh_durable())

    def boom(*rows, **kwargs):
        raise OSError("/home/secret/only/here/catalog.json is unwritable")

    monkeypatch.setattr(werkplace_http, "record_filings", boom)

    submitted = _submit(wired, _V1_TEXT)
    wired.run_durable(submitted.document_id)
    body = _catalog_warning_body(wired.job_status(submitted.document_id).catalog_warning)

    assert body is not None
    assert body["code"] == "catalog_publication_failed"
    assert body["failure_code"] == "catalog_unavailable"
    assert body["attempts"] == 1
    assert "failure_stage" in body
    rendered = json.dumps(body)
    assert "/home/secret" not in rendered
    assert "unwritable" not in rendered
