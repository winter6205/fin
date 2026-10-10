"""EP7 replay against real storage: the boundary proof ``p2:162`` asks for.

The in-process suite (``test_structured_evidence_replay.py``) proves the chain's
decisions. It cannot prove the boundary, because a fixture holds no database and
no child. This lane does, in the shape T13 established: a **real built iknow
child process** over stdio, driven through the **real**
:class:`~fin.host.evidence_session_registry.EvidenceSessionRegistry`, against
**real PostgreSQL** in a per-run schema.

What crosses a real boundary here, and would not cross one in a fixture:

* the stored snapshot makes a real JSONB round trip through ``fin_run`` and is
  read back by a SECOND store instance, standing in for a restart;
* the read is served by iknow's own ``decideGrantRead`` against the real
  ``evidence_grants`` row, so the authorization / expiry / purge refusals are
  storage decisions and not asserted strings;
* the deadline comparison runs against the two real spellings — fin's
  whole-second ``Z`` text and the millisecond text PostgreSQL hands back.

Prerequisites are strict: a built iknow, a reachable database. Anything missing
is a named non-zero failure, never a skip.

This lane also records what it does NOT claim. The seed is an owner-validated
retained snapshot written directly, exactly as T13's is: it proves the replay
boundary, not the Query lane's ability to produce a citable row, which P1-I6
still owns.
"""

from __future__ import annotations

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

from fin.host.evidence_contract import (
    ACCESS_DENIED,
    EVIDENCE_EXPIRED,
    EVIDENCE_UNAVAILABLE,
    MALFORMED_EVIDENCE_CONTRACT,
    UNSUPPORTED_SOURCE_BINDING,
)
from fin.host.evidence_session_registry import EvidenceSessionRegistry
from fin.host.structured_evidence import build_stored_record
from fin.host.structured_evidence_admission import admit_structured_evidence
from fin.host.structured_evidence_replay import (
    find_replayable_record,
    replay_stored_evidence,
)
from fin.host.thread_store import PostgresThreadStore, RunRecord
from fin.retrieve.evidence_route import (
    EvidenceIdentityBinding,
    deadline_within,
    identity_env,
)

pytestmark = [pytest.mark.sql_live]

sys.path.insert(0, str(Path(__file__).parent.parent / "retrieve"))

from live_iknow_support import _EmbeddingHandler  # noqa: E402


_PG_SCHEMA_RE = re.compile(r"^p2i2_replay_[0-9a-f]{12}$")

#: The embedding width the live schema is created at.
EMBED_DIMENSIONS = 8

#: The iknow build root every live lane binds to.
_IKNOW_ROOT_ENV = "FIN_LIVE_IKNOW_ROOT"


# ----- frozen expectations, chosen before the run from the spec --------------

SCOPE_LIVE = "p2i2-owner-scope-live"
SCOPE_PURGED = "p2i2-owner-scope-purged"
SCOPE_REVOKED = "p2i2-owner-scope-revoked"
SCOPE_EXPIRED = "p2i2-owner-scope-expired"

SOURCE_LIVE = "p2i2-source-live"
SOURCE_PURGED = "p2i2-source-purged"
SOURCE_REVOKED = "p2i2-source-revoked"
SOURCE_EXPIRED = "p2i2-source-expired"
#: The drifted grant's own source. ``source_versions`` is unique on
#: ``(source_ref, source_version)``, so a second row under the live source would
#: collide — and reusing the live source would also put the drifted support
#: outside this identity's authorized subset, which would make the read refuse
#: for the wrong reason.
SOURCE_DRIFTED = "p2i2-source-drifted"

AUTH_REV = "p2i2-auth-rev-1"
AUTH_REV_MOVED = "p2i2-auth-rev-2"
PURGE_REV = "p2i2-purge-rev-0"
PURGE_REV_MOVED = "p2i2-purge-rev-1"

#: One reference per fence. Isolating them into their own scopes is what keeps
#: one case from moving a revision under another, exactly as T13 does.
LIVE_REF = "e1000000-0000-4000-8000-000000000001"
PURGED_REF = "e2000000-0000-4000-8000-000000000002"
REVOKED_REF = "e3000000-0000-4000-8000-000000000003"
EXPIRED_REF = "e4000000-0000-4000-8000-000000000004"
DRIFTED_REF = "e5000000-0000-4000-8000-000000000005"

SUPPORT_LIVE = "51000000-0000-4000-8000-000000000001"
SUPPORT_PURGED = "52000000-0000-4000-8000-000000000002"
SUPPORT_REVOKED = "53000000-0000-4000-8000-000000000003"
SUPPORT_EXPIRED = "54000000-0000-4000-8000-000000000004"
SUPPORT_DRIFTED = "55000000-0000-4000-8000-000000000005"

