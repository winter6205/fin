"""Cross-process ingest-worker resume, proved on the real durable stores.

The hermetic suite (``tests/corpus/test_ingest_worker.py``) pins the
worker's semantics on the process-local stores. The T3 acceptance
clause it cannot claim is *"after restart"* in the literal sense: a
different process, holding nothing but the configured DSN and object
store, discovers and finishes a job a dead process left mid-run.

Shape (the same two-process pattern as
``tests/corpus/test_ingest_durable_restart.py``, whose gating fixtures
and cross-process helpers are imported here rather than restated):

* Process A runs the real accept door on the configured object store,
  claims the job the way a worker does (``pending -> fetching``) and
  then exits — the interruption this ticket must survive.
* Process B starts a fresh :class:`~fin.corpus.ingest_worker.IngestWorker`
  over its own store adapters: it must *discover* the interrupted job
  from durable state (no re-upload, no handed-over objects), finish it
  to ``ready``, and persist the ``content_identities`` map on the
  ``cataloging -> ready`` edge.
* The parent — a third process — reads the job row, the pointer, the
  status door and the audit trail back.

The push port is the contract fake in both children: this leg proves
the fin-side durable resume, not the live iknow service (that gate is
the round-end gold run). Skipped when no Postgres DSN / object store
is configured — blocked evidence for the run record, never a fake
pass.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

# Reuse the established gate: same DSN keys, same wipe discipline, same
# object-store requirement, same real-child runner. No second scheme.
from test_ingest_durable_restart import (  # noqa: E402  (sibling module)
    _delete_objects,
    _run_child,
    clean_tables,
    configured_object_store,
    postgres_url,
)

from fin.corpus.ingest_state import (  # noqa: E402
    IngestState,
    PostgresIngestStateStore,
)
from fin.corpus.ingest_state_errors import InvalidStateError  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
_MD_DIR = REPO_ROOT / "data/corpus/dataset/documents/md"
V1_SOURCE = _MD_DIR / "0000320193-24-000006__table_dense__md__md.md"
_SCOPE = "fin-durable-worker"


def _document_id(scope: str, data: bytes) -> str:
    """The public upload's content-derived id, written out longhand."""
    digest = hashlib.sha256(data).digest()
    preimage = (
        b"fin-corpus-document-v1\x00" + scope.encode("utf-8") + b"\x00" + digest
    )
    return "doc_" + hashlib.sha256(preimage).hexdigest()

# Process A: the real accept door on the configured object store, then
# the worker's claim edge — and the process dies there, mid-run.
_ACCEPT_AND_CLAIM_CHILD = """
import json, os, sys
from pathlib import Path
from fin.corpus.ingest_endpoint import IngestEndpoint
from fin.corpus.ingest_state import PostgresIngestStateStore
from fin.corpus.metadata_store import PostgresMetadataStore
from fin.corpus.object_store import create_object_store
from fin.retrieve.fake_push_port import FakePushPort

dsn = os.environ["FIN_RESTART_DSN"]
data = Path(sys.argv[1]).read_bytes()
scope = sys.argv[2]
states = PostgresIngestStateStore(dsn)
endpoint = IngestEndpoint(
    push_port=FakePushPort(),
    object_store=create_object_store(),
    metadata_store=PostgresMetadataStore(dsn),
    state_store=states,
    corpus_scope_id=scope,
)
submission = endpoint.submit_durable(data, declared_format="md")
doc = submission.document_id
states.transition(
    doc,
    "fetching",
    expected_state=submission.state,
    # The same claim metadata run_durable stamps on its CAS edge: this
    # child simulates the worker's claim, so it must claim identically.
    metadata={"claim_version": submission.version},
)
print(json.dumps({
    "pid": os.getpid(),
    "document_id": doc,
    "accepted_state": submission.state.value,
    "state": states.get_job(doc).state.value,
    "version": submission.version,
}))
"""

# Process B: a fresh worker over its own adapters. It knows only the
# DSN and the journal directory — the job is discovered from durable
# state, not handed over.
_RESUME_WORKER_CHILD = """
import json, os, sys
from pathlib import Path
from fin.corpus.ingest_endpoint import IngestEndpoint
from fin.corpus.ingest_state import PostgresIngestStateStore
from fin.corpus.ingest_worker import IngestWorker
from fin.corpus.metadata_store import PostgresMetadataStore
from fin.corpus.object_store import create_object_store
from fin.retrieve.fake_push_port import FakePushPort

dsn = os.environ["FIN_RESTART_DSN"]
doc, journal_dir = sys.argv[1], sys.argv[2]
states = PostgresIngestStateStore(dsn)
endpoint = IngestEndpoint(
    push_port=FakePushPort(),
    object_store=create_object_store(),
    metadata_store=PostgresMetadataStore(dsn),
    state_store=states,
)
worker = IngestWorker(endpoint, journal_dir=Path(journal_dir))
found = sorted(job.document_id for job in worker.resumable_jobs())
outcomes = worker.run_pending()
print(json.dumps({
    "pid": os.getpid(),
    "found": found,
    "ran": [outcome.ran for outcome in outcomes],
    "states": [outcome.state.value for outcome in outcomes],
    "busy": [outcome.busy for outcome in outcomes],
    "job_state": states.get_job(doc).state.value,
}))
"""

