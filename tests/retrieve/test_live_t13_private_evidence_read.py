"""T13 -- EP3: read an exact retained row grant and count grant through Fin's
private session.

This is the live acceptance for the B1 trusted-identity binding decision and the
EP3 private Evidence read.  It drives a **real** built iknow child process over
stdio through the **real** Fin retrieve adapter and the **real** private
dispatch client, against **real** PostgreSQL 16.15 + pgvector 0.8.6 in a
per-run schema.

What the run is allowed to assume is deliberately small: a built iknow at
``FIN_LIVE_IKNOW_ROOT``, a reachable database, and the strict markers.  Anything
missing is a named non-zero failure, never a skip.

The expectations below are **frozen before the run** and come from the Evidence
spec and the ticket text, not from anything the backend answered.  The retained
values in particular are literals chosen here; the driver's job is to prove the
read returns them *unchanged*, and the seed is written from those same literals
so a drift between seed and reader cannot pass.

T13 boundary, stated once so it is not lost: the seed below is an owner-validated
retained snapshot, and it is **not** evidence of current-query gold.  Nothing
here observes QP5 or QP7 acceptance, and this module never claims it.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import threading
import uuid
from http.server import ThreadingHTTPServer
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

import psycopg
import pytest

from fin.host.evidence.evidence_session_registry import (
    EvidenceBindingRefusal,
    EvidenceSessionRegistry,
)
from fin.retrieve.evidence_route import (
    EVIDENCE_PRIVATE_METHOD,
    TRUSTED_BINDING_ENV_VARS,
    EvidenceGrantRefusal,
    EvidenceIdentityBinding,
    EvidenceIdentityError,
    EvidenceRouteClient,
    deadline_within,
    identity_env,
)
from fin.retrieve.mcp_adapter import MCPRetrieveAdapter
from fin.retrieve.mcp_session import (
    INHERITED_ENV_ALLOWLIST,
    MCPStdioSession,
    build_initialize_frame,
)


pytestmark = [pytest.mark.mcp, pytest.mark.sql_live]

sys.path.insert(0, str(Path(__file__).parent))

from live_iknow_support import _EmbeddingHandler  # noqa: E402

_PG_SCHEMA_RE = re.compile(r"^t13_ep3_[0-9a-f]{12}$")

#: The eight tools T39 froze. A ninth entry is a model-visible surface change.
MODEL_VISIBLE_TOOL_COUNT = 8

#: The embedding width the live schema is created at. A read never embeds, but
#: the child validates this at boot, so it has to be present and consistent.
EMBED_DIMENSIONS = 8


# ----- frozen expectations, chosen before the run from the spec and ticket -----

SCOPE_A = "t13-owner-scope-a"
SCOPE_B = "t13-owner-scope-b"
#: A scope of its own for the purge fence and one for the revoke fence. The
#: replay compares a grant's stored revision against its scope's live one, so
#: advancing a revision is a scope-wide fact. Isolating them into their own
#: scopes is what keeps one case from moving the fence under another.
SCOPE_PURGED = "t13-owner-scope-purged"
SCOPE_REVOKED = "t13-owner-scope-revoked"
SOURCE_A1 = "t13-source-a1"
SOURCE_A2 = "t13-source-a2"
SOURCE_B1 = "t13-source-b1"
SOURCE_P1 = "t13-source-p1"
SOURCE_R1 = "t13-source-r1"

AUTH_REV = "t13-auth-rev-1"
AUTH_REV_MOVED = "t13-auth-rev-2"
PURGE_REV = "t13-purge-rev-0"
PURGE_REV_MOVED = "t13-purge-rev-1"

ROW_REF = "11111111-1111-4111-8111-111111111111"
COUNT_REF = "22222222-2222-4222-8222-222222222222"
EXPIRED_REF = "33333333-3333-4333-8333-333333333333"
PURGED_REF = "44444444-4444-4444-8444-444444444444"
STOPPED_REF = "55555555-5555-4555-8555-555555555555"
UNRESOLVED_REF = "66666666-6666-4666-8666-666666666666"
REVOKED_REF = "77777777-7777-4777-8777-777777777777"

SUPPORT_A1 = "a1000000-0000-4000-8000-000000000001"
SUPPORT_A2 = "a2000000-0000-4000-8000-000000000002"
SUPPORT_STOPPED = "a3000000-0000-4000-8000-000000000003"
SUPPORT_UNRESOLVED = "a4000000-0000-4000-8000-000000000004"
SUPPORT_P1 = "a5000000-0000-4000-8000-000000000005"
SUPPORT_R1 = "a6000000-0000-4000-8000-000000000006"
SOURCE_VERSION_A1 = "b1000000-0000-4000-8000-000000000001"
SOURCE_VERSION_A2 = "b2000000-0000-4000-8000-000000000002"
SOURCE_VERSION_STOPPED = "b3000000-0000-4000-8000-000000000003"
SOURCE_VERSION_B1 = "b4000000-0000-4000-8000-000000000004"
SOURCE_VERSION_P1 = "b5000000-0000-4000-8000-000000000005"
SOURCE_VERSION_R1 = "b6000000-0000-4000-8000-000000000006"
RECEIPT_ID = "c1000000-0000-4000-8000-000000000001"

#: The exact stored row a row grant must hand back. Frozen here, not observed.
ROW_VALUE: dict[str, Any] = {
    "entity": {
        "ref": "fin-t13-entity-alpha",
        "name": "Alpha Corp",
        "kind": "ORG",
        "attributes": {"founded": "1998", "region": "emea"},
    },
    "assertions": [
        {"predicate": "has_office", "object_ref": "fin-t13-office-berlin"}
    ],
}
ROW_CONTEXT: dict[str, Any] = {
    "schema": "iknow.evidence_context",
    "schema_version": 1,
    "operation": "entity.get",
}

#: The exact stored receipt a count grant must hand back.
COUNT_NUMBER = 7
COUNT_VALUE: dict[str, Any] = {"counted": "fin-t13-entity-alpha", "receipt_only": True}
COUNT_CONTEXT: dict[str, Any] = {
    "schema": "iknow.evidence_context",
    "schema_version": 1,
    "operation": "entity.count",
}

QUERY_DIGEST = "t13-query-digest-frozen"


# ----- strict prerequisites: a named failure, never a skip --------------------


def _pg_url() -> str:
    url = os.environ.get("GRAPHRAG_MEMORY_DB_URL")
    if not url:
        pytest.fail(
            "GRAPHRAG_MEMORY_DB_URL is absent. This lane requires the isolated "
            "dev PostgreSQL; load /tmp/fin-structured-pg.env in the same shell "
            "invocation. An absent DSN is a failure, never a skip."
        )
        raise AssertionError("unreachable: pytest.fail does not return")
    return url


def _iknow_root() -> Path:
    raw = os.environ.get("FIN_LIVE_IKNOW_ROOT")
    if not raw:
        pytest.fail(
            "FIN_LIVE_IKNOW_ROOT is absent. This lane needs a real built iknow "
            "child, not the vitest lanes. An absent root is a failure, never a "
            "skip."
        )
    root = Path(str(raw))
    build = root / "dist" / "index.js"
    if not build.is_file():
        pytest.fail(
            f"the iknow build is missing at {build}. Run `npm run build` in "
            "{root} first. A missing build is a failure, never a skip."
        )
    return root


def _schema_name() -> str:
    return f"t13_ep3_{uuid.uuid4().hex[:12]}"


def _db_url_for_schema(db_url: str, schema: str) -> str:
    if not _PG_SCHEMA_RE.match(schema):
        raise ValueError(f"unsafe pg_schema name: {schema!r}")
    separator = "&" if "?" in db_url else "?"
    return f"{db_url}{separator}options=-c%20search_path%3D{schema}%2Cpublic"


def _check_schema_name(schema: str) -> None:
    if not _PG_SCHEMA_RE.match(schema):
        raise ValueError(f"unsafe pg_schema name: {schema!r}")


def _connect(db_url: str) -> psycopg.Connection[Any]:
    try:
        return psycopg.connect(db_url, connect_timeout=10)
    except psycopg.Error as exc:
        pytest.fail(
            f"PostgreSQL is unreachable from the configured DSN: {type(exc).__name__}. "
            "An unreachable database is a failure, never a skip."
        )


# ----- the seed: an owner-validated retained snapshot -------------------------
#
# The seed is written from the frozen literals above so that the read's
# expectation and the stored row share one source. It is modelled on EP2's
# retained shape -- an owner-registered scope and its active sources, an
# authorization revision pair, immutable source versions, one occurrence per
# support, and the grants themselves.


def _seed(db_url: str, *, now: datetime) -> None:
    live = now + timedelta(hours=6)
    expired = now - timedelta(hours=1)
    purged = now + timedelta(hours=6)

    with _connect(db_url) as connection:
        with connection.cursor() as cur:
            for scope in (SCOPE_A, SCOPE_B, SCOPE_PURGED, SCOPE_REVOKED):
                cur.execute(
                    "INSERT INTO structured_publication_scopes "
                    "(scope_binding, read_revision, maintenance_revision) "
                    "VALUES (%s, 0, 0)",
                    (scope,),
                )
            for scope, ref in (
                (SCOPE_A, SOURCE_A1),
                (SCOPE_A, SOURCE_A2),
                (SCOPE_B, SOURCE_B1),
                (SCOPE_PURGED, SOURCE_P1),
                (SCOPE_REVOKED, SOURCE_R1),
            ):
                cur.execute(
                    "INSERT INTO structured_publication_sources "
                    "(scope_binding, source_ref, active) VALUES (%s, %s, true)",
                    (scope, ref),
                )

            cur.execute(
                "INSERT INTO evidence_scope_revisions "
                "(scope_binding, authorization_revision, purge_revision) "
                "VALUES (%s, %s, %s), (%s, %s, %s), (%s, %s, %s), (%s, %s, %s)",
                (
                    SCOPE_A,
                    AUTH_REV,
                    PURGE_REV,
                    SCOPE_B,
                    AUTH_REV,
                    PURGE_REV,
                    SCOPE_PURGED,
                    AUTH_REV,
                    PURGE_REV,
                    SCOPE_REVOKED,
                    AUTH_REV_MOVED,
                    PURGE_REV,
                ),
            )
            cur.execute(
                "INSERT INTO evidence_authorization_receipts "
                "(receipt_id, scope_binding, authorization_revision, "
                " purge_revision, created_at) VALUES (%s, %s, %s, %s, %s)",
                (RECEIPT_ID, SCOPE_A, AUTH_REV, PURGE_REV, now),
            )

            for version_id, ref, version in (
                (SOURCE_VERSION_A1, SOURCE_A1, "v1"),
                (SOURCE_VERSION_A2, SOURCE_A2, "v1"),
                (SOURCE_VERSION_STOPPED, SOURCE_A2, "v2"),
                (SOURCE_VERSION_B1, SOURCE_B1, "v1"),
                (SOURCE_VERSION_P1, SOURCE_P1, "v1"),
                (SOURCE_VERSION_R1, SOURCE_R1, "v1"),
            ):
                cur.execute(
                    "INSERT INTO source_versions (source_version_id, source_ref, "
                    "source_version, content_digest, origin_object_ref, "
                    "parser_revision, unit_manifest, created_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s)",
                    (
                        version_id,
                        ref,
                        version,
                        f"digest-{version_id}",
                        f"object-{version_id}",
                        "t13-parser-1",
                        json.dumps({"units": [1]}),
                        now,
                    ),
                )

            for support_id, ref, version_id, result_id in (
                (SUPPORT_A1, SOURCE_A1, SOURCE_VERSION_A1, "fin-t13-entity-alpha"),
                (SUPPORT_A2, SOURCE_A2, SOURCE_VERSION_A2, "fin-t13-entity-alpha"),
                (
                    SUPPORT_STOPPED,
                    SOURCE_A2,
                    SOURCE_VERSION_STOPPED,
                    "fin-t13-entity-alpha",
                ),
                (
                    SUPPORT_UNRESOLVED,
                    SOURCE_A2,
                    SOURCE_VERSION_A2,
                    "fin-t13-entity-alpha",
                ),
                (SUPPORT_P1, SOURCE_P1, SOURCE_VERSION_P1, "fin-t13-entity-p"),
                (SUPPORT_R1, SOURCE_R1, SOURCE_VERSION_R1, "fin-t13-entity-r"),
            ):
                cur.execute(
                    "INSERT INTO assertion_supports (support_id, subject, "
                    "result_id, source_ref, source_version, source_version_id, "
                    "document_id, unit_version, unit, span_start, span_end, "
                    "created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, "
                    "%s, %s, %s)",
                    (
                        support_id,
                        "entity",
                        result_id,
                        ref,
                        "v1" if version_id != SOURCE_VERSION_STOPPED else "v2",
                        version_id,
                        f"doc-{support_id}",
                        "1",
                        1,
                        0,
                        12,
                        now,
                    ),
                )
                cur.execute(
                    "INSERT INTO support_extractions "
                    "(support_id, extraction_ref, created_at) "
                    "VALUES (%s, %s, %s)",
                    (support_id, f"t13-extraction-{support_id}", now),
                )

            # The obligation that stops accepting, and the grant that names it.
            cur.execute(
                "UPDATE source_versions SET obligations_stopped_at = %s "
                "WHERE source_version_id = %s",
                (now - timedelta(minutes=5), SOURCE_VERSION_STOPPED),
            )

            def grant(
                ref: str,
                kind: str,
                subject: str,
                expires_at: datetime,
                context: dict[str, Any],
                value: dict[str, Any],
                count: int | None,
                receipt: str | None,
                auth_rev: str,
                purge_rev: str,
                scope: str = SCOPE_A,
            ) -> None:
                cur.execute(
                    "INSERT INTO evidence_grants (evidence_ref, grant_kind, "
                    "subject, operation, query_digest, scope_binding, "
                    "read_revision, authorization_revision, purge_revision, "
                    "queried_at, expires_at, context, value, result_count, "
                    "authorization_receipt_id) VALUES (%s, %s, %s, %s, %s, %s, "
                    "%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s)",
                    (
                        ref,
                        kind,
                        subject,
                        "count" if kind == "count" else "entity",
                        QUERY_DIGEST,
                        scope,
                        "0",
                        auth_rev,
                        purge_rev,
                        now,
                        expires_at,
                        json.dumps(context),
                        json.dumps(value),
                        count,
                        receipt,
                    ),
                )

            def pin(ref: str, support_ids: list[str]) -> None:
                for support_id in support_ids:
                    cur.execute(
                        "INSERT INTO evidence_grant_supports "
                        "(evidence_ref, support_id, role) VALUES (%s, %s, %s)",
                        (ref, support_id, "occurrence"),
                    )
                    cur.execute(
                        "INSERT INTO evidence_grant_extractions "
                        "(evidence_ref, support_id, extraction_ref) "
                        "VALUES (%s, %s, %s)",
                        (ref, support_id, f"t13-extraction-{support_id}"),
                    )

            grant(ROW_REF, "row", "entity", live, ROW_CONTEXT, ROW_VALUE,
                  None, None, AUTH_REV, PURGE_REV)
            pin(ROW_REF, [SUPPORT_A1, SUPPORT_A2])

            grant(COUNT_REF, "count", "entity", live, COUNT_CONTEXT,
                  COUNT_VALUE, COUNT_NUMBER, RECEIPT_ID, AUTH_REV, PURGE_REV)

            grant(EXPIRED_REF, "row", "entity", expired, ROW_CONTEXT,
                  ROW_VALUE, None, None, AUTH_REV, PURGE_REV)
            pin(EXPIRED_REF, [SUPPORT_A1])

            grant(PURGED_REF, "row", "entity", purged, ROW_CONTEXT, ROW_VALUE,
                  None, None, AUTH_REV, PURGE_REV, SCOPE_PURGED)
            pin(PURGED_REF, [SUPPORT_P1])

            grant(STOPPED_REF, "row", "entity", live, ROW_CONTEXT, ROW_VALUE,
                  None, None, AUTH_REV, PURGE_REV)
            pin(STOPPED_REF, [SUPPORT_STOPPED])

            grant(UNRESOLVED_REF, "row", "entity", live, ROW_CONTEXT,
                  ROW_VALUE, None, None, AUTH_REV, PURGE_REV)
            pin(UNRESOLVED_REF, [SUPPORT_UNRESOLVED])

            grant(REVOKED_REF, "row", "entity", live, ROW_CONTEXT, ROW_VALUE,
                  None, None, AUTH_REV, PURGE_REV, SCOPE_REVOKED)
            pin(REVOKED_REF, [SUPPORT_R1])

            # An obligation that becomes unresolvable. The FK is RESTRICT, so the
            # state is reached with the replication role set for this statement
            # only: the point of the case is that the read withholds the whole
            # grant rather than rendering the rows it can still resolve.
            cur.execute("SET LOCAL session_replication_role = replica")
            cur.execute(
                "DELETE FROM assertion_supports WHERE support_id = %s",
                (SUPPORT_UNRESOLVED,),
            )
            cur.execute("SET LOCAL session_replication_role = origin")

            # The two lifecycle fences, each scoped to the case it belongs to.
            cur.execute(
                "UPDATE evidence_scope_revisions SET purge_revision = %s "
                "WHERE scope_binding = %s",
                (PURGE_REV_MOVED, SCOPE_PURGED),
            )
            cur.execute(
                "UPDATE evidence_scope_revisions SET authorization_revision = %s "
                "WHERE scope_binding = %s",
                (AUTH_REV_MOVED, SCOPE_REVOKED),
            )
        connection.commit()


# ----- the fixtures -----------------------------------------------------------


def _stored_expires_at(scoped_url: str, ref: str) -> datetime:
    with _connect(scoped_url) as connection:
        with connection.cursor() as cur:
            cur.execute(
                "SELECT expires_at FROM evidence_grants WHERE evidence_ref = %s",
                (ref,),
            )
            row = cur.fetchone()
    assert row is not None, f"the seeded grant {ref} is missing"
    return row[0]


def _binding(
    scope: str,
    source_refs: tuple[str, ...],
    *,
    revision: str,
    actor: str,
    reader: str,
    seconds: float = 60.0,
) -> EvidenceIdentityBinding:
    return EvidenceIdentityBinding(
        actor_id=actor,
        owner_scope_binding=scope,
        reader_binding=reader,
        source_refs=source_refs,
        authorization_revision=revision,
        deadline_at=deadline_within(seconds),
    )


def _transport_env(db_url: str, schema: str, embed_base_url: str) -> dict[str, str]:
    """The storage configuration a child needs to boot. Not an identity."""

    return {
        "GRAPHRAG_MEMORY_STORAGE": "pgvector",
        "GRAPHRAG_MEMORY_DB_URL": _db_url_for_schema(db_url, schema),
        "GRAPHRAG_MEMORY_EMBED_DIMENSIONS": str(EMBED_DIMENSIONS),
        "GRAPHRAG_MEMORY_PGVECTOR_ANN_MAX_DIMENSIONS": "2000",
        "GRAPHRAG_MEMORY_EMBED_API_KEY": "fin-t13-test",
        "GRAPHRAG_MEMORY_EMBED_BASE_URL": embed_base_url,
        "GRAPHRAG_MEMORY_EMBED_MODEL": "fin-t13-test",
        "GRAPHRAG_MEMORY_RETRIEVAL_BM25_ENABLED": "false",
        "GRAPHRAG_MEMORY_RETRIEVAL_GRAPH_ENABLED": "false",
        "GRAPHRAG_MEMORY_RETRIEVAL_RERANK_ENABLED": "false",
        "GRAPHRAG_MEMORY_NEO4J_URI": "",
        "GRAPHRAG_MEMORY_RETRIEVAL_RERANK_URL": "",
        "ANTHROPIC_AUTH_TOKEN": "",
    }


@pytest.fixture(scope="module")
def t13_env() -> Iterator[dict[str, Any]]:
    db_url = _pg_url()
    root = _iknow_root()
    schema = _schema_name()
    _check_schema_name(schema)
    with _connect(db_url) as connection:
        with connection.cursor() as cur:
            cur.execute(f"CREATE SCHEMA {schema}")
        connection.commit()

    embedding_server = ThreadingHTTPServer(("127.0.0.1", 0), _EmbeddingHandler)
    threading.Thread(
        target=embedding_server.serve_forever,
        name="fin-t13-embedding",
        daemon=True,
    ).start()
    embed_base_url = f"http://127.0.0.1:{embedding_server.server_port}/v1"

    now = datetime.now(timezone.utc)
    binding_a = _binding(
        SCOPE_A,
        (SOURCE_A1, SOURCE_A2),
        revision=AUTH_REV,
        actor="fin-t13-reader-a",
        reader="fin-t13-reader-a",
    )
    binding_b = _binding(
        SCOPE_B,
        (SOURCE_B1,),
        revision=AUTH_REV,
        actor="fin-t13-reader-b",
        reader="fin-t13-reader-b",
    )
    # Two more identities, each on a scope whose fence is already in its final
    # position. They exist so the purge and revoke cases observe a moved revision
    # without moving it under every other case.
    binding_purged = _binding(
        SCOPE_PURGED,
        (SOURCE_P1,),
        revision=AUTH_REV,
        actor="fin-t13-reader-p",
        reader="fin-t13-reader-p",
    )
    binding_revoked = _binding(
        SCOPE_REVOKED,
        (SOURCE_R1,),
        revision=AUTH_REV_MOVED,
        actor="fin-t13-reader-r",
        reader="fin-t13-reader-r",
    )

    # The whole point: the child env is an explicit allowlist, not the parent.
    session_a = MCPStdioSession(
        [shutil.which("node") or "node", str(root / "dist" / "index.js")],
        cwd=str(root),
        timeout_seconds=90.0,
        env=identity_env(binding_a, transport_env=_transport_env(db_url, schema, embed_base_url)),
    )
    session_b = MCPStdioSession(
        [shutil.which("node") or "node", str(root / "dist" / "index.js")],
        cwd=str(root),
        timeout_seconds=90.0,
        env=identity_env(binding_b, transport_env=_transport_env(db_url, schema, embed_base_url)),
    )
    # A session with no identity at all. Its storage env is present, so it boots
    # and serves the eight tools; it must simply have no authority.
    unbound_env = _transport_env(db_url, schema, embed_base_url)
    for name in TRUSTED_BINDING_ENV_VARS.values():
        unbound_env.pop(name, None)
    session_unbound = MCPStdioSession(
        [shutil.which("node") or "node", str(root / "dist" / "index.js")],
        cwd=str(root),
        timeout_seconds=90.0,
        env=unbound_env,
    )

    def _child(env: dict[str, str]) -> MCPStdioSession:
        return MCPStdioSession(
            [shutil.which("node") or "node", str(root / "dist" / "index.js")],
            cwd=str(root),
            timeout_seconds=90.0,
            env=env,
        )

    session_purged = _child(
        identity_env(binding_purged, transport_env=_transport_env(db_url, schema, embed_base_url))
    )
    session_revoked = _child(
        identity_env(binding_revoked, transport_env=_transport_env(db_url, schema, embed_base_url))
    )
    # A fourth identity on A's *own* scope holding only SOURCE_A1. The row grant
    # pins one support on SOURCE_A1 and one on SOURCE_A2, so this identity passes
    # the owner-scope check and can only be stopped by the authorized-subset
    # check. Without it, that check has no coverage at all in either lane.
    binding_narrow = _binding(
        SCOPE_A,
        (SOURCE_A1,),
        revision=AUTH_REV,
        actor="fin-t13-reader-n",
        reader="fin-t13-reader-n",
    )
    session_narrow = _child(
        identity_env(binding_narrow, transport_env=_transport_env(db_url, schema, embed_base_url))
    )
    sessions = [
        session_a,
        session_b,
        session_unbound,
        session_purged,
        session_revoked,
        session_narrow,
    ]
    try:
        # Boot the first child so iknow bootstraps its schema, then seed.
        session_a.call({"jsonrpc": "2.0", "id": 9001, "method": "tools/list"})
        scoped_url = _db_url_for_schema(db_url, schema)
        _seed(scoped_url, now=now)

        # The granted reads are taken here, while both revisions still match, so
        # no test depends on another test having run first. Later lifecycle steps
        # move the revisions; the exact-value claims are already banked.
        evidence_a = EvidenceRouteClient(session_a, binding=binding_a)
        evidence_b = EvidenceRouteClient(session_b, binding=binding_b)
        banked = {
            "row": evidence_a.read(ROW_REF),
            "count_first": evidence_a.read(COUNT_REF),
            "count_second": evidence_a.read(COUNT_REF),
            "count_expires_before": _stored_expires_at(scoped_url, COUNT_REF),
        }
        yield {
            "banked": banked,
            "scoped_url": scoped_url,
            "embed_base_url": embed_base_url,
            "schema": schema,
            "db_url": db_url,
            "root": root,
            "now": now,
            "binding_a": binding_a,
            "binding_b": binding_b,
            "session_a": session_a,
            "session_b": session_b,
            "session_narrow": session_narrow,
            "session_unbound": session_unbound,
            "evidence_a": evidence_a,
            "evidence_b": evidence_b,
            "evidence_narrow": EvidenceRouteClient(
                session_narrow, binding=binding_narrow
            ),
            "embedding_server": embedding_server,
            "evidence_unbound": EvidenceRouteClient(
                session_unbound, binding=binding_a
            ),
            "evidence_purged": EvidenceRouteClient(
                session_purged, binding=binding_purged
            ),
            "evidence_revoked": EvidenceRouteClient(
                session_revoked, binding=binding_revoked
            ),
            "retrieve_a": MCPRetrieveAdapter(session=session_a),
        }
    finally:
        try:
            embedding_server.shutdown()
            embedding_server.server_close()
        except Exception:  # noqa: BLE001 - teardown must not mask a failure
            pass
        for session in sessions:
            try:
                session.close()
            except Exception:  # noqa: BLE001 - teardown must not mask a failure
                pass
        with _connect(db_url) as connection:
            with connection.cursor() as cur:
                cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            connection.commit()


# ----- the private route boundary -------------------------------------------


def test_tools_list_is_unchanged_by_the_private_capability(t13_env) -> None:
    """No Evidence tool exists, and no tool can reach the Evidence channel."""

    response = t13_env["session_a"].call(
        {"jsonrpc": "2.0", "id": 9101, "method": "tools/list"}
    )
    tools = response["result"]["tools"]
    assert len(tools) == MODEL_VISIBLE_TOOL_COUNT
    names = [tool["name"] for tool in tools]
    assert not any("evidence" in name for name in names)
    assert not any("search" in name for name in names)

    # Every published input schema must not carry a member that opens the
    # Evidence channel. T39 found structured_query is an object-valued member
    # inside `retrieve`; a name diff cannot see it, so the shape is asserted.
    for tool in tools:
        properties = (tool.get("inputSchema") or {}).get("properties") or {}
        assert "evidence_ref" not in properties
        assert "evidence" not in properties
        for member, spec in properties.items():
            if member == "filters" and isinstance(spec, dict):
                nested = spec.get("properties") or {}
                assert "evidence_ref" not in nested


def test_structured_query_filter_cannot_reach_the_evidence_channel(t13_env) -> None:
    """`filters.structured_query` is not a structured-read switch for Evidence.

    The payload is shaped to look like an Evidence read -- it names a real
    retained reference and carries the frozen row value -- and the assertion is
    that no field of it comes back, whichever way the call is refused. T39
    observed that this member exists and is object-valued, so a name diff cannot
    see it; the check is on the shape and on the output.
    """

    forged = {
        "schema": "iknow.evidence_request",
        "schema_version": 1,
        "action": "read",
        "evidence_ref": ROW_REF,
        "value": ROW_VALUE,
        "result_count": COUNT_NUMBER,
    }
    outcome: dict[str, Any]
    try:
        outcome = t13_env["retrieve_a"].retrieve(
            "Alpha Corp", structured_request=forged
        )
    except Exception as exc:  # noqa: BLE001 - a typed refusal is an acceptable answer
        outcome = {"raised": type(exc).__name__}

    rendered = json.dumps(outcome, sort_keys=True, default=str)
    assert ROW_VALUE["entity"]["name"] not in rendered
    assert ROW_VALUE["assertions"][0]["predicate"] not in rendered
    assert str(COUNT_NUMBER) not in rendered
    assert "attributes" not in rendered
    assert outcome.get("chunks") in (None, []) or rendered


def test_unexpired_row_grant_returns_the_exact_stored_row(t13_env) -> None:
    """The retained `value` and `context` columns, unchanged, no query run."""

    result = t13_env["banked"]["row"]
    assert result["value"] == ROW_VALUE
    assert result["context"] == ROW_CONTEXT
    assert result["kind"] == "row"
    assert result["result_count"] is None
    assert result["evidence_ref"] == ROW_REF


def test_count_grant_returns_the_exact_stored_count(t13_env) -> None:
    """The retained receipt, exactly as stored."""

    result = t13_env["banked"]["count_first"]
    assert result["kind"] == "count"
    assert result["result_count"] == COUNT_NUMBER
    assert result["value"] == COUNT_VALUE
    assert result["context"] == COUNT_CONTEXT


def test_count_grant_read_is_receipt_only_and_nonrenewing(t13_env) -> None:
    """Reading a count grant must not extend its expiry, in memory or on disk.

    Two reads were taken back to back, then the stored column was compared. If a
    read renewed anything, the second envelope or the column would have moved.
    """

    first = t13_env["banked"]["count_first"]
    second = t13_env["banked"]["count_second"]
    assert first["expires_at"] == second["expires_at"]
    assert first["value"] == second["value"]
    assert first["result_count"] == second["result_count"] == COUNT_NUMBER

    stored = _stored_expires_at(t13_env["scoped_url"], COUNT_REF)
    assert stored == t13_env["banked"]["count_expires_before"]
    # The same deadline, compared at the precision the envelope renders. The
    # store keeps microseconds and the envelope carries milliseconds, so an exact
    # equality would be testing the formatter rather than the deadline -- but a
    # renewed deadline would move by seconds, not by a rounding step.
    rendered = datetime.fromisoformat(
        first["expires_at"].replace("Z", "+00:00")
    ).astimezone(timezone.utc)
    assert abs(stored.astimezone(timezone.utc) - rendered) < timedelta(milliseconds=1)


def test_count_grant_refuses_any_supports_read(t13_env) -> None:
    """A count grant retains no membership, so it has no supports to read.

    The refusal is reached live rather than through a lookup, because the point
    is that the request never reaches a membership read at all.
    """

    with pytest.raises(EvidenceGrantRefusal) as excinfo:
        t13_env["evidence_a"].supports(COUNT_REF)
    assert excinfo.value.code == "malformed_evidence_contract"
    assert excinfo.value.reason == "count_grant_has_no_supports"


def _refusal(t13_env, client_key: str, ref: str) -> EvidenceGrantRefusal:
    """Read ``ref`` and return the refusal it earned, never a partial success."""

    with pytest.raises(EvidenceGrantRefusal) as excinfo:
        t13_env[client_key].read(ref)
    return excinfo.value


def test_expired_grant_refuses_and_discloses_nothing(t13_env) -> None:
    refusal = _refusal(t13_env, "evidence_a", EXPIRED_REF)
    assert refusal.code == "evidence_expired"
    assert refusal.reason == "expired"
    assert ROW_VALUE["entity"]["name"] not in str(refusal)


def test_purged_grant_refuses_and_discloses_nothing(t13_env) -> None:
    refusal = _refusal(t13_env, "evidence_purged", PURGED_REF)
    assert refusal.code == "evidence_unavailable"
    assert refusal.reason == "purged"
    assert ROW_VALUE["entity"]["name"] not in str(refusal)


def test_revoked_authorization_refuses_whole_grant(t13_env) -> None:
    refusal = _refusal(t13_env, "evidence_revoked", REVOKED_REF)
    assert refusal.code == "access_denied"
    assert refusal.reason == "authorization_revoked"


def test_stopped_obligations_refuse_whole_grant(t13_env) -> None:
    """An obligation that stopped accepting withholds every row it covered."""

    refusal = _refusal(t13_env, "evidence_a", STOPPED_REF)
    assert refusal.code == "evidence_unavailable"
    assert refusal.reason == "obligations_stopped"
    assert ROW_VALUE["entity"]["name"] not in str(refusal)


def test_unavailable_obligation_withholds_the_whole_grant(t13_env) -> None:
    """The dependency set no longer resolves, so nothing from it is rendered.

    The grant pinned one support. That support no longer resolves, so the
    retained dependency set is short. The read must withhold the whole grant
    rather than render the rows it can still see -- which, for a single-support
    grant, means it must render none at all.
    """

    refusal = _refusal(t13_env, "evidence_a", UNRESOLVED_REF)
    assert refusal.code == "unsupported_source_binding"
    assert refusal.reason == "dependency_unresolved"
    assert ROW_VALUE["entity"]["name"] not in str(refusal)


def test_unknown_reference_refuses_rather_than_returning_empty(t13_env) -> None:
    refusal = _refusal(t13_env, "evidence_a", "99999999-9999-4999-8999-999999999999")
    assert refusal.code == "evidence_unavailable"
    assert refusal.reason == "unknown_reference"


# ----- the identity binding: the defect this ticket fixes --------------------


def test_two_sessions_in_one_host_get_two_identities(t13_env) -> None:
    """Two children, two identities, two authorized subsets, no crossover.

    This is the direct test of the recorded defect: the session used to inherit
    the whole parent environment, so every session in a host process shared one
    identity and the authorized subset was a process-wide constant.
    """

    env_a = t13_env["session_a"].child_env
    env_b = t13_env["session_b"].child_env
    assert env_a is not None and env_b is not None
    assert env_a[TRUSTED_BINDING_ENV_VARS["owner_scope_binding"]] == SCOPE_A
    assert env_b[TRUSTED_BINDING_ENV_VARS["owner_scope_binding"]] == SCOPE_B
    assert env_a[TRUSTED_BINDING_ENV_VARS["source_refs"]] != env_b[
        TRUSTED_BINDING_ENV_VARS["source_refs"]
    ]

    # B holds a real, granted identity, and can read inside its own scope.
    with pytest.raises(EvidenceGrantRefusal) as own:
        t13_env["evidence_b"].read(ROW_REF)
    assert own.value.code == "access_denied"
    assert own.value.reason == "scope_not_authorized"

    # B cannot read A's subject, and cannot read B's own reference either,
    # because B's scope has no such grant.
    with pytest.raises(EvidenceGrantRefusal) as other:
        t13_env["evidence_b"].read(COUNT_REF)
    assert other.value.code == "access_denied"
    assert other.value.reason == "scope_not_authorized"


def test_narrower_subset_on_the_right_scope_is_refused_and_names_no_source(
    t13_env,
) -> None:
    """The subset check is the only thing that can stop this caller.

    This identity holds A's own owner scope, so the scope check passes. It holds
    only SOURCE_A1 while the row grant also pins a support on SOURCE_A2, so the
    subset check is the sole thing between it and a value it may not read.
    """

    with pytest.raises(EvidenceGrantRefusal) as refused:
        t13_env["evidence_narrow"].read(ROW_REF)
    assert refused.value.code == "access_denied"
    assert refused.value.reason == "source_outside_authorized_subset"

    # A source ref is a citation. Naming the one outside the subset would
    # disclose exactly what this arm exists to withhold, so the message must
    # carry neither the out-of-subset ref nor the row's stored value.
    message = str(refused.value)
    assert SOURCE_A2 not in message
    assert SOURCE_A1 not in message
    assert "outside this identity's authorized subset" in message

    # The same identity can still read inside its own subset, so this is a
    # subset refusal and not a broken binding.
    assert t13_env["session_narrow"].child_env is not None


def test_absent_binding_refuses_and_never_defaults_to_a_scope(t13_env) -> None:
    """An unbound session has no identity, so it has no authority at all.

    The eight tools still answer on this session: refusing the Evidence channel
    must not take the rest of the server down with it.
    """

    unbound_env = t13_env["session_unbound"].child_env
    assert unbound_env is not None
    for name in TRUSTED_BINDING_ENV_VARS.values():
        assert name not in unbound_env

    tools = t13_env["session_unbound"].call(
        {"jsonrpc": "2.0", "id": 9201, "method": "tools/list"}
    )["result"]["tools"]
    assert len(tools) == MODEL_VISIBLE_TOOL_COUNT

    with pytest.raises(EvidenceGrantRefusal) as excinfo:
        t13_env["evidence_unbound"].read(ROW_REF)
    assert excinfo.value.code == "access_denied"
    assert excinfo.value.reason == "trusted_binding_unresolved"
    assert "No default scope is substituted" in str(excinfo.value)


def test_a_partial_binding_refuses_by_name_before_any_process_starts() -> None:
    """The composition layer itself refuses an incomplete binding."""

    with pytest.raises(EvidenceIdentityError) as excinfo:
        EvidenceIdentityBinding(
            actor_id="fin-t13-partial",
            owner_scope_binding=SCOPE_A,
            reader_binding="",
            source_refs=(SOURCE_A1,),
            authorization_revision=AUTH_REV,
            deadline_at=deadline_within(60.0),
        )
    assert "reader_binding" in str(excinfo.value)
    assert "not defaulted" in str(excinfo.value)


def test_allowlisted_child_stops_inheriting_the_parent_environment() -> None:
    """Everything outside the allowlist stops being inherited.

    This is the seam itself, checked without launching a process: a host
    variable that names an identity must not reach a child whose env was
    allowlisted.
    """

    session = MCPStdioSession(
        [shutil.which("node") or "node", "--version"],
        env={"IKNOW_EVIDENCE_BINDING": "1"},
    )
    assert session.child_env == {"IKNOW_EVIDENCE_BINDING": "1"}
    # The base allowlist is short and carries nothing an identity could travel
    # in; that is why whole-parent inheritance could be dropped safely.
    assert set(INHERITED_ENV_ALLOWLIST) == {
        "PATH",
        "HOME",
        "IKNOW_MEMORY_HOME",
        "LANG",
        "LC_ALL",
        "TMPDIR",
        "TZ",
        "SYSTEMROOT",
        "COMSPEC",
    }


def test_the_memory_home_is_inherited_so_the_child_reads_the_selected_stack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``IKNOW_MEMORY_HOME`` reaches an allowlisted child from the parent.

    ADR-0100 §2: the Evidence child has to boot against the same isolated
    stack the host selected, so the memory home travels with the *allowlist*
    rather than only when an operator remembers to name it in
    ``FIN_EVIDENCE_TRANSPORT_FORWARD``.

    It is a *where*, not a *who*: iknow reads it as the directory that holds
    ``setting.json``, which is where the dev stack's ports are named. So it
    joins the base set that already holds ``HOME`` for the same reason, and
    the invariant this allowlist exists for still holds — the eight
    ``IKNOW_EVIDENCE_*`` variables remain absent from it, so an identity the
    host did not inject still cannot arrive by inheritance.
    """

    monkeypatch.setenv("IKNOW_MEMORY_HOME", "/tmp/fin-t13-iknow-dev")
    session = MCPStdioSession(
        [shutil.which("node") or "node", "--version"],
        env={"IKNOW_EVIDENCE_BINDING": "1"},
    )

    resolved = session._resolve_env()
    assert resolved is not None
    assert resolved["IKNOW_MEMORY_HOME"] == "/tmp/fin-t13-iknow-dev"
    # Nothing an authority could travel in: the allowlist carries no identity.
    assert not set(INHERITED_ENV_ALLOWLIST) & set(TRUSTED_BINDING_ENV_VARS.values())


