"""The durable publication sequence over the audit trail (T6).

The end-to-end claim of ``specs/qa-chain-corpus-retrieval.md`` section
"Durable catalog publication and recovery", driven through the endpoint's
real ``submit_durable`` / ``run_durable`` pair against the in-memory
ports: accepted metadata is durable BEFORE the worker runs, the push is
recorded before the catalog is touched, the pending row carries the
complete physical row as redo input, and a failed upsert leaves the job
in ``cataloging`` with a typed, count-safe warning.

Recovery cases cover the two restart gaps the spec names: a durable
``push_succeeded`` retries only the upsert, and a crash between the
pending row and the terminal append recovers from the pending row
instead of claiming ``ready``.

The Postgres-backed store cases live in
``test_ingest_state_store_durable.py`` and run only with ``FIN_TEST_DSN``
(reported separately).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

import fin.corpus.catalog as catalog_mod
from fin.corpus.catalog import record_filings
from fin.corpus.catalog_publication import (
    CATALOG_PUBLICATION_FAILED,
    CATALOG_PUBLICATION_PENDING,
    CATALOG_PUBLICATION_SUCCEEDED,
    PUSH_SUCCEEDED,
    UPLOAD_METADATA_ACCEPTED,
    read_accepted_metadata,
    read_push_succeeded,
)
from fin.corpus.errors import CatalogRowError
from fin.corpus.ingest_endpoint import (
    IngestDurableCommitError,
    IngestEndpoint,
)
from fin.corpus.ingest_state import InMemoryIngestStateStore, IngestState
from fin.corpus.ingest_state_errors import IngestStepError
from fin.corpus.metadata_store import InMemoryMetadataStore
from fin.corpus.object_store import InMemoryObjectStore
from fin.retrieve.fake_push_port import FakePushPort
from fin.retrieve.push_port import PushServiceUnavailableError

_SCOPE = "fin-default"
_TEXT = b"# Handbook\n\nOnboarding starts with a laptop and an account.\n"


class _RowRejectingCatalog:
    """A catalog that refuses the upsert with a typed row error."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error or CatalogRowError("row #0 field 'title' is empty")

    def record_filings(
        self, rows: list[dict[str, Any]], *, catalog_path: Path | None = None
    ) -> None:
        raise self.error


def _endpoint(**overrides: Any) -> tuple[IngestEndpoint, InMemoryIngestStateStore]:
    states = InMemoryIngestStateStore()
    kwargs: dict[str, Any] = {
        "push_port": FakePushPort(),
        "object_store": InMemoryObjectStore(),
        "metadata_store": InMemoryMetadataStore(),
        "state_store": states,
        "corpus_scope_id": _SCOPE,
    }
    kwargs.update(overrides)
    return IngestEndpoint(**kwargs), states


def _actions(states: InMemoryIngestStateStore, document_id: str) -> list[str]:
    return [
        row.action
        for row in states.list_audit(document_id)
        if row.action.startswith(
            (
                "upload_metadata_accepted",
                "push_succeeded",
                "catalog_publication",
            )
        )
    ]


def _event_metadata(
    states: InMemoryIngestStateStore, document_id: str, action: str
) -> dict[str, Any]:
    for row in reversed(states.list_audit(document_id)):
        if row.action == action:
            return dict(row.metadata)
    raise AssertionError(f"no {action} audit row for {document_id}")


# ---------- accept: accepted metadata is durable before the worker runs ----------


def test_accepted_metadata_is_persisted_at_accept_before_any_run() -> None:
    """The worker must never start without its redo input durable."""
    endpoint, states = _endpoint()

    submission = endpoint.submit_durable(
        _TEXT, filename="Team Handbook.md"
    )

    assert _actions(states, submission.document_id) == [
        UPLOAD_METADATA_ACCEPTED
    ]
    metadata = _event_metadata(states, submission.document_id, UPLOAD_METADATA_ACCEPTED)
    assert metadata["version"] == submission.version
    assert metadata["title"] == "Team Handbook.md"
    # An accept cannot classify (that needs the parse), so it records
    # ``unknown`` for "no override"; the run classifies and puts the
    # result on the pending-publication row.
    assert metadata["doc_type"] == "unknown"
    assert metadata["schema_version"] == 1


