"""Restart persistence for a corpus ingest job, proved across real processes.

The T2c acceptance clause is that an accepted job is readable by a DIFFERENT
process afterwards. That claim cannot be made by rebuilding an object in place,
so every case here drives the accept through the real product door in one Python
process and reads it back in another one, exactly as
``cc1a9cb`` did for thread-attachment bytes
(``tests/host/test_live_attachment_upload_door.py``
``test_live_upload_bytes_persist_after_uvicorn_exit_in_fresh_adapter_process``).
The reader is a fresh ``python -c`` process that constructs its own
:class:`~fin.corpus.ingest_state.PostgresIngestStateStore` and
:class:`~fin.corpus.metadata_store.PostgresMetadataStore` from the configured
DSN, so nothing it reports can come from an object the writer kept alive.

Scope of the durability claimed here: the **job row**, the **audit trail**,
the **object pointer** (key, ``sha256`` version, byte size, content type) and —
where an S3-compatible service is configured — the raw **bytes** themselves.
The legs are split by what they need:

* the job / audit / pointer legs run on the process-local byte store, because
  they claim nothing about where the bytes live and must keep running on a
  machine with no object service;
* :func:`test_original_bytes_are_read_by_a_fresh_process_from_the_configured_store`
  makes the byte leg the way ``cc1a9cb`` made it for thread attachments. It
  gates on the same pair
  ``tests/host/test_live_attachment_upload_door.py:138`` gates on
  (``FIN_OBJECT_STORE_URL`` or ``FIN_OBJECT_STORE_ENDPOINT``, plus
  ``FIN_OBJECT_STORE_BUCKET``) and builds its byte adapter only through the
  product's own :func:`~fin.corpus.object_store.create_object_store`, so a
  degraded or in-memory store cannot satisfy it. No second configuration
  scheme; the bucket it wrote to is named in the run record.

Replacement vocabulary is asserted against
``specs/corpus-serve-ingest.md:113`` (``ready`` is terminal) and
``specs/corpus-serve-ingest.md:150`` (a replacement "does **not** create a
second logical version coexisting with the first"), i.e. one document carries
exactly one live job row and the generation it replaced stays readable in the
audit trail — not as a second ``ready`` row.

Skipped when neither ``FIN_INGEST_STATE_DSN`` nor ``FIN_CHECKPOINT_URL`` is
set — the repo's convention for Postgres-backed durability, shared with
``tests/corpus/test_ingest_state_store_durable.py``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest

from fin.corpus.ingest_endpoint import IngestEndpoint
from fin.corpus.ingest_state import (
    InMemoryIngestStateStore,
    IngestState,
    IngestStateStore,
    PostgresIngestStateStore,
)
from fin.corpus.ingest_state_errors import IllegalTransitionError
from fin.corpus.metadata_store import PostgresMetadataStore
from fin.corpus.object_store import InMemoryObjectStore
from fin.retrieve.fake_push_port import FakePushPort

REPO_ROOT = Path(__file__).resolve().parents[2]
POSTGRES_URL_ENV = "FIN_CHECKPOINT_URL"
STATE_DSN_ENV = "FIN_INGEST_STATE_DSN"
CHILD_DSN_ENV = "FIN_RESTART_DSN"
# The object-store pair the thread-attachment live door already gates on, read
# here with the same names and no extra key of its own.
OBJECT_STORE_URL_ENV = "FIN_OBJECT_STORE_URL"
OBJECT_STORE_ENDPOINT_ENV = "FIN_OBJECT_STORE_ENDPOINT"
OBJECT_STORE_BUCKET_ENV = "FIN_OBJECT_STORE_BUCKET"
DOC = "0000320193-24-000006"
# The trusted scope every public accept in this module uses. The
# document id is derived from it plus the content digest.
SCOPE = "fin-durable-restart"


def _document_id(scope: str, data: bytes) -> str:
    """The public upload's content-derived id, written out longhand."""
    digest = hashlib.sha256(data).digest()
    preimage = (
        b"fin-corpus-document-v1\x00" + scope.encode("utf-8") + b"\x00" + digest
    )
    return "doc_" + hashlib.sha256(preimage).hexdigest()

