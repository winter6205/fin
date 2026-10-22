"""T3 real-boundary round trip: durable accept -> worker -> real iknow push.

The default ``uv run pytest -q`` never runs this file — it is marked
``mcp`` (``tests/retrieve/conftest.py`` deselects that mark unless
``-m mcp`` names it) and its own fixtures additionally gate on the
durable pair (Postgres DSN + object-store trio), so an environment
without them records a skip, i.e. blocked evidence, never a fake pass::

    uv run pytest -q -m mcp tests/retrieve/test_live_durable_worker.py

Every existing durable-worker leg (``tests/corpus/test_ingest_worker*.py``,
``test_ingest_durable_restart.py``) proves the fin-side durability with
the contract fake: the push port echoes. The one claim none of them can
make is the full T3 chain across BOTH real boundaries at once — bytes
that leave the request in a durable object store, a job that survives in
Postgres, a worker that re-reads them through the pointer, and a REAL
iknow-memory service that computes the ``content_identity`` the
``cataloging -> ready`` edge then persists (spec
``corpus-serve-ingest`` Decision 2: the service's answer is the only
authoritative one). Meanwhile the live MCP pushes
(``test_live_corpus_ingest.py``) drive the synchronous ``ingest`` door,
which never touches the durable job states. This file joins the two:

1. ``submit_durable`` on the configured SeaweedFS + Postgres accepts the
   committed AAPL fixture — one object, one pointer, one ``pending`` row;
2. ``IngestWorker.run_job`` (a fresh worker object, journal on disk)
   claims via the CAS edge, re-reads the bytes through the durable
   pointer (no re-upload — the same key is proven to hold the same
   object afterwards) and pushes through ``MCPPushPort`` bound to the
   spawned real stdio server on the pg_es branch;
3. the job reaches ``ready`` and the audit row's persisted
   ``content_identities`` map is non-empty, keyed by exactly the pushed
   envelopes' ``source_ref`` values, each value matching the producing
   side's published oracle formula over that envelope's whole body —
   and equal to the row real iknow PG actually stored;
4. a raw ``read_source`` tools/call on the same live session resolves
   one pushed unit's frozen body BYTE-identically (the operator-console
   read path over the worker's own push);
5. a second ``run_job`` joins the ready generation without re-running.

Cleanup follows this repo's live-test discipline: the iknow side lives
in per-run unique fin-owned PG schema + ES indices that
``live_iknow`` drops at teardown (the "delete the source rows" pattern
of the other pg_es drivers); the fin job/audit/pointer tables are wiped
before and after; the one object key the case wrote is deleted through
the product port and proven gone with the typed not-found.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any, Iterator

import psycopg
import pytest

from fin.corpus.ingest_endpoint import IngestEndpoint
from fin.corpus.ingest_state import IngestState, PostgresIngestStateStore
from fin.corpus.ingest_worker import IngestWorker
from fin.corpus.metadata_store import PostgresMetadataStore
from fin.corpus.object_store import create_object_store
from fin.corpus.object_store_errors import UnknownObjectKeyError
from fin.retrieve.mcp_push_port import MCPPushPort

from live_iknow_support import (
    IKNOW_ENTRY,
    IKNOW_PG_URL,
    drop_es_indices,
    live_iknow,
)
from test_live_content_identity import _IDENTITY_RE, _oracle_content_identity


pytestmark = pytest.mark.mcp

_PROJECT_ROOT = Path(__file__).resolve().parents[2]

# The committed representative fixture the live ingest route already
# pushes (test_live_corpus_ingest.py): a real 10-K rendering whose units
# 切分 into many children, so the identity map and the stored rows are a
# batch, not a single echo.
_FIXTURE = (
    _PROJECT_ROOT
    / "data/corpus/dataset/documents/md/AAPL-2022Q3__table_dense__md__md.md"
)

_RUN = uuid.uuid4().hex[:10]
# Per-run unique trusted scope: the content-derived document id (and
# every source_ref this case pushes) is then unaddressable by any other
# live driver sharing this cluster.
_SCOPE = f"fin-t3dur-{_RUN}"
_CHUNK_INDEX = f"fin_t3dur_chunks_{_RUN}"
_ENTITY_INDEX = f"fin_t3dur_entities_{_RUN}"
_PG_SCHEMA = f"fin_t3dur_pg_{_RUN}"

_STATE_DSN_ENV = "FIN_INGEST_STATE_DSN"
_POSTGRES_URL_ENV = "FIN_CHECKPOINT_URL"
_OBJECT_STORE_URL_ENV = "FIN_OBJECT_STORE_URL"
_OBJECT_STORE_ENDPOINT_ENV = "FIN_OBJECT_STORE_ENDPOINT"
_OBJECT_STORE_BUCKET_ENV = "FIN_OBJECT_STORE_BUCKET"

_REQUEST_IDS = itertools.count(1)


def _dsn() -> str:
    return (
        os.environ.get(_STATE_DSN_ENV) or os.environ.get(_POSTGRES_URL_ENV) or ""
    ).strip()


def _document_id(scope: str, data: bytes) -> str:
    """The public upload's content-derived id, written out longhand."""
    digest = hashlib.sha256(data).digest()
    preimage = (
        b"fin-corpus-document-v1\x00" + scope.encode("utf-8") + b"\x00" + digest
    )
    return "doc_" + hashlib.sha256(preimage).hexdigest()