def test_an_explicit_doc_type_override_is_recorded_at_accept() -> None:
    endpoint, states = _endpoint()

    submission = endpoint.submit_durable(
        _TEXT, filename="a.md", doc_type="table_dense"
    )

    metadata = _event_metadata(states, submission.document_id, UPLOAD_METADATA_ACCEPTED)
    assert metadata["doc_type"] == "table_dense"


def test_the_accepted_row_is_json_serializable_and_typed() -> None:
    """The column is JSONB: the payload must survive a round trip and
    never carry a raw exception, path or credential."""
    endpoint, states = _endpoint()

    submission = endpoint.submit_durable(
        _TEXT, filename="a.md", company="Acme", period="2024Q1"
    )

    raw = json.dumps(
        _event_metadata(states, submission.document_id, UPLOAD_METADATA_ACCEPTED)
    )
    assert json.loads(raw)["company"] == "Acme"
    assert "/" not in json.loads(raw).get("title", "a.md")


def test_a_company_and_period_accept_persists_the_event() -> None:
    """Company/period are optional display metadata on the one public
    flow, so that accept still persists its redo input. Only a job
    accepted before this contract has no row and keeps the legacy
    recovery leg (the byte-identical restart contract lives in
    ``tests/corpus/test_ingest_durable_restart.py``)."""
    endpoint, states = _endpoint()

    submission = endpoint.submit_durable(
        _TEXT, company="AAPL", period="2024Q3", declared_format="md"
    )

    accepted = read_accepted_metadata(states.list_audit(submission.document_id))
    assert accepted is not None
    assert accepted.company == "AAPL"
    assert accepted.period == "2024Q3"
    assert [row.action for row in states.list_audit(submission.document_id)] == [
        "job_created",
        UPLOAD_METADATA_ACCEPTED,
    ]


def test_a_failed_accepted_metadata_write_rejects_the_accept() -> None:
    """If the redo input is not durable, nothing may be accepted: no
    worker will ever be able to publish the row."""

    class _RefusingStates(InMemoryIngestStateStore):
        def append_audit(
            self,
            document_id: str,
            action: str,
            metadata: Any,
            *,
            actor: str | None = None,
        ) -> Any:
            raise RuntimeError("audit store is down")

    endpoint = IngestEndpoint(
        push_port=FakePushPort(),
        object_store=InMemoryObjectStore(),
        metadata_store=InMemoryMetadataStore(),
        state_store=_RefusingStates(),
        corpus_scope_id=_SCOPE,
    )

    with pytest.raises(Exception):
        endpoint.submit_durable(_TEXT, filename="a.md")


# ---------- the full publication sequence ----------


def test_the_run_appends_the_five_events_in_order(tmp_path: Path) -> None:
    """push_succeeded -> pending -> upsert -> succeeded -> ready, with the
    accepted metadata already durable from the accept."""
    catalog_path = tmp_path / "catalog.json"
    endpoint, states = _endpoint(
        on_ingested=_writing_hook(catalog_path)
    )

    submission = endpoint.submit_durable(
        _TEXT, filename="Team Handbook.md"
    )
    outcome = endpoint.run_durable(submission.document_id)

    assert outcome.state is IngestState.READY
    assert _actions(states, submission.document_id) == [
        UPLOAD_METADATA_ACCEPTED,
        PUSH_SUCCEEDED,
        CATALOG_PUBLICATION_PENDING,
        CATALOG_PUBLICATION_SUCCEEDED,
    ]


def test_the_pending_row_carries_the_complete_physical_row(
    tmp_path: Path,
) -> None:
    """The pending row IS the redo input: recovery upserts exactly what is
    in it, never a row rebuilt from the id."""
    catalog_path = tmp_path / "catalog.json"
    endpoint, states = _endpoint(on_ingested=_writing_hook(catalog_path))

    submission = endpoint.submit_durable(
        _TEXT,
        filename="Team Handbook.md",
        company="Acme",
        period="2024Q1",
    )
    endpoint.run_durable(submission.document_id)

    pending = _event_metadata(
        states, submission.document_id, CATALOG_PUBLICATION_PENDING
    )
    row = pending["catalog_row"]
    assert row["document_id"] == submission.document_id
    assert row["title"] == "Team Handbook.md"
    assert row["doc_type"] == "narrative"
    assert row["company"] == "Acme"
    assert row["period"] == "2024Q1"
    assert pending["publication_id"] == (
        f"catalog-v1:{submission.document_id}:{submission.version}"
    )
    assert pending["attempt"] == 1