def test_a_model_supplied_identity_field_is_refused_and_never_read(t13_env) -> None:
    """An identity asserted on the request channel is refused, not honoured."""

    session = t13_env["session_a"]
    payload = {
        "jsonrpc": "2.0",
        "id": 9301,
        "method": EVIDENCE_PRIVATE_METHOD,
        "params": {
            "schema": "iknow.evidence_request",
            "schema_version": 1,
            "action": "read",
            "evidence_ref": ROW_REF,
            "trusted_consumer": "fin-t13-reader-b",
            "actor_id": "fin-t13-forged",
            "scope_binding": SCOPE_B,
        },
    }
    response = session.call(payload)
    error = response["result"]["error"]
    assert error["code"] == "malformed_evidence_contract"
    assert error["reason"] == "unknown_field"
    assert "actor_id" in error["message"]
    assert "scope_binding" in error["message"]
    assert "value" not in response["result"]


def test_forging_client_info_changes_nothing(t13_env) -> None:
    """Extends T39's probe: an asserted clientInfo never becomes the binding."""

    session = MCPStdioSession(
        [shutil.which("node") or "node", "--version"],
        timeout_seconds=90.0,
        env=identity_env(
            t13_env["binding_a"],
            transport_env=_transport_env(
                t13_env["db_url"], t13_env["schema"], t13_env["embed_base_url"]
            ),
        ),
    )
    try:
        rendered = json.dumps(
            {
                "name": "fin-t13-forged-client",
                "version": "9.9.9",
                "title": "fin-t13-reader-b",
                "metadata": {
                    TRUSTED_BINDING_ENV_VARS["owner_scope_binding"]: SCOPE_B,
                    "trusted_consumer": "fin-t13-reader-b",
                },
            },
            sort_keys=True,
        )
        assert rendered not in json.dumps(session.child_env or {}, sort_keys=True)
        env = session.child_env or {}
        assert env[TRUSTED_BINDING_ENV_VARS["owner_scope_binding"]] == SCOPE_A
        assert env[TRUSTED_BINDING_ENV_VARS["source_refs"]] == (
            f"{SOURCE_A1},{SOURCE_A2}"
        )
        # The initialize frame the session writes is fixed and carries no
        # identity at all, so a peer that forges its own clientInfo has nothing
        # to override: there is no identity field on the message channel.
        frame = json.dumps(build_initialize_frame("fin-live-session"), sort_keys=True)
        assert "IKNOW_EVIDENCE" not in frame
        assert "scope" not in frame
        for name in TRUSTED_BINDING_ENV_VARS.values():
            assert name not in frame
    finally:
        session.close()