def _object_store_pair() -> tuple[str, str]:
    endpoint = (
        os.environ.get(_OBJECT_STORE_URL_ENV)
        or os.environ.get(_OBJECT_STORE_ENDPOINT_ENV)
        or ""
    ).strip()
    bucket = (os.environ.get(_OBJECT_STORE_BUCKET_ENV) or "").strip()
    return endpoint, bucket


@pytest.fixture
def durable_env() -> str:
    """The fin-side durable pair, gated the same way the corpus modules gate.

    A missing DSN or object-store trio skips the leg (blocked evidence);
    the fin job/audit/pointer tables are wiped before and after so no
    row of this run decides the next one's state.
    """
    if not _dsn():
        pytest.skip(
            f"neither {_STATE_DSN_ENV} nor {_POSTGRES_URL_ENV} is set; the "
            "durable worker legs cannot run"
        )
    endpoint, bucket = _object_store_pair()
    if not (endpoint and bucket):
        pytest.skip(
            f"{_OBJECT_STORE_URL_ENV} / {_OBJECT_STORE_ENDPOINT_ENV} and "
            f"{_OBJECT_STORE_BUCKET_ENV} are not configured; the durable "
            "accept would have nowhere to put the raw bytes"
        )
    _wipe_fin_tables()
    yield _dsn()
    _wipe_fin_tables()


def _wipe_fin_tables() -> None:
    with psycopg.connect(_dsn()) as conn:
        conn.execute("DELETE FROM fin_ingest_audit")
        conn.execute("DELETE FROM fin_ingest_job")
        conn.execute("DELETE FROM fin_corpus_object_pointer")
        conn.commit()


@pytest.fixture(scope="module")
def live() -> Iterator[Any]:
    """One real iknow stdio process on the pg_es branch for the module.

    Same spawn and teardown discipline as ``test_live_content_identity``:
    per-run fin-owned schema and ES indices, dropped at teardown, which
    is how this case "deletes" the source rows it pushed.
    """
    if not IKNOW_ENTRY.is_file():
        pytest.skip(
            f"{IKNOW_ENTRY} is missing; run `npm run build` in the "
            "iknow-memory checkout to exercise the live durable round trip"
        )
    monkeypatch = pytest.MonkeyPatch()
    try:
        with live_iknow(
            monkeypatch,
            storage="pg_es",
            es_chunk_index=_CHUNK_INDEX,
            es_entity_index=_ENTITY_INDEX,
            pg_schema=_PG_SCHEMA,
        ) as session:
            yield session
    finally:
        monkeypatch.undo()
        drop_es_indices(_CHUNK_INDEX, _ENTITY_INDEX)


def _read_source(live: Any, source_ref: str) -> dict[str, Any]:
    """One raw ``tools/call read_source`` on the live session, decoded."""
    response = live.session.call(
        {
            "jsonrpc": "2.0",
            "id": next(_REQUEST_IDS),
            "method": "tools/call",
            "params": {
                "name": "read_source",
                "arguments": {"source_ref": source_ref},
            },
        },
        20.0,
    )
    assert "error" not in response, response
    result = response["result"]
    assert result.get("isError") is not True, result
    return json.loads(result["content"][0]["text"])