def test_push_succeeded_records_the_content_identity_map(tmp_path: Path) -> None:
    catalog_path = tmp_path / "catalog.json"
    endpoint, states = _endpoint(on_ingested=_writing_hook(catalog_path))

    submission = endpoint.submit_durable(_TEXT, filename="a.md")
    endpoint.run_durable(submission.document_id)

    metadata = _event_metadata(states, submission.document_id, PUSH_SUCCEEDED)
    assert metadata["version"] == submission.version
    assert metadata["content_identities"]


def test_the_job_lands_on_ready_only_after_the_success_event(
    tmp_path: Path,
) -> None:
    catalog_path = tmp_path / "catalog.json"
    endpoint, states = _endpoint(on_ingested=_writing_hook(catalog_path))

    submission = endpoint.submit_durable(_TEXT, filename="a.md")
    endpoint.run_durable(submission.document_id)

    ready = [row for row in states.list_audit(submission.document_id) if row.to_state]
    assert ready[-1].to_state is IngestState.READY
    succeeded = _event_metadata(
        states, submission.document_id, CATALOG_PUBLICATION_SUCCEEDED
    )
    assert succeeded["attempt"] == 1


# ---------- failure: the job stays cataloging ----------


def test_a_failed_upsert_keeps_the_job_cataloging_and_records_the_typing(
    tmp_path: Path,
) -> None:
    """A refused row is a typed failure, not a job failure and not a
    silent skip: the job must stay unready with the failure durable."""
    endpoint, states = _endpoint(
        on_ingested=_writing_hook(tmp_path / "catalog.json")
    )
    # Swap in a catalog that refuses the write after the pending row.
    endpoint._on_ingested = _rejecting_hook(CatalogRowError("title is empty"))

    submission = endpoint.submit_durable(_TEXT, filename="a.md")
    endpoint.run_durable(submission.document_id)

    assert states.get_job(submission.document_id).state is IngestState.CATALOGING
    metadata = _event_metadata(
        states, submission.document_id, CATALOG_PUBLICATION_FAILED
    )
    assert metadata["failure_code"] == "catalog_row_invalid"
    assert metadata["failure_stage"] == "validate"
    assert metadata["attempt"] == 1


def test_a_failed_upsert_never_persists_the_exception_text(tmp_path: Path) -> None:
    endpoint, states = _endpoint()
    endpoint._on_ingested = _rejecting_hook(
        CatalogRowError("row #0 field 'title' at /home/secret/path")
    )

    submission = endpoint.submit_durable(_TEXT, filename="a.md")
    endpoint.run_durable(submission.document_id)

    raw = json.dumps(
        _event_metadata(states, submission.document_id, CATALOG_PUBLICATION_FAILED)
    )
    assert "/home/secret" not in raw
    assert "row #0" not in raw


def test_an_unexpected_programming_error_propagates_as_a_typed_failure(
    tmp_path: Path,
) -> None:
    """A bug is not a catalog warning: it propagates through the existing
    typed job-failure path."""
    endpoint, states = _endpoint()
    endpoint._on_ingested = _rejecting_hook(KeyError("bug"))

    submission = endpoint.submit_durable(_TEXT, filename="a.md")
    outcome = endpoint.run_durable(submission.document_id)

    assert outcome.state is IngestState.FAILED
    assert states.get_job(submission.document_id).state is IngestState.FAILED


# ---------- recovery: the pending row is the redo input ----------