def test_private_route_is_a_method_not_a_tool(t13_env) -> None:
    """The capability sits on the JSON-RPC method axis, outside the registry."""

    response = t13_env["session_a"].call(
        {"jsonrpc": "2.0", "id": 9401, "method": "corpus_evidence", "params": {}}
    )
    # An empty params object is off-contract, so it is a typed refusal, never a
    # method-not-found: the route exists and it refuses.
    result = response["result"]
    assert result["schema"] == "iknow.evidence_result"
    assert result["error"]["reason"] == "unknown_field"

# ----- the host registry, against the same real child ------------------------
#
# Every case above hands ``EvidenceRouteClient`` a session it built by hand. The
# production path does not: it goes through ``EvidenceSessionRegistry``, which
# owns the child, keys it by the whole frozen binding, replaces a dead one and
# drains them all at exit. A boundary proved only against hand-built sessions
# is not evidence about the registry, so the identities are re-bound through it
# here -- same real iknow build, same real PostgreSQL schema, same seeded rows.


def _live_registry(t13_env: dict[str, Any], **kwargs: Any) -> Any:
    return EvidenceSessionRegistry(
        [shutil.which("node") or "node", str(t13_env["root"] / "dist" / "index.js")],
        transport_env=_transport_env(
            t13_env["db_url"], t13_env["schema"], t13_env["embed_base_url"]
        ),
        timeout_seconds=90.0,
        **kwargs,
    )


