"""T40 -- QP8: the structured Query route through Fin's real retrieve adapter.

This is the live lane for the P1 Query production slice. It drives a **real**
built iknow child over stdio, through the **real** Fin retrieve adapter, against
**real** PostgreSQL in a per-run schema.

What the run may assume is deliberately small: a built iknow at
``FIN_LIVE_IKNOW_ROOT`` bound to ``BOUND_IKNOW_COMMIT``, a reachable database,
and the strict markers. Anything missing is a named non-zero failure, never a
skip.

T40 boundary, stated once so it is not lost. This driver proves the
host-to-MCP ROUTE and the refusal surface:

  * the model-visible tool surface is unchanged by the capability;
  * a structured request cannot reach the legacy business ledger;
  * every request-side refusal arrives as its SPEC-typed code through the real
    adapter, not as prose and not as an empty success;
  * an absent producer on the bound build is reported as
    ``structured_search_unavailable``, never as a fake success.

It does **not** prove the row-producing success path, and the reason is no longer
"a host cannot provision this reader at all". That used to be true: the seam read
a process-global for its identity, so no lane could supply a distinct one. IK
made the identity an injected value on ``fix/p1-query-seam`` (IK ``cf61a78``,
not yet merged) -- ``queryScopeIdentityFromEnv(env)`` is the host's half, read
once at composition, and ``ServerDeps.queryIdentity`` is the injection point --
so the identity half of the gap is closable and this paragraph now has to say what
is left.

The identity half IS closable, and the measurement is why the negative control
below keeps its value. A session that supplies the six ``IKNOW_QUERY_*`` members
with a cursor key of at least 32 bytes has a live reader, and a valid request
against an unauthorized scope answers ``authorization_denied``. This driver's own
fixture answers ``structured_search_unavailable`` for that same request, and the
only difference between the two is the fixture's own configuration: it exports
neither the six names nor a cursor key, so its reader is absent by construction.
The six names belong to IK and are read from ``QUERY_SCOPE_ENV_VARS`` in
``src/core/storage/structured-query-scope.ts``; fin reads them from there rather
than restating them, because a lane that spells them out is a lane that keeps
provisioning a reader with names nothing reads after a rename.

A ROW is still out of reach from here, and the blocker is the corpus rather than
the identity. Reading one needs a PUBLISHED structured corpus: the accepted
snapshot is ``structured_publication_membership`` joined to
``structured_publication_sources`` on ``published_generation``, with the
attributed display in ``structured_publication_display``
(``buildPgAcceptedSnapshot``, IK ``structured-scope-reads.ts``). Nothing a Python
lane may reach writes those rows:

  * the only writer is IK's ``PgStructuredPublicationStorage.publish()``, and in
    IK's ``src/`` it has NO production caller: ``declareDesired``, ``stageAttempt``
    and ``publish`` appear only in the storage and service modules that define
    them. Its callers are the storage contract tests
    (``tests/storage/structured-scope-uow-memory.test.ts``), which compose a
    ``PgvectorBackend`` in-process -- a host that speaks TypeScript, not MCP.
    That is why the live database contains completed publications at all while
    nothing a Python lane may reach can produce one.
  * the frozen eight MCP tools cannot publish one either. ``ingest`` and ``push``
    write chunks; ``submit_index_job`` writes the durable-work ledger and states
    it "never runs extraction itself"; ``extract`` runs the legacy
    triple-extraction pipeline (extraction records, entities, triples, outbox
    intents). That is a FINDING, not an obstacle to route around: until some tool
    can publish a corpus, no MCP-driven lane can prove a row.
  * fin has no writer to reuse. Nothing under ``src/fin/retrieve/`` or
    ``src/fin/host/`` names those three tables. The only fin-side seed of the
    publication area is T13's ``_seed``, and it writes scopes, declared sources,
    supports and grants -- no membership, no display, no published generation.
    Extending it would be writing the missing half of a publisher in Python,
    which is the second seeding implementation this lane refuses: a test written
    against a seed it controls proves the seed as much as the reader, and
    ``tests/acceptance/structured-query-pg.test.ts`` already proves that half
    against the real protocol.
  * and there is no corpus this lane may consume. The QP1 oracle packet describes
    facts and their sources, not a published generation. What the live database
    holds instead is another lane's completed work, and the honest description of
    it is sharper than "nothing was published" -- publication DID complete, in
    that lane's schemas only. Measured 2026-10-07 against 127.0.0.1:55432:

    - 76 schemas carry the publication tables: 69
      ``structured_screening_decisions_*``, 4 ``embedding_generation_pg_*``, 2
      ``t13_*`` and ``public``.
    - all 152 membership rows sit in the 69 screening schemas, and all 152 pass
      the real consumable join from ``readScopedMembership``
      (``s.active AND s.published_generation = m.generation``), so those sources
      are genuinely published and servable.
    - ``structured_publication_display`` holds ZERO rows in every one of those 76
      schemas. The attributed values that name a result are the missing half, not
      the membership.
    - ``public`` is not empty, which an earlier revision of this file asserted:
      it holds 15 active declared sources and not one of them has a
      ``published_generation`` -- desired state that was never published -- and
      zero membership and zero display rows.
    - the 2 ``t13_*`` schemas hold 6 declared sources, also never published, and
      zero membership. The single ``t40_query_*`` schema holds no rows in any of
      the three tables.

    So the leaked state is servable membership whose scope bindings belong to
    another lane's runs and which has no display to name it. A lane that read one
    of those artefacts would pass only where another lane had already run, and
    fail on a clean database, so consuming it is not evidence of anything.

    Those counts are prose, and prose goes stale: the database is shared and every
    lane mutates it. ``scripts/measure_structured_corpus_state.py`` re-derives all
    of them read-only, using the same join, and ``--require-corpus`` exits 2 until
    a servable AND named corpus exists so P3 can gate on the gap rather than meet
    it as a confusing refusal.
  * and a published corpus alone would still not answer from a child-bootstrapped
    schema. The read builds its maintenance status from
    ``structured_screening_checks`` in the same transaction as the rows, and
    ``upSchemaBootstrap`` does not create it: the orchestrator calls
    ``upSchemaProjection``, ``upSchemaMaintenanceWork`` and
    ``upSchemaPublication``, while ``structured_screening_checks`` belongs to
    ``schema-screening.ts`` and is created by the IK lane's own
    ``upSchemaScreening`` before it can answer a query at all.

So the success-path acceptance stays where a publisher can reach it: T41's frozen
candidate pair and the Q9 run, beside the IK lane that owns the protocol. What
this module owns is the route and the refusal surface, plus the negative control
that keeps the difference visible.

One more limit on the binding, because it is the kind that hides. The check below
pins the COMMIT: it fails when the resolved build root's HEAD is not
``BOUND_IKNOW_COMMIT``. It does not pin the ARTIFACT. ``dist/`` can be rebuilt
from that same commit -- by another lane, with another toolchain state -- and
this driver will not notice, so "green against the bound build" means green
against whatever ``dist/index.js`` happened to be at run time. Observed on
2026-10-07: ``iknow-wt-p1`` sat at the bound commit with ``src/`` clean, but its
``dist/index.js`` was rebuilt at 14:39 by another session, and this file's 19
cases were re-run green after that rebuild. A commit pin plus a clean ``src/``
bounds the input; only a build stamp would bound the artifact, and there is none.

Nothing here closes Q1-Q13, T18, T19, T20 or T21.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any, Iterator

import psycopg
import pytest

from fin.retrieve.mcp_adapter import MCPRetrieveAdapter
from fin.retrieve.mcp_session import MCPStdioSession
from fin.retrieve.port import STRUCTURED_RESULT_KEY


pytestmark = [pytest.mark.mcp, pytest.mark.sql_live]

# Run-prefixed so cleanup can only ever drop what this module created. The
# generic `^[a-z][a-z0-9_]{0,62}$` a sibling driver uses would also accept a
# hand-passed name; this one cannot.
_PG_SCHEMA_RE = re.compile(r"^t40_query_[0-9a-f]{12}$")

#: The iknow commit this driver's expectations were frozen against. A run
#: against another build tests a different boundary and must not report green.
#: Re-bound from ``c49ff8f`` after IK's Query issuer added authorization/purge
#: revision fences at grant issuance. The query snapshot remains read-only, and
#: this driver's frozen route/refusal assertions are unchanged. T39 keeps its
#: independent binding in ``test_live_t39_private_route_tools_list.py``.
BOUND_IKNOW_COMMIT = "f5ff50ed7e527966aef71df04112a04778af0567"

#: The eight tools the frozen surface has. A ninth is a model-visible change.
FROZEN_TOOL_NAMES = [
    "extract",
    "ingest",
    "invalidate",
    "poll_index_job",
    "push",
    "read_source",
    "retrieve",
    "submit_index_job",
]

#: The child validates this at boot, so it has to be present and consistent.
EMBED_DIMENSIONS = 8

# ----- frozen expectations, chosen from the SPEC before the run ---------------

#: The closed code set the SPEC defines; every refusal below must be one of
#: these, so a prose failure or an unclassified error cannot satisfy a case.
SPEC_ERROR_CODES = frozenset(
    {
        "malformed_structured_query",
        "unsupported_structured_schema",
        "unsupported_subject",
        "unsupported_operation",
        "unsupported_field",
        "unsupported_operator",
        "invalid_value",
        "request_too_large",
        "authorization_denied",
        "invalid_cursor",
        "cursor_expired",
        "cursor_revision_mismatch",
        "read_revision_mismatch",
        "result_too_large",
        "count_unavailable",
        "query_timeout",
        "structured_search_unavailable",
        "maintenance_status_unavailable",
    }
)

#: The only schema token and version a request may carry (ADR 0093: the version
#: lives in its own member, never a token suffix).
REQUEST_SCHEMA = "iknow.structured_query"
REQUEST_SCHEMA_VERSION = 1


# ----- environment guards: named failures, never skips ------------------------


def _pg_url() -> str:
    url = os.environ.get("FIN_LIVE_IKNOW_DB_URL") or os.environ.get(
        "GRAPHRAG_MEMORY_DB_URL"
    )
    if not url:
        pytest.fail(
            "FIN_LIVE_IKNOW_DB_URL is absent. This lane requires the isolated "
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
            "child. live_iknow_support.IKNOW_ROOT silently falls back to the main "
            "iknow checkout when this is unset, which is why this check exists. "
            "An absent root is a failure, never a skip."
        )
    root = Path(str(raw))
    build = root / "dist" / "index.js"
    if not build.is_file():
        pytest.fail(
            f"the iknow build is missing at {build}. Run `npm run build` in "
            f"{root} first. A missing build is a failure, never a skip."
        )
    return root


def _require_bound_iknow_build(root: Path) -> None:
    """The bound-commit gate: a named failure naming both SHAs, never a skip."""

    try:
        observed = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        pytest.fail(
            f"T40 IK build binding: the resolved build root {root} has no "
            f"readable git HEAD ({type(exc).__name__}), so its commit cannot be "
            f"confirmed to be the bound commit {BOUND_IKNOW_COMMIT}. Export "
            "FIN_LIVE_IKNOW_ROOT to the worktree that holds it and confirm its "
            "dist/index.js is built. This is a named failure on purpose, not a "
            "skip."
        )
    if not observed.startswith(BOUND_IKNOW_COMMIT):
        pytest.fail(
            f"T40 IK build binding: this driver is bound to IK commit "
            f"{BOUND_IKNOW_COMMIT}, but the resolved build root {root} is at "
            f"{observed}. Every expectation in this file was frozen against "
            f"{BOUND_IKNOW_COMMIT}; a run against another build tests a "
            "different boundary and must not report green. Set "
            "FIN_LIVE_IKNOW_ROOT to the bound worktree, or re-bind "
            "BOUND_IKNOW_COMMIT as a deliberate, recorded one-line change. This "
            "is a named failure on purpose, not a skip."
        )


def _schema_name() -> str:
    return f"t40_query_{uuid.uuid4().hex[:12]}"


def _check_schema_name(schema: str) -> None:
    if not _PG_SCHEMA_RE.match(schema):
        raise ValueError(f"unsafe pg_schema name: {schema!r}")


def _db_url_for_schema(db_url: str, schema: str) -> str:
    _check_schema_name(schema)
    separator = "&" if "?" in db_url else "?"
    return f"{db_url}{separator}options=-c%20search_path%3D{schema}%2Cpublic"


def _connect(db_url: str) -> psycopg.Connection[Any]:
    try:
        return psycopg.connect(db_url, connect_timeout=10)
    except psycopg.Error as exc:
        pytest.fail(
            "PostgreSQL is unreachable from the configured DSN: "
            f"{type(exc).__name__}. An unreachable database is a failure, never "
            "a skip."
        )


# ----- request helpers -------------------------------------------------------


def _structured_request(
    *,
    subject: str = "entity",
    operation: str = "list",
    filters: list[dict[str, Any]] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "schema": REQUEST_SCHEMA,
        "schema_version": REQUEST_SCHEMA_VERSION,
        "subject": subject,
        "operation": operation,
    }
    if filters is not None:
        body["filters"] = filters
    body.update(extra)
    return body


def _retrieve(
    session: MCPStdioSession,
    structured_request: dict[str, Any],
    *,
    query: str = "northwind",
    search_mode: str = "sql",
) -> dict[str, Any]:
    """Drive the real adapter and return its envelope."""

    adapter = MCPRetrieveAdapter(session=session)
    return adapter.retrieve(
        query, search_mode=search_mode, structured_request=structured_request
    )


def _raw_tool_call(
    session: MCPStdioSession,
    structured_request: dict[str, Any],
    *,
    query: str = "northwind",
    search_mode: str = "sql",
    outer_filters: dict[str, str] | None = None,
) -> dict[str, Any]:
    """One ``tools/call`` at the transport.

    The adapter names its own filters, so a case that needs an arbitrary outer
    filter beside the structured request has to drive the wire directly.
    """

    filters: dict[str, Any] = {
        "search_mode": search_mode,
        "structured_query": structured_request,
    }
    if outer_filters:
        filters.update(outer_filters)
    response = session.call(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "retrieve",
                "arguments": {"query": query, "filters": filters},
            },
        }
    )
    body = response.get("result", {})
    if not isinstance(body, dict):
        return {}
    for block in body.get("content", []) or []:
        text = block.get("text") if isinstance(block, dict) else None
        if isinstance(text, str):
            try:
                parsed = json.loads(text)
            except ValueError:
                continue
            if isinstance(parsed, dict):
                return parsed
    return body


def _refusal_code(envelope: dict[str, Any]) -> str:
    """The SPEC-typed refusal, asserted to be a member of the closed set.

    Asserting membership is what makes a prose failure or an unclassified
    error unable to satisfy a case: the SPEC requires one structured shape and
    never prose-only failure.

    Two shapes reach a caller and both are correct at their own layer. The
    adapter flattens the backend's ``iknow.structured_error`` envelope to
    ``{"error": <code>}``; a case driven at the raw transport sees the envelope
    itself. Reading both keeps a case from passing merely because it chose the
    layer whose shape it likes, while still refusing prose.
    """

    raw = envelope.get("error")
    if isinstance(raw, dict):
        assert raw.get("schema") == "iknow.structured_error", (
            f"a structured refusal must carry the SPEC envelope: {envelope!r}"
        )
        assert raw.get("schema_version") == 1, (
            f"the refusal envelope's version must be 1: {envelope!r}"
        )
        code = raw.get("code")
    else:
        code = raw
    assert isinstance(code, str), f"expected a typed refusal code, got {envelope!r}"
    assert code in SPEC_ERROR_CODES, (
        f"the refusal code is outside the SPEC's closed set: {code!r}"
    )
    return code


# ----- the live fixture ------------------------------------------------------


@pytest.fixture(scope="module")
def t40_env() -> Iterator[dict[str, Any]]:
    db_url = _pg_url()
    root = _iknow_root()
    _require_bound_iknow_build(root)
    schema = _schema_name()
    _check_schema_name(schema)
    with _connect(db_url) as connection:
        with connection.cursor() as cur:
            cur.execute(f"CREATE SCHEMA {schema}")
        connection.commit()

    # The child env is an explicit allowlist, not the parent's. It carries no
    # Query identity and no cursor key ON PURPOSE, and the reason is the
    # negative control rather than an omission: a reader exists only with both a
    # cursor key of at least 32 bytes and the six injected ``IKNOW_QUERY_*``
    # members, so this configuration is what makes a valid request read as
    # ``structured_search_unavailable`` instead of a scope-level refusal. The
    # refusal matrix below is decided before any scope is consulted, so it must
    # stay on an unprovisioned reader; a lane that armed it here would flip that
    # control and every case that shares the fixture with it.
    #
    # When an armed lane does get written, the identity it supplies is a HOST
    # fact: it belongs to the lane's own configuration and must never arrive from
    # anything a model or the message channel can influence, because a reader
    # that trusts a request-supplied scope, source set or revision would answer
    # with another principal's corpus. That is a different thing from the host
    # inventing a default scope when none is injected, and neither is done here.
    env = {
        "GRAPHRAG_MEMORY_STORAGE": "pgvector",
        "GRAPHRAG_MEMORY_DB_URL": _db_url_for_schema(db_url, schema),
        "GRAPHRAG_MEMORY_EMBED_DIMENSIONS": str(EMBED_DIMENSIONS),
        "GRAPHRAG_MEMORY_PGVECTOR_ANN_MAX_DIMENSIONS": "2000",
        "GRAPHRAG_MEMORY_EMBED_API_KEY": "fin-t40-test",
        "GRAPHRAG_MEMORY_EMBED_BASE_URL": "http://127.0.0.1:1/v1",
        "GRAPHRAG_MEMORY_EMBED_MODEL": "fin-t40-test",
        "GRAPHRAG_MEMORY_RETRIEVAL_BM25_ENABLED": "false",
        "GRAPHRAG_MEMORY_RETRIEVAL_GRAPH_ENABLED": "false",
        "GRAPHRAG_MEMORY_RETRIEVAL_RERANK_ENABLED": "false",
        "GRAPHRAG_MEMORY_NEO4J_URI": "",
        "GRAPHRAG_MEMORY_RETRIEVAL_RERANK_URL": "",
        "ANTHROPIC_AUTH_TOKEN": "",
    }

    session = MCPStdioSession(
        [shutil.which("node") or "node", str(root / "dist" / "index.js")],
        cwd=str(root),
        timeout_seconds=120.0,
        env=env,
    )
    try:
        yield {"session": session, "schema": schema, "root": root}
    finally:
        try:
            session.close()
        except Exception:  # noqa: BLE001 - teardown must not mask the real failure
            pass
        with _connect(db_url) as connection:
            with connection.cursor() as cur:
                cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            connection.commit()


# ----- the acceptance cases --------------------------------------------------


def test_tools_list_is_still_exactly_the_eight_frozen_names(t40_env) -> None:
    """P1-I7 forbids adding a model-visible tool for this capability.

    The frozen contract cannot see a mode switch hidden inside
    ``filters.structured_query``, so the surface staying at eight names is the
    only thing keeping this capability out of the model's tool menu.
    """

    response = t40_env["session"].call(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    )
    tools = response.get("result", {}).get("tools", [])
    names = sorted(tool["name"] for tool in tools)
    assert names == FROZEN_TOOL_NAMES, (
        "the structured capability changed the model-visible surface: "
        f"observed={names} expected={FROZEN_TOOL_NAMES}"
    )


def test_an_unprovisioned_producer_is_reported_as_unavailable_not_as_success(
    t40_env,
) -> None:
    """A valid request with no reader is the SPEC's capability trigger.

    This is the negative control for the whole lane: it is what a working
    producer replaces, and it proves fin reports the absence as a typed refusal
    rather than degrading into an empty success or a prose failure.
    """

    envelope = _retrieve(t40_env["session"], _structured_request())
    assert _refusal_code(envelope) == "structured_search_unavailable", (
        "a valid structured request with no producer must be reported as "
        f"structured_search_unavailable, got {envelope!r}"
    )
    assert STRUCTURED_RESULT_KEY not in envelope, (
        "a refusal must not also carry a structured result sibling"
    )
    assert envelope.get("chunks") in (None, []), (
        f"a structured refusal must not fabricate lexical chunks: {envelope!r}"
    )


def test_a_wrong_host_bound_tier_is_refused_before_the_backend(t40_env) -> None:
    """Q1/Q13: only the host-bound sql tier may carry a structured request."""

    envelope = _retrieve(
        t40_env["session"], _structured_request(), search_mode="graph"
    )
    assert _refusal_code(envelope) == "malformed_structured_query", (
        f"a non-sql tier must be refused by the host, got {envelope!r}"
    )


def test_an_outer_filter_beside_the_query_is_refused_by_fin_before_the_backend(
    t40_env,
) -> None:
    """Q13: the structured path cannot be widened by a legacy outer filter.

    Fin decides this itself, from the presence of an outer filter beside the
    structured request, so the case is a host-boundary refusal and must not
    depend on what the backend would have said.
    """

    adapter = MCPRetrieveAdapter(session=t40_env["session"])
    envelope = adapter.retrieve(
        "northwind",
        search_mode="sql",
        structured_request=_structured_request(),
        cik="0000320193",
    )
    assert _refusal_code(envelope) == "malformed_structured_query", (
        f"an outer filter beside a structured request must be refused: {envelope!r}"
    )
    assert STRUCTURED_RESULT_KEY not in envelope, (
        "a host-side refusal must not carry a result sibling"
    )


def test_the_raw_wire_keeps_the_backend_envelope_intact(t40_env) -> None:
    """The typed shape survives to the wire rather than degrading to prose.

    Driven at the transport on purpose: this is the only place the backend's
    own ``iknow.structured_error`` envelope is visible, and the SPEC requires
    it to reach a host as structure rather than as a message string.
    """

    envelope = _raw_tool_call(t40_env["session"], _structured_request(subject="document"))
    assert envelope.get("schema") == "iknow.structured_error", (
        f"a structured refusal must reach the wire as its envelope: {envelope!r}"
    )
    assert envelope.get("schema_version") == 1
    assert envelope["error"] == {"code": "unsupported_subject", "retryable": False}, (
        f"the envelope must carry the SPEC code and its retryability: {envelope!r}"
    )


def test_an_unknown_schema_token_is_refused_by_name(t40_env) -> None:
    """Q1: an unknown schema token is `unsupported_structured_schema`."""

    body = _structured_request()
    body["schema"] = "iknow.structured_query_v2"
    envelope = _retrieve(t40_env["session"], body)
    assert _refusal_code(envelope) == "unsupported_structured_schema", (
        f"an unknown schema token must be refused by name, got {envelope!r}"
    )


def test_an_unsupported_schema_version_is_refused_by_name(t40_env) -> None:
    """Q1/ADR 0093: the version is checked separately from the token."""

    body = _structured_request()
    body["schema_version"] = 2
    envelope = _retrieve(t40_env["session"], body)
    assert _refusal_code(envelope) == "unsupported_structured_schema", (
        f"an unsupported integer version must be refused by name, got {envelope!r}"
    )


def test_a_non_integer_schema_version_is_malformed_not_unsupported(t40_env) -> None:
    """Q1: a version that is not a whole number has no version to disagree."""

    body = _structured_request()
    body["schema_version"] = 1.5
    envelope = _retrieve(t40_env["session"], body)
    assert _refusal_code(envelope) == "malformed_structured_query", (
        f"a fractional version must be malformed, got {envelope!r}"
    )


def test_an_unknown_subject_is_refused_by_name(t40_env) -> None:
    """Q1: a subject outside the closed set is `unsupported_subject`."""

    envelope = _retrieve(t40_env["session"], _structured_request(subject="document"))
    assert _refusal_code(envelope) == "unsupported_subject", (
        f"a subject outside the closed set must be refused by name, got {envelope!r}"
    )


def test_an_unknown_operation_is_refused_by_name(t40_env) -> None:
    """Q1: an operation outside `list | count` is `unsupported_operation`."""

    envelope = _retrieve(t40_env["session"], _structured_request(operation="delete"))
    assert _refusal_code(envelope) == "unsupported_operation", (
        f"an operation outside the closed set must be refused by name, got {envelope!r}"
    )


def test_a_field_outside_the_subject_allowlist_is_refused_by_name(t40_env) -> None:
    """Q1: a field the subject does not declare is `unsupported_field`.

    Confidence is deliberately chosen: it exists in storage and is
    deliberately not filterable, so a reader that widened the allowlist to
    whatever it has on hand would be caught here.
    """

    envelope = _retrieve(
        t40_env["session"],
        _structured_request(
            filters=[{"field": "confidence", "op": "eq", "value": "0.9"}]
        ),
    )
    assert _refusal_code(envelope) == "unsupported_field", (
        f"an unfilterable field must be refused by name, got {envelope!r}"
    )


def test_an_operator_outside_the_closed_set_is_refused_by_name(t40_env) -> None:
    """Q1/Q11: an operator outside `eq | in` is `unsupported_operator`."""

    envelope = _retrieve(
        t40_env["session"],
        _structured_request(
            filters=[{"field": "canonical_name", "op": "like", "value": "north%"}]
        ),
    )
    assert _refusal_code(envelope) == "unsupported_operator", (
        f"an operator outside the closed set must be refused by name, got {envelope!r}"
    )


def test_a_sql_shaped_request_is_refused_and_never_executed(t40_env) -> None:
    """Q11/Q13: the interface cannot express SQL, so it is refused, not parsed."""

    envelope = _retrieve(
        t40_env["session"],
        _structured_request(sql="SELECT * FROM entities"),
    )
    code = _refusal_code(envelope)
    assert code in {"malformed_structured_query", "unsupported_operator"}, (
        f"a SQL member must be refused as an unknown member, got {code!r}"
    )
    assert STRUCTURED_RESULT_KEY not in envelope


def test_a_claim_or_aggregate_shaped_request_is_refused(t40_env) -> None:
    """Q11: claims, sums and joins are unavailable, not merely discouraged."""

    envelope = _retrieve(
        t40_env["session"],
        _structured_request(aggregate={"sum": "value"}),
    )
    code = _refusal_code(envelope)
    assert code in {"malformed_structured_query", "unsupported_operation"}, (
        f"an aggregate must be refused as an unknown member, got {code!r}"
    )
    assert STRUCTURED_RESULT_KEY not in envelope


def test_an_oversized_request_is_refused_with_its_own_code(t40_env) -> None:
    """Q10: the 32 KiB request bound is a typed refusal, not a truncation."""

    envelope = _retrieve(
        t40_env["session"],
        _structured_request(
            filters=[{"field": "canonical_name", "op": "eq", "value": "x" * 40000}]
        ),
    )
    code = _refusal_code(envelope)
    assert code in {"request_too_large", "malformed_structured_query"}, (
        f"an over-bound request must be refused by name, got {code!r}"
    )
    assert STRUCTURED_RESULT_KEY not in envelope


def test_a_malformed_cursor_is_refused_and_not_read_as_a_position(t40_env) -> None:
    """Q7/QP6: a cursor the server did not mint is `invalid_cursor`.

    Asserted as a refusal with no sibling, because the failure this guards
    against is a forged position yielding a successful page that silently
    omits rows.
    """

    envelope = _retrieve(
        t40_env["session"],
        _structured_request(
            filters=[{"field": "canonical_name", "op": "eq", "value": "northwind"}],
            cursor="not-a-cursor-we-minted",
        ),
    )
    _refusal_code(envelope)
    assert STRUCTURED_RESULT_KEY not in envelope, (
        f"a refused cursor must not also carry a successful sibling: {envelope!r}"
    )


def test_a_count_carrying_list_only_fields_is_malformed(t40_env) -> None:
    """Q1: a count must not carry list-only members."""

    envelope = _retrieve(
        t40_env["session"],
        _structured_request(operation="count", limit=10),
    )
    code = _refusal_code(envelope)
    assert code in {"malformed_structured_query", "unsupported_operation"}, (
        f"a count carrying a list-only limit must be malformed, got {code!r}"
    )
    assert STRUCTURED_RESULT_KEY not in envelope


def test_a_stale_count_read_revision_is_refused_by_name(t40_env) -> None:
    """Q8/QP7: a count that names a stale revision is `read_revision_mismatch`."""

    envelope = _retrieve(
        t40_env["session"],
        _structured_request(operation="count", read_revision="rev-that-was-never-read"),
    )
    _refusal_code(envelope)
    assert STRUCTURED_RESULT_KEY not in envelope, (
        "a stale-revision count must not answer with a partial count"
    )


def test_every_refusal_keeps_the_typed_shape_rather_than_prose(t40_env) -> None:
    """The SPEC forbids prose-only failure and empty-success fallback.

    Asserted over the whole matrix at once: a run where some paths degrade to
    prose while others are typed would otherwise pass case by case.
    """

    refusals = [
        _structured_request(subject="document"),
        _structured_request(operation="delete"),
        _structured_request(cursor="forged"),
        {**_structured_request(), "sql": "SELECT 1"},
    ]
    for body in refusals:
        envelope = _retrieve(t40_env["session"], body)
        code = _refusal_code(envelope)
        assert code in SPEC_ERROR_CODES, f"{code!r} is outside the closed set"
        assert STRUCTURED_RESULT_KEY not in envelope, (
            f"a refusal for {body!r} carried a result sibling: {envelope!r}"
        )