def test_recovery_retries_only_the_upsert_when_push_succeeded_is_durable(
    tmp_path: Path,
) -> None:
    """The spec's first recovery gap: a durable ``push_succeeded`` means
    recovery redoes the catalog write and NOTHING else.

    The terminal-append failure below is that gap exactly: the push and
    its success record are durable, the row was never upserted, and the
    job sits in ``cataloging``. Recovery must write the row without
    pushing a second time — re-pushing would be a second delivery of a
    completed one.
    """
    catalog_path = tmp_path / "catalog.json"
    states = _TerminalAppendFails()
    endpoint, _ = _endpoint(
        on_ingested=_writing_hook(catalog_path), state_store=states
    )

    submission = endpoint.submit_durable(_TEXT, filename="a.md")
    failed = endpoint.run_durable(submission.document_id)

    # The success append was refused, so the run neither wrote a row nor
    # claimed ready; the job stays exactly where recovery can find it.
    # ``CATALOGING``, not ``FAILED``: the push landed and the pending row
    # is durable, so ``failed`` would be a false claim about the pipeline
    # and would route the retry through a second delivery.
    assert states.get_job(submission.document_id).state is IngestState.CATALOGING
    assert failed.state is IngestState.CATALOGING
    assert failed.ran is True
    assert failed.busy is False
    # The typed audit failure still propagates to the caller; only the
    # job's own record of the run is withheld.
    assert isinstance(failed.error, IngestStepError)
    assert failed.error.step == "ingest"
    assert failed.error.cause.__class__ is IngestDurableCommitError
    assert failed.error.cause.stage == "publication_audit"

    # The terminal append now works; recovery re-adopts the job and redoes
    # the publication from the durable pending row.
    states.refuse_terminal = False
    outcome = endpoint.run_durable(submission.document_id)

    assert outcome.state is IngestState.READY
    rows = json.loads(catalog_path.read_text(encoding="utf-8"))["filings"]
    assert len(rows) == 1
    assert rows[0]["document_id"] == submission.document_id


def test_recovery_of_a_durable_push_never_calls_the_push_port(
    tmp_path: Path,
) -> None:
    """The spec's redo boundary, asserted at the PORT.

    A happy-path-only assertion would pass against the broken chain: the
    re-run pushes the same envelopes again, the fake reports them as
    duplicates and the run still lands ``ready``. So the claim is proved
    by the push port's own call count, which a re-run cannot avoid.

    ``push_succeeded`` is durable and the pending row is durable, so
    recovery has nothing to push. What it does have is the recorded
    ``catalog_row`` and the identity map the completing push persisted.
    """
    catalog_path = tmp_path / "catalog.json"
    states = _TerminalAppendFails()
    push = _CountingPushPort()
    endpoint, _ = _endpoint(
        on_ingested=_writing_hook(catalog_path),
        state_store=states,
        push_port=push,
    )

    submission = endpoint.submit_durable(_TEXT, filename="a.md")
    endpoint.run_durable(submission.document_id)
    assert states.get_job(submission.document_id).state is IngestState.CATALOGING
    pushes_after_first_run = push.calls

    states.refuse_terminal = False
    outcome = endpoint.run_durable(submission.document_id)

    assert outcome.state is IngestState.READY
    assert push.calls == pushes_after_first_run, (
        "recovery re-pushed a document whose push_succeeded was already "
        "durable; the spec says it retries only the catalog upsert"
    )


