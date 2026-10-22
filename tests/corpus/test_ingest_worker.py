"""Hermetic tests for the durable corpus ingest worker (T3-A).

The T2 accept door persists bytes + pointer + a ``pending`` job and
nothing executed that job. These tests pin the worker half of the
permanent-upload contract (specs/corpus-serve-ingest.md, Decision 3):

* discovery — only resumable states are listed from the durable store;
* a ``pending`` job runs to ``ready`` reading the bytes back from the
  object store through the durable pointer (no re-upload);
* the authoritative ``content_identity`` map the push returned is
  persisted on the ``cataloging -> ready`` transition metadata;
* a job interrupted mid-run (process restart) is resumed by a fresh
  worker over the same durable state;
* a concurrent second worker cannot double-run one version — it gets
  the typed busy outcome or joins the completed one;
* an adopter that loses a mid-run adoption race gets the same busy
  outcome and never fails the live job it was resuming;
* malformed bytes end in a typed ``failed`` job visible through
  ``job_status``, never an empty success;
* a push dependency failure is retryable: ``failed -> fetching`` with
  the journal and the store's ``(source_ref, body_content_id)`` dedup
  making the replay idempotent;
* a journal-skipped restart run still hands out the COMPLETE
  identity map (idempotent re-push read-back, never local recompute).

Everything runs on the in-memory stores, the contract-fake push port
and tmp-path journals: the default suite stays hermetic.
"""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path
from typing import Any, Mapping

import pytest

from fin.corpus.ingest_endpoint import (
    IngestDurableCommitError,
    IngestEndpoint,
)
from fin.corpus.ingest_state import (
    InMemoryIngestStateStore,
    IngestState,
)
from fin.corpus.ingest_state_errors import (
    IngestStepError,
    InvalidStateError,
    UnknownJobError,
)
from fin.corpus.ingest_state_memory import InMemoryIngestStateStore as _Memory
from fin.corpus.ingest_worker import RESUMABLE_STATES, IngestWorker
from fin.corpus.metadata_store import InMemoryMetadataStore
from fin.corpus.object_store import InMemoryObjectStore, ObjectPointer
from fin.retrieve.fake_push_port import FakePushPort
from fin.retrieve.iknow_identity import compute_iknow_content_identity
from fin.retrieve.push_port import PushServiceUnavailableError

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_MD_SAMPLE = (
    _PROJECT_ROOT
    / "data/corpus/dataset/documents/md/AAPL-2022Q3__table_dense__md__md.md"
)
_SCOPE = "fin-ingest-worker"

_TXT_V1 = (
    "Quarterly results overview.\n\n"
    "Revenue grew across every segment this quarter.\n\n"
    "The company reaffirmed its full-year guidance."
).encode("utf-8")


def _document_id(scope: str, data: bytes) -> str:
    """The public upload's content-derived id, written out longhand."""
    digest = hashlib.sha256(data).digest()
    preimage = (
        b"fin-corpus-document-v1\x00" + scope.encode("utf-8") + b"\x00" + digest
    )
    return "doc_" + hashlib.sha256(preimage).hexdigest()


DOC = _document_id(_SCOPE, _TXT_V1)

# A supported format whose bytes are broken: accepted at the door
# (format recognition passes), then a typed parse failure in the worker
# (spec "format / doc_type validation staging").
_MALFORMED_DOCX = b"PK\x03\x04not really a zip container at all"
# PDF leg of the same class: the door accepts the declared format, the
# shared PDF page core then names the container malformed rather than
# the run yielding an empty success.
_MALFORMED_PDF = b"%PDF-1.4\nnot a real PDF body at all\n%%EOF\n"


class _RecordingObjectStore(InMemoryObjectStore):
    """Object store that records reads and writes by key."""

    def __init__(self) -> None:
        super().__init__()
        self.puts: list[str] = []
        self.gets: list[str] = []

    def put(
        self,
        key: str,
        payload: bytes,
        *,
        content_type: str | None = None,
    ) -> ObjectPointer:
        self.puts.append(key)
        return super().put(key, payload, content_type=content_type)

    def get(self, key: str) -> bytes:
        self.gets.append(key)
        return super().get(key)