# Two committed renderings of one accession: real filing bytes, different
# content, therefore two distinct ``(document_id, version)`` generations.
_MD_DIR = REPO_ROOT / "data/corpus/dataset/documents/md"
V1_SOURCE = _MD_DIR / "0000320193-24-000006__table_dense__md__md.md"
V2_SOURCE = _MD_DIR / "0000320193-24-000006__narrative__md__md.md"

# The byte leg's inputs. Byte-for-byte equality is only evidence when the
# payload has structure a truncation or a re-encode could survive, so both
# cases are committed real filing bytes: a 35,324-byte binary PDF (the same
# gold payload the thread-attachment door reads back) and the 57,547-byte
# table-dense Markdown rendering of a real 10-K.
_GOLD_PDF = REPO_ROOT / "data/corpus/g4_attachment_gold/g4-mixed-chart-001.pdf"
BYTE_CASES: tuple[tuple[Path, str, str], ...] = (
    (_GOLD_PDF, "pdf", "application/pdf"),
    (V1_SOURCE, "md", "text/markdown"),
)
# Named by what makes each case evidence, not by an arbitrary index.
BYTE_CASE_IDS = ("binary-pdf-35324-bytes", "real-filing-markdown-57547-bytes")

_CREDENTIAL_RE = re.compile(r"//[^\s/@:]+:[^\s@]+@")

# Written by a real child process: the product accept door, then the settled
# worker edges that make this generation terminal ``ready``.
_ACCEPT_TO_READY_CHILD = """
import hashlib, json, os, sys
from pathlib import Path
from fin.corpus.ingest_endpoint import IngestEndpoint
from fin.corpus.ingest_state import PostgresIngestStateStore
from fin.corpus.metadata_store import PostgresMetadataStore
from fin.corpus.object_store import InMemoryObjectStore
from fin.retrieve.fake_push_port import FakePushPort

dsn = os.environ["FIN_RESTART_DSN"]
data = Path(sys.argv[1]).read_bytes()
scope = sys.argv[2]
states = PostgresIngestStateStore(dsn)
endpoint = IngestEndpoint(
    push_port=FakePushPort(),
    object_store=InMemoryObjectStore(),
    metadata_store=PostgresMetadataStore(dsn),
    state_store=states,
    corpus_scope_id=scope,
)
submission = endpoint.submit_durable(data, declared_format="md")
doc = submission.document_id
for state in ("fetching", "extracting", "cataloging", "ready"):
    states.transition(doc, state, actor="child-worker")
print(json.dumps({
    "pid": os.getpid(),
    "document_id": submission.document_id,
    "accepted_state": submission.state.value,
    "state": states.get_job(doc).state.value,
    "version": submission.version,
    "size_bytes": len(data),
    "sha256": hashlib.sha256(data).hexdigest(),
}))
"""