# The poller's door, rebuilt in the parent from the DSN alone.
_STATUS_CHILD = """
import json, os, sys
from fin.corpus.ingest_endpoint import IngestEndpoint
from fin.corpus.ingest_state import PostgresIngestStateStore
from fin.corpus.metadata_store import PostgresMetadataStore
from fin.retrieve.fake_push_port import FakePushPort

dsn = os.environ["FIN_RESTART_DSN"]
doc = sys.argv[1]
states = PostgresIngestStateStore(dsn)
status = IngestEndpoint(
    push_port=FakePushPort(),
    metadata_store=PostgresMetadataStore(dsn),
    state_store=states,
).job_status(doc)
print(json.dumps({
    "pid": os.getpid(),
    "state": status.state.value,
    "version": status.version,
    "text_ready": status.text_ready,
    "error_type": status.error_type,
    "audit": [
        {
            "action": row.action,
            "actor": row.actor,
            "from_state": (
                None if row.from_state is None else row.from_state.value
            ),
            "to_state": (
                None if row.to_state is None else row.to_state.value
            ),
            "metadata": dict(row.metadata),
        }
        for row in states.list_audit(doc)
    ],
}))
"""


def _child(dsn: str, program: str, *args: str) -> dict[str, Any]:
    report = _run_child(program, *args, dsn=dsn)
    assert int(report["pid"]) != os.getpid(), report
    return report


def test_a_dead_workers_job_is_resumed_and_readied_by_a_fresh_process(
    clean_tables: str,
    configured_object_store: str,
    tmp_path: Path,
) -> None:
    """``pending`` -> claim -> (process dies) -> resume to ``ready``.

    The resume process never saw the upload: it discovers the
    interrupted job through the durable state-filtered listing, reads
    the raw bytes back through the durable pointer from the configured
    object store, and the parent proves the whole generation from its
    own adapters — including the ``content_identities`` map persisted
    on the ``cataloging -> ready`` edge and no second accept
    (one pointer, one object key generation).
    """
    payload = V1_SOURCE.read_bytes()
    version = hashlib.sha256(payload).hexdigest()
    doc = _document_id(_SCOPE, payload)
    object_key = f"filings/{doc}/{version}"
    assert configured_object_store, "the object-store gate must name a bucket"
    journal_dir = tmp_path / "push-journal"
    try:
        accepted = _child(
            clean_tables, _ACCEPT_AND_CLAIM_CHILD, str(V1_SOURCE), _SCOPE
        )
        assert accepted["document_id"] == doc, accepted
        assert accepted["accepted_state"] == "pending", accepted
        assert accepted["state"] == "fetching", accepted
        assert accepted["version"] == version, accepted

        resumed = _child(
            clean_tables,
            _RESUME_WORKER_CHILD,
            doc,
            str(journal_dir),
        )

        # Discovery, not instruction: the worker found this one job
        # from durable state in a process that never accepted it.
        assert resumed["found"] == [doc], resumed
        assert resumed["ran"] == [True], resumed
        assert resumed["busy"] == [False], resumed
        assert resumed["states"] == ["ready"], resumed
        assert resumed["job_state"] == "ready", resumed

        report = _child(clean_tables, _STATUS_CHILD, doc)
        assert report["state"] == "ready", report
        assert report["version"] == version, report
        assert report["text_ready"] is True, report
        assert report["error_type"] is None, report

        edges = [
            (row["action"], row["from_state"], row["to_state"])
            for row in report["audit"]
        ]
        assert edges == [
            ("job_created", None, "pending"),
            ("transition", "pending", "fetching"),
            ("transition", "fetching", "extracting"),
            ("transition", "extracting", "cataloging"),
            ("transition", "cataloging", "ready"),
        ], report["audit"]
        # The claim belongs to the exited child; the resume edges to a
        # different pid — no process object carried across.
        claim = report["audit"][1]
        assert claim["actor"].endswith(f"pid={accepted['pid']}"), claim
        assert claim["metadata"].get("claim_version") == version, claim
        ready = report["audit"][-1]
        assert ready["actor"].endswith(f"pid={resumed['pid']}"), ready
        identities = ready["metadata"]["content_identities"]
        assert identities, ready["metadata"]
    finally:
        _delete_objects([object_key])


def test_the_durable_listing_filters_states(clean_tables: str) -> None:
    """The state-filtered discovery read on real rows, not the fake.

    The resume sweep depends on ``list_jobs(states=...)`` returning
    exactly the resumable generation on the durable store; a filter
    bug would either re-run a ready document or strand a pending one.
    """
    store = PostgresIngestStateStore(clean_tables)
    try:
        store.create_job("DONE-DOC", actor="pytest")
        for state in (
            IngestState.FETCHING,
            IngestState.EXTRACTING,
            IngestState.CATALOGING,
            IngestState.READY,
        ):
            store.transition("DONE-DOC", state, actor="pytest")
        store.create_job("QUEUED-DOC", actor="pytest")

        resumable = store.list_jobs(
            states=[IngestState.PENDING, IngestState.FAILED]
        )
        assert [job.document_id for job in resumable] == ["QUEUED-DOC"]
        assert len(store.list_jobs()) == 2
        assert store.list_jobs(states=[]) == ()
        with pytest.raises(InvalidStateError):
            store.list_jobs(states=["queued"])
    finally:
        store.close()