class _CountingPushPort:
    """Push-port spy around the contract fake; can fail named calls."""

    def __init__(
        self, *, fail_calls: set[int] | None = None
    ) -> None:
        self.inner = FakePushPort()
        self.calls = 0
        self.responses: list[dict[str, Any]] = []
        self._fail_calls = set(fail_calls or ())

    def push(self, envelopes: Any) -> dict[str, Any]:
        self.calls += 1
        if self.calls in self._fail_calls:
            raise PushServiceUnavailableError(
                f"push backend down on call {self.calls}"
            )
        response = self.inner.push(envelopes)
        self.responses.append(response)
        return response

    def invalidate(self, *args: Any, **kwargs: Any) -> Any:
        return self.inner.invalidate(*args, **kwargs)

    def recall(self, *args: Any, **kwargs: Any) -> Any:
        return self.inner.recall(*args, **kwargs)


class _AdoptionRaceStore(InMemoryIngestStateStore):
    """State store where a competing live adopter lands one move first.

    The first ``-> extracting`` drive is made by that other worker: the
    job is advanced just before this worker's own adoption transition,
    so the transition raises exactly the busy error a losing adopter
    hits against a live job.
    """

    def __init__(self) -> None:
        super().__init__()
        self._race_played = False

    def transition(
        self,
        document_id: str,
        to_state: IngestState | str,
        *,
        actor: str | None = None,
        expected_state: IngestState | str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> IngestState:
        if to_state == IngestState.EXTRACTING and not self._race_played:
            self._race_played = True
            super().transition(document_id, IngestState.EXTRACTING)
        return super().transition(
            document_id,
            to_state,
            actor=actor,
            expected_state=expected_state,
            metadata=metadata,
        )


class _Stack:
    """One durable endpoint over the three accept-time ports + a spy."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        push: _CountingPushPort | None = None,
        on_ingested: Any = None,
        states: InMemoryIngestStateStore | None = None,
    ) -> None:
        self.objects = _RecordingObjectStore()
        self.pointers = InMemoryMetadataStore()
        self.states = states or InMemoryIngestStateStore()
        self.push = push or _CountingPushPort()
        self.journal_dir = tmp_path / "push-journal"
        self.endpoint = IngestEndpoint(
            push_port=self.push,
            invalidation_port=self.push.inner,
            object_store=self.objects,
            metadata_store=self.pointers,
            state_store=self.states,
            on_ingested=on_ingested,
            corpus_scope_id=_SCOPE,
        )

    def submit(self, data: bytes = _TXT_V1, **overrides: Any) -> Any:
        overrides.setdefault("declared_format", "txt")
        return self.endpoint.submit_durable(data, **overrides)

    def worker(self, **overrides: Any) -> IngestWorker:
        overrides.setdefault("journal_dir", self.journal_dir)
        return IngestWorker(self.endpoint, **overrides)

    def status(self, document_id: str = DOC) -> Any:
        return self.endpoint.job_status(document_id)

    def ready_metadata(self, document_id: str = DOC) -> dict[str, Any]:
        """The audit metadata the ``cataloging -> ready`` edge carried."""
        rows = [
            row
            for row in self.states.list_audit(document_id)
            if row.action == "transition"
            and row.from_state is IngestState.CATALOGING
            and row.to_state is IngestState.READY
        ]
        assert len(rows) == 1, rows
        return dict(rows[0].metadata)


# ----- discovery ------------------------------------------------------


def test_only_resumable_states_are_listed(tmp_path: Path) -> None:
    store = _Memory()
    store.create_job("PENDING-DOC")
    store.create_job("READY-DOC")
    for state in (
        IngestState.FETCHING,
        IngestState.EXTRACTING,
        IngestState.CATALOGING,
        IngestState.READY,
    ):
        store.transition("READY-DOC", state)
    store.create_job("FAILED-DOC")
    store.transition("FAILED-DOC", IngestState.FETCHING)
    with pytest.raises(IngestStepError):
        store.fail_step(
            "FAILED-DOC", step="ingest", cause=RuntimeError("boom")
        )
    store.create_job("INFLIGHT-DOC")
    store.transition("INFLIGHT-DOC", IngestState.FETCHING)

    assert list(RESUMABLE_STATES) == [
        IngestState.PENDING,
        IngestState.FAILED,
        IngestState.FETCHING,
        IngestState.EXTRACTING,
        IngestState.OCR,
        IngestState.CATALOGING,
    ]
    listed = {
        job.document_id for job in store.list_jobs(states=RESUMABLE_STATES)
    }
    assert listed == {"PENDING-DOC", "FAILED-DOC", "INFLIGHT-DOC"}
    # string names filter too; the unfiltered read keeps its old shape
    assert {
        job.document_id
        for job in store.list_jobs(states=["fetching", "extracting"])
    } == {"INFLIGHT-DOC"}
    assert len(store.list_jobs()) == 4
    assert store.list_jobs(states=()) == ()
    with pytest.raises(InvalidStateError):
        store.list_jobs(states=["running"])


# ----- happy path -----------------------------------------------------


def test_worker_runs_a_pending_job_to_ready_from_stored_bytes(
    tmp_path: Path,
) -> None:
    stack = _Stack(tmp_path)
    submission = stack.submit()

    outcomes = stack.worker().run_pending()

    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome.ran is True
    assert outcome.busy is False
    assert outcome.state is IngestState.READY
    assert outcome.version == submission.version
    status = stack.status()
    assert status.state is IngestState.READY
    assert status.text_ready is True
    assert status.version == submission.version
    # The bytes were read back through the durable pointer, not
    # re-supplied: the object store saw exactly one write (the accept)
    # and the worker fetched the pointer's key.
    key = f"filings/{DOC}/{submission.version}"
    assert stack.objects.puts == [key]
    assert key in stack.objects.gets
    # The pushed text landed at the service.
    response = stack.push.responses[0]
    assert response["written"] >= 1


def test_worker_pushes_bodies_that_recall_from_the_service(
    tmp_path: Path,
) -> None:
    source = _MD_SAMPLE.read_bytes()
    stack = _Stack(tmp_path)
    stack.submit(source, declared_format="md")

    outcome = stack.worker().run_pending()[0]

    assert outcome.state is IngestState.READY
    assert outcome.result is not None
    pushed = "".join(envelope.body for envelope in outcome.result.envelopes)
    # The stored document's own text reached the service (frozen
    # normalization may adjust whitespace, the content must not change).
    assert "Securities and Exchange Commission" in pushed
    for envelope in outcome.result.envelopes:
        for chunk in envelope.chunks:
            assert chunk.content in envelope.body


def test_content_identities_are_persisted_on_the_ready_edge(
    tmp_path: Path,
) -> None:
    stack = _Stack(tmp_path)
    stack.submit()

    outcome = stack.worker().run_pending()[0]

    assert outcome.result is not None
    persisted = stack.ready_metadata()["content_identities"]
    assert persisted  # never an empty map on a completing run
    assert set(persisted) == {
        envelope.source_ref for envelope in outcome.result.envelopes
    }
    # The values are the service-returned identities, which for the
    # contract fake equal the iknow formula over the pushed body
    # (spec Decision 2) — fin never recomputes them itself.
    for envelope in outcome.result.envelopes:
        assert persisted[envelope.source_ref] == (
            compute_iknow_content_identity(envelope.body)
        )


# ----- restart resume -------------------------------------------------


def test_fresh_worker_resumes_a_job_left_mid_run(tmp_path: Path) -> None:
    stack = _Stack(tmp_path)
    stack.submit()
    # A dead worker got as far as "the bytes are in hand, parsing
    # started" — only durable state survives to the next process.
    stack.states.transition(DOC, IngestState.FETCHING)
    stack.states.transition(DOC, IngestState.EXTRACTING)

    outcomes = stack.worker().run_pending()

    assert [o.state for o in outcomes] == [IngestState.READY]
    assert outcomes[0].ran is True
    assert stack.status().state is IngestState.READY
    # A second sweep finds nothing left to do.
    assert stack.worker().run_pending() == ()


def test_interrupted_cataloging_resumes_without_reupload(tmp_path: Path) -> None:
    stack = _Stack(tmp_path)
    submission = stack.submit()
    for state in (
        IngestState.FETCHING,
        IngestState.EXTRACTING,
        IngestState.CATALOGING,
    ):
        stack.states.transition(DOC, state)

    outcome = stack.worker().run_pending()[0]

    assert outcome.state is IngestState.READY
    assert submission.state is IngestState.PENDING
    assert stack.objects.puts == [f"filings/{DOC}/{submission.version}"]


# ----- concurrency ----------------------------------------------------


def test_concurrent_workers_run_one_version_exactly_once(
    tmp_path: Path,
) -> None:
    stack = _Stack(tmp_path)
    stack.submit()
    workers = [stack.worker(), stack.worker()]
    start = threading.Barrier(2)
    results: list[Any] = []
    results_lock = threading.Lock()

    def run(worker: IngestWorker) -> None:
        start.wait()
        outcome = worker.run_job(DOC)
        with results_lock:
            results.append(outcome)

    threads = [
        threading.Thread(target=run, args=(worker,)) for worker in workers
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(results) == 2
    runners = [outcome for outcome in results if outcome.ran]
    others = [outcome for outcome in results if not outcome.ran]
    assert len(runners) == 1, results
    assert len(others) == 1, results
    loser = others[0]
    # The loser either joined the finished job or got the typed busy
    # outcome for the in-flight one — it never ran a second chain.
    assert loser.busy or loser.state is IngestState.READY
    assert stack.status().state is IngestState.READY
    # One write set at the service: the losing worker pushed nothing.
    total_written = sum(
        response["written"] for response in stack.push.responses
    )
    assert total_written == len(runners[0].result.records)


def test_adoption_race_loser_joins_busy_and_never_fails_a_live_job(
    tmp_path: Path,
) -> None:
    race = _AdoptionRaceStore()
    stack = _Stack(tmp_path, states=race)
    stack.submit()
    # A live worker claimed the job; a second one adopts it mid-run and
    # loses the adoption transition race to the first one's move.
    race.transition(DOC, IngestState.FETCHING)

    outcome = stack.worker().run_job(DOC)

    # The loser joins with the typed busy outcome — it never runs a
    # second chain and never marks the other worker's live job failed.
    assert outcome.ran is False
    assert outcome.busy is True
    assert outcome.state is IngestState.EXTRACTING
    assert stack.push.calls == 0
    status = stack.status()
    assert status.state is IngestState.EXTRACTING
    assert not [
        row
        for row in race.list_audit(DOC)
        if row.action == "step_failed"
    ]


# ----- typed failures -------------------------------------------------


def test_malformed_supported_format_fails_typed_and_pushes_nothing(
    tmp_path: Path,
) -> None:
    stack = _Stack(tmp_path)
    submitted = stack.submit(_MALFORMED_DOCX, declared_format="docx")

    outcome = stack.worker().run_pending()[0]

    assert outcome.state is IngestState.FAILED
    assert stack.push.calls == 0
    status = stack.status(submitted.document_id)
    assert status.state is IngestState.FAILED
    assert status.error_type == "MalformedSourceParseError"
    assert status.error_message


def test_malformed_pdf_fails_typed_and_pushes_nothing(
    tmp_path: Path,
) -> None:
    stack = _Stack(tmp_path)
    submitted = stack.submit(_MALFORMED_PDF, declared_format="pdf")

    outcome = stack.worker().run_pending()[0]

    assert outcome.state is IngestState.FAILED
    assert stack.push.calls == 0
    status = stack.status(submitted.document_id)
    assert status.state is IngestState.FAILED
    assert status.error_type == "MalformedSourceParseError"
    assert status.error_message


def test_corrupted_object_fails_typed_before_any_push(
    tmp_path: Path,
) -> None:
    stack = _Stack(tmp_path)
    stack.submit()
    # Overwrite the stored bytes under the same key with a different
    # digest: the durable pointer no longer vouches for what is read.
    key = f"filings/{DOC}/{hashlib.sha256(_TXT_V1).hexdigest()}"
    stack.objects.put(key, b"someone replaced the raw object")

    outcome = stack.worker().run_pending()[0]

    assert outcome.state is IngestState.FAILED
    assert stack.push.calls == 0
    status = stack.status()
    assert status.error_type == IngestDurableCommitError.__name__


def test_failed_outcome_carries_a_typed_step_error(tmp_path: Path) -> None:
    stack = _Stack(tmp_path)
    stack.submit(_MALFORMED_DOCX, declared_format="docx")

    outcome = stack.worker().run_pending()[0]

    assert isinstance(outcome.error, IngestStepError)


# ----- retryability + idempotence --------------------------------------


def test_push_dependency_failure_retries_without_reupload(
    tmp_path: Path,
) -> None:
    push = _CountingPushPort(fail_calls={1})
    stack = _Stack(tmp_path, push=push)
    submission = stack.submit()

    first = stack.worker().run_pending()[0]
    assert first.state is IngestState.FAILED
    assert stack.status().error_type == PushServiceUnavailableError.__name__

    retry = stack.worker().run_pending()[0]

    assert retry.state is IngestState.READY
    assert retry.ran is True
    # No re-upload: the accept wrote the object once; the retry read it
    # back through the pointer.
    assert stack.objects.puts == [f"filings/{DOC}/{submission.version}"]
    assert stack.status().state is IngestState.READY
    assert push.calls == 2


class _PushSuccessLostStore(InMemoryIngestStateStore):
    """A store that loses the ``push_succeeded`` row once, then recovers.

    The crash the identity read-back exists for: the envelopes landed
    and the journal says so, but fin never recorded the push, so there
    is no durable proof of it. That is the ONE leg where recovery must
    re-run the chain (spec line 131) — and where the resumed run's own
    push skips the journaled batch, leaving the identity map incomplete
    unless the run reads the stored pairs back.
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
        if action == "push_succeeded" and self.refuse_push_success:
            raise RuntimeError("the audit store lost the push-success row")
        return super().append_audit(
            document_id, action, metadata, actor=actor
        )


def test_journal_skipped_batches_still_yield_the_complete_identity_map(
    tmp_path: Path,
) -> None:
    """The push identity map is complete even when the push was skipped.

    The recovery run's main push consults the on-disk journal, finds the
    first run's batch already landed, and sends nothing for it — so that
    run's OWN map is missing those pairs. The run must therefore read
    them back from the service through an idempotent re-push, never
    compute them locally; a locally computed identity would be a pair no
    row holds.

    The failure is placed BEFORE ``push_succeeded`` is durable, which is
    what makes the leg exist: with a durable push-success the run has no
    push left to do at all and skips the whole step (see
    ``tests/corpus/test_ingest_publication_durable.py``). This test
    covers the complementary leg, and the two together pin both sides.
    """
    states = _PushSuccessLostStore()
    hook_calls = {"count": 0}

    def hook(_row: Any) -> None:
        hook_calls["count"] += 1

    stack = _Stack(tmp_path, on_ingested=hook, states=states)
    stack.submit()

    first = stack.worker().run_pending()[0]
    # The push completed and was journaled, but the record of it was
    # lost — no durable proof, so this is a genuine job failure.
    assert first.state is IngestState.FAILED
    assert stack.status().error_type == IngestDurableCommitError.__name__

    states.refuse_push_success = False
    second = stack.worker().run_pending()[0]

    assert second.state is IngestState.READY
    assert second.result is not None
    # The retry's main push skipped the completed batch: no push call
    # for it, so that run's own map would be empty.
    journal_skipped_run = stack.push.responses[-1]
    assert journal_skipped_run["written"] == 0
    assert journal_skipped_run["duplicates"] >= 1
    # The persisted map is nevertheless complete — the pairs came back
    # from the service via the idempotent re-push, not a local recompute.
    persisted = stack.ready_metadata()["content_identities"]
    assert set(persisted) == {
        envelope.source_ref for envelope in second.result.envelopes
    }
    assert hook_calls["count"] == 1


# ----- input boundaries ------------------------------------------------


def test_running_an_unknown_job_is_the_stored_typed_miss(
    tmp_path: Path,
) -> None:
    stack = _Stack(tmp_path)

    with pytest.raises(UnknownJobError):
        stack.worker().run_job("NEVER-ACCEPTED")


def test_worker_without_a_state_store_is_a_typed_config_failure(
    tmp_path: Path,
) -> None:
    del tmp_path
    endpoint = IngestEndpoint(push_port=FakePushPort())

    with pytest.raises(IngestDurableCommitError):
        IngestWorker(endpoint)


def test_run_pending_reports_real_states_through_job_status(
    tmp_path: Path,
) -> None:
    stack = _Stack(tmp_path)
    stack.submit()
    worker = stack.worker()
    assert worker.resumable_jobs()[0].state is IngestState.PENDING

    worker.run_pending()

    assert stack.status().state is IngestState.READY