# Read by a DIFFERENT real process, through the product adapters it builds
# itself from the configured DSN: the job row, the durable pointer, the
# poller's own status door and the audit trail of every generation.
_READBACK_CHILD = """
import json, os, sys
from fin.corpus.ingest_endpoint import IngestEndpoint
from fin.corpus.ingest_state import PostgresIngestStateStore
from fin.corpus.metadata_store import MetadataPointerNotFoundError
from fin.corpus.metadata_store import PostgresMetadataStore
from fin.retrieve.fake_push_port import FakePushPort

dsn = os.environ["FIN_RESTART_DSN"]
doc = sys.argv[1]
states = PostgresIngestStateStore(dsn)
pointers = PostgresMetadataStore(dsn)
job = states.get_job(doc)
try:
    pointer = pointers.get_pointer(doc).pointer
except MetadataPointerNotFoundError:
    pointer = None
status = IngestEndpoint(
    push_port=FakePushPort(), metadata_store=pointers, state_store=states
).job_status(doc)
print(json.dumps({
    "pid": os.getpid(),
    "job": {
        "document_id": job.document_id,
        "state": job.state.value,
        "error_type": job.error_type,
        "error_message": job.error_message,
    },
    "pointer": (
        None
        if pointer is None
        else {
            "object_key": pointer.key,
            "sha256": pointer.sha256,
            "size_bytes": pointer.size_bytes,
            "content_type": pointer.content_type,
        }
    ),
    "status": {
        "job_id": status.job_id,
        "document_id": status.document_id,
        "version": status.version,
        "state": status.state.value,
        "text_ready": status.text_ready,
    },
    "job_documents": sorted(row.document_id for row in states.list_jobs()),
    "audit": [
        {
            "action": row.action,
            "actor": row.actor,
            "from_state": None if row.from_state is None else row.from_state.value,
            "to_state": None if row.to_state is None else row.to_state.value,
            "metadata": dict(row.metadata),
        }
        for row in states.list_audit(doc)
    ],
}))
"""

# Process A of the byte leg: the real accept door on the real object store,
# then it exits. Nothing is handed to the reader but the DSN and the path of
# the committed fixture.
_ACCEPT_ON_REAL_BYTES_CHILD = """
import hashlib, json, os, sys
from pathlib import Path
from fin.corpus.ingest_endpoint import IngestEndpoint
from fin.corpus.ingest_state import PostgresIngestStateStore
from fin.corpus.metadata_store import PostgresMetadataStore
from fin.corpus.object_store import create_object_store
from fin.retrieve.fake_push_port import FakePushPort

dsn = os.environ["FIN_RESTART_DSN"]
data = Path(sys.argv[1]).read_bytes()
scope, fmt = sys.argv[2], sys.argv[3]
# The product's own selector: with no endpoint or bucket configured this
# raises ObjectStoreNotConfiguredError instead of quietly degrading, so the
# case cannot be satisfied by a process-local store.
objects = create_object_store()
states = PostgresIngestStateStore(dsn)
pointers = PostgresMetadataStore(dsn)
submission = IngestEndpoint(
    push_port=FakePushPort(),
    object_store=objects,
    metadata_store=pointers,
    state_store=states,
    corpus_scope_id=scope,
).submit_durable(data, declared_format=fmt)
pointer = pointers.get_pointer(submission.document_id).object
print(json.dumps({
    "pid": os.getpid(),
    "adapter": type(objects).__name__,
    "bucket": os.environ.get("FIN_OBJECT_STORE_BUCKET", ""),
    "document_id": submission.document_id,
    "state": submission.state.value,
    "version": submission.version,
    "size_bytes": len(data),
    "sha256": hashlib.sha256(data).hexdigest(),
    "pointer": {
        "object_key": pointer.key,
        "sha256": pointer.sha256,
        "size_bytes": pointer.size_bytes,
        "content_type": pointer.content_type,
    },
}))
"""