SOURCE_VERSION_LIVE = "61000000-0000-4000-8000-000000000001"
SOURCE_VERSION_PURGED = "62000000-0000-4000-8000-000000000002"
SOURCE_VERSION_REVOKED = "63000000-0000-4000-8000-000000000003"
SOURCE_VERSION_EXPIRED = "64000000-0000-4000-8000-000000000004"
SOURCE_VERSION_DRIFTED = "65000000-0000-4000-8000-000000000005"

#: The three grant-time identity tokens. The grant's ``context`` column carries
#: exactly these three in IK's own producer
#: (``structured-query-reads.ts`` ``grantContext``), so the seed writes the
#: production shape rather than a shape invented here.
SCOPE_BINDING_TOKEN = "scope-fin-tenant-p2i2"
QUERY_DIGEST_TOKEN = "sha256:p2i2-query-digest"
READ_REVISION_TOKEN = "rev-p2i2-118"

#: The retained payload the grant holds. Its content is irrelevant to the
#: boundary and deliberately distinctive, so a case can assert it never appears
#: in a refusal.
RETAINED_VALUE = {"retained": "the-grants-own-value-column", "marker": "p2i2-retained"}

#: A whole-second queried_at, so the frozen snapshot's 365-day deadline lands on
#: a whole second too. fin's snapshot contract requires that; the live lane
#: would otherwise be refused at admission for a clock artefact.
QUERIED_AT = "2026-10-03T00:00:00Z"
QUERIED_AT_INSTANT = datetime(2026, 10, 3, tzinfo=timezone.utc)
EXPIRES_AT = "2027-10-03T00:00:00Z"
EXPIRES_AT_INSTANT = QUERIED_AT_INSTANT + timedelta(days=365)

#: The expired grant's own queried_at, far enough in the past that its 365-day
#: deadline is behind every clock that can be reading this lane. It has to be the
#: GRANT that is expired, not just fin's copy: the case is that iknow's own read
#: decision refuses, so the deadline on both sides must agree and must be past.
EXPIRED_QUERIED_AT = "2024-01-01T00:00:00Z"
EXPIRED_QUERIED_AT_INSTANT = datetime(2024, 1, 1, tzinfo=timezone.utc)
EXPIRED_EXPIRES_AT_INSTANT = EXPIRED_QUERIED_AT_INSTANT + timedelta(days=365)

#: The instant the seed runs at, kept far from both deadlines so the live and
#: expired grants differ by the deadline alone.
SEED_NOW = datetime(2026, 12, 1, tzinfo=timezone.utc)


def _maintenance() -> dict[str, Any]:
    return {
        "maintenance_revision": "maintenance-rev-p2i2",
        "sources_in_scope": 1,
        "published_sources": 1,
        "desired_index": {
            "pending": 0,
            "running": 0,
            "ready": 1,
            "failed": 0,
            "stale": 0,
        },
        "conflict_checks": {
            "pending": 0,
            "running": 0,
            "ready": 1,
            "failed": 0,
            "stale": 0,
        },
        "ruleset_revision": "structural-alternatives:1",
        "coverage": "structural_candidates_only",
    }