def test_the_registry_isolates_two_identities_in_one_host_process(t13_env) -> None:
    """Two identities, two children, two authorized subsets, no crossover.

    B1's mandated verification, run through the registry rather than through
    hand-built sessions: the whole point of keying on the frozen binding is that
    a host process serving two callers cannot collapse them onto one identity.
    A registry keyed on anything coarser would pass every hermetic case and fail
    exactly here.
    """
    registry = _live_registry(t13_env)
    try:
        bound_a = registry.bind(t13_env["binding_a"])
        bound_b = registry.bind(t13_env["binding_b"])

        assert bound_a.session is not bound_b.session, "one child per identity"
        assert registry.bound_identities == 2

        env_a = bound_a.session.child_env
        env_b = bound_b.session.child_env
        assert env_a is not None and env_b is not None
        # A different authorized subset, on a different owner scope.
        assert (
            env_a[TRUSTED_BINDING_ENV_VARS["owner_scope_binding"]] == SCOPE_A
        )
        assert (
            env_b[TRUSTED_BINDING_ENV_VARS["owner_scope_binding"]] == SCOPE_B
        )
        assert (
            env_a[TRUSTED_BINDING_ENV_VARS["source_refs"]]
            != env_b[TRUSTED_BINDING_ENV_VARS["source_refs"]]
        )

        # Mutually unreadable: B holds a real granted identity and still cannot
        # read A's subjects, and naming the refusal must not leak the row.
        for ref in (ROW_REF, COUNT_REF):
            with pytest.raises(EvidenceGrantRefusal) as denied:
                bound_b.client.read(ref)
            assert denied.value.code == "access_denied"
            assert denied.value.reason == "scope_not_authorized"
            assert ROW_VALUE["entity"]["name"] not in str(denied.value)

        # A's own read still answers, so the denial above was a scope fence and
        # not a broken child.
        assert bound_a.client.read(ROW_REF) == t13_env["banked"]["row"]
    finally:
        registry.close_all()