# Process B: its own adapters, built from its own environment, reading the
# pointer row and then the bytes the pointer names.
_BYTES_READBACK_CHILD = """
import hashlib, json, os, sys
from pathlib import Path
from fin.corpus.ingest_endpoint import IngestEndpoint
from fin.corpus.ingest_state import PostgresIngestStateStore
from fin.corpus.metadata_store import PostgresMetadataStore
from fin.corpus.object_store import create_object_store
from fin.retrieve.fake_push_port import FakePushPort

dsn = os.environ["FIN_RESTART_DSN"]
doc, payload_path = sys.argv[1], sys.argv[2]
states = PostgresIngestStateStore(dsn)
pointers = PostgresMetadataStore(dsn)
objects = create_object_store()
job = states.get_job(doc)
pointer = pointers.get_pointer(doc).object
status = IngestEndpoint(
    push_port=FakePushPort(), metadata_store=pointers, state_store=states
).job_status(doc)
stored = objects.get(pointer.key)
expected = Path(payload_path).read_bytes()
print(json.dumps({
    "pid": os.getpid(),
    "adapter": type(objects).__name__,
    "bucket": os.environ.get("FIN_OBJECT_STORE_BUCKET", ""),
    "job": {
        "document_id": job.document_id,
        "state": job.state.value,
        "error_type": job.error_type,
        "error_message": job.error_message,
    },
    "pointer": {
        "object_key": pointer.key,
        "sha256": pointer.sha256,
        "size_bytes": pointer.size_bytes,
        "content_type": pointer.content_type,
    },
    "status": {
        "document_id": status.document_id,
        "version": status.version,
        "state": status.state.value,
    },
    "stored": {
        "size_bytes": len(stored),
        "sha256": hashlib.sha256(stored).hexdigest(),
    },
    "expected": {
        "size_bytes": len(expected),
        "sha256": hashlib.sha256(expected).hexdigest(),
    },
    "bytes_identical": stored == expected,
    # A cheap second shape check: a truncated or re-encoded body differs
    # here even before the hash is compared.
    "stored_first_and_last": [stored[:16].hex(), stored[-16:].hex()],
    "expected_first_and_last": [expected[:16].hex(), expected[-16:].hex()],
}))
"""


def _configured_url() -> str:
    """The DSN this suite runs on, or the empty string.

    ``FIN_INGEST_STATE_DSN`` first, matching the sibling durable module: the
    root conftest unsets a ``FIN_CHECKPOINT_URL`` its 2-second probe cannot
    reach, and a silent skip of the restart leg is the one outcome this file
    exists to prevent.
    """
    return (
        os.environ.get(STATE_DSN_ENV) or os.environ.get(POSTGRES_URL_ENV) or ""
    ).strip()


@pytest.fixture
def postgres_url() -> str:
    url = _configured_url()
    if not url:
        pytest.skip(
            f"neither {STATE_DSN_ENV} nor {POSTGRES_URL_ENV} is set; ingest "
            "restart persistence is untested in this environment"
        )
    return url


@pytest.fixture
def clean_tables(postgres_url: str) -> Iterator[str]:
    """An empty job, audit and pointer set, before and after the case.

    Initialize the real stores before clearing their tables so the fixture
    also works against a fresh database/schema. Clear both sides: a run killed
    mid-case would otherwise decide the next run's row counts.
    """
    initializer = PostgresIngestStateStore(postgres_url)
    initializer.close()
    PostgresMetadataStore(postgres_url)
    _wipe(postgres_url)
    yield postgres_url
    _wipe(postgres_url)


def _wipe(url: str) -> None:
    with psycopg.connect(url) as conn:
        conn.execute("DELETE FROM fin_ingest_audit")
        conn.execute("DELETE FROM fin_ingest_job")
        conn.execute("DELETE FROM fin_corpus_object_pointer")
        conn.commit()


def _object_store_configured() -> bool:
    """The endpoint-plus-bucket pair, checked exactly as the live door checks it."""
    endpoint = (
        os.environ.get(OBJECT_STORE_URL_ENV)
        or os.environ.get(OBJECT_STORE_ENDPOINT_ENV)
        or ""
    ).strip()
    bucket = (os.environ.get(OBJECT_STORE_BUCKET_ENV) or "").strip()
    return bool(endpoint and bucket)


@pytest.fixture
def configured_object_store() -> str:
    """Require real object storage, and hand back the bucket it writes to.

    The sibling of ``real_s3_object_store`` in
    ``tests/host/test_live_attachment_upload_door.py``: a missing store makes
    this leg blocked evidence for the run record, never a fake pass, because
    no in-memory stand-in can answer a cross-process byte question.
    """
    if not _object_store_configured():
        pytest.skip(
            f"{OBJECT_STORE_URL_ENV} / {OBJECT_STORE_ENDPOINT_ENV} and "
            f"{OBJECT_STORE_BUCKET_ENV} are not configured; the cross-process "
            "raw-byte leg did not run"
        )
    return str(os.environ.get(OBJECT_STORE_BUCKET_ENV) or "").strip()


