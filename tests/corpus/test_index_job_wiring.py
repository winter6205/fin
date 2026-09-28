"""Hermetic tests for the worker-side index-job handoff (T4 wiring).

The text pipeline (``tests/corpus/test_ingest_worker.py``) ends at
``ready``; these tests pin what the worker does with the already-landed
tracker/ledger seam (``fin.corpus.index_job``):

* a run that lands ``ready`` triggers exactly one
  ``submit_after_push`` carrying the authoritative pair map read back
  off the ``cataloging -> ready`` audit edge (never the in-memory run
  result, never a recomputed identity);
* a refused submit never corrupts the landed text state — the job stays
  ``ready``, the sweep keeps its outcomes, and the failure is durable
  per-pair in the ledger with the graph side left visibly pending;
* restart resume: a fresh worker over the same durable state re-submits
  a ready job whose pairs have no durable ``job_id`` — never recorded,
  or transiently refused — without re-pushing (push call count and
  object writes unchanged), and once every pair holds a job the sweep
  is a network no-op;
* a duplicate submit joins at the port — same pair, same ``job_id``,
  never a second active job;
* wiring boundaries: a tracker without its ledger (or vice versa) is a
  construction refusal, and a ready job whose edge carries no pair map
  submits nothing.

Everything runs on the in-memory stores, the contract-fake push port and
the contract-fake index-job port: the default suite stays hermetic.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Mapping

import pytest

from fin.corpus.index_job import IndexJobLedger, IndexJobTracker
from fin.corpus.ingest_endpoint import IngestEndpoint
from fin.corpus.ingest_state import InMemoryIngestStateStore, IngestState
from fin.corpus.ingest_worker import IngestWorker
from fin.corpus.metadata_store import InMemoryMetadataStore
from fin.corpus.object_store import InMemoryObjectStore
from fin.retrieve.fake_index_job_port import FakeIndexJobPort
from fin.retrieve.fake_push_port import FakePushPort

DOC = "DOC-1"

_TXT_V1 = (
    "Quarterly results overview.\n\n"
    "Revenue grew across every segment this quarter.\n\n"
    "The company reaffirmed its full-year guidance."
).encode("utf-8")


class _CountingPushPort:
    """Push-port spy: the resume tests pin that nothing re-pushes."""

    def __init__(self) -> None:
        self.inner = FakePushPort()
        self.calls = 0

    def push(self, envelopes: Any) -> dict[str, Any]:
        self.calls += 1
        return self.inner.push(envelopes)

    def invalidate(self, *args: Any, **kwargs: Any) -> Any:
        return self.inner.invalidate(*args, **kwargs)

    def recall(self, *args: Any, **kwargs: Any) -> Any:
        return self.inner.recall(*args, **kwargs)


class _RecordingObjectStore(InMemoryObjectStore):
    """Object store that records writes: resume must not write again."""

    def __init__(self) -> None:
        super().__init__()
        self.puts: list[str] = []

    def put(
        self,
        key: str,
        payload: bytes,
        *,
        content_type: str | None = None,
    ) -> Any:
        self.puts.append(key)
        return super().put(key, payload, content_type=content_type)


class _Stack:
    """Durable endpoint + path-backed index-job ledger + fake index port."""

    def __init__(self, tmp_path: Path) -> None:
        self.objects = _RecordingObjectStore()
        self.pointers = InMemoryMetadataStore()
        self.states = InMemoryIngestStateStore()
        self.push = _CountingPushPort()
        self.endpoint = IngestEndpoint(
            push_port=self.push,
            invalidation_port=self.push.inner,
            object_store=self.objects,
            metadata_store=self.pointers,
            state_store=self.states,
        )
        self.journal_dir = tmp_path / "push-journal"
        self.index_port = FakeIndexJobPort()
        self.ledger_path = tmp_path / "index_jobs.jsonl"
        self.ledger = IndexJobLedger(path=self.ledger_path)
        self.tracker = IndexJobTracker(self.index_port, self.ledger)

    def submit(self) -> Any:
        return self.endpoint.submit_durable(
            _TXT_V1, declared_format="txt", document_id=DOC
        )

    def worker(self, *, tracked: bool = True) -> IngestWorker:
        kwargs: dict[str, Any] = {}
        if tracked:
            kwargs = {
                "index_job_tracker": self.tracker,
                "index_job_ledger": self.ledger,
            }
        return IngestWorker(
            self.endpoint, journal_dir=self.journal_dir, **kwargs
        )

    def state(self) -> IngestState:
        return self.states.get_job(DOC).state

    def ready_edge_identities(self) -> Mapping[str, str]:
        """The pair map the ``cataloging -> ready`` edge persisted."""
        rows = [
            row
            for row in self.states.list_audit(DOC)
            if row.action == "transition" and row.to_state is IngestState.READY
        ]
        assert len(rows) == 1, rows
        identities = rows[0].metadata.get("content_identities")
        assert isinstance(identities, Mapping) and identities
        return identities


# ----- submit-after-push hook ------------------------------------------


def test_ready_landing_submits_with_the_persisted_pair_map(
    tmp_path: Path,
) -> None:
    stack = _Stack(tmp_path)
    stack.submit()

    outcomes = stack.worker().run_pending()

    assert [outcome.state for outcome in outcomes] == [IngestState.READY]
    identities = stack.ready_edge_identities()
    assert stack.index_port.submit_calls == len(identities)
    entries = {
        entry["source_ref"]: entry for entry in stack.ledger.entries(DOC)
    }
    assert set(entries) == set(identities)
    for source_ref, entry in entries.items():
        # The submitted pair is the audit-edge map verbatim, and the
        # service's job answer is persisted beside it.
        assert entry["content_identity"] == identities[source_ref]
        assert entry["job_id"]
        assert entry["last_error"] is None
    # The text state is the endpoint's fact, untouched by the hook.
    assert stack.state() is IngestState.READY


def test_submit_failure_keeps_ready_and_records_per_pair(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    stack = _Stack(tmp_path)
    stack.submit()
    stack.index_port.inject_submit_failure("index_job_unavailable", sticky=True)

    with caplog.at_level(
        logging.ERROR, logger="fin.corpus.ingest_worker"
    ):
        outcomes = stack.worker().run_pending()

    # The sweep completed with the real outcome; the refusal surfaced as
    # a logged failure, never as an exception out of the worker.
    assert [outcome.state for outcome in outcomes] == [IngestState.READY]
    assert stack.state() is IngestState.READY
    assert any(
        record.name == "fin.corpus.ingest_worker" and record.exc_info
        for record in caplog.records
    )
    identities = stack.ready_edge_identities()
    entries = stack.ledger.entries(DOC)
    assert len(entries) == len(identities)
    for entry in entries:
        assert entry["job_id"] is None
        assert entry["last_error"]["code"] == "index_job_unavailable"
    # Graph side stays visibly pending: the polled shape carries the
    # refused pairs' recorded error — never an error-less empty success.
    graph = stack.tracker.graph_status(DOC)
    assert graph is not None
    assert graph["jobs"] == []
    assert graph["community_index_state"] is None
    assert graph["error"]["code"] == "index_job_unavailable"
    assert graph["error"]["message"]


# ----- restart resume ----------------------------------------------------


def test_transiently_refused_submit_is_retried_on_the_next_sweep(
    tmp_path: Path,
) -> None:
    """A refused submit leaves pairs with no durable job_id; the resume
    predicate is "owes a pair without a job", so a later sweep completes
    the handoff once the service is back (submit is an idempotent join).
    """
    stack = _Stack(tmp_path)
    stack.submit()
    stack.index_port.inject_submit_failure("index_job_unavailable", sticky=True)
    stack.worker().run_pending()
    refused = {
        entry["source_ref"]: entry for entry in stack.ledger.entries(DOC)
    }
    assert refused and all(entry["job_id"] is None for entry in refused.values())

    stack.index_port.clear_injected_failures()
    stack.worker().run_pending()

    recovered = {
        entry["source_ref"]: entry for entry in stack.ledger.entries(DOC)
    }
    assert set(recovered) == set(refused)
    for source_ref, entry in recovered.items():
        # The pair that had no durable job now has one: the handoff was
        # completed by the resume, not invented.
        assert refused[source_ref]["job_id"] is None
        assert entry["job_id"] is not None
        assert entry["content_identity"] == refused[source_ref]["content_identity"]
        assert entry["last_error"] is None
    # Once every pair has a durable job the resume is a network no-op.
    submits = stack.index_port.submit_calls
    stack.worker().run_pending()
    assert stack.index_port.submit_calls == submits


# ----- restart resume ----------------------------------------------------


def test_restart_resume_re_submits_a_ready_untracked_job_without_repush(
    tmp_path: Path,
) -> None:
    stack = _Stack(tmp_path)
    stack.submit()
    # The dead process: text landed, the submit never ran.
    untracked = stack.worker(tracked=False).run_pending()
    assert [outcome.state for outcome in untracked] == [IngestState.READY]
    push_calls = stack.push.calls
    puts = len(stack.objects.puts)
    assert stack.index_port.submit_calls == 0

    outcomes = stack.worker().run_pending()

    # The resume edge acts on the ready job, not a re-run: nothing new
    # was swept and nothing was pushed again.
    assert outcomes == ()
    assert stack.push.calls == push_calls
    assert len(stack.objects.puts) == puts
    identities = stack.ready_edge_identities()
    assert stack.index_port.submit_calls == len(identities)
    for entry in stack.ledger.entries(DOC):
        assert entry["content_identity"] == identities[entry["source_ref"]]
        assert entry["job_id"]
    assert stack.state() is IngestState.READY


def test_resume_reads_the_durable_ledger_after_a_process_restart(
    tmp_path: Path,
) -> None:
    stack = _Stack(tmp_path)
    stack.submit()
    stack.worker().run_pending()
    submit_calls = stack.index_port.submit_calls

    # Fresh process: a new ledger instance over the same path, a new
    # tracker over it, a new sweep.
    restarted_ledger = IndexJobLedger(path=stack.ledger_path)
    restarted_tracker = IndexJobTracker(FakeIndexJobPort(), restarted_ledger)
    assert {
        entry["source_ref"] for entry in restarted_ledger.entries(DOC)
    } == set(stack.ready_edge_identities())
    IngestWorker(
        stack.endpoint,
        journal_dir=stack.journal_dir,
        index_job_tracker=restarted_tracker,
        index_job_ledger=restarted_ledger,
    ).run_pending()

    assert stack.index_port.submit_calls == submit_calls  # no re-submit
    assert restarted_tracker.graph_status(DOC) is not None  # jobs replayed


def test_duplicate_submit_joins_the_same_job(tmp_path: Path) -> None:
    stack = _Stack(tmp_path)
    stack.submit()
    stack.worker().run_pending()
    first = {
        entry["source_ref"]: entry["job_id"]
        for entry in stack.ledger.entries(DOC)
    }

    # A re-submit of the same generation (e.g. a retried hook) joins at
    # the port: same pairs, same job ids, no second active job.
    stack.tracker.submit_after_push(DOC, stack.ready_edge_identities())

    second = {
        entry["source_ref"]: entry["job_id"]
        for entry in stack.ledger.entries(DOC)
    }
    assert second == first


# ----- wiring boundaries --------------------------------------------------


def test_tracker_without_ledger_is_a_construction_refusal(
    tmp_path: Path,
) -> None:
    stack = _Stack(tmp_path)
    with pytest.raises(ValueError):
        IngestWorker(
            stack.endpoint, index_job_tracker=stack.tracker
        )
    with pytest.raises(ValueError):
        IngestWorker(stack.endpoint, index_job_ledger=stack.ledger)


def test_ready_job_without_a_persisted_pair_map_submits_nothing(
    tmp_path: Path,
) -> None:
    stack = _Stack(tmp_path)
    # A ready job whose edge carries no identities (e.g. a hand-driven
    # state walk): there is no pair to submit, and the resume must not
    # invent one.
    stack.states.create_job(DOC)
    for state in (
        IngestState.FETCHING,
        IngestState.EXTRACTING,
        IngestState.CATALOGING,
        IngestState.READY,
    ):
        stack.states.transition(DOC, state)

    stack.worker().run_pending()

    assert stack.index_port.submit_calls == 0
    assert stack.ledger.entries(DOC) == []
    assert stack.state() is IngestState.READY