def test_recovery_upserts_the_recorded_catalog_row_verbatim(
    tmp_path: Path,
) -> None:
    """The redo input is the recorded row, not a rebuilt one.

    A recovery that re-derived the row from the id would be the
    reconstruction the contract forbids, and it would show up here as a
    difference from what the pending event recorded. So the assertion is
    field-by-field against the recorded ``catalog_row`` — including the
    optional ``company`` / ``period`` a rebuild would have to invent.

    The recorded row is mutated in place before recovery so the two
    cannot be confused: if the run rebuilt from the id it would publish
    the ORIGINAL title, and the comparison would fail.
    """
    catalog_path = tmp_path / "catalog.json"
    states = _TerminalAppendFails()
    endpoint, _ = _endpoint(
        on_ingested=_writing_hook(catalog_path), state_store=states
    )

    submission = endpoint.submit_durable(
        _TEXT,
        filename="a.md",
        company="Acme",
        period="2024Q1",
    )
    endpoint.run_durable(submission.document_id)

    # Change the durable redo input so a rebuilt row cannot match it.
    recorded = _event_metadata(
        states, submission.document_id, CATALOG_PUBLICATION_PENDING
    )["catalog_row"]
    recorded["title"] = "Recorded title, not a rebuilt one"

    states.refuse_terminal = False
    endpoint.run_durable(submission.document_id)

    upserted = json.loads(catalog_path.read_text(encoding="utf-8"))["filings"]
    assert len(upserted) == 1
    # Compared field-by-field against the RECORDED row rather than as a
    # whole-document equality: the catalog enriches what it writes (it
    # stamps its own ``extract_method``), and that enrichment is not
    # what this test is about. Every field the recorded row owns must
    # match it exactly, so a rebuilt row cannot pass.
    for field in (
        "document_id",
        "version",
        "title",
        "doc_type",
        "page_count",
        "company",
        "period",
        "source_sha256",
    ):
        assert upserted[0].get(field) == recorded.get(field), field
    assert upserted[0]["title"] == "Recorded title, not a rebuilt one"
    assert upserted[0]["company"] == "Acme"
    assert upserted[0]["period"] == "2024Q1"


def test_recovery_lands_the_identity_map_the_push_persisted(
    tmp_path: Path,
) -> None:
    """The ``cataloging -> ready`` edge carries the RECORDED pairs, read
    not recomputed — and the witness is the push PORT, not equality.

    Recovery skipped the push, so it has no service answer of its own:
    the map can only come off the ``push_succeeded`` event. Equality
    with the recorded map is therefore NOT a sufficient assertion — a
    re-run that re-pushed would recompute the identical map by
    idempotent read-back and pass it. So the pairs are marked
    unrecoverable (the port would refuse every call) and the claim is
    that recovery still lands them.

    A re-run against this port raises, which is exactly what the spec
    forbids: the recovery must need no second delivery at all.
    """
    states = _TerminalAppendFails()
    push = _UnreachableAfterFirstPush()
    endpoint, _ = _endpoint(
        on_ingested=_writing_hook(tmp_path / "catalog.json"),
        state_store=states,
        push_port=push,
    )

    submission = endpoint.submit_durable(_TEXT, filename="a.md")
    endpoint.run_durable(submission.document_id)
    recorded = _event_metadata(
        states, submission.document_id, PUSH_SUCCEEDED
    )["content_identities"]
    assert recorded, "the first run must have persisted a non-empty map"
    # From here the retrieval service is gone for good.
    push.refuse = True

    states.refuse_terminal = False
    outcome = endpoint.run_durable(submission.document_id)

    assert outcome.state is IngestState.READY
    assert push.calls == 1, "recovery called a push port it did not need"
    edge = [
        row
        for row in states.list_audit(submission.document_id)
        if row.action == "transition" and row.to_state is IngestState.READY
    ]
    assert len(edge) == 1
    assert edge[0].metadata["content_identities"] == recorded


def test_a_recovery_with_no_durable_push_succeeded_still_runs_the_chain(
    tmp_path: Path,
) -> None:
    """The other leg is unchanged: no proof the push landed means re-push.

    Spec line 131 is explicit that the two systems share no atomic
    transaction, so a run that crashed before recording
    ``push_succeeded`` must resend and let the upstream idempotency
    contract converge. Narrowing the skip to the durable case is what
    keeps this leg working, so it is pinned rather than assumed.
    """
    states = _PushSuccessAppendFails()
    push = _CountingPushPort()
    endpoint, _ = _endpoint(
        on_ingested=_writing_hook(tmp_path / "catalog.json"),
        state_store=states,
        push_port=push,
    )

    submission = endpoint.submit_durable(_TEXT, filename="a.md")
    first = endpoint.run_durable(submission.document_id)

    # The push landed but the record of it did not: exactly the gap the
    # spec says recovery reconciles by resending, never by assuming.
    assert first.state is IngestState.FAILED
    assert read_push_succeeded(
        states.list_audit(submission.document_id),
        version=submission.version,
    ) is False
    pushes_before = push.calls

    states.refuse_push_success = False
    outcome = endpoint.run_durable(submission.document_id)

    assert outcome.state is IngestState.READY
    assert push.calls > pushes_before, (
        "without a durable push_succeeded the run must resend the "
        "envelopes; the spec allows only the durable case to skip"
    )