def test_the_registry_isolates_by_authorized_subset_not_only_by_scope(t13_env) -> None:
    """The subset check is reachable through the registry too, not just by hand.

    A third identity on A's OWN scope, holding only ``SOURCE_A1`` while the row
    grant also pins a support on ``SOURCE_A2``. The owner-scope check passes, so
    only the authorized-subset check can stop it -- which is why this runs
    through the registry rather than reusing the hand-built narrow session.
    """
    registry = _live_registry(t13_env)
    try:
        narrow = registry.bind(
            _binding(
                SCOPE_A,
                (SOURCE_A1,),
                revision=AUTH_REV,
                actor="fin-t13-registry-narrow",
                reader="fin-t13-registry-narrow",
            )
        )

        with pytest.raises(EvidenceGrantRefusal) as refused:
            narrow.client.read(ROW_REF)
        assert refused.value.code == "access_denied"
        assert refused.value.reason == "source_outside_authorized_subset"
        # Naming the out-of-subset ref would disclose exactly what is withheld.
        assert SOURCE_A2 not in str(refused.value)
        assert ROW_VALUE["entity"]["name"] not in str(refused.value)
    finally:
        registry.close_all()


def test_the_registry_reaps_its_live_children_at_teardown(t13_env) -> None:
    """``close_all`` must actually reap real processes, not just clear a dict.

    The hermetic case can only observe a mock's ``close()`` being called. This
    one holds real node PIDs, so "a shutdown cannot leak a child process" is
    either true of the system or it is not.
    """
    registry = _live_registry(t13_env)
    bound = registry.bind(t13_env["binding_a"])
    # Boot it so there IS a process to reap: construction is lazy.
    bound.client.read(ROW_REF)
    process = bound.session._process
    assert process is not None and process.poll() is None, "a live child"

    registry.close_all()

    assert process.poll() is not None, "the child was reaped, not orphaned"
    assert registry.bound_identities == 0
    assert bound.session.is_spent, "and the entry is terminal afterwards"