def _iknow_rows(sql: str) -> list[tuple[Any, ...]]:
    with psycopg.connect(IKNOW_PG_URL, autocommit=True) as connection:
        return list(connection.execute(sql).fetchall())


def test_durable_accept_and_worker_push_reaches_ready_on_the_real_service(
    durable_env: str, live: Any, tmp_path: Path
) -> None:
    """The full T3 chain across both real boundaries, then read back."""
    dsn = durable_env
    payload = _FIXTURE.read_bytes()
    version = hashlib.sha256(payload).hexdigest()
    document_id = _document_id(_SCOPE, payload)
    object_key = f"filings/{document_id}/{version}"

    objects = create_object_store()
    # The real adapter, not a process-local stand-in (same witness as the
    # T2 cross-process byte leg): without this the bucket claims below
    # would be about memory.
    assert type(objects).__name__ == "S3CompatibleObjectStore", objects
    endpoint = IngestEndpoint(
        push_port=MCPPushPort(session=live.session, timeout_seconds=60.0),
        object_store=objects,
        metadata_store=PostgresMetadataStore(dsn),
        state_store=PostgresIngestStateStore(dsn),
        corpus_scope_id=_SCOPE,
    )
    states = PostgresIngestStateStore(dsn)
    try:
        submission = endpoint.submit_durable(payload, declared_format="md")
        assert submission.document_id == document_id, submission
        assert submission.state is IngestState.PENDING, submission
        assert submission.version == version, submission
        # The accept's byte work is done and ONLY the accept's: one
        # identity-derived object holding exactly the submitted bytes.
        assert objects.get(object_key) == payload

        worker = IngestWorker(endpoint, journal_dir=tmp_path / "journal")
        outcome = worker.run_job(document_id)
        assert outcome.ran is True, outcome
        assert outcome.busy is False, outcome
        assert outcome.state is IngestState.READY, outcome
        result = outcome.result
        assert result is not None
        assert result.written >= 1, result
        assert result.block_count == result.written + result.duplicates, result
        assert result.envelopes, "the durable run pushed no envelopes"
        # No re-upload: the run consumed the one accepted object; the key
        # still holds exactly the accepted bytes.
        assert objects.get(object_key) == payload
        assert states.get_job(document_id).state is IngestState.READY

        # (3) the persisted authority: content_identities on the
        # cataloging -> ready edge, keyed by the pushed source_refs, each
        # the producing side's formula over the WHOLE envelope body.
        audit = states.list_audit(document_id)
        ready_rows = [
            row
            for row in audit
            if row.action == "transition" and row.to_state is IngestState.READY
        ]
        assert len(ready_rows) == 1, audit
        persisted = dict(ready_rows[0].metadata["content_identities"])
        bodies = {env.source_ref: env.body for env in result.envelopes}
        assert set(persisted) == set(bodies), (
            "the persisted identity map is not keyed by exactly the "
            "envelopes this run pushed"
        )
        assert persisted, "the ready edge persisted an empty identity map"
        for source_ref, identity in persisted.items():
            assert _IDENTITY_RE.match(identity), (source_ref, identity)
            assert identity == _oracle_content_identity(bodies[source_ref]), (
                f"the persisted identity for {source_ref} is not the "
                "producing side's formula over that envelope's whole body"
            )
        # The service's own verdict, read straight from iknow PG: the
        # stored rows carry exactly the pairs fin persisted (the run's
        # per-run schema holds nothing but this case's pushes).
        stored = dict(
            _iknow_rows(
                "SELECT source_ref, content_identity "
                f"FROM {_PG_SCHEMA}.source_documents"
            )
        )
        assert stored == persisted, (
            "the identities fin persisted on the ready edge are not the "
            "rows the real service stored beside the same source_ref"
        )

        # (4) the read-back boundary: read_source resolves one pushed
        # unit's frozen text byte-identically.
        probe = result.envelopes[0]
        read = _read_source(live, probe.source_ref)
        assert read["source_ref"] == probe.source_ref, read
        assert read["content"] == probe.body, (
            "read_source did not return the frozen body the worker pushed"
        )

        # (5) a second sweep of the ready generation joins, never re-runs.
        again = worker.run_job(document_id)
        assert again.ran is False and again.state is IngestState.READY, again
    finally:
        states.close()
        objects.delete(object_key)
        with pytest.raises(UnknownObjectKeyError):
            objects.get(object_key)