class _PushSuccessAppendFails(InMemoryIngestStateStore):
    """A store that loses the ``push_succeeded`` row while refusing nothing else.

    The spec's reconciliation gap: the remote push succeeded but fin
    never recorded it, so recovery has no durable proof and must resend.
    """

    def __init__(self) -> None:
        super().__init__()
        self.refuse_push_success = True

    def append_audit(
        self,
        document_id: str,
        action: str,
        metadata: Any,
        *,
        actor: str | None = None,
    ) -> Any:
        if action == PUSH_SUCCEEDED and self.refuse_push_success:
            raise RuntimeError("the audit store lost the push-success row")
        return super().append_audit(document_id, action, metadata, actor=actor)


class _CountingPushPort:
    """Push-port spy counting push CALLS, not envelopes or outcomes.

    The count is the only honest witness that a run did not push: a
    re-push of already-stored envelopes returns clean duplicates, so
    ``written`` / ``duplicates`` / ``block_total`` all look the same
    either way.
    """

    def __init__(self) -> None:
        self.inner = FakePushPort()
        self.calls = 0

    @property
    def push_calls(self) -> int:
        return self.calls

    def push(self, envelopes: Any) -> Any:
        self.calls += 1
        return self.inner.push(envelopes)

    def invalidate(self, *args: Any, **kwargs: Any) -> Any:
        return self.inner.invalidate(*args, **kwargs)

    def recall(self, block_id: str) -> Any:
        return self.inner.recall(block_id)


class _UnreachableAfterFirstPush(_CountingPushPort):
    """A push port that goes away once its first call has landed.

    Stands in for the case that makes the recovery boundary observable:
    the text is already at the service, so a run that still needs to
    push is a run that fails for a reason the contract does not allow.
    """

    def __init__(self) -> None:
        super().__init__()
        self.refuse = False

    def push(self, envelopes: Any) -> Any:
        if self.refuse:
            raise PushServiceUnavailableError(
                "the retrieval service is down"
            )
        return super().push(envelopes)


class _PendingAppendRefused(InMemoryIngestStateStore):
    """A store that REFUSES the pending-publication append and drops it.

    The redo input never lands, while the push and its ``push_succeeded``
    record already did. This is the harder of the two restart gaps: the
    durable push proof says "do not push again", and the missing redo
    input says "there is nothing to upsert". A recovery that treats the
    second fact as "this was never a recovery" falls through to the full
    chain and delivers the same envelopes a second time — the exact
    outcome spec line 131 forbids.
    """

    def __init__(self) -> None:
        super().__init__()
        self.refuse_pending = True

    def append_audit(
        self,
        document_id: str,
        action: str,
        metadata: Any,
        *,
        actor: str | None = None,
    ) -> Any:
        if action == CATALOG_PUBLICATION_PENDING and self.refuse_pending:
            raise RuntimeError("the audit store refused the pending append")
        return super().append_audit(document_id, action, metadata, actor=actor)


def test_a_durable_push_whose_redo_row_is_missing_never_pushes_again(
    tmp_path: Path,
) -> None:
    """A durable ``push_succeeded`` is a proof, and no later missing row
    may override it.

    Spec line 131: "If ``push_succeeded`` is durable, recovery retries
    only the catalog upsert from the recorded ``catalog_row``, without
    pushing again" — and the instruction for a case where recovery cannot
    do that is "keep the job unready and return a typed failure", not
    "run the chain". Re-pushing would be a second delivery of a
    completed push, which the spec explicitly refuses to call a
    recovery.

    So the push port's call count is the whole claim, and the job state
    is the half that goes wrong quietly: ``ready`` would claim a
    publication that never happened, and ``failed`` would send the retry
    edge back through the push of work that already landed.
    """
    catalog_path = tmp_path / "catalog.json"
    states = _PendingAppendRefused()
    push = _CountingPushPort()
    endpoint, _ = _endpoint(
        on_ingested=_writing_hook(catalog_path),
        state_store=states,
        push_port=push,
    )

    submission = endpoint.submit_durable(_TEXT, filename="a.md")
    endpoint.run_durable(submission.document_id)
    pushes_after_first_run = push.calls
    assert pushes_after_first_run == 1

    outcome = endpoint.run_durable(submission.document_id)

    assert push.calls == pushes_after_first_run, (
        "a durable push_succeeded was overridden by a missing redo row and "
        "the chain re-pushed; the spec says recovery never pushes again"
    )
    assert outcome.state is IngestState.CATALOGING
    assert states.get_job(submission.document_id).state is IngestState.CATALOGING


