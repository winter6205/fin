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
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Final

from fin.corpus.ingest_endpoint import (
    DurableRunOutcome,
    IngestDurableCommitError,
    IngestEndpoint,
)
from fin.corpus.ingest_state import IngestJob, IngestState
from fin.retrieve.push_batching import PushJournal

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
    """

    def __init__(
        self,
        endpoint: IngestEndpoint,
        *,
        journal_dir: Path | None = None,
        actor: str | None = None,
    ) -> None:
        states = endpoint.state_store
        if states is None:
            raise IngestDurableCommitError(
                "the ingest worker needs an endpoint wired to an ingest "
                "state store; there would be no durable job to resume",
                stage="unconfigured",
            )
        self._endpoint = endpoint
        self._states = states
        self._journal_dir = journal_dir
        self._actor = actor
        if journal_dir is not None:
            journal_dir.mkdir(parents=True, exist_ok=True)

    def resumable_jobs(self) -> tuple[IngestJob, ...]:
        """The jobs a sweep may act on, in deterministic id order."""
        return self._states.list_jobs(states=RESUMABLE_STATES)

    def run_job(self, document_id: str) -> DurableRunOutcome:
        """Drive one job through the endpoint's durable run edge."""
        return self._endpoint.run_durable(
            document_id,
            journal=self._journal_for(document_id),
            actor=self._actor,
        )

    def run_pending(self) -> tuple[DurableRunOutcome, ...]:
        """One sweep: run every resumable job, report every outcome.

        A job that fails lands typed ``failed`` on the store and the
        sweep continues; the retry edge (``failed -> fetching``) plus
        the journals make the next sweep the recovery.
        """
        return tuple(
            self.run_job(job.document_id) for job in self.resumable_jobs()
        )

    def _journal_for(self, document_id: str) -> PushJournal | None:
        if self._journal_dir is None:
            return None
        # The file name is the digest of the id, not the id: document
        # ids are operator-supplied and must not steer the filesystem.
        digest = hashlib.sha256(document_id.encode("utf-8")).hexdigest()
        return PushJournal(path=self._journal_dir / f"{digest}.jsonl")


__all__ = ["IngestWorker", "RESUMABLE_STATES"]