def _delete_objects(keys: list[str]) -> None:
    """Delete every object a case wrote, through the same port it was written to.

    The removal is checked, not assumed: a byte leg that leaves objects behind
    changes what the next run finds in the bucket.
    """
    if not keys:
        return
    from fin.corpus.object_store import create_object_store
    from fin.corpus.object_store_errors import UnknownObjectKeyError

    objects = create_object_store()
    for key in keys:
        objects.delete(key)
        with pytest.raises(UnknownObjectKeyError):
            objects.get(key)


def _redact(text: str) -> str:
    """Strip any ``user:pass@`` DSN a driver echoed into its output."""
    return _CREDENTIAL_RE.sub("//***:***@", text)


def _run_child(program: str, *args: str, dsn: str) -> dict[str, Any]:
    """One real child process, its own stores, its own pid."""
    completed = subprocess.run(
        [sys.executable, "-c", program, *args],
        cwd=str(REPO_ROOT),
        env={**os.environ, CHILD_DSN_ENV: dsn},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, (
        f"child process failed (exit={completed.returncode}): "
        f"{_redact(completed.stderr).strip()}"
    )
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError:  # pragma: no cover - failure diagnostics
        pytest.fail(f"child printed no JSON: {_redact(completed.stdout)!r}")


def _reader(dsn: str, document_id: str) -> dict[str, Any]:
    """A fresh process reading the persisted ingest state back."""
    report = _run_child(_READBACK_CHILD, document_id, dsn=dsn)
    assert int(report["pid"]) != os.getpid(), report
    return report


def _parent_service(url: str) -> IngestEndpoint:
    """The product door, built from the same DSN the child rebuilds."""
    return IngestEndpoint(
        push_port=FakePushPort(),
        object_store=InMemoryObjectStore(),
        metadata_store=PostgresMetadataStore(url),
        state_store=PostgresIngestStateStore(url),
        corpus_scope_id=SCOPE,
    )


def _pointer_expectation(document_id: str, payload: bytes) -> dict[str, Any]:
    version = hashlib.sha256(payload).hexdigest()
    return {
        "object_key": f"filings/{document_id}/{version}",
        "sha256": version,
        "size_bytes": len(payload),
        "content_type": "text/markdown",
    }


# ----- the restart leg, in both directions ---------------------------


def test_a_job_accepted_here_is_read_by_a_fresh_process(
    clean_tables: str,
) -> None:
    """Accept in this process; a different process reports the same job.

    The writer never hands the reader an object: the child rebuilds both
    stores from the DSN and reads the row, the pointer and the status door.
    """
    payload = V1_SOURCE.read_bytes()
    document_id = _document_id(SCOPE, payload)
    submission = _parent_service(clean_tables).submit_durable(
        payload, declared_format="md"
    )
    assert submission.document_id == document_id
    assert submission.state is IngestState.PENDING

    report = _reader(clean_tables, document_id)

    assert report["job"] == {
        "document_id": document_id,
        "state": "pending",
        "error_type": None,
        "error_message": None,
    }, report
    assert report["pointer"] == _pointer_expectation(document_id, payload), report
    # The poller's own door, in the second process, names the accepted
    # version rather than a constant.
    assert report["status"] == {
        "job_id": document_id,
        "document_id": document_id,
        "version": submission.version,
        "state": "pending",
        "text_ready": False,
    }, report
    assert report["job_documents"] == [document_id], report
    assert [row["action"] for row in report["audit"]] == [
        "job_created",
        "upload_metadata_accepted",
    ], report


def test_a_ready_generation_written_by_another_process_is_readable_here(
    clean_tables: str,
) -> None:
    """The harder direction: the accepting process is gone before the read.

    A child runs the real accept door and drives its generation to the
    terminal ``ready``, then exits. This process — a different pid that never
    held the writer's objects — must still see the accepted identity and the
    generation that reached readiness.
    """
    payload = V1_SOURCE.read_bytes()
    document_id = _document_id(SCOPE, payload)
    written = _run_child(
        _ACCEPT_TO_READY_CHILD, str(V1_SOURCE), SCOPE, dsn=clean_tables
    )
    assert int(written["pid"]) != os.getpid(), written
    assert written["document_id"] == document_id, written
    assert written["accepted_state"] == "pending", written
    assert written["state"] == "ready", written
    assert written["version"] == written["sha256"] == hashlib.sha256(
        payload
    ).hexdigest(), written

    report = _reader(clean_tables, document_id)

    assert report["job"] == {
        "document_id": document_id,
        "state": "ready",
        "error_type": None,
        "error_message": None,
    }, report
    assert report["pointer"] == _pointer_expectation(document_id, payload), report
    assert report["status"]["version"] == written["version"], report
    assert report["status"]["text_ready"] is True, report
    # The row that opened this generation was written by the exited child,
    # not by any object this reader holds.
    assert report["audit"][0]["actor"].endswith(f"pid={written['pid']}"), report
    assert f"pid={os.getpid()}" not in report["audit"][0]["actor"], report
    assert [
        (row["action"], row["from_state"], row["to_state"])
        for row in report["audit"]
    ] == [
        ("job_created", None, "pending"),
        ("upload_metadata_accepted", None, None),
        ("transition", "pending", "fetching"),
        ("transition", "fetching", "extracting"),
        ("transition", "extracting", "cataloging"),
        ("transition", "cataloging", "ready"),
    ], report


# ----- replacement of a ready source, seen by the second reader -------


def test_changed_bytes_are_a_separate_document_not_a_replacement(
    clean_tables: str,
) -> None:
    """A changed file is another public document.

    The same trusted scope plus different bytes is a different identity,
    so the ready generation keeps its job and pointer while the second
    upload opens its own ``pending`` job. ``ready`` therefore stays
    terminal for both; nothing is superseded. Generation one is written
    and readied by a child process and a third process reads both
    documents back.
    """
    first_payload = V1_SOURCE.read_bytes()
    first_document_id = _document_id(SCOPE, first_payload)
    first_version = hashlib.sha256(first_payload).hexdigest()
    first = _run_child(
        _ACCEPT_TO_READY_CHILD, str(V1_SOURCE), SCOPE, dsn=clean_tables
    )
    assert first["document_id"] == first_document_id, first
    assert first["state"] == "ready", first

    # ``ready`` is terminal: no lifecycle edge leaves it. Asserted before the
    # second upload, from a store instance the writer never touched.
    states = PostgresIngestStateStore(clean_tables)
    with pytest.raises(IllegalTransitionError):
        states.transition(first_document_id, IngestState.FETCHING)

    second_payload = V2_SOURCE.read_bytes()
    second_document_id = _document_id(SCOPE, second_payload)
    second_version = hashlib.sha256(second_payload).hexdigest()
    assert second_document_id != first_document_id
    assert second_version != first_version
    submission = _parent_service(clean_tables).submit_durable(
        second_payload, declared_format="md"
    )
    assert submission.document_id == second_document_id
    assert submission.state is IngestState.PENDING
    assert submission.version == second_version
    states.close()

    report = _reader(clean_tables, second_document_id)

    # The new document's own pending generation, as a fresh reader sees it.
    assert report["job"]["state"] == "pending", report
    assert report["job"]["document_id"] == second_document_id, report
    assert report["pointer"] == _pointer_expectation(
        second_document_id, second_payload
    ), report
    assert report["status"] == {
        "job_id": second_document_id,
        "document_id": second_document_id,
        "version": second_version,
        "state": "pending",
        "text_ready": False,
    }, report
    # Two documents, each with one live job row: the second accept did
    # not reopen or supersede the first.
    assert report["job_documents"] == sorted(
        [first_document_id, second_document_id]
    ), report

    # The prior generation is untouched by the second accept.
    prior = _reader(clean_tables, first_document_id)
    assert prior["job"]["state"] == "ready", prior
    assert prior["pointer"] == _pointer_expectation(
        first_document_id, first_payload
    ), prior
    assert prior["status"]["version"] == first_version, prior
    assert not [
        row for row in prior["audit"] if row["action"] == "job_superseded"
    ], prior["audit"]
    # The row that opened generation one belongs to the exited child, not
    # to this reader or to the second accept.
    assert prior["audit"][0]["action"] == "job_created", prior["audit"]
    assert prior["audit"][0]["actor"].endswith(f"pid={first['pid']}"), prior
    assert f"pid={os.getpid()}" not in prior["audit"][0]["actor"], prior
    transitions = [
        row for row in prior["audit"] if row["action"] == "transition"
    ]
    assert transitions[-1]["to_state"] == "ready", prior["audit"]
    # The only audit row after readiness is the refused escape above, which
    # the durable store records under its own action.
    rejected = [
        row for row in prior["audit"] if row["action"] == "transition_rejected"
    ]
    assert [(row["from_state"], row["to_state"]) for row in rejected] == [
        ("ready", "fetching")
    ], prior["audit"]


def test_the_pinned_vocabulary_makes_ready_terminal() -> None:
    """``specs/corpus-serve-ingest.md:113``, asserted without a database.

    Exactly seven states, and no lifecycle edge leaves ``ready`` — so the
    replacement below is never a transition out of a completed generation.
    Hermetic on purpose: the vocabulary is a product promise, not a
    deployment feature.
    """
    assert [state.value for state in IngestState] == [
        "pending",
        "fetching",
        "extracting",
        "ocr",
        "cataloging",
        "ready",
        "failed",
    ]
    store: IngestStateStore = InMemoryIngestStateStore()
    store.create_job(DOC, actor="pytest")
    for state in ("fetching", "extracting", "cataloging", "ready"):
        assert store.transition(DOC, state, actor="pytest") is not None
    assert store.get_job(DOC).state is IngestState.READY
    with pytest.raises(IllegalTransitionError):
        store.transition(DOC, IngestState.FETCHING, actor="pytest")
    assert store.get_job(DOC).state is IngestState.READY


def test_the_durable_store_reopens_a_ready_generation_as_pending(
    clean_tables: str,
) -> None:
    """The same vocabulary on real rows: ``ready`` is terminal, and the only
    door that reopens a document's job is the settled replacement, which
    opens it in ``pending`` and leaves the prior generation in the audit.
    """
    store: IngestStateStore = PostgresIngestStateStore(clean_tables)
    assert isinstance(store, PostgresIngestStateStore)
    try:
        store.create_job(DOC, actor="pytest")
        for state in ("fetching", "extracting", "cataloging", "ready"):
            assert store.transition(DOC, state, actor="pytest") is not None
        assert store.get_job(DOC).state is IngestState.READY
        with pytest.raises(IllegalTransitionError):
            store.transition(DOC, IngestState.FETCHING, actor="pytest")
        reopened = store.supersede_job(
            DOC, actor="pytest", metadata={"superseded_version": "v1"}
        )
        assert reopened.state is IngestState.PENDING
        assert store.get_job(DOC).state is IngestState.PENDING
        # Still one job for the document: the generation was replaced, not
        # duplicated (spec ``:150``).
        assert [job.document_id for job in store.list_jobs()] == [DOC]
        prior = [
            row for row in store.list_audit(DOC) if row.action == "job_superseded"
        ]
        assert [(row.from_state, row.to_state) for row in prior] == [
            (IngestState.READY, IngestState.PENDING)
        ]
    finally:
        store.close()


# ----- the raw-byte leg, on the store the deployment configures ---------


@pytest.mark.parametrize(
    "source, declared_format, content_type",
    BYTE_CASES,
    ids=list(BYTE_CASE_IDS),
)
def test_original_bytes_are_read_by_a_fresh_process_from_the_configured_store(
    clean_tables: str,
    configured_object_store: str,
    source: Path,
    declared_format: str,
    content_type: str,
) -> None:
    """The leg the job-row cases above deliberately could not claim.

    Process A runs the real accept door with the byte adapter the product's own
    :func:`~fin.corpus.object_store.create_object_store` selects, so the raw
    bytes go to the configured object store and the pointer plus the ``pending``
    job row go to real Postgres. A exits. Process B — a third pid that never met
    A, holding nothing but the DSN and its own environment — rebuilds all three
    adapters, reads the pointer back, fetches the bytes it names and reports
    them. This is the same shape as
    ``test_live_upload_bytes_persist_after_uvicorn_exit_in_fresh_adapter_process``,
    with the permanent-corpus door in place of the thread-attachment one.

    Byte identity is asked twice and independently: B compares the stored body
    with the committed fixture itself, and the parent recomputes the digest from
    its own read of that fixture rather than trusting B's arithmetic. The job's
    own SHA-256 must be the digest B calculated over the bytes it fetched, which
    is what makes the pointer a claim about the object rather than about a
    buffer A once had.

    No worker has run, so B must also see the accept-time state unchanged: the
    same ``document_id``, the version the digest names, and ``pending``.
    """
    payload = source.read_bytes()
    version = hashlib.sha256(payload).hexdigest()
    document_id = _document_id(SCOPE, payload)
    # Derived before any write: the key is identity-based, so the case can name
    # the only object it owns (and delete it) even if the accept dies halfway.
    object_key = f"filings/{document_id}/{version}"
    expected_pointer = {
        "object_key": object_key,
        "sha256": version,
        "size_bytes": len(payload),
        "content_type": content_type,
    }
    try:
        written = _run_child(
            _ACCEPT_ON_REAL_BYTES_CHILD,
            str(source),
            SCOPE,
            declared_format,
            dsn=clean_tables,
        )
        assert int(written["pid"]) != os.getpid(), written
        # The real adapter, the configured bucket, the identity-derived key:
        # anything else would leave this leg claiming what it did not run.
        assert written["adapter"] == "S3CompatibleObjectStore", written
        assert written["bucket"] == configured_object_store, written
        assert written["state"] == "pending", written
        assert written["document_id"] == document_id, written
        assert written["version"] == version, written
        assert written["size_bytes"] == len(payload), written
        # The pointer row, as the accepting process itself wrote it.
        assert written["pointer"] == expected_pointer, written
        assert written["pointer"]["sha256"] == written["sha256"], written

        report = _run_child(
            _BYTES_READBACK_CHILD,
            document_id,
            str(source),
            dsn=clean_tables,
        )
        assert int(report["pid"]) not in {os.getpid(), int(written["pid"])}, report
        assert report["adapter"] == "S3CompatibleObjectStore", report
        assert report["bucket"] == configured_object_store, report

        # What the exited accept left, as an unrelated process reports it.
        assert report["job"] == {
            "document_id": document_id,
            "state": "pending",
            "error_type": None,
            "error_message": None,
        }, report
        assert report["pointer"] == expected_pointer, report
        assert report["status"] == {
            "document_id": document_id,
            "version": version,
            "state": "pending",
        }, report

        # The bytes themselves crossed the boundary.
        assert report["bytes_identical"] is True, {
            "stored": report["stored"],
            "expected": report["expected"],
        }
        assert report["stored_first_and_last"] == report["expected_first_and_last"], (
            report
        )
        assert report["stored"] == {
            "size_bytes": len(payload),
            "sha256": version,
        }, report
        # Recomputed here, so a reader-side bug cannot be the only witness.
        assert report["stored"] == report["expected"], report
        assert report["pointer"]["sha256"] == report["stored"]["sha256"], report
    finally:
        _delete_objects([object_key])
    # Zero keys of this case survive: the delete above is followed by a read
    # that has to raise the typed not-found.