def test_a_missing_redo_row_is_reported_as_a_typed_failure_not_a_job_one(
    tmp_path: Path,
) -> None:
    """The outcome carries the typed failure; the job does not.

    Spec line 131 asks for a typed failure when recovery cannot prove
    the publication. It travels on the outcome, in the same
    :class:`IngestStepError` shape a job failure would have — the
    operator watching the run sees the refusal. The job's own state is
    withheld because ``failed`` is the one thing this state cannot be
    recovered from, exactly as it is for a refused terminal append.
    """
    states = _PendingAppendRefused()
    endpoint, _ = _endpoint(
        on_ingested=_writing_hook(tmp_path / "catalog.json"),
        state_store=states,
    )

    submission = endpoint.submit_durable(_TEXT, filename="a.md")
    endpoint.run_durable(submission.document_id)
    outcome = endpoint.run_durable(submission.document_id)

    assert isinstance(outcome.error, IngestStepError)
    assert outcome.error.cause.__class__ is IngestDurableCommitError
    assert outcome.error.cause.stage == "publication_audit"
    assert states.get_job(submission.document_id).state is IngestState.CATALOGING


class _TerminalAppendFails(InMemoryIngestStateStore):
    """A store whose terminal publication append fails, simulating a crash.

    ``catalog_publication_succeeded`` is the append the spec calls out:
    it fails while the push record and the pending row stay durable,
    which is precisely the state a crash between the row and its success
    event leaves behind. The job must stay unready and recover from the
    pending row rather than re-push.
    """

    def __init__(self) -> None:
        super().__init__()
        self.refuse_terminal = True

    def append_audit(
        self,
        document_id: str,
        action: str,
        metadata: Any,
        *,
        actor: str | None = None,
    ) -> Any:
        if action == CATALOG_PUBLICATION_SUCCEEDED and self.refuse_terminal:
            raise RuntimeError("the audit store refused the terminal append")
        return super().append_audit(document_id, action, metadata, actor=actor)


def test_a_pending_row_with_no_terminal_event_recovers_the_upsert(
    tmp_path: Path,
) -> None:
    """The spec's terminal-append gap: a durable pending row and a job
    still in cataloging means recovery redoes the idempotent upsert."""
    catalog_path = tmp_path / "catalog.json"
    fail_once = _FlakyCatalog(catalog_path)
    endpoint, states = _endpoint(on_ingested=fail_once)

    submission = endpoint.submit_durable(_TEXT, filename="a.md")
    endpoint.run_durable(submission.document_id)

    # The first attempt failed; the job is cataloging with a pending row
    # and a typed failure, and no success.
    assert states.get_job(submission.document_id).state is IngestState.CATALOGING
    assert CATALOG_PUBLICATION_SUCCEEDED not in _actions(
        states, submission.document_id
    )
    fail_once.error = None

    # The recovery re-adopts the cataloging job and redoes the upsert from
    # the durable pending row.
    endpoint.run_durable(submission.document_id)

    assert states.get_job(submission.document_id).state is IngestState.READY
    assert _event_metadata(
        states, submission.document_id, CATALOG_PUBLICATION_SUCCEEDED
    )["attempt"] == 2
    rows = json.loads(catalog_path.read_text(encoding="utf-8"))["filings"]
    assert len(rows) == 1
    assert rows[0]["title"] == "a.md"


# ---------- polling projection ----------


