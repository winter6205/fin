"""EP10 recovery against real storage: the ``p2:162`` boundary proof.

The in-process suite (``test_structured_evidence_recovery.py``) proves the
sweep's decisions. It cannot prove the boundary, because a fixture holds no
database and no child. This lane does, in the shape T13 and P2-I2 established: a
**real built iknow child process** over stdio, driven through the **real**
:class:`~fin.host.evidence.evidence_session_registry.EvidenceSessionRegistry`, against
**real PostgreSQL**.

What crosses a real boundary here, and would not cross one in a fixture:

* the recovery gate is a real row, written by the real drain on a real gap, and
  read back by a **second** ``PostgresThreadStore`` — a restart, which is the
  case T33 exists for;
* the revalidation verdicts are iknow's own ``decideGrantRead`` decisions
  against real ``evidence_grants`` rows, so "revoked" and "purged" are storage
  facts and not seeded refusal strings;
* the denied payloads are shown to be **gone from ``fin_run``** by reading the
  JSONB column back with SQL, not by trusting fin's own accessor.

**Two scopes, two consumers, and what each one can actually decide.** One binding
holds one scope, and the store's decision order is NOT caller-relative:
``decideGrantRead`` (``evidence-store.ts:299``) compares the GRANT's pinned
revisions against the grant's OWN scope and denies there, before ``callerRefusal``
(``evidence-read.ts:428``) ever looks at who is asking. Measured in this lane:
consumer A and consumer B each independently reach ``authorization_revoked`` on
scope A's revoked grant and ``purged`` on scope B's purged one. A lifecycle
denial is a fact about the payload, not about the reader, so every sweep may
scrub it — which is what the spec asks for, "revalidate **all** retained
references and scrub denied ones" (``specs/structured-corpus-evidence.md``).

``scope_not_authorized`` is the other half, and it is reachable only for a grant
that is otherwise VALID in its own scope, because a denied grant is refused
before identity is consulted. So the lane carries a fourth reference: live in
scope B, and invisible to consumer A. Consumer A must report it
``scope_not_authorized``, leave that payload in place, and still release the
gate — the live form of the rule that a mis-bound port may not delete the
corpus.

Prerequisites are strict: a built iknow, a reachable database. Anything missing
is a named non-zero failure, never a skip.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import sys
import threading
import uuid
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator

import psycopg
import pytest

from fin.host.evidence.evidence_contract import (
    ACCESS_DENIED,
    EVIDENCE_UNAVAILABLE,
    RETRIEVE_UNAVAILABLE,
)
from fin.host.evidence.evidence_session_registry import EvidenceSessionRegistry
from fin.host.evidence.structured_evidence import build_stored_record
from fin.host.evidence.structured_evidence_admission import admit_structured_evidence
from fin.host.evidence.structured_evidence_invalidation import (
    StructuredInvalidationRefusal,
    drain_invalidation_batch,
)
from fin.host.evidence.structured_evidence_recovery import (
    DENIED,
    UNREADABLE,
    VERIFIED,
    recovery_is_withheld,
    replay_retained_reference,
    revalidate_retained_references,
    sweep_recovered_evidence,
)
from fin.host.evidence.structured_evidence_replay import find_replayable_record
from fin.host.thread_store import PostgresThreadStore, RunRecord
from fin.retrieve.evidence_route import (
    EvidenceIdentityBinding,
    deadline_within,
)

pytestmark = [pytest.mark.sql_live]

sys.path.insert(0, str(Path(__file__).parent.parent / "retrieve"))

from live_iknow_support import _EmbeddingHandler  # noqa: E402


_PG_SCHEMA_RE = re.compile(r"^p2i3_recovery_[0-9a-f]{12}$")

#: The embedding width the live schema is created at.
EMBED_DIMENSIONS = 8

_IKNOW_ROOT_ENV = "FIN_LIVE_IKNOW_ROOT"

#: The two consumers the lane recovers. Per-consumer because the gate is
#: per-consumer, and one binding cannot hold both scopes.
CONSUMER_A = "fin-p2i3-consumer-a"
CONSUMER_B = "fin-p2i3-consumer-b"

# ----- frozen expectations, chosen before the run from the spec --------------

SCOPE_A = "p2i3-owner-scope-a"
SCOPE_B = "p2i3-owner-scope-b"

SOURCE_A_LIVE = "p2i3-source-a-live"
SOURCE_A_REVOKED = "p2i3-source-a-revoked"
SOURCE_B_PURGED = "p2i3-source-b-purged"
SOURCE_B_LIVE = "p2i3-source-b-live"

LIVE_REF = "a1000000-0000-4000-8000-000000000001"
REVOKED_REF = "a2000000-0000-4000-8000-000000000002"
PURGED_REF = "a3000000-0000-4000-8000-000000000003"
SCOPE_B_LIVE_REF = "b4000000-0000-4000-8000-000000000004"

SUPPORT_A_LIVE = "b1000000-0000-4000-8000-000000000001"
SUPPORT_A_REVOKED = "b2000000-0000-4000-8000-000000000002"
SUPPORT_B_PURGED = "b3000000-0000-4000-8000-000000000003"
SUPPORT_B_LIVE = "b4000000-0000-4000-8000-000000000004"

VERSION_A_LIVE = "c1000000-0000-4000-8000-000000000001"
VERSION_A_REVOKED = "c2000000-0000-4000-8000-000000000002"
VERSION_B_PURGED = "c3000000-0000-4000-8000-000000000003"
VERSION_B_LIVE = "c4000000-0000-4000-8000-000000000004"

AUTH_REV = "p2i3-auth-rev-1"
AUTH_REV_MOVED = "p2i3-auth-rev-2"
PURGE_REV = "p2i3-purge-rev-0"
PURGE_REV_MOVED = "p2i3-purge-rev-1"

SCOPE_BINDING_TOKEN = "scope-fin-tenant-p2i3"
QUERY_DIGEST_TOKEN = "sha256:p2i3-query-digest"
READ_REVISION_TOKEN = "rev-p2i3-118"

#: Deliberately distinctive, so a case can assert this payload never appears in
#: a refusal — including the refusals this lane provokes.
RETAINED_VALUE = {"retained": "the-grants-own-value-column", "marker": "p2i3-retained"}

QUERIED_AT = "2026-10-03T00:00:00Z"
QUERIED_AT_INSTANT = datetime(2026, 10, 3, tzinfo=timezone.utc)
EXPIRES_AT = "2027-10-03T00:00:00Z"
EXPIRES_AT_INSTANT = QUERIED_AT_INSTANT + timedelta(days=365)

#: Far from every deadline, so a denial here is a lifecycle fence and not a
#: clock artefact.
SEED_NOW = datetime(2026, 12, 1, tzinfo=timezone.utc)

SEED_NOW_TEXT = "2026-12-01T00:00:00Z"


def _maintenance() -> dict[str, Any]:
    return {
        "maintenance_revision": "maintenance-rev-p2i3",
        "sources_in_scope": 1,
        "published_sources": 1,
        "desired_index": {"pending": 0, "running": 0, "ready": 1, "failed": 0, "stale": 0},
        "conflict_checks": {"pending": 0, "running": 0, "ready": 1, "failed": 0, "stale": 0},
        "ruleset_revision": "structural-alternatives:1",
        "coverage": "structural_candidates_only",
    }


def _record(evidence_ref: str) -> dict[str, Any]:
    row: dict[str, Any] = {
        "schema": "iknow.structured_result",
        "schema_version": 1,
        "subject": "entity",
        "operation": "list",
        "query_digest": QUERY_DIGEST_TOKEN,
        "scope_binding": SCOPE_BINDING_TOKEN,
        "read_revision": READ_REVISION_TOKEN,
        "queried_at": QUERIED_AT,
        "maintenance": _maintenance(),
        "rows": [
            {
                "id": "00000000-0000-4000-8000-00000000aa01",
                "canonical_name": "Northwind Logistics",
                "type": "organization",
                "display_support_id": SUPPORT_A_LIVE,
                "display_disagreement": False,
                "evidence_ref": evidence_ref,
                "evidence_expires_at": EXPIRES_AT,
                "provenance": [
                    {
                        "support_id": SUPPORT_A_LIVE,
                        "source_ref": SOURCE_A_LIVE,
                        "source_version": "source-version-1",
                        "location": {
                            "document_id": "example-document",
                            "version": "frozen-unit-version",
                            "unit": 1,
                        },
                        "span": {"start": 0, "end": 42},
                        "extraction_ref": "extraction-revision-1",
                    }
                ],
                "provenance_complete": True,
                "provenance_next_ref": None,
            }
        ],
        "next_cursor": None,
    }
    admitted = admit_structured_evidence(row)
    assert admitted.admitted, f"the seed sibling was refused: {admitted.refusal}"
    return build_stored_record(dict(admitted.as_dicts()[0]["snapshot"]))


# ----- strict prerequisites: a named failure, never a skip ------------------


def _dsn() -> str:
    url = os.environ.get("FIN_CHECKPOINT_URL") or os.environ.get(
        "GRAPHRAG_MEMORY_DB_URL"
    )
    if not url:
        pytest.fail(
            "neither FIN_CHECKPOINT_URL nor GRAPHRAG_MEMORY_DB_URL is set. This "
            "lane needs the isolated dev PostgreSQL for BOTH the fin turn table "
            "and the iknow evidence schema. Load "
            "scripts/structured-live-env.sh in the same shell invocation. An "
            "absent DSN is a failure, never a skip."
        )
        raise AssertionError("unreachable: pytest.fail does not return")
    return url


def _iknow_root() -> Path:
    raw = os.environ.get(_IKNOW_ROOT_ENV)
    if not raw:
        pytest.fail(
            f"{_IKNOW_ROOT_ENV} is absent. This lane needs a real built iknow "
            "child, not the vitest lanes. An absent root is a failure, never a "
            "skip."
        )
    root = Path(str(raw))
    build = root / "dist" / "index.js"
    if not build.is_file():
        pytest.fail(
            f"the iknow build is missing at {build}. Run `npm run build` in "
            f"{root} first. A missing build is a failure, never a skip."
        )
    return root


def _schema_name() -> str:
    return f"p2i3_recovery_{uuid.uuid4().hex[:12]}"


def _db_url_for_schema(db_url: str, schema: str) -> str:
    if not _PG_SCHEMA_RE.match(schema):
        raise ValueError(f"unsafe pg_schema name: {schema!r}")
    separator = "&" if "?" in db_url else "?"
    return f"{db_url}{separator}options=-c%20search_path%3D{schema}%2Cpublic"


def _connect(db_url: str) -> psycopg.Connection[Any]:
    try:
        return psycopg.connect(db_url, connect_timeout=10)
    except psycopg.Error as exc:
        pytest.fail(
            f"PostgreSQL is unreachable from the configured DSN: "
            f"{type(exc).__name__}. An unreachable database is a failure, never "
            "a skip."
        )


def _transport_env(db_url: str, schema: str, embed_base_url: str) -> dict[str, str]:
    return {
        "GRAPHRAG_MEMORY_STORAGE": "pgvector",
        "GRAPHRAG_MEMORY_DB_URL": _db_url_for_schema(db_url, schema),
        "GRAPHRAG_MEMORY_EMBED_DIMENSIONS": str(EMBED_DIMENSIONS),
        "GRAPHRAG_MEMORY_PGVECTOR_ANN_MAX_DIMENSIONS": "2000",
        "GRAPHRAG_MEMORY_EMBED_API_KEY": "fin-p2i3-test",
        "GRAPHRAG_MEMORY_EMBED_BASE_URL": embed_base_url,
        "GRAPHRAG_MEMORY_EMBED_MODEL": "fin-p2i3-test",
        "GRAPHRAG_MEMORY_RETRIEVAL_BM25_ENABLED": "false",
        "GRAPHRAG_MEMORY_RETRIEVAL_GRAPH_ENABLED": "false",
        "GRAPHRAG_MEMORY_RETRIEVAL_RERANK_ENABLED": "false",
        "GRAPHRAG_MEMORY_NEO4J_URI": "",
        "GRAPHRAG_MEMORY_RETRIEVAL_RERANK_URL": "",
        "ANTHROPIC_AUTH_TOKEN": "",
    }


def _binding(
    scope: str,
    source_refs: tuple[str, ...],
    *,
    actor: str,
    authorization_revision: str,
) -> EvidenceIdentityBinding:
    """One already-bound reader.

    ``authorization_revision`` is the scope's CURRENT revision, because iknow
    re-checks it against the scope's live one before it will decide a grant at
    all (``structured-scope-uow.ts`` ``assertCurrentAuthorization``, reached from
    ``withAuthorizedScope``). A reader minted at a revision the scope has since
    moved past is refused at the identity check with ``access_denied`` before one
    grant is consulted, so it can neither verify nor deny anything.

    That refusal arrives as reason ``backend_unavailable`` (measured), which is
    not an invalidation verdict, so a stale binding yields a sweep that denies
    NOTHING and releases the gate rather than one that fails loudly. Hence the
    revision is supplied explicitly per binding above rather than defaulted.
    """

    return EvidenceIdentityBinding(
        actor_id=actor,
        owner_scope_binding=scope,
        reader_binding=actor,
        source_refs=source_refs,
        authorization_revision=authorization_revision,
        deadline_at=deadline_within(900.0),
    )


# ----- the seed -------------------------------------------------------------

_REFS = [LIVE_REF, REVOKED_REF, PURGED_REF, SCOPE_B_LIVE_REF]
_SUPPORTS = [SUPPORT_A_LIVE, SUPPORT_A_REVOKED, SUPPORT_B_PURGED, SUPPORT_B_LIVE]
_VERSIONS = [VERSION_A_LIVE, VERSION_A_REVOKED, VERSION_B_PURGED, VERSION_B_LIVE]
_SOURCES = [SOURCE_A_LIVE, SOURCE_A_REVOKED, SOURCE_B_PURGED, SOURCE_B_LIVE]
_SCOPES = [SCOPE_A, SCOPE_B]


def _cleanup(db_url: str) -> None:
    """Remove every row this lane wrote, in dependency order.

    WHY this exists, restated because it is the trap, and restated CORRECTLY:
    the tables are PER-RUN, not shared. ``public`` must stay on the child's
    ``search_path`` or the child dies at boot with ``type "vector" does not
    exist`` -- the extension and its type live in ``public`` -- but the child's
    own DDL is unqualified, so the first entry on the path wins and the evidence
    and publication tables are created in the RUN schema, shadowing any copy in
    ``public``. Measured after ``tools/list``: ``evidence_grants``,
    ``evidence_scope_revisions`` and ``structured_publication_scopes`` all
    resolve to the run schema.

    So this must be handed the SCOPED url. Pointed at the unscoped one it
    cleans ``public`` -- which the lane never wrote to -- and leaves the lane's
    own rows in place, so an interrupted earlier run collides with this one on
    the frozen references instead of being cleared. Run BEFORE seeding for that
    reason, and at teardown so the run schema is left as found.
    """

    with _connect(db_url) as connection:
        with connection.cursor() as cur:
            cur.execute(
                "DELETE FROM evidence_grant_extractions WHERE evidence_ref = ANY(%s)",
                (_REFS,),
            )
            cur.execute(
                "DELETE FROM evidence_grant_supports WHERE evidence_ref = ANY(%s)",
                (_REFS,),
            )
            cur.execute("DELETE FROM evidence_grants WHERE evidence_ref = ANY(%s)", (_REFS,))
            cur.execute(
                "DELETE FROM support_extractions WHERE support_id = ANY(%s)", (_SUPPORTS,)
            )
            cur.execute(
                "DELETE FROM assertion_supports WHERE support_id = ANY(%s)", (_SUPPORTS,)
            )
            cur.execute(
                "DELETE FROM source_versions WHERE source_version_id = ANY(%s)", (_VERSIONS,)
            )
            cur.execute(
                "DELETE FROM structured_publication_sources WHERE source_ref = ANY(%s)",
                (_SOURCES,),
            )
            cur.execute(
                "DELETE FROM evidence_scope_revisions WHERE scope_binding = ANY(%s)",
                (_SCOPES,),
            )
            cur.execute(
                "DELETE FROM structured_publication_scopes WHERE scope_binding = ANY(%s)",
                (_SCOPES,),
            )
        connection.commit()


def _seed(scoped_url: str) -> None:
    """Write the scopes, sources, revisions and grants the sweep will judge.

    The two fences are the point of the seed, and each is a disagreement between
    a GRANT's pinned revision and its OWN scope's live one — which is the only
    disagreement ``decideGrantRead`` looks at (``evidence-store.ts:299``). The
    fence is deliberately NOT in the scope row shared by the surviving grant: the
    scope revision is what a reader's binding is minted against, so moving it
    revokes the reader itself rather than the one grant it is meant to fence.

    * REVOKED_REF pins the pre-move authorization revision and scope A has since
      moved, so that grant alone is ``authorization_revoked``;
    * PURGED_REF pins the pre-move purge revision and scope B has since moved, so
      that grant alone is ``purged``;
    * LIVE_REF pins what scope A holds NOW — minted after the move — so it
      survives;
    * SCOPE_B_LIVE_REF pins both of scope B's current revisions, so it is valid
      and reachable, which is what leaves ``scope_not_authorized`` reachable for
      the other consumer to be refused with.
    """

    grant_context = json.dumps(
        {
            "query_digest": QUERY_DIGEST_TOKEN,
            "scope_binding": SCOPE_BINDING_TOKEN,
            "read_revision": READ_REVISION_TOKEN,
        }
    )

    with _connect(scoped_url) as connection:
        with connection.cursor() as cur:
            for scope in (SCOPE_A, SCOPE_B):
                cur.execute(
                    f"INSERT INTO structured_publication_scopes "
                    "(scope_binding, read_revision, maintenance_revision) "
                    "VALUES (%s, 0, 0)",
                    (scope,),
                )
            for scope, ref in (
                (SCOPE_A, SOURCE_A_LIVE),
                (SCOPE_A, SOURCE_A_REVOKED),
                (SCOPE_B, SOURCE_B_PURGED),
                (SCOPE_B, SOURCE_B_LIVE),
            ):
                cur.execute(
                    f"INSERT INTO structured_publication_sources "
                    "(scope_binding, source_ref, active) VALUES (%s, %s, true)",
                    (scope, ref),
                )

            cur.execute(
                f"INSERT INTO evidence_scope_revisions "
                "(scope_binding, authorization_revision, purge_revision) "
                "VALUES (%s, %s, %s), (%s, %s, %s)",
                (
                    SCOPE_A,
                    AUTH_REV,
                    PURGE_REV,
                    SCOPE_B,
                    AUTH_REV,
                    PURGE_REV,
                ),
            )

            for version_id, ref in (
                (VERSION_A_LIVE, SOURCE_A_LIVE),
                (VERSION_A_REVOKED, SOURCE_A_REVOKED),
                (VERSION_B_PURGED, SOURCE_B_PURGED),
                (VERSION_B_LIVE, SOURCE_B_LIVE),
            ):
                cur.execute(
                    f"INSERT INTO source_versions (source_version_id, source_ref, "
                    "source_version, content_digest, origin_object_ref, "
                    "parser_revision, unit_manifest, created_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s)",
                    (
                        version_id,
                        ref,
                        "v1",
                        f"digest-{version_id}",
                        f"object-{version_id}",
                        "p2i3-parser-1",
                        json.dumps({"units": [1]}),
                        SEED_NOW,
                    ),
                )

            for support_id, version_id, source_ref in (
                (SUPPORT_A_LIVE, VERSION_A_LIVE, SOURCE_A_LIVE),
                (SUPPORT_A_REVOKED, VERSION_A_REVOKED, SOURCE_A_REVOKED),
                (SUPPORT_B_PURGED, VERSION_B_PURGED, SOURCE_B_PURGED),
                (SUPPORT_B_LIVE, VERSION_B_LIVE, SOURCE_B_LIVE),
            ):
                cur.execute(
                    f"INSERT INTO assertion_supports (support_id, subject, "
                    "result_id, source_ref, source_version, source_version_id, "
                    "document_id, unit_version, unit, span_start, span_end, "
                    "created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, "
                    "%s, %s, %s)",
                    (
                        support_id,
                        "entity",
                        f"fin-p2i3-{support_id[:4]}",
                        source_ref,
                        "v1",
                        version_id,
                        f"doc-{support_id}",
                        "1",
                        1,
                        0,
                        12,
                        SEED_NOW,
                    ),
                )
                cur.execute(
                    f"INSERT INTO support_extractions "
                    "(support_id, extraction_ref, created_at) "
                    "VALUES (%s, %s, %s)",
                    (support_id, f"p2i3-extraction-{support_id}", SEED_NOW),
                )

            def grant(
                ref: str, *, scope: str, support_id: str, auth_rev: str, purge_rev: str
            ) -> None:
                cur.execute(
                    f"INSERT INTO evidence_grants (evidence_ref, grant_kind, "
                    "subject, operation, query_digest, scope_binding, "
                    "read_revision, authorization_revision, purge_revision, "
                    "queried_at, expires_at, context, value, result_count, "
                    "authorization_receipt_id) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                    "%s::jsonb, %s::jsonb, %s, %s)",
                    (
                        ref,
                        "row",
                        "entity",
                        "list",
                        QUERY_DIGEST_TOKEN,
                        scope,
                        READ_REVISION_TOKEN,
                        auth_rev,
                        purge_rev,
                        QUERIED_AT_INSTANT,
                        EXPIRES_AT_INSTANT,
                        grant_context,
                        json.dumps(RETAINED_VALUE),
                        None,
                        None,
                    ),
                )
                cur.execute(
                    f"INSERT INTO evidence_grant_supports "
                    "(evidence_ref, support_id, role) VALUES (%s, %s, %s)",
                    (ref, support_id, "occurrence"),
                )
                cur.execute(
                    f"INSERT INTO evidence_grant_extractions "
                    "(evidence_ref, support_id, extraction_ref) "
                    "VALUES (%s, %s, %s)",
                    (ref, support_id, f"p2i3-extraction-{support_id}"),
                )

            grant(
                LIVE_REF,
                scope=SCOPE_A,
                support_id=SUPPORT_A_LIVE,
                auth_rev=AUTH_REV_MOVED,
                purge_rev=PURGE_REV,
            )
            grant(
                REVOKED_REF,
                scope=SCOPE_A,
                support_id=SUPPORT_A_REVOKED,
                auth_rev=AUTH_REV,
                purge_rev=PURGE_REV,
            )
            grant(
                PURGED_REF,
                scope=SCOPE_B,
                support_id=SUPPORT_B_PURGED,
                auth_rev=AUTH_REV,
                purge_rev=PURGE_REV,
            )
            grant(
                SCOPE_B_LIVE_REF,
                scope=SCOPE_B,
                support_id=SUPPORT_B_LIVE,
                auth_rev=AUTH_REV,
                purge_rev=PURGE_REV_MOVED,
            )

            # The two lifecycle fences. Each revokes exactly the grants that
            # pinned the pre-move value, which is why LIVE_REF pins the moved
            # revision above and scope A's reader binding carries it too.
            cur.execute(
                f"UPDATE evidence_scope_revisions SET authorization_revision = %s "
                "WHERE scope_binding = %s",
                (AUTH_REV_MOVED, SCOPE_A),
            )
            cur.execute(
                f"UPDATE evidence_scope_revisions SET purge_revision = %s "
                "WHERE scope_binding = %s",
                (PURGE_REV_MOVED, SCOPE_B),
            )
        connection.commit()


# ----- the fin side ---------------------------------------------------------

_CREATED_THREADS: list[str] = []


def _drop_threads(db_url: str) -> None:
    if not _CREATED_THREADS:
        return
    store = PostgresThreadStore(db_url)
    for thread_id in _CREATED_THREADS:
        store.delete_thread(thread_id)
    _CREATED_THREADS.clear()


def _store_records(db_url: str, records: list[dict[str, Any]]) -> str:
    """Persist records through a real store and return the thread id."""

    store = PostgresThreadStore(db_url)
    thread_id = f"p2i3-{uuid.uuid4()}"
    _CREATED_THREADS.append(thread_id)
    store.create_thread(id=thread_id, title="p2i3 recovery thread")
    store.append_run(
        RunRecord(
            id=f"run-{uuid.uuid4()}",
            thread_id=thread_id,
            question="Which organizations are in scope?",
            status="done",
            status_reason="",
            answer="Northwind Logistics.",
            structured_evidence=records,
        )
    )
    return thread_id


def _stored_records(db_url: str, thread_id: str) -> list[dict[str, Any]]:
    """Read the history back over a SEPARATE store instance.

    Re-opening the store is what stands in for a restart: the records come back
    through the JSONB column, not through the object that was written.
    """

    return PostgresThreadStore(db_url).list_runs(thread_id)[0].structured_evidence


def _raw_refs_in_column(db_url: str, thread_id: str) -> list[str]:
    """The references present in ``fin_run``, read with SQL rather than fin.

    Used for the residue and deletion claims, so neither depends on the
    accessor whose behaviour is under test.
    """

    with _connect(db_url) as connection:
        with connection.cursor() as cur:
            cur.execute(
                "SELECT structured_evidence FROM fin_run WHERE thread_id = %s",
                (thread_id,),
            )
            row = cur.fetchone()
    if row is None:
        return []
    return [
        entry["snapshot"]["evidence_ref"]
        for entry in row[0]
        if isinstance(entry.get("snapshot"), dict) and entry["snapshot"].get("evidence_ref")
    ]


def _gap_batch() -> dict[str, Any]:
    return {"events": [], "next_checkpoint": "ckpt-p2i3-after-gap", "gap": True}


@pytest.fixture(scope="module")
def recovery_env() -> Iterator[dict[str, Any]]:
    db_url = _dsn()
    root = _iknow_root()
    schema = _schema_name()
    if not _PG_SCHEMA_RE.match(schema):
        raise ValueError(f"unsafe pg_schema name: {schema!r}")

    with _connect(db_url) as connection:
        with connection.cursor() as cur:
            cur.execute(f"CREATE SCHEMA {schema}")
        connection.commit()

    embedding_server = ThreadingHTTPServer(("127.0.0.1", 0), _EmbeddingHandler)
    threading.Thread(
        target=embedding_server.serve_forever, name="fin-p2i3-embedding", daemon=True
    ).start()
    embed_base_url = f"http://127.0.0.1:{embedding_server.server_port}/v1"

    scoped_url = _db_url_for_schema(db_url, schema)
    command = [shutil.which("node") or "node", str(root / "dist" / "index.js")]
    transport = _transport_env(db_url, schema, embed_base_url)

    registry = EvidenceSessionRegistry(
        command, transport_env=transport, timeout_seconds=90.0
    )
    bindings = {
        "a": _binding(
            SCOPE_A,
            (SOURCE_A_LIVE, SOURCE_A_REVOKED),
            actor="fin-p2i3-reader-a",
            authorization_revision=AUTH_REV_MOVED,
        ),
        "b": _binding(
            SCOPE_B,
            (SOURCE_B_PURGED, SOURCE_B_LIVE),
            actor="fin-p2i3-reader-b",
            authorization_revision=AUTH_REV,
        ),
    }

    # Binding only launches the process; the child's own bootstrap creates the
    # evidence tables, so a tools/list call must come BEFORE the seed or the
    # seed races the child's DDL against this lane's DML.
    try:
        first = registry.bind(bindings["a"])
        first.session.call({"jsonrpc": "2.0", "id": 9101, "method": "tools/list"})
        _cleanup(scoped_url)
        _seed(scoped_url)
        yield {
            "registry": registry,
            "bindings": bindings,
            "scoped_url": scoped_url,
            # The lane reaches its own rows through the SCOPED url, and every
            # helper below takes ``env["db_url"]``. Yielding the scoped url under
            # that key is what keeps the seed, the store and the raw column read
            # on the same schema as the child. ``base_url`` is the unscoped DSN
            # and exists only for schema-level DDL, which cannot run from inside
            # the schema it is about to drop.
            "db_url": scoped_url,
            "base_url": db_url,
            "schema": schema,
            "now": SEED_NOW,
        }
    finally:
        registry.close_all()
        try:
            embedding_server.shutdown()
            embedding_server.server_close()
        except Exception:  # noqa: BLE001 - teardown must not mask a failure
            pass
        with _connect(db_url) as connection:
            with connection.cursor() as cur:
                cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            connection.commit()
        _drop_threads(scoped_url)
        _cleanup(scoped_url)


def _reader(env: dict[str, Any], identity: str) -> Any:
    return env["registry"].bind(env["bindings"][identity]).client


def _open_gap(env: dict[str, Any], consumer_id: str) -> Any:
    """Store the four references, then make the real drain refuse on a gap.

    A context manager, and the teardown is load bearing rather than tidy: a
    sweep scrubs GLOBALLY (it enumerates every thread, because "every retained
    reference" is the requirement), so a thread left over from an earlier case
    would make this case's ``scrubbed_entries`` a count of what earlier cases
    happened to retain. Yielding one thread per case and dropping it afterwards
    is what keeps those counts exact. It also keeps
    :func:`~fin.host.evidence.structured_evidence_replay.find_replayable_record` honest,
    which refuses two records naming one reference.

    Yields the thread id, so a case can read the column back with SQL.
    """

    @contextlib.contextmanager
    def opened() -> Iterator[str]:
        thread_id = _store_records(env["db_url"], [_record(ref) for ref in _REFS])
        try:
            with pytest.raises(StructuredInvalidationRefusal) as caught:
                drain_invalidation_batch(
                    PostgresThreadStore(env["db_url"]),
                    consumer_id=consumer_id,
                    fetch=lambda cursor: _gap_batch(),
                    acknowledge=lambda checkpoint: pytest.fail(
                        f"a gap acknowledged {checkpoint!r}"
                    ),
                )
            assert caught.value.code == RETRIEVE_UNAVAILABLE
            yield thread_id
        finally:
            PostgresThreadStore(env["db_url"]).delete_thread(thread_id)

    return opened()


# ----- the boundary, against real storage -----------------------------------


def test_a_gap_closes_a_gate_that_a_restart_still_sees(recovery_env) -> None:
    """The gate is a durable row, not a process fact.

    The read is made through a SECOND ``PostgresThreadStore``, over a connection
    opened after the gap, so nothing in this assertion can be a leftover object.
    This is the ``p2:162`` restart proof: a fresh instance sees the gate the
    previous one closed.
    """

    with _open_gap(recovery_env, CONSUMER_A):
        restarted = PostgresThreadStore(recovery_env["db_url"])
        state = restarted.read_recovery_state(CONSUMER_A)
        assert state.withheld is True
        assert "gap=True" in state.reason
        # And the watermark the gap refused on is exactly where it was: unmoved.
        checkpoint = restarted.read_invalidation_checkpoint(CONSUMER_A)
        assert checkpoint.acked_checkpoint == ""
        assert checkpoint.applied_checkpoint == ""
        assert checkpoint.last_seq == 0


def test_nothing_retained_is_servable_while_recovery_is_open(recovery_env) -> None:
    """T33's clause: snapshots stay unavailable until recovery completes.

    The live reference is the honest case for this. It is NOT revoked and NOT
    purged — the store would happily serve it — so the only thing standing
    between the reader and its payload is the gate. That is what makes this the
    proof rather than a restatement of the revoked cases.
    """

    with _open_gap(recovery_env, CONSUMER_A) as thread_id:
        records = _stored_records(recovery_env["db_url"], thread_id)
        assert len(records) == len(_REFS), "the seed did not survive the JSONB round trip"
        reader = _reader(recovery_env, "a")

        for evidence_ref in _REFS:
            found = find_replayable_record(records, evidence_ref)
            assert found is not None, evidence_ref
            outcome = replay_retained_reference(
                PostgresThreadStore(recovery_env["db_url"]),
                found,
                consumer_id=CONSUMER_A,
                reader=reader,
                now=SEED_NOW,
            )
            assert outcome.replayed is False, f"{evidence_ref} was served mid-recovery"
            assert outcome.refusal is not None
            assert outcome.refusal.code == EVIDENCE_UNAVAILABLE
            # No snapshot on the refusal: a withheld citation is not an empty one.
            assert outcome.evidence is None
            # And the withheld payload never leaks into the refusal text.
            assert RETAINED_VALUE["marker"] not in str(outcome.refusal)

        # The payloads are still in storage — the gate withholds, it does not delete.
        assert sorted(_raw_refs_in_column(recovery_env["db_url"], thread_id)) == sorted(_REFS)


def test_revalidation_asks_the_real_store_and_finds_the_revoked_grant(
    recovery_env,
) -> None:
    """The verdict is iknow's own decision, made now, about a live row.

    The stored record is byte-identical to the live one: same digest, same
    grant-time tokens, same deadline. The only thing that has changed is the
    grant's authorization revision against its scope's live one, which fin's
    frozen copy cannot observe. So this case is what separates the ticket's
    "current" from the weaker grant-time comparison.

    It is also where the lane pins the decision ORDER, because three of the four
    references are decided before iknow ever asks who is asking
    (``evidence-store.ts:299``): REVOKED_REF and PURGED_REF are lifecycle denials
    any reader may act on, and only SCOPE_B_LIVE_REF is refused for belonging to
    another scope. A sweep handed a mis-bound port therefore still reaches the
    invalidations, and still declines to delete the one reference it cannot
    judge.
    """

    with _open_gap(recovery_env, CONSUMER_A):
        reader = _reader(recovery_env, "a")

        report = revalidate_retained_references(
            PostgresThreadStore(recovery_env["db_url"]), reader=reader, now=SEED_NOW
        )
        by_ref = {v.evidence_ref: v for v in report.verdicts}
        assert by_ref[REVOKED_REF].state == DENIED
        assert by_ref[REVOKED_REF].code == ACCESS_DENIED
        assert by_ref[REVOKED_REF].reason == "authorization_revoked"
        # The other scope's purge fence is a fact about that grant, not about this
        # reader, so this binding reaches it too.
        assert by_ref[PURGED_REF].state == DENIED
        assert by_ref[PURGED_REF].reason == "purged"
        assert by_ref[LIVE_REF].state == VERIFIED
        # And the reference that is merely out of this binding's scope is not this
        # binding's to judge, so the sweep must say so rather than guess.
        assert by_ref[SCOPE_B_LIVE_REF].state == UNREADABLE
        assert by_ref[SCOPE_B_LIVE_REF].reason == "scope_not_authorized"
        assert report.denied == (REVOKED_REF, PURGED_REF)
        assert report.verified == (LIVE_REF,)


def test_a_sweep_scrubs_the_denied_from_real_storage_then_releases(
    recovery_env,
) -> None:
    """T33's completion clause, in the order it demands.

    No denied snapshot is served before the recovered events apply: the gate is
    shut while recovery runs, and the denied payloads are deleted from
    ``fin_run`` — read back with SQL, not through fin's own accessor — before
    the gate opens.
    """

    with _open_gap(recovery_env, CONSUMER_A) as thread_id:
        store = PostgresThreadStore(recovery_env["db_url"])
        reader = _reader(recovery_env, "a")

        outcome = sweep_recovered_evidence(
            store, consumer_id=CONSUMER_A, reader=reader, now=SEED_NOW
        )
        assert outcome.released is True, outcome.refusal
        assert outcome.denied == (REVOKED_REF, PURGED_REF)
        # Both denied payloads, in the one run that retained them: the sweep
        # deletes what it judged and nothing else.
        assert outcome.scrubbed_entries == 2
        assert outcome.scrubbed_runs == 1
        assert outcome.examined == len(_REFS)

        # The denied payloads are gone from the column itself, verified with SQL.
        refs = _raw_refs_in_column(recovery_env["db_url"], thread_id)
        assert REVOKED_REF not in refs
        assert PURGED_REF not in refs
        # The reference outside this binding's scope was NOT deleted: a sweep
        # that scrubbed on a caller-identity refusal would have removed the
        # corpus, and this is where that would show.
        assert sorted(refs) == sorted([LIVE_REF, SCOPE_B_LIVE_REF])

        # The gate is open only now, and the surviving reference serves.
        assert (
            recovery_is_withheld(PostgresThreadStore(recovery_env["db_url"]), CONSUMER_A)
            is False
        )
        records = _stored_records(recovery_env["db_url"], thread_id)
        assert find_replayable_record(records, REVOKED_REF) is None
        replay = replay_retained_reference(
            PostgresThreadStore(recovery_env["db_url"]),
            find_replayable_record(records, LIVE_REF),
            consumer_id=CONSUMER_A,
            reader=reader,
            now=SEED_NOW,
        )
        assert replay.replayed is True, replay.refusal
        assert replay.evidence is not None
        assert replay.evidence.snapshot["evidence_ref"] == LIVE_REF


def test_a_denied_reference_is_refused_by_the_store_even_without_the_gate(
    recovery_env,
) -> None:
    """The third mechanism: fin can only serve what the store grants, right now.

    The gate is bypassed here on purpose — this calls the replay primitive
    directly, with no recovery state consulted at all. A revoked reference is
    still refused, because the read is a live decision by iknow. That is what
    makes the gate defence in depth for the recovery WINDOW rather than the only
    thing standing between a denial and a payload.
    """

    from fin.host.evidence.structured_evidence_replay import replay_stored_evidence

    with _stored_only(recovery_env) as thread_id:
        records = _stored_records(recovery_env["db_url"], thread_id)
        found = find_replayable_record(records, PURGED_REF)
        assert found is not None

        outcome = replay_stored_evidence(
            found, reader=_reader(recovery_env, "b"), now=SEED_NOW
        )
        assert outcome.replayed is False
        assert outcome.refusal is not None
        assert outcome.refusal.code == EVIDENCE_UNAVAILABLE
        assert outcome.refusal.reason == "purged"
        assert outcome.evidence is None


def _stored_only(env: dict[str, Any]) -> Any:
    """Store the four references and drop them again, with no gate opened.

    For the cases that exercise the replay primitive directly and need a stored
    record rather than a recovery.
    """

    @contextlib.contextmanager
    def stored() -> Iterator[str]:
        thread_id = _store_records(env["db_url"], [_record(ref) for ref in _REFS])
        try:
            yield thread_id
        finally:
            PostgresThreadStore(env["db_url"]).delete_thread(thread_id)

    return stored()


def test_the_purged_grant_is_scrubbed_by_its_own_consumers_sweep(
    recovery_env,
) -> None:
    """The second consumer's sweep reaches the verdict its own scope produced.

    Consumer B holds scope B, so it is the only binding that can see its purge
    fence as anything but a denial it shares with everyone — and it is still not
    a licence to touch scope A's live reference. Both halves are asserted: the
    purge is scrubbed, and scope A's surviving grant is left in place.
    """

    with _open_gap(recovery_env, CONSUMER_B) as thread_id:
        store = PostgresThreadStore(recovery_env["db_url"])

        outcome = sweep_recovered_evidence(
            store, consumer_id=CONSUMER_B, reader=_reader(recovery_env, "b"), now=SEED_NOW
        )
        assert outcome.released is True, outcome.refusal
        assert outcome.denied == (REVOKED_REF, PURGED_REF)
        assert outcome.scrubbed_entries == 2

        refs = _raw_refs_in_column(recovery_env["db_url"], thread_id)
        assert PURGED_REF not in refs
        # Scope A's live reference is in the OTHER scope, so this binding cannot
        # judge it and must not delete it. REVOKED_REF is denied here too — a
        # lifecycle denial is not caller-relative — so it goes as well.
        assert sorted(refs) == sorted([LIVE_REF, SCOPE_B_LIVE_REF])


def test_a_sweep_never_advances_the_acknowledgement(recovery_env) -> None:
    """The stream position stays owed to the drain, live.

    Acknowledging here would retire events fin never received — the exact thing
    the gap means — so the sweep has no statement that can move it.
    """

    with _open_gap(recovery_env, CONSUMER_A):
        store = PostgresThreadStore(recovery_env["db_url"])
        before = store.read_invalidation_checkpoint(CONSUMER_A)

        outcome = sweep_recovered_evidence(
            store, consumer_id=CONSUMER_A, reader=_reader(recovery_env, "a"), now=SEED_NOW
        )
        assert outcome.released is True, outcome.refusal

        after = PostgresThreadStore(recovery_env["db_url"]).read_invalidation_checkpoint(
            CONSUMER_A
        )
        assert after.acked_checkpoint == before.acked_checkpoint == ""
        assert after.applied_checkpoint == before.applied_checkpoint == ""
        assert after.last_seq == before.last_seq == 0


def test_a_withheld_gate_survives_a_successful_drain(recovery_env) -> None:
    """Only the sweep opens the gate; a drain that resumes the stream does not."""

    with _open_gap(recovery_env, CONSUMER_A):
        store = PostgresThreadStore(recovery_env["db_url"])
        with pytest.raises(StructuredInvalidationRefusal):
            drain_invalidation_batch(
                store,
                consumer_id=CONSUMER_A,
                fetch=lambda cursor: _gap_batch(),
                acknowledge=lambda checkpoint: pytest.fail("unreachable"),
            )
        assert recovery_is_withheld(store, CONSUMER_A) is True

        # Release through the sweep, and confirm the gate then reads open.
        sweep_recovered_evidence(
            store, consumer_id=CONSUMER_A, reader=_reader(recovery_env, "a"), now=SEED_NOW
        )
        assert recovery_is_withheld(
            PostgresThreadStore(recovery_env["db_url"]), CONSUMER_A
        ) is False


def test_the_evidence_tables_are_shared_and_the_lane_owns_only_its_rows(
    recovery_env,
) -> None:
    """Pin the trap, so a future reader of this lane does not rediscover it.

    The tables are PER-RUN. ``public`` stays on the child's ``search_path``
    because the ``vector`` type lives there, but the child's own DDL is
    unqualified, so the evidence and publication tables are created in the run
    schema and shadow any copy in ``public``. Both halves of that are asserted
    here against ``env["db_url"]``, which is the SCOPED url: the lane's rows are
    where its own seed put them, and the lane owns only the rows named by its
    frozen identifiers, which ``_cleanup`` removes.
    """

    with _connect(recovery_env["db_url"]) as connection:
        with connection.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM evidence_grants WHERE evidence_ref = ANY(%s)",
                (_REFS,),
            )
            assert cur.fetchone()[0] == len(_REFS)
            cur.execute(
                "SELECT count(*) FROM evidence_scope_revisions "
                "WHERE scope_binding = ANY(%s)",
                (_SCOPES,),
            )
            assert cur.fetchone()[0] == 2