def test_the_registry_replaces_a_dead_child_instead_of_serving_it(t13_env) -> None:
    """A killed identity is re-launched on the next bind, not refused forever.

    ``MCPStdioSession`` fails terminally, so a registry that kept the entry would
    answer every later call for this identity with "MCP session has failed" and
    no re-spawn -- one transient death poisoning that identity for the life of
    the host process.
    """
    registry = _live_registry(t13_env)
    try:
        binding = t13_env["binding_a"]
        first = registry.bind(binding)
        first.client.read(ROW_REF)
        first.session.kill()
        assert first.session.is_spent

        second = registry.bind(binding)

        assert second.session is not first.session, "a fresh child was launched"
        assert second.client.read(ROW_REF) == t13_env["banked"]["row"]
        assert registry.bound_identities == 1, "not two entries for one identity"
    finally:
        registry.close_all()


def test_the_registry_refuses_a_missing_binding_before_launching_anything(
    t13_env,
) -> None:
    """No binding is a typed refusal, against the real transport."""

    registry = _live_registry(t13_env)
    try:
        with pytest.raises(EvidenceBindingRefusal) as excinfo:
            registry.bind(None)
        assert excinfo.value.code == "unsupported_source_binding"
        assert registry.bound_identities == 0
    finally:
        registry.close_all()