def test_a_ready_job_polls_with_no_catalog_warning(tmp_path: Path) -> None:
    catalog_path = tmp_path / "catalog.json"
    endpoint, _ = _endpoint(on_ingested=_writing_hook(catalog_path))

    submission = endpoint.submit_durable(_TEXT, filename="a.md")
    endpoint.run_durable(submission.document_id)

    status = endpoint.job_status(submission.document_id)

    assert status.catalog_warning is None
    assert status.catalog_warning_count == 0


def test_a_failed_publication_polls_a_typed_warning() -> None:
    endpoint, _ = _endpoint()
    endpoint._on_ingested = _rejecting_hook(CatalogRowError("title is empty"))

    submission = endpoint.submit_durable(_TEXT, filename="a.md")
    endpoint.run_durable(submission.document_id)

    status = endpoint.job_status(submission.document_id)

    assert status.catalog_warning is not None
    assert status.catalog_warning.code == "catalog_publication_failed"
    assert status.catalog_warning.failure_code == "catalog_row_invalid"
    assert status.catalog_warning.failure_stage == "validate"
    assert status.catalog_warning.attempts == 1
    assert status.catalog_warning_count == 1


def test_the_warning_never_carries_raw_exception_text() -> None:
    endpoint, _ = _endpoint()
    endpoint._on_ingested = _rejecting_hook(
        OSError("/home/secret/path/catalog.json is not writable")
    )

    submission = endpoint.submit_durable(_TEXT, filename="a.md")
    endpoint.run_durable(submission.document_id)

    warning = endpoint.job_status(submission.document_id).catalog_warning
    assert warning is not None
    assert warning.failure_code == "catalog_unavailable"
    rendered = repr(warning)
    assert "/home/secret" not in rendered
    assert "not writable" not in rendered


def test_the_warning_count_is_documents_not_attempts(tmp_path: Path) -> None:
    """Two documents with unresolved publications count 2, however many
    times each was retried."""
    endpoint, _ = _endpoint()
    endpoint._on_ingested = _rejecting_hook(CatalogRowError("always"))

    first = endpoint.submit_durable(b"first document", filename="a.md")
    second = endpoint.submit_durable(b"second document", filename="b.md")
    endpoint.run_durable(first.document_id)
    endpoint.run_durable(second.document_id)
    endpoint.run_durable(first.document_id)

    assert endpoint.catalog_warning_count() == 2


def test_recovery_clears_the_warning_and_the_count(tmp_path: Path) -> None:
    catalog_path = tmp_path / "catalog.json"
    fail_once = _FlakyCatalog(catalog_path)
    endpoint, _ = _endpoint(on_ingested=fail_once)

    submission = endpoint.submit_durable(_TEXT, filename="a.md")
    endpoint.run_durable(submission.document_id)
    assert endpoint.job_status(submission.document_id).catalog_warning is not None

    fail_once.error = None
    endpoint.run_durable(submission.document_id)

    status = endpoint.job_status(submission.document_id)
    assert status.catalog_warning is None
    assert status.catalog_warning_count == 0


# ---------- helpers ----------


def _writing_hook(catalog_path: Path):
    """The production-shaped hook writing the generic physical row."""

    def hook(doc: Any) -> None:
        row: dict[str, Any] = {
            "document_id": doc.document_id,
            "version": doc.version,
            "title": doc.title,
            "doc_type": doc.doc_type,
            "page_count": doc.page_count,
        }
        if doc.company:
            row["company"] = doc.company
        if doc.period:
            row["period"] = doc.period
        if doc.source_sha256:
            row["source_sha256"] = doc.source_sha256
        record_filings([row], catalog_path=catalog_path)

    return hook


def _rejecting_hook(error: Exception):
    """A hook that always raises, standing in for a failing catalog."""

    def hook(doc: Any) -> None:
        raise error

    return hook


class _FlakyCatalog:
    """Fails its first write attempt, then succeeds."""

    def __init__(self, catalog_path: Path) -> None:
        self.catalog_path = catalog_path
        self.error: Exception | None = CatalogRowError("temporary refusal")

    def __call__(self, doc: Any) -> None:
        if self.error is not None:
            raise self.error
        _writing_hook(self.catalog_path)(doc)


@pytest.fixture(autouse=True)
def _isolate_catalog_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(
        catalog_mod, "DEFAULT_CATALOG_PATH", tmp_path / "unused.json"
    )
