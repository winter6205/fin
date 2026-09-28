"""The durable corpus ingest worker (corpus-serve-ingest, plan T3).

The accept door (``IngestEndpoint.submit_durable``) persists raw bytes
+ pointer + a ``pending`` job and nothing else; this module is the
"those run off the request" half. The worker is deliberately thin — the
state machine, the claim CAS and the pipeline composition all live on
the endpoint (``IngestEndpoint.run_durable``) — because a second
pipeline would be a second splitter, which ADR 0037 forbids.

What the worker owns:

* **discovery** — :meth:`IngestWorker.resumable_jobs` lists exactly the
  states a resume edge can act on (everything except ``ready``) from
  the durable job store, so a worker started after a process restart
  picks up queued, failed and interrupted jobs without any re-upload.
* **journal lifecycle** — one persistent push journal per document
  under ``journal_dir``, so a replayed run skips exactly the batches
  that already landed and a partial push is never double-written or
  reported complete.
* **the sweep** — :meth:`IngestWorker.run_pending` drives every
  resumable job once and returns the real outcomes; per-job failures
  are typed on the job itself (visible through ``job_status``), never
  an exception that aborts the sweep.
* **the index-job handoff** — when a tracker is wired, a run that lands
  ``ready`` is followed by :meth:`IndexJobTracker.submit_after_push`
  with the pair map the ``cataloging -> ready`` edge persisted, and each
  tick re-attempts submit for ready jobs any pair of which still has no
  durable ``job_id`` in the ledger (a restart whose submit never ran, or
  a transient refusal that must not strand the pair). A submit failure
  is recorded in the ledger and logged; it never touches the landed
  text state.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from fin.corpus.index_job import IndexJobLedger, IndexJobTracker
from fin.corpus.ingest_endpoint import (
    DurableRunOutcome,
    IngestDurableCommitError,
    IngestEndpoint,
)
from fin.corpus.ingest_state import IngestJob, IngestState
from fin.retrieve.push_batching import PushJournal

logger = logging.getLogger(__name__)

#: Every persisted state except terminal ``ready`` is resumable:
#: ``pending``/``failed`` enter through the claim edge, and
#: ``fetching``/``extracting``/``ocr``/``cataloging`` are the mid-run
#: interruptions a restart re-adopts from durable state.
RESUMABLE_STATES: Final[tuple[IngestState, ...]] = (
    IngestState.PENDING,
    IngestState.FAILED,
    IngestState.FETCHING,
    IngestState.EXTRACTING,
    IngestState.OCR,
    IngestState.CATALOGING,
)


class IngestWorker:
    """Sweep resumable ingest jobs through one endpoint.

    Parameters
    ----------
    endpoint:
        The :class:`~fin.corpus.ingest_endpoint.IngestEndpoint` whose
        durable ports (object, pointer, state stores) execute each job.
        A job-state store must be wired; without one there is nothing
        durable to resume, which is the typed ``unconfigured`` failure.
    journal_dir:
        Directory for the per-document push journals. When set, journal
        records survive the process, which is what makes a restart
        replay skip completed batches; when omitted the journals are
        per-run in-memory and only store-level idempotency applies.
    actor:
        Audit actor stamped on the worker's transitions; defaults to
        the store's process identity.
    index_job_tracker:
        Optional :class:`~fin.corpus.index_job.IndexJobTracker`. When
        wired, every ready landing submits its index jobs and every
        sweep resumes submit for ready jobs the ledger never recorded.
        When omitted the worker runs the text pipeline only (the
        synchronous contract is untouched).
    index_job_ledger:
        The ledger the tracker writes — consulted to tell an already
        submitted job from one a restart still owes a submit to.
        Required together with the tracker.
    """

    def __init__(
        self,
        endpoint: IngestEndpoint,
        *,
        journal_dir: Path | None = None,
        actor: str | None = None,
        index_job_tracker: IndexJobTracker | None = None,
        index_job_ledger: IndexJobLedger | None = None,
    ) -> None:
        states = endpoint.state_store
        if states is None:
            raise IngestDurableCommitError(
                "the ingest worker needs an endpoint wired to an ingest "
                "state store; there would be no durable job to resume",
                stage="unconfigured",
            )
        if (index_job_tracker is None) != (index_job_ledger is None):
            raise ValueError(
                "the index-job handoff needs both the tracker and its "
                "ledger; one without the other cannot tell a submitted "
                "job from an owed one"
            )
        self._endpoint = endpoint
        self._states = states
        self._journal_dir = journal_dir
        self._actor = actor
        self._index_job_tracker = index_job_tracker
        self._index_job_ledger = index_job_ledger
        if journal_dir is not None:
            journal_dir.mkdir(parents=True, exist_ok=True)

    def resumable_jobs(self) -> tuple[IngestJob, ...]:
        """The jobs a sweep may act on, in deterministic id order."""
        return self._states.list_jobs(states=RESUMABLE_STATES)

    def run_job(self, document_id: str) -> DurableRunOutcome:
        """Drive one job through the endpoint's durable run edge.

        A run that leaves the job ``ready`` is followed by the
        index-job submit when a tracker is wired: the submit reads the
        authoritative pair map back off the ``cataloging -> ready``
        audit edge — the same source a restart resume reads — so the
        hook never depends on the in-memory run result and a refused
        submit can never reach the landed state.
        """
        outcome = self._endpoint.run_durable(
            document_id,
            journal=self._journal_for(document_id),
            actor=self._actor,
        )
        if outcome.state is IngestState.READY:
            self._submit_index_jobs(document_id)
        return outcome

    def run_pending(self) -> tuple[DurableRunOutcome, ...]:
        """One sweep: run every resumable job, report every outcome.

        A job that fails lands typed ``failed`` on the store and the
        sweep continues; the retry edge (``failed -> fetching``) plus
        the journals make the next sweep the recovery. The sweep ends
        with the index-job resume (see :meth:`resume_index_jobs`).
        """
        outcomes = tuple(
            self.run_job(job.document_id) for job in self.resumable_jobs()
        )
        self.resume_index_jobs()
        return outcomes

    def resume_index_jobs(self) -> None:
        """Re-attempt the index-job submit for ready jobs still owing one.

        The restart edge of the handoff: a job whose text landed in a
        dead process before its submit ran is still ``ready`` with the
        pair map on its audit edge, and so is a job whose last submit
        round was transiently refused — the predicate is *some pair has
        no durable job_id*, not *the ledger has no entries*, or a
        refusal would strand the pair forever. Submit is an idempotent
        join at the service, so a re-attempt never opens a second job;
        once every pair has a job the sweep stays a network no-op.
        Never re-pushes and never recomputes an identity.
        """
        if self._index_job_tracker is None or self._index_job_ledger is None:
            return
        for job in self._states.list_jobs(states=(IngestState.READY,)):
            if not self._owes_index_job_handoff(job.document_id):
                continue
            self._submit_index_jobs(job.document_id)

    def _owes_index_job_handoff(self, document_id: str) -> bool:
        """True when the ready job's pair map has a pair the ledger
        holds no durable job for (never recorded, or transiently
        refused)."""
        if self._index_job_ledger is None:
            return False
        identities = self._ready_edge_identities(document_id)
        if not identities:
            return False
        tracked = {
            entry["source_ref"]: entry
            for entry in self._index_job_ledger.entries(document_id)
        }
        return any(
            tracked.get(source_ref) is None
            or tracked[source_ref]["job_id"] is None
            for source_ref in identities
        )

    def _submit_index_jobs(self, document_id: str) -> None:
        """One submit round for a ready job; failures stay out of the sweep.

        A refused submit is already recorded per-pair in the ledger by
        the tracker and surfaces here as the aggregated typed error —
        logged, never propagated: the landed ``ready`` text state is
        the endpoint's fact and stands regardless; the graph side stays
        pending and visible through the status poll.
        """
        if self._index_job_tracker is None:
            return
        identities = self._ready_edge_identities(document_id)
        if not identities:
            logger.warning(
                "ingest worker: ready job %s carries no content_identities "
                "on its ready edge; index-job submit skipped",
                document_id,
            )
            return
        try:
            self._index_job_tracker.submit_after_push(document_id, identities)
        except Exception:
            logger.exception(
                "ingest worker: index-job submit failed for %s "
                "(the landed text state is unaffected)",
                document_id,
            )

    def _ready_edge_identities(self, document_id: str) -> Mapping[str, str]:
        """The pair map the latest ``-> ready`` transition persisted.

        The audit edge is the single source: the identities are the
        service's own push-response values, so a resume reads exactly
        what the completing run was told to hand off.
        """
        for row in reversed(self._states.list_audit(document_id)):
            if row.action != "transition" or row.to_state is not IngestState.READY:
                continue
            identities = row.metadata.get("content_identities")
            if not isinstance(identities, Mapping):
                return {}
            return {
                source_ref: identity
                for source_ref, identity in identities.items()
                if isinstance(source_ref, str) and isinstance(identity, str)
            }
        return {}

    def _journal_for(self, document_id: str) -> PushJournal | None:
        if self._journal_dir is None:
            return None
        # The file name is the digest of the id, not the id: document
        # ids are operator-supplied and must not steer the filesystem.
        digest = hashlib.sha256(document_id.encode("utf-8")).hexdigest()
        return PushJournal(path=self._journal_dir / f"{digest}.jsonl")


__all__ = ["IngestWorker", "RESUMABLE_STATES"]