def _snapshot(*, evidence_ref: str, queried_at: str = QUERIED_AT) -> dict[str, Any]:
    """One frozen snapshot in the exact shape EP6 admitted.

    Built through :func:`build_stored_record`'s input contract rather than by
    hand: the lane stores what admission would have stored, so the replay reads
    the same shape production writes.
    """

    expiry = (
        datetime.strptime(queried_at, "%Y-%m-%dT%H:%M:%SZ") + timedelta(days=365)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    row: dict[str, Any] = {
        "schema": "iknow.structured_result",
        "schema_version": 1,
        "subject": "entity",
        "operation": "list",
        "query_digest": QUERY_DIGEST_TOKEN,
        "scope_binding": SCOPE_BINDING_TOKEN,
        "read_revision": READ_REVISION_TOKEN,
        "queried_at": queried_at,
        "maintenance": _maintenance(),
        "rows": [
            {
                "id": "00000000-0000-4000-8000-00000000aa01",
                "canonical_name": "Northwind Logistics",
                "type": "organization",
                "display_support_id": SUPPORT_LIVE,
                "display_disagreement": False,
                "evidence_ref": evidence_ref,
                "evidence_expires_at": expiry,
                "provenance": [
                    {
                        "support_id": SUPPORT_LIVE,
                        "source_ref": SOURCE_LIVE,
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
    return dict(admitted.as_dicts()[0]["snapshot"])


def _record(evidence_ref: str, queried_at: str = QUERIED_AT) -> dict[str, Any]:
    return build_stored_record(_snapshot(evidence_ref=evidence_ref, queried_at=queried_at))


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
    return f"p2i2_replay_{uuid.uuid4().hex[:12]}"


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
    """The storage configuration a child needs to boot. Not an identity."""

    return {
        "GRAPHRAG_MEMORY_STORAGE": "pgvector",
        "GRAPHRAG_MEMORY_DB_URL": _db_url_for_schema(db_url, schema),
        "GRAPHRAG_MEMORY_EMBED_DIMENSIONS": str(EMBED_DIMENSIONS),
        "GRAPHRAG_MEMORY_PGVECTOR_ANN_MAX_DIMENSIONS": "2000",
        "GRAPHRAG_MEMORY_EMBED_API_KEY": "fin-p2i2-test",
        "GRAPHRAG_MEMORY_EMBED_BASE_URL": embed_base_url,
        "GRAPHRAG_MEMORY_EMBED_MODEL": "fin-p2i2-test",
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
    revision: str,
    actor: str,
    reader: str,
) -> EvidenceIdentityBinding:
    return EvidenceIdentityBinding(
        actor_id=actor,
        owner_scope_binding=scope,
        reader_binding=reader,
        source_refs=source_refs,
        authorization_revision=revision,
        deadline_at=deadline_within(900.0),
    )


# ----- the seed: an owner-validated retained snapshot ------------------------


def _lane_rows() -> tuple[list[str], list[str], list[str], list[str], list[str]]:
    """Every identifier this lane owns, grouped by the table that holds it."""

    refs = [LIVE_REF, PURGED_REF, REVOKED_REF, EXPIRED_REF, DRIFTED_REF]
    supports = [
        SUPPORT_LIVE,
        SUPPORT_PURGED,
        SUPPORT_REVOKED,
        SUPPORT_EXPIRED,
        SUPPORT_DRIFTED,
    ]
    versions = [
        SOURCE_VERSION_LIVE,
        SOURCE_VERSION_PURGED,
        SOURCE_VERSION_REVOKED,
        SOURCE_VERSION_EXPIRED,
        SOURCE_VERSION_DRIFTED,
    ]
    sources = [SOURCE_LIVE, SOURCE_PURGED, SOURCE_REVOKED, SOURCE_EXPIRED, SOURCE_DRIFTED]
    scopes = [SCOPE_LIVE, SCOPE_PURGED, SCOPE_REVOKED, SCOPE_EXPIRED]
    return refs, supports, versions, sources, scopes


def _cleanup(db_url: str) -> None:
    """Remove every row this lane wrote, in dependency order.

    WHY this exists. The evidence tables are SHARED: iknow's bootstrap runs
    ``CREATE TABLE IF NOT EXISTS`` under ``search_path = <run schema>, public``,
    and ``public`` must stay on that path because the pgvector ``vector`` type
    lives there — dropping it fails the child at boot with
    ``ensureTable failed (type "vector" does not exist)``. So the tables are
    created once in ``public`` and every lane's rows land beside every other
    lane's. The per-run schema isolates the turn table, not these.

    T13 predates this and leaves its rows behind. This lane removes its own, both
    before seeding (so a run that was interrupted does not collide with the next
    one on the frozen references) and at teardown (so the shared database is left
    as found).
    """

    refs, supports, versions, sources, scopes = _lane_rows()
    with _connect(db_url) as connection:
        with connection.cursor() as cur:
            cur.execute(
                "DELETE FROM evidence_grant_extractions WHERE evidence_ref = ANY(%s)",
                (refs,),
            )
            cur.execute(
                "DELETE FROM evidence_grant_supports WHERE evidence_ref = ANY(%s)",
                (refs,),
            )
            cur.execute("DELETE FROM evidence_grants WHERE evidence_ref = ANY(%s)", (refs,))
            cur.execute(
                "DELETE FROM support_extractions WHERE support_id = ANY(%s)", (supports,)
            )
            cur.execute("DELETE FROM assertion_supports WHERE support_id = ANY(%s)", (supports,))
            cur.execute(
                "DELETE FROM source_versions WHERE source_version_id = ANY(%s)", (versions,)
            )
            cur.execute(
                "DELETE FROM structured_publication_sources WHERE source_ref = ANY(%s)",
                (sources,),
            )
            cur.execute(
                "DELETE FROM evidence_scope_revisions WHERE scope_binding = ANY(%s)",
                (scopes,),
            )
            cur.execute(
                "DELETE FROM structured_publication_scopes WHERE scope_binding = ANY(%s)",
                (scopes,),
            )
        connection.commit()


def _seed(scoped_url: str) -> None:
    """Write the scopes, sources, revisions and grants replay reads.

    The grants are written by hand from the frozen literals above so the reader's
    expectation and the stored row share one source. Every grant's ``context``
    column carries exactly the three tokens IK's producer puts there, so the
    replay's identity comparison runs against the production context shape.
    """

    live_expires = EXPIRES_AT_INSTANT

    def grant_context() -> str:
        return json.dumps(
            {
                "query_digest": QUERY_DIGEST_TOKEN,
                "scope_binding": SCOPE_BINDING_TOKEN,
                "read_revision": READ_REVISION_TOKEN,
            }
        )

    def drifted_context() -> str:
        return json.dumps(
            {
                "query_digest": "sha256:p2i2-a-different-query",
                "scope_binding": SCOPE_BINDING_TOKEN,
                "read_revision": READ_REVISION_TOKEN,
            }
        )

    with _connect(scoped_url) as connection:
        with connection.cursor() as cur:
            for scope in (SCOPE_LIVE, SCOPE_PURGED, SCOPE_REVOKED, SCOPE_EXPIRED):
                cur.execute(
                    f"INSERT INTO structured_publication_scopes "
                    "(scope_binding, read_revision, maintenance_revision) "
                    "VALUES (%s, 0, 0)",
                    (scope,),
                )
            for scope, ref in (
                (SCOPE_LIVE, SOURCE_LIVE),
                (SCOPE_LIVE, SOURCE_DRIFTED),
                (SCOPE_PURGED, SOURCE_PURGED),
                (SCOPE_REVOKED, SOURCE_REVOKED),
                (SCOPE_EXPIRED, SOURCE_EXPIRED),
            ):
                cur.execute(
                    f"INSERT INTO structured_publication_sources "
                    "(scope_binding, source_ref, active) VALUES (%s, %s, true)",
                    (scope, ref),
                )

            cur.execute(
                f"INSERT INTO evidence_scope_revisions "
                "(scope_binding, authorization_revision, purge_revision) "
                "VALUES (%s, %s, %s), (%s, %s, %s), (%s, %s, %s), (%s, %s, %s)",
                (
                    SCOPE_LIVE,
                    AUTH_REV,
                    PURGE_REV,
                    SCOPE_PURGED,
                    AUTH_REV,
                    PURGE_REV,
                    SCOPE_REVOKED,
                    AUTH_REV,
                    PURGE_REV,
                    SCOPE_EXPIRED,
                    AUTH_REV,
                    PURGE_REV,
                ),
            )

            for version_id, ref in (
                (SOURCE_VERSION_LIVE, SOURCE_LIVE),
                (SOURCE_VERSION_PURGED, SOURCE_PURGED),
                (SOURCE_VERSION_REVOKED, SOURCE_REVOKED),
                (SOURCE_VERSION_EXPIRED, SOURCE_EXPIRED),
                (SOURCE_VERSION_DRIFTED, SOURCE_DRIFTED),
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
                        "p2i2-parser-1",
                        json.dumps({"units": [1]}),
                        SEED_NOW,
                    ),
                )

            for support_id, version_id, source_ref in (
                (SUPPORT_LIVE, SOURCE_VERSION_LIVE, SOURCE_LIVE),
                (SUPPORT_PURGED, SOURCE_VERSION_PURGED, SOURCE_PURGED),
                (SUPPORT_REVOKED, SOURCE_VERSION_REVOKED, SOURCE_REVOKED),
                (SUPPORT_EXPIRED, SOURCE_VERSION_EXPIRED, SOURCE_EXPIRED),
                (SUPPORT_DRIFTED, SOURCE_VERSION_DRIFTED, SOURCE_DRIFTED),
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
                        f"fin-p2i2-{support_id[:4]}",
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
                    (support_id, f"p2i2-extraction-{support_id}", SEED_NOW),
                )

            def grant(
                ref: str,
                *,
                expires_at: datetime,
                scope: str,
                context: str,
                support_id: str,
                auth_rev: str,
                purge_rev: str,
                queried_at: datetime = QUERIED_AT_INSTANT,
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
                        queried_at,
                        expires_at,
                        context,
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
                    (ref, support_id, f"p2i2-extraction-{support_id}"),
                )

            grant(
                LIVE_REF,
                expires_at=live_expires,
                scope=SCOPE_LIVE,
                context=grant_context(),
                support_id=SUPPORT_LIVE,
                auth_rev=AUTH_REV,
                purge_rev=PURGE_REV,
            )
            # A grant whose stored context names a DIFFERENT query than the
            # snapshot froze. The read itself succeeds — the fence has not moved
            # and the deadline has not passed — so this is the case that proves
            # the identity comparison is doing work the read does not.
            grant(
                DRIFTED_REF,
                expires_at=live_expires,
                scope=SCOPE_LIVE,
                context=drifted_context(),
                support_id=SUPPORT_DRIFTED,
                auth_rev=AUTH_REV,
                purge_rev=PURGE_REV,
            )
            grant(
                EXPIRED_REF,
                expires_at=EXPIRED_EXPIRES_AT_INSTANT,
                queried_at=EXPIRED_QUERIED_AT_INSTANT,
                scope=SCOPE_EXPIRED,
                context=grant_context(),
                support_id=SUPPORT_EXPIRED,
                auth_rev=AUTH_REV,
                purge_rev=PURGE_REV,
            )
            grant(
                PURGED_REF,
                expires_at=live_expires,
                scope=SCOPE_PURGED,
                context=grant_context(),
                support_id=SUPPORT_PURGED,
                auth_rev=AUTH_REV,
                purge_rev=PURGE_REV,
            )
            # The grant pins AUTH_REV while its scope has moved to
            # AUTH_REV_MOVED below: revocation is a DISAGREEMENT between the two,
            # so pinning the moved revision here would leave the fence intact.
            grant(
                REVOKED_REF,
                expires_at=live_expires,
                scope=SCOPE_REVOKED,
                context=grant_context(),
                support_id=SUPPORT_REVOKED,
                auth_rev=AUTH_REV,
                purge_rev=PURGE_REV,
            )

            # The two lifecycle fences, each scoped to the case it belongs to.
            cur.execute(
                f"UPDATE evidence_scope_revisions SET purge_revision = %s "
                "WHERE scope_binding = %s",
                (PURGE_REV_MOVED, SCOPE_PURGED),
            )
            cur.execute(
                f"UPDATE evidence_scope_revisions SET authorization_revision = %s "
                "WHERE scope_binding = %s",
                (AUTH_REV_MOVED, SCOPE_REVOKED),
            )
        connection.commit()


#: Threads this lane created, removed at teardown. ``fin_run`` is a real table
#: in the shared database rather than a per-run schema, so a lane that writes it
#: has to take its own rows back out.
_CREATED_THREADS: list[str] = []


def _drop_threads(db_url: str) -> None:
    """Remove every thread this run created."""

    if not _CREATED_THREADS:
        return
    store = PostgresThreadStore(db_url)
    for thread_id in _CREATED_THREADS:
        store.delete_thread(thread_id)
    _CREATED_THREADS.clear()


def _store_and_thread(db_url: str, records: list[dict[str, Any]]) -> tuple[str, str]:
    """Persist records through a real store and return the thread and run ids."""

    store = PostgresThreadStore(db_url)
    thread_id = f"p2i2-{uuid.uuid4()}"
    run_id = f"run-{uuid.uuid4()}"
    _CREATED_THREADS.append(thread_id)
    store.create_thread(id=thread_id, title="p2i2 replay thread")
    store.append_run(
        RunRecord(
            id=run_id,
            thread_id=thread_id,
            question="Which organizations are in scope?",
            status="done",
            status_reason="",
            answer="Northwind Logistics.",
            structured_evidence=records,
        )
    )
    return thread_id, run_id


def _stored_records(db_url: str, thread_id: str) -> list[dict[str, Any]]:
    """Read the history back over a SEPARATE store instance.

    Re-opening the store is what stands in for a restart: the records come back
    through the JSONB column, not through the object that was written.
    """

    return PostgresThreadStore(db_url).list_runs(thread_id)[0].structured_evidence


@pytest.fixture(scope="module")
def replay_env() -> Iterator[dict[str, Any]]:
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
        target=embedding_server.serve_forever,
        name="fin-p2i2-embedding",
        daemon=True,
    ).start()
    embed_base_url = f"http://127.0.0.1:{embedding_server.server_port}/v1"

    scoped_url = _db_url_for_schema(db_url, schema)
    command = [shutil.which("node") or "node", str(root / "dist" / "index.js")]
    transport = _transport_env(db_url, schema, embed_base_url)

    # The registry the prerequisite landed, used the way production would use
    # it: one identity per frozen binding, resolved per call rather than frozen
    # at build time.
    registry = EvidenceSessionRegistry(
        command, transport_env=transport, timeout_seconds=90.0
    )

    bindings = {
        "live": _binding(
            SCOPE_LIVE,
            (SOURCE_LIVE,),
            revision=AUTH_REV,
            actor="fin-p2i2-reader-live",
            reader="fin-p2i2-reader-live",
        ),
        "drifted": _binding(
            SCOPE_LIVE,
            (SOURCE_LIVE, SOURCE_DRIFTED),
            revision=AUTH_REV,
            actor="fin-p2i2-reader-drifted",
            reader="fin-p2i2-reader-drifted",
        ),
        "expired": _binding(
            SCOPE_EXPIRED,
            (SOURCE_EXPIRED,),
            revision=AUTH_REV,
            actor="fin-p2i2-reader-expired",
            reader="fin-p2i2-reader-expired",
        ),
        "purged": _binding(
            SCOPE_PURGED,
            (SOURCE_PURGED,),
            revision=AUTH_REV,
            actor="fin-p2i2-reader-purged",
            reader="fin-p2i2-reader-purged",
        ),
        "revoked": _binding(
            SCOPE_REVOKED,
            (SOURCE_REVOKED,),
            revision=AUTH_REV_MOVED,
            actor="fin-p2i2-reader-revoked",
            reader="fin-p2i2-reader-revoked",
        ),
    }

    # Make the first child answer a call BEFORE the seed runs. Binding only
    # launches the process; the child's own bootstrap creates the evidence
    # tables, so seeding while it is still starting would race its DDL against
    # this lane's DML on the same tables and leave the child unable to serve.
    try:
        first = registry.bind(bindings["live"])
        first.session.call({"jsonrpc": "2.0", "id": 9001, "method": "tools/list"})
        # Before seeding, so an interrupted earlier run cannot collide with this
        # one on the frozen references. See _cleanup for why this is needed.
        _cleanup(db_url)
        _seed(scoped_url)
        yield {
            "registry": registry,
            "bindings": bindings,
            "scoped_url": scoped_url,
            "schema": schema,
            "db_url": db_url,
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
        # The evidence tables and ``fin_run`` are shared, so the rows this run
        # wrote outlive the run schema unless they are removed explicitly.
        _drop_threads(db_url)
        _cleanup(db_url)


def _replay(env: dict[str, Any], record: Any, identity: str) -> Any:
    client = env["registry"].bind(env["bindings"][identity]).client
    return replay_stored_evidence(record, reader=client, now=env["now"])


# ----- the chain, against real storage --------------------------------------


def test_a_stored_record_replays_against_a_real_iknow_child(replay_env) -> None:
    """The whole chain, live: JSONB round trip, real read, real comparison.

    This is the ``p2:162`` proof. Every step crosses a boundary a fixture does
    not have: the record was written to and read back from ``fin_run``, the read
    was served by iknow's own grant decision against the ``evidence_grants`` row,
    and the deadline was compared across the two real spellings.
    """

    thread_id, _ = _store_and_thread(
        replay_env["db_url"], [_record(LIVE_REF)]
    )
    records = _stored_records(replay_env["db_url"], thread_id)
    assert len(records) == 1

    found = find_replayable_record(records, LIVE_REF)
    assert found is not None, "the stored record was not found by its own reference"

    outcome = replay_stored_evidence(
        found, reader=replay_env["registry"].bind(replay_env["bindings"]["live"]).client, now=SEED_NOW
    )
    assert outcome.replayed is True, outcome.refusal
    assert outcome.evidence_ref == LIVE_REF
    assert outcome.evidence is not None
    # fin serves its own frozen copy, unchanged, across the round trip.
    assert outcome.evidence.snapshot["evidence_ref"] == LIVE_REF
    assert outcome.evidence.snapshot["expires_at"] == EXPIRES_AT
    # The envelope is what iknow actually holds for that reference.
    assert outcome.evidence.envelope["evidence_ref"] == LIVE_REF
    assert outcome.evidence.envelope["value"] == RETAINED_VALUE


def test_the_history_entry_is_found_after_a_restart(replay_env) -> None:
    """A resume finds the record by the reference the caller names."""

    thread_id, _ = _store_and_thread(replay_env["db_url"], [_record(LIVE_REF)])
    records = _stored_records(replay_env["db_url"], thread_id)
    assert find_replayable_record(records, LIVE_REF) is not None
    assert find_replayable_record(records, "e9000000-0000-4000-8000-000000000009") is None


def test_a_tampered_stored_row_is_refused_before_any_read(replay_env) -> None:
    """The digest comparison, against a real JSONB column.

    The mutation is written through SQL so the corruption is in PostgreSQL, not
    in a Python object, and the row is read back before the replay sees it.
    """

    thread_id, _ = _store_and_thread(replay_env["db_url"], [_record(LIVE_REF)])
    with _connect(replay_env["db_url"]) as connection:
        with connection.cursor() as cur:
            cur.execute(
                "UPDATE fin_run SET structured_evidence = jsonb_set("
                "  structured_evidence, '{0,snapshot,value,canonical_name}', "
                "  to_jsonb('Tampered Corp'::text)) "
                "WHERE thread_id = %s",
                (thread_id,),
            )
        connection.commit()

    records = _stored_records(replay_env["db_url"], thread_id)
    assert records[0]["snapshot"]["value"]["canonical_name"] == "Tampered Corp"

    outcome = _replay(replay_env, records[0], "live")
    assert outcome.replayed is False
    assert outcome.refusal is not None
    assert outcome.refusal.code == MALFORMED_EVIDENCE_CONTRACT
    # The refusal still names the reference that was asked about.
    assert outcome.evidence_ref == LIVE_REF


def test_a_grant_whose_identity_drifted_is_refused(replay_env) -> None:
    """The cross-storage comparison, against a real grant row.

    The read SUCCEEDS here — the fence has not moved, the deadline has not
    passed, and iknow hands the envelope back. What differs is the stored
    ``context``, so this is the case that proves the identity comparison does work
    the read does not: without it this replay would serve a citation from under a
    grant that no longer denotes what was recorded.
    """

    thread_id, _ = _store_and_thread(replay_env["db_url"], [_record(DRIFTED_REF)])
    records = _stored_records(replay_env["db_url"], thread_id)

    client = replay_env["registry"].bind(replay_env["bindings"]["drifted"]).client
    envelope = client.read(DRIFTED_REF)
    assert envelope["evidence_ref"] == DRIFTED_REF
    # The read itself is granted: the fence has not moved and the deadline holds.
    assert envelope["context"]["query_digest"] != QUERY_DIGEST_TOKEN

    outcome = replay_stored_evidence(records[0], reader=client, now=SEED_NOW)
    assert outcome.replayed is False
    assert outcome.refusal is not None
    assert outcome.refusal.code == EVIDENCE_UNAVAILABLE
    assert "query_digest" in outcome.refusal.reason
    # The refusal discloses no payload.
    assert RETAINED_VALUE["marker"] not in str(outcome.refusal)


def test_an_expired_grant_refuses_on_expiry_and_discloses_nothing(replay_env) -> None:
    outcome = _replay(replay_env, _record(EXPIRED_REF, EXPIRED_QUERIED_AT), "expired")
    assert outcome.replayed is False
    assert outcome.evidence is None
    assert outcome.refusal is not None
    assert outcome.refusal.code == EVIDENCE_EXPIRED
    assert outcome.refusal.reason == "expired"
    assert RETAINED_VALUE["marker"] not in str(outcome.refusal)


def test_a_purged_grant_refuses_on_purge_and_discloses_nothing(replay_env) -> None:
    outcome = _replay(replay_env, _record(PURGED_REF), "purged")
    assert outcome.replayed is False
    assert outcome.evidence is None
    assert outcome.refusal is not None
    # The store's purge refusal, carried through with its reason intact.
    assert outcome.refusal.code == EVIDENCE_UNAVAILABLE
    assert outcome.refusal.reason == "purged"
    assert RETAINED_VALUE["marker"] not in str(outcome.refusal)


def test_a_revoked_authorization_refuses_with_access_denied(replay_env) -> None:
    outcome = _replay(replay_env, _record(REVOKED_REF), "revoked")
    assert outcome.replayed is False
    assert outcome.evidence is None
    assert outcome.refusal is not None
    assert outcome.refusal.code == ACCESS_DENIED
    assert outcome.refusal.reason == "authorization_revoked"


def test_expiry_and_purge_stay_distinguishable_across_real_storage(replay_env) -> None:
    """Two refusals, one code family, two facts — decided by storage, not by us."""

    expired = _replay(replay_env, _record(EXPIRED_REF), "expired")
    purged = _replay(replay_env, _record(PURGED_REF), "purged")
    assert expired.refusal is not None and purged.refusal is not None
    assert expired.refusal.code != purged.refusal.code
    assert expired.refusal.reason != purged.refusal.reason


def test_a_wrong_identity_is_refused_rather_than_served(replay_env) -> None:
    """An identity that does not hold the grant's scope cannot read it.

    This is the refusal a replay earns when the identity is wrong, and it is the
    reason the chain takes a bound port rather than minting one: the host has no
    trusted source for the authority facts, so the identity stays the caller's
    duty and a wrong one is refused rather than defaulted.
    """

    outcome = _replay(replay_env, _record(LIVE_REF), "purged")
    assert outcome.replayed is False
    assert outcome.refusal is not None
    assert outcome.refusal.code == ACCESS_DENIED
    assert outcome.refusal.reason in {"scope_not_authorized", "authorization_revoked"}


def test_an_unknown_reference_refuses_rather_than_returning_empty(replay_env) -> None:
    """A record naming a reference the backend does not hold is refused."""

    thread_id, _ = _store_and_thread(
        replay_env["db_url"],
        [_record("e9000000-0000-4000-8000-000000000009")],
    )
    records = _stored_records(replay_env["db_url"], thread_id)
    outcome = _replay(replay_env, records[0], "live")
    assert outcome.replayed is False
    assert outcome.refusal is not None
    assert outcome.refusal.code == EVIDENCE_UNAVAILABLE
    assert outcome.refusal.reason == "unknown_reference"


def test_a_scrubbed_history_entry_can_no_longer_be_replayed(replay_env) -> None:
    """EP9's scrub removes the payload, and a scrubbed entry replays to nothing.

    This is the composition the invalidation lane was waiting for: after a
    scrub, the history keeps its identifiers and the citation is simply
    unavailable — never a successful empty value.
    """

    store = PostgresThreadStore(replay_env["db_url"])
    thread_id, _ = _store_and_thread(replay_env["db_url"], [_record(LIVE_REF)])
    assert len(_stored_records(replay_env["db_url"], thread_id)) == 1

    outcome = store.scrub_structured_evidence_payloads(
        consumer_id="p2i2-live",
        evidence_refs=[LIVE_REF],
        next_checkpoint="cp-1",
        last_seq=1,
    )
    assert outcome.remaining_entries == 0

    records = _stored_records(replay_env["db_url"], thread_id)
    assert records == []
    assert find_replayable_record(records, LIVE_REF) is None


def test_replay_issues_no_query_and_publishes_no_tool(replay_env) -> None:
    """The replay rides the private method, and the model plane never sees it.

    The read is dispatched on the bound client's own session, so the check that
    matters is that the boundary is unchanged by replaying: the same eight
    model-visible tools, and no tool whose input names an ``evidence_ref``.
    """

    bound = replay_env["registry"].bind(replay_env["bindings"]["live"])
    response = bound.session.call(
        {"jsonrpc": "2.0", "id": 9901, "method": "tools/list"}
    )
    names = [tool["name"] for tool in response["result"]["tools"]]
    assert len(names) == 8
    assert not any("evidence" in name for name in names)
    for tool in response["result"]["tools"]:
        properties = (tool.get("inputSchema") or {}).get("properties") or {}
        assert "evidence_ref" not in properties
        assert "evidence" not in properties

    # And the replay itself still works on that same session afterwards.
    outcome = replay_stored_evidence(
        _record(LIVE_REF), reader=bound.client, now=SEED_NOW
    )
    assert outcome.replayed is True, outcome.refusal


# ----- destructive: last, because it removes a row other cases read ----
#
# The dependency case deletes the support the live grant pinned, so it must
# run AFTER every case that replays that grant. pytest runs a module in file
# order, which is what makes this placement the guarantee rather than a hope.


def test_the_dependency_unresolved_refusal_reaches_the_caller(replay_env) -> None:
    """A grant whose obligation set is short is refused whole, not partly.

    Driven by deleting the support row the grant pinned, under the replication
    role so the RESTRICT foreign key allows it — the same technique T13 uses.
    """

    with _connect(replay_env["scoped_url"]) as connection:
        with connection.cursor() as cur:
            cur.execute("SET LOCAL session_replication_role = replica")
            cur.execute(
                f"DELETE FROM assertion_supports "
                "WHERE support_id = %s",
                (SUPPORT_LIVE,),
            )
            cur.execute("SET LOCAL session_replication_role = origin")
        connection.commit()

    outcome = _replay(replay_env, _record(LIVE_REF), "live")
    assert outcome.replayed is False
    assert outcome.refusal is not None
    assert outcome.refusal.code == UNSUPPORTED_SOURCE_BINDING
    assert outcome.refusal.reason == "dependency_unresolved"
    assert RETAINED_VALUE["marker"] not in str(outcome.refusal)


def test_the_child_environment_carries_only_the_allowlisted_identity(
    replay_env,
) -> None:
    """The binding stays an explicit allowlist; nothing is inherited by default."""

    env = replay_env["bindings"]["live"].child_env()
    assert set(env) == {
        "IKNOW_EVIDENCE_BINDING",
        "IKNOW_EVIDENCE_ACTOR_ID",
        "IKNOW_EVIDENCE_OWNER_SCOPE",
        "IKNOW_EVIDENCE_READER",
        "IKNOW_EVIDENCE_SOURCE_REFS",
        "IKNOW_EVIDENCE_AUTH_REVISION",
        "IKNOW_EVIDENCE_DEADLINE_AT",
    }
    assert env["IKNOW_EVIDENCE_OWNER_SCOPE"] == SCOPE_LIVE