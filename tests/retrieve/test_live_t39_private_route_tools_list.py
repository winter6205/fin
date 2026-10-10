"""T39 (EP3/EP4): live private-``corpus_evidence`` route + ``tools/list`` freeze.

This driver exists to close one evidence gap that the 2026-10-05 T39 decision
record left open: **the observed half of "expected/observed ``tools/list``"**
(``docs/validation/2026-10-05-t39-private-evidence-transport-decision.md``,
D4 and open-unknown 3). That record froze the *expected* eight tool names by
reading IK's source; it never ran a server. This file runs one and freezes the
baseline as an observation.

What this driver exercises, on the real boundary:

* the **actual** ``MCPRetrieveAdapter`` from ``src/fin/retrieve/``, not a fake;
* the **built** IK MCP process (``dist/index.js``) spawned as a child, not an
  in-process handler or a fixture server;
* **one shared persistent** ``MCPStdioSession`` — the retrieve leg and the
  Evidence-route probe ride the same session, which is the shape D3 froze;
* **real, isolated PostgreSQL** (pgvector storage mode) in a per-run schema
  that is dropped CASCADE at teardown.

Why the tests assert what they assert
-------------------------------------
T39's contract is that the Evidence capability is a **private, non-tool**
JSON-RPC request method on the existing stdio connection, so the model-visible
tool listing must not change. At the IK pin this driver is bound to, IK
**registers** the ``corpus_evidence`` dispatcher but refuses it: this driver
was first frozen against a pin with no dispatcher at all, and EP3 has since
landed. What the live run therefore shows, and what it does not:

* ``corpus_evidence`` answers with a JSON-RPC **result** carrying a typed
  in-envelope refusal, not with a tool result and not with method-not-found.
  This is a **negative** observation: the dispatch reached the private
  channel and the channel refused it. It shows nothing about a *granted*
  read, which needs a real trusted binding and a real grant;
* the tool listing observed **before** and **after** that dispatch is
  byte-identical, which is the exact invariant T13/T14 must keep holding.

A tool-**name** comparison is not sufficient on its own, and this driver does
not rely on it alone. What it freezes instead is the per-tool top-level
``inputSchema`` **property keys** (see
``test_input_schema_property_keys_are_frozen_per_tool``), because a new
model-visible switch can be added to an existing tool's schema without
registering, renaming or removing anything. Any added, removed or reordered
top-level key fails there.

The nested ``retrieve.filters`` member is frozen **verbatim**, by deep equality
over the whole member. That member is the concrete reason the name diff cannot
work, so it must not be weakened: at the bound build ``retrieve`` publishes an
**object-valued** filter value — ``filters.structured_query`` reaches the wire
as ``additionalProperties.anyOf = [{"type": "string"}, {"type": "object", …}]``.
A structured-read switch is therefore already model-visible *inside* an existing
tool, with no tool added, renamed or removed, which is exactly the change a
name diff is blind to.

Build binding — why this driver pins a commit
---------------------------------------------
``live_iknow_support.IKNOW_ROOT`` silently falls back to the main IK checkout
when ``FIN_LIVE_IKNOW_ROOT`` is unset. T39 is bound to one build
(``BOUND_IKNOW_COMMIT``), and two different builds otherwise answer the same
frozen command — which is how an earlier round of this file came to assert a
``filters`` shape taken from the *unpinned* build. ``live_iknow_support`` is
shared with other Fin live tests and is not this file's to change, so the guard
lives here instead: the resolved build root's git HEAD is read at runtime and
compared against the bound commit, and any mismatch — or an unreadable or
absent build — is a named non-zero failure naming both the expected and the
observed SHA. A green run against an unbound build is no longer reachable.

Strict dependency behaviour
---------------------------
This driver carries both ``mcp`` and ``sql_live``. A missing build, a missing
Node, or an unreachable/unusable PostgreSQL always exits **non-zero** and never
skips: the opt-in mark contract in ``pyproject.toml`` treats an absent live
dependency as blocked evidence, and a green-skip would misreport an unobserved
boundary as a pass. There is deliberately no ``pytest.skip`` call in this file.
The three paths differ in *how* they fail, and the difference is deliberate:

* missing build — named ``pytest.fail`` naming the entry file and the build step;
* missing Node — raised by ``live_iknow_support._node_executable`` as a bare
  ``AssertionError`` naming ``IKNOW_NODE``; that helper is not this file's to
  change, and a transport-level refusal is a loud failure too;
* unreachable or unusable PostgreSQL — wrapped here into a named
  ``pytest.fail`` naming ``FIN_LIVE_IKNOW_DB_URL``, because a bare
  ``psycopg.OperationalError`` does not say which dependency is missing;
* unbound IK build — named ``pytest.fail`` from ``_require_bound_iknow_build``
  naming the expected commit and the observed one (or the absence of a readable
  HEAD), so a run against the wrong build cannot report green.

The driver records nothing about secrets: the DSN is read from
``FIN_LIVE_IKNOW_DB_URL`` and only its *shape* (host/port/database) is
asserted, never its value.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import uuid
import warnings
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator

import pytest

import psycopg

from fin.retrieve.mcp_adapter import MCPRetrieveAdapter
from fin.retrieve.mcp_session import MCP_PROTOCOL_VERSION
from fin.retrieve.mcp_session import MCPStdioSession
from fin.retrieve.mcp_session import MCPStdioTransportError

from live_iknow_support import (
    EMBED_DIMENSIONS,
    IKNOW_ROOT,
    _EmbeddingHandler,
    _node_executable,
)


# Both opt-in marks: this is a real MCP process AND a real PostgreSQL. Run
# with ``-m "mcp and sql_live"``; see the record for the frozen invocation.
pytestmark = [pytest.mark.mcp, pytest.mark.sql_live]

# The IK build T39 is bound to, and the single point of re-binding. Everything
# frozen below — the eight names, the per-tool property keys, the nested
# ``filters`` member, and the typed private refusal — was observed on *this*
# build. A different build may legitimately publish a different schema, and a
# re-bind must be a deliberate one-edit act recorded in the ticket record, not
# something a stray environment variable can do silently.
#
# The matching worktree is ``/home/winner/projects/iknow-memory-wt-s0-authority``
# (the S0 candidate); export ``FIN_LIVE_IKNOW_ROOT`` to that path when running
# this driver. ``live_iknow_support.IKNOW_ROOT`` falls back to the main IK
# checkout at ``eea1e07`` when that variable is unset, and the two builds answer
# the same commands differently — hence the runtime guard below.
BOUND_IKNOW_COMMIT = "5a47db8d982ed6688111a41bb79bd85aeb0cadb3"

# The expected tool listing, read from IK's ``buildRegistry([...])`` at the
# bound commit (T39 record D4). Asserted as an ordered list because the
# registration order at ``src/index.ts:581-604`` is itself part of the frozen
# baseline.
EXPECTED_TOOL_NAMES: tuple[str, ...] = (
    "ingest",
    "invalidate",
    "push",
    "extract",
    "retrieve",
    "read_source",
    "submit_index_job",
    "poll_index_job",
)

# The per-tool top-level ``inputSchema`` property keys, frozen as an ordered
# tuple per tool and re-derived from a live ``tools/list`` at the bound IK
# commit — not copied from the record this driver came with. Key *order* is
# frozen alongside the key *set* because a reorder is a change to the
# model-visible surface too.
#
# This table, not the tool-name tuple above, is the guard that matters for
# T39's exclusion requirement. It is a freeze of the published schema, not an
# inference from it: a new model-visible input can be added to an existing
# tool's schema without registering, renaming or adding a tool, and only a
# property-key freeze sees that.
#
# ``ingest`` and ``push`` legitimately publish no properties at all, so their
# tuple is empty rather than absent.
EXPECTED_TOOL_INPUT_PROPERTIES: dict[str, tuple[str, ...]] = {
    "ingest": (),
    "invalidate": ("source_ref", "invalidated_at", "triggered_by"),
    "push": (),
    "extract": ("chunk_ids", "source_ref"),
    "retrieve": (
        "query",
        "valid_at",
        "filters",
        "limit",
        "preset",
        "wait_for_drain",
        "wait_for_drain_timeout_ms",
    ),
    "read_source": ("source_ref", "id", "position"),
    "submit_index_job": ("source_ref", "content_identity"),
    "poll_index_job": ("job_id",),
}

# The private capability name fixed by the Evidence spec, and the identity
# shape that must never arrive on the model-reachable plane.
PRIVATE_METHOD = "corpus_evidence"

# The envelope a private Evidence refusal arrives in. It is a JSON-RPC result,
# not a JSON-RPC error: the method exists and refuses, which is the whole point
# of EP3 having landed since this driver was first frozen.
EVIDENCE_RESULT_SCHEMA = "iknow.evidence_result"

# ``retrieve``'s ``filters`` member, frozen verbatim from a live ``tools/list``
# at the bound IK commit (``BOUND_IKNOW_COMMIT``) — not copied from the record,
# not read off the Zod source. Member order is the published order, and the
# whole member is pinned by deep equality rather than one hand-picked sub-key,
# so a change anywhere inside it fails here.
#
# This member is the evidence for T39's whole point. It publishes an
# **object-valued** filter key on the wire: ``additionalProperties.anyOf`` has
# a second ``{"type": "object", …}`` branch alongside ``{"type": "string"}``.
# That object branch is how ``filters.structured_query`` — a model-visible
# structured-read switch, already present at this build — reaches any host that
# reads the published schema. It exists inside an **existing** tool, with no
# tool added, renamed or removed, which is precisely the change a
# before/after ``tools/list`` **name** diff cannot detect.
#
# An earlier round of this file probed the *unpinned* default build, read the
# string-only form there, concluded no object branch existed, and replaced this
# value with ``{"type": "string"}``. That was a regression, not a correction:
# the unpinned build is ``eea1e07``, not the build T39 is bound to. The value
# below is what ``BOUND_IKNOW_COMMIT`` actually publishes.
EXPECTED_RETRIEVE_FILTERS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "propertyNames": {"type": "string"},
    "additionalProperties": {
        "anyOf": [
            {"type": "string"},
            {
                "type": "object",
                "propertyNames": {"type": "string"},
                "additionalProperties": {},
            },
        ]
    },
}

_REQUEST_TIMEOUT_SECONDS = 60.0

# A schema name is interpolated into DDL, so it is generated here and then
# re-validated against the same closed shape ``live_iknow_support`` uses.
_PG_SCHEMA_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


def _git_head_sha(root: Path) -> str | None:
    """The exact commit of the build root at ``root``, or ``None``.

    ``None`` covers every "cannot be established" case uniformly: no such
    directory, not a git checkout, a git binary that is missing or fails, or a
    HEAD that does not resolve. Callers turn that single sentinel into one
    named failure, so an unreadable build cannot be mistaken for a matching
    one.
    """

    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        # No git on PATH, the directory is unreadable, or git hung. All three
        # mean the build's identity could not be established.
        return None
    if completed.returncode != 0:
        return None
    sha = completed.stdout.strip()
    return sha or None


def _require_bound_iknow_build() -> None:
    """Fail unless the resolved IK build **is** the build T39 is bound to.

    This is the root-cause guard. ``live_iknow_support`` resolves the IK build
    from ``FIN_LIVE_IKNOW_ROOT`` and **silently falls back** to the main
    checkout when it is unset, so an unpinned run exercises a different build
    than the ticket is bound to — two builds answering one frozen command. That
    fallback is shared with other Fin live tests and is not this file's to
    change, so the check is enforced here, where the ticket owns the file.

    It is deliberately run as an autouse fixture *and* at the head of
    ``_live_private_route``: the fixture makes it impossible for a future test
    in this file to skip the guard, and the direct call means the failure
    happens before a schema, an embedding server or a child process exists.
    """
    observed = _git_head_sha(IKNOW_ROOT)
    if observed is None:
        pytest.fail(
            "T39 IK build binding: the resolved build root "
            f"{IKNOW_ROOT} has no readable git HEAD, so its commit cannot be "
            f"confirmed to be the bound commit {BOUND_IKNOW_COMMIT}. T39 is "
            f"bound to {BOUND_IKNOW_COMMIT}; export FIN_LIVE_IKNOW_ROOT to the "
            "worktree that holds it "
            "(/home/winner/projects/iknow-memory-wt-s0-authority) and confirm "
            "its dist/index.js is built. Observed: no readable HEAD at "
            f"{IKNOW_ROOT}. This is a named failure on purpose, not a skip."
        )
    if observed != BOUND_IKNOW_COMMIT:
        pytest.fail(
            "T39 IK build binding: this driver is bound to IK commit "
            f"{BOUND_IKNOW_COMMIT}, but the resolved build root {IKNOW_ROOT} "
            f"is at {observed}. Every expectation in this file was observed on "
            f"{BOUND_IKNOW_COMMIT}; a run against another build tests a "
            "different boundary and must not report green. Expected: "
            f"{BOUND_IKNOW_COMMIT}. Observed: {observed}. Set "
            "FIN_LIVE_IKNOW_ROOT to the bound worktree, or re-bind "
            "BOUND_IKNOW_COMMIT as a deliberate, recorded one-line change. "
            "This is a named failure on purpose, not a skip."
        )


@pytest.fixture(autouse=True)
def _bound_iknow_build_guard() -> Iterator[None]:
    """Every item in this file refuses to run against an unbound IK build."""

    _require_bound_iknow_build()
    yield


def _pg_url() -> str:
    """The strict PostgreSQL DSN, by variable name only.

    A missing variable is a **named failure**, never a skip: T39 requires a
    real isolated PostgreSQL and a driver that cannot find one has proved
    nothing.
    """

    url = os.environ.get("FIN_LIVE_IKNOW_DB_URL")
    if not url:
        pytest.fail(
            "FIN_LIVE_IKNOW_DB_URL is unset. T39 requires a real isolated "
            "PostgreSQL; point it at a live pgvector instance. This is a "
            "named failure on purpose, not a skip."
        )
    return url


def _schema_name() -> str:
    """A per-run, fin-owned schema name."""

    name = f"t39_route_{uuid.uuid4().hex[:16]}"
    if not _PG_SCHEMA_RE.match(name):  # pragma: no cover - generated shape
        pytest.fail(f"generated an illegal schema name: {name!r}")
    return name


def _db_url_for_schema(db_url: str, schema: str) -> str:
    """Same endpoint, every connection pinned at ``schema`` via libpq."""

    if not _PG_SCHEMA_RE.match(schema):
        raise ValueError(f"unsafe pg_schema name: {schema!r}")
    separator = "&" if "?" in db_url else "?"
    return f"{db_url}{separator}options=-c%20search_path%3D{schema}%2Cpublic"


@contextmanager
def _pg_connect() -> Iterator[Any]:
    """A real connection to the isolated PostgreSQL, or a named failure.

    A missing variable is already a **named failure** (see ``_pg_url``). An
    *unreachable or unusable* server is the other half of the same boundary:
    left bare it surfaces as ``psycopg.OperationalError``, which says that
    something is wrong without saying which dependency is missing. It is
    wrapped here so the failure names ``FIN_LIVE_IKNOW_DB_URL``, and so the
    driver keeps a single named-failure shape across all three strict
    dependencies. The DSN's *value* is never recorded — the variable name
    only.
    """

    try:
        connection = psycopg.connect(_pg_url(), autocommit=True)
    except psycopg.OperationalError as error:
        pytest.fail(
            f"FIN_LIVE_IKNOW_DB_URL does not reach a usable PostgreSQL: "
            f"{type(error).__name__}. T39 requires a real isolated PostgreSQL; "
            "check the host, port and credentials in that variable. This is a "
            "named failure on purpose, not a skip."
        )
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def _create_pg_schema(schema: str) -> None:
    if not _PG_SCHEMA_RE.match(schema):
        raise ValueError(f"unsafe pg_schema name: {schema!r}")
    with _pg_connect() as connection:
        connection.execute(f"CREATE SCHEMA {schema}")


def _drop_pg_schema(schema: str) -> None:
    """Teardown of this run's schema only — never anything in ``public``."""

    if not _PG_SCHEMA_RE.match(schema):
        raise ValueError(f"unsafe pg_schema name: {schema!r}")
    with _pg_connect() as connection:
        connection.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")


def _input_properties(tools: list[dict[str, Any]]) -> dict[str, tuple[str, ...]]:
    """Per-tool top-level ``inputSchema`` property keys, in published order.

    A tool with no ``properties`` member yields an empty tuple rather than
    raising, so a schema that drops the member entirely is reported as a key
    change instead of an opaque ``KeyError``.
    """

    properties: dict[str, tuple[str, ...]] = {}
    for tool in tools:
        schema = tool.get("inputSchema") or {}
        assert isinstance(schema, dict), tool.get("name")
        declared = schema.get("properties") or {}
        assert isinstance(declared, dict), tool.get("name")
        properties[tool["name"]] = tuple(declared.keys())
    return properties


@contextmanager
def _live_private_route(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """One real IK MCP process over real isolated PostgreSQL.

    Mirrors ``live_iknow_support``'s environment shape but selects IK's
    ``pgvector`` storage mode directly instead of its ``pg_es`` mode: this
    ticket needs real PostgreSQL as the retained-record backing store, and
    pgvector needs neither Elasticsearch nor Neo4j, both of which are
    unavailable in this environment. BM25 / graph / rerank are disabled, which
    is the ADR-sanctioned vector-only escape hatch (see the same comment in
    ``live_iknow_support``).

    The environment is installed with ``monkeypatch.setenv`` rather than an
    ``env=`` argument because ``MCPStdioSession`` spawns its child **without**
    one (``subprocess.Popen(self._command, cwd=..., bufsize=0)``), so the child
    inherits the parent process environment. That inheritance is itself part of
    what T39's trusted-identity decision has to account for.

    Setup is inside the ``try`` and every teardown step is guarded on its own,
    so a failure part-way through setup cannot leak the schema or the stub
    server that were already created, and one failing cleanup step cannot skip
    the rest. A cleanup problem is reported on its own when the run would
    otherwise have passed, and as a warning when the run already failed for a
    more informative reason, so it never masks the primary failure.
    """

    # Before anything is created. The build binding is checked here as well as
    # in the autouse fixture so that an unbound build fails before a schema, an
    # embedding server or a child IK process exists.
    _require_bound_iknow_build()

    entry = IKNOW_ROOT / "dist" / "index.js"
    if not entry.is_file():
        pytest.fail(
            f"{entry} is missing; run `npm run build` in {IKNOW_ROOT}. T39 "
            "requires the built IK MCP process, so this is a named failure "
            "rather than a skip."
        )

    schema = _schema_name()
    embedding_server: ThreadingHTTPServer | None = None
    thread: threading.Thread | None = None
    session: MCPStdioSession | None = None
    primary: BaseException | None = None
    try:
        _create_pg_schema(schema)

        embedding_server = ThreadingHTTPServer(("127.0.0.1", 0), _EmbeddingHandler)
        thread = threading.Thread(
            target=embedding_server.serve_forever,
            name="fin-t39-embedding",
            daemon=True,
        )
        thread.start()

        for key, value in {
            "GRAPHRAG_MEMORY_STORAGE": "pgvector",
            "GRAPHRAG_MEMORY_DB_URL": _db_url_for_schema(_pg_url(), schema),
            "GRAPHRAG_MEMORY_EMBED_DIMENSIONS": str(EMBED_DIMENSIONS),
            "GRAPHRAG_MEMORY_PGVECTOR_ANN_MAX_DIMENSIONS": "2000",
            "GRAPHRAG_MEMORY_EMBED_API_KEY": "fin-live-test",
            "GRAPHRAG_MEMORY_EMBED_BASE_URL": (
                f"http://127.0.0.1:{embedding_server.server_port}/v1"
            ),
            "GRAPHRAG_MEMORY_EMBED_MODEL": "fin-live-test",
            "GRAPHRAG_MEMORY_RETRIEVAL_BM25_ENABLED": "false",
            "GRAPHRAG_MEMORY_RETRIEVAL_GRAPH_ENABLED": "false",
            "GRAPHRAG_MEMORY_RETRIEVAL_RERANK_ENABLED": "false",
            "GRAPHRAG_MEMORY_NEO4J_URI": "",
            "GRAPHRAG_MEMORY_RETRIEVAL_RERANK_URL": "",
            "ANTHROPIC_AUTH_TOKEN": "",
        }.items():
            monkeypatch.setenv(key, value)

        session = MCPStdioSession(
            [_node_executable(), str(entry)],
            cwd=str(IKNOW_ROOT),
            timeout_seconds=_REQUEST_TIMEOUT_SECONDS,
        )
        # The real adapter, bound to the shared persistent session.
        retrieve = MCPRetrieveAdapter(session=session, timeout_seconds=60.0)
        yield {"session": session, "retrieve": retrieve, "schema": schema}
    except BaseException as error:  # noqa: BLE001 - re-raised after teardown
        # Captured so a teardown problem can be reported without replacing the
        # failure that actually ended the run.
        primary = error
        raise
    finally:
        cleanup_errors = _teardown(
            schema,
            session=session,
            embedding_server=embedding_server,
            thread=thread,
        )
        if cleanup_errors:
            report = f"T39 teardown left resources behind: {cleanup_errors}"
            if primary is None:
                pytest.fail(report)
            # The run already failed for a more informative reason; surface
            # the leak in the summary rather than masking that failure.
            warnings.warn(report, stacklevel=2)


def _teardown(
    schema: str,
    *,
    session: MCPStdioSession | None,
    embedding_server: ThreadingHTTPServer | None,
    thread: threading.Thread | None,
) -> list[str]:
    """Release everything this run created; return what could not be released.

    Each step is guarded on its own so one failing step cannot strand the
    resources after it, and the child session is closed before the schema it
    is using is dropped. The schema is attempted even when the stub server or
    the session was never constructed, so a failure part-way through setup
    still drops what setup did create.
    """

    errors: list[str] = []
    if session is not None:
        try:
            session.close()
        except Exception as error:  # noqa: BLE001 - one step must not strand the next
            errors.append(f"session.close: {type(error).__name__}")
    if embedding_server is not None:
        try:
            embedding_server.shutdown()
        except Exception as error:  # noqa: BLE001 - one step must not strand the next
            errors.append(f"embedding shutdown: {type(error).__name__}")
        try:
            embedding_server.server_close()
        except Exception as error:  # noqa: BLE001 - one step must not strand the next
            errors.append(f"embedding close: {type(error).__name__}")
    if thread is not None:
        try:
            thread.join(timeout=5)
        except Exception as error:  # noqa: BLE001 - one step must not strand the next
            errors.append(f"embedding thread join: {type(error).__name__}")
        if thread.is_alive():
            # A live stub thread would hold the process open past the run and
            # keep the run's schema alive with it.
            errors.append("embedding thread outlived the 5s join")
    try:
        _drop_pg_schema(schema)
    except Exception as error:  # noqa: BLE001 - a leaked schema must be reported
        errors.append(f"drop schema: {type(error).__name__}")
    return errors


def _tools_list(live: Any, request_id: int) -> list[dict[str, Any]]:
    """One ``tools/list`` on the shared session; the handshake proves itself."""

    response = live["session"].call(
        {"jsonrpc": "2.0", "id": request_id, "method": "tools/list"},
        _REQUEST_TIMEOUT_SECONDS,
    )
    assert "error" not in response, response
    tools = response["result"]["tools"]
    assert isinstance(tools, list), tools
    return tools


def _assert_private_dispatch_refused(response: dict[str, Any], probe_id: int) -> None:
    """A private dispatch carries no content, whatever shape the refusal takes.

    At this bound the private method is registered, so a refusal arrives as a
    JSON-RPC *result* whose ``error`` member is a typed in-envelope refusal
    rather than a JSON-RPC method-not-found. That is a weaker observation than
    the one this file froze when no dispatcher existed, so it is stated
    positively instead: the dispatch must not yield any content member, and the
    refusal must name itself with an in-set code and a reason.
    """
    assert "result" in response, response
    result = response["result"]
    assert result.get("schema") == EVIDENCE_RESULT_SCHEMA, result
    refusal = result.get("error")
    assert isinstance(refusal, dict), result
    assert refusal.get("code"), result
    assert refusal.get("reason"), result
    assert refusal.get("retryable") is False, result
    for content_member in ("value", "result_count", "receipt", "supports", "row"):
        assert content_member not in result, (
            f"the private dispatch at probe {probe_id} disclosed {content_member}: {result}"
        )
    assert "id" in response and response["id"] == probe_id, response


def test_observed_tools_list_matches_expected_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OBSERVED baseline: the real IK process advertises exactly the eight
    tools D4 expected, in the registry's own order.

    This is the missing observed half of the return evidence. A ``tools/list``
    answer can only arrive after ``MCPStdioSession``'s ``initialize``
    handshake succeeded, so it also proves the handshake against the real
    stdio process.
    """

    with _live_private_route(monkeypatch) as live:
        tools = _tools_list(live, 100)
        observed = tuple(tool["name"] for tool in tools)
        assert observed == EXPECTED_TOOL_NAMES, (
            f"observed tools/list {observed} != expected {EXPECTED_TOOL_NAMES}"
        )
        for tool in tools:
            assert tool.get("description"), tool["name"]
            assert tool.get("inputSchema"), tool["name"]

        # The retrieve leg runs through the real adapter on the SAME session,
        # which is the shared-persistent-session shape D3 froze. An empty
        # retained set is a valid typed answer here: this ticket proves the
        # leg reaches the wire, it does not assert corpus content.
        result = live["retrieve"].retrieve("operating revenue", limit=1)
        assert isinstance(result, dict), result


def test_input_schema_property_keys_are_frozen_per_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real regression guard for T39's exclusion requirement: every
    tool's top-level ``inputSchema`` property keys, asserted **exactly**.

    Why this test exists rather than the name comparison next door: the
    name comparison cannot detect the change T39 most has to exclude. A
    property-key freeze sees it. Any top-level key added to or removed from
    any tool's schema fails here, and so does a reordering, because the
    published key order is frozen too. The table this asserts was re-derived
    from a live ``tools/list`` at the bound IK commit rather than copied from
    the record, which is why ``ingest`` and ``push`` carry an empty tuple:
    they publish no ``properties`` member at all.

    The nested ``retrieve.filters`` assertion below is the observation that
    gives this table its point. At the bound build ``retrieve`` **does**
    publish an object-valued filter key: ``filters.structured_query`` is
    already model-reachable through a value the published schema admits as an
    object. That is a model-visible structured-read switch sitting inside an
    existing tool, added without registering, renaming or removing any tool —
    so a before/after ``tools/list`` **name** comparison stays green straight
    through the change this table exists to catch. Freezing the per-tool
    property keys is what makes the exclusion checkable instead of assumed.

    An earlier round of this file asserted the opposite here — that the live
    ``filters`` member was string-valued, with no object branch — and replaced
    the nested assertion accordingly. That was a **regression**: it was probed
    on the unpinned default IK build rather than on ``BOUND_IKNOW_COMMIT``.
    The observation below is the bound build's, and reverting the claim is
    restoring the evidence rather than adding to it.

    ``retrieve`` legitimately keeps its ``filters`` key regardless. What is
    excluded is the Evidence channel reaching the model plane through it or
    through any other model-visible input; that is a binding constraint on
    T13/T14, and this test is the mechanical half of it.
    """

    with _live_private_route(monkeypatch) as live:
        tools = _tools_list(live, 120)
        observed = _input_properties(tools)
        assert observed == EXPECTED_TOOL_INPUT_PROPERTIES, (
            "a tool's top-level inputSchema property keys changed; the model-"
            "visible surface is no longer the frozen one. observed="
            f"{observed} expected={EXPECTED_TOOL_INPUT_PROPERTIES}"
        )

        # The same capture must also hold across a private-route dispatch:
        # the private axis must not perturb any published schema either.
        response = live["session"].call(
            {
                "jsonrpc": "2.0",
                "id": 121,
                "method": PRIVATE_METHOD,
                "params": {"action": "read", "evidence_ref": "t39-probe"},
            },
            _REQUEST_TIMEOUT_SECONDS,
        )
        _assert_private_dispatch_refused(response, 121)
        assert _input_properties(_tools_list(live, 122)) == (
            EXPECTED_TOOL_INPUT_PROPERTIES
        )

        # ``retrieve``'s filters member is frozen verbatim above. It admits an
        # **object** value as well as a string one, which is what makes
        # ``filters.structured_query`` model-visible on this build — and
        # therefore what makes a name diff insufficient. Freezing the whole
        # member keeps the object branch itself from being widened.
        retrieve = next(tool for tool in tools if tool["name"] == "retrieve")
        filters = retrieve["inputSchema"]["properties"]["filters"]
        assert filters == EXPECTED_RETRIEVE_FILTERS_SCHEMA, (
            "retrieve's published filters member changed shape on the bound "
            f"build {BOUND_IKNOW_COMMIT}; a model-visible structured-read "
            f"input was added, removed or widened. observed={filters} "
            f"expected={EXPECTED_RETRIEVE_FILTERS_SCHEMA}"
        )


def test_private_method_is_not_a_tool_and_listing_is_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``corpus_evidence`` is not a registered tool, and a call to it leaves
    ``tools/list`` byte-identical.

    This is the core T39 invariant. The call below is attempted against the
    real process and must answer with a JSON-RPC method error rather than a
    tool result — that is what shows no such tool is registered. The listing
    captured before and after must be deeply equal, and must not contain the
    private method or any evidence/search-shaped tool.

    What this does **not** show, and the record no longer claims: that the
    capability is routed on the method axis. At this pin there is no
    dispatcher, so the request is *refused*, not routed. The evidence that
    the private axis is the only permitted route is the D1 decision plus the
    frozen schema keys in
    ``test_input_schema_property_keys_are_frozen_per_tool``.
    """

    with _live_private_route(monkeypatch) as live:
        before = _tools_list(live, 200)

        response = live["session"].call(
            {
                "jsonrpc": "2.0",
                "id": 201,
                "method": PRIVATE_METHOD,
                "params": {"action": "read", "evidence_ref": "t39-probe"},
            },
            _REQUEST_TIMEOUT_SECONDS,
        )

        # IK at this pin registers the private dispatcher. A *content-bearing*
        # result would mean an evidence tool became model-reachable and must
        # fail the run loudly; a typed in-envelope refusal is the expected
        # observation.
        _assert_private_dispatch_refused(response, 201)

        after = _tools_list(live, 202)

        assert after == before, "tools/list changed across a private dispatch"
        names = tuple(tool["name"] for tool in after)
        assert PRIVATE_METHOD not in names, names
        assert not [name for name in names if "evidence" in name], names
        assert not [name for name in names if "search" in name], names


def _tools_list_for_forged_client() -> list[str]:
    """Tool names the real process hands to a client that asserts a forged
    ``clientInfo``.

    This is the decisive observation for D2's rejected alternative. Fin's only
    identity-shaped field on the wire is the static ``clientInfo`` constant it
    asserts at ``initialize``. If the server treated that field as an identity,
    a forged value would change what the server returns. It does not, so
    ``clientInfo`` carries no authority and cannot be the trusted identity
    source.
    """

    entry = IKNOW_ROOT / "dist" / "index.js"
    process = subprocess.Popen(
        [_node_executable(), str(entry)],
        cwd=str(IKNOW_ROOT),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )
    try:
        def send(message: dict[str, Any]) -> None:
            assert process.stdin is not None
            process.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
            process.stdin.flush()

        send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    # Not fin. Asserted freely by whoever opens the pipe.
                    "clientInfo": {"name": "not-fin-at-all", "version": "9.9.9"},
                },
            }
        )
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})

        assert process.stdout is not None
        lines: list[dict[str, Any]] = []
        deadline = time.monotonic() + _REQUEST_TIMEOUT_SECONDS
        while time.monotonic() < deadline and len(lines) < 2:
            raw = process.stdout.readline()
            if not raw:
                break
            if raw.strip():
                lines.append(json.loads(raw.decode("utf-8")))
        listing = next(line for line in lines if line.get("id") == 2)
        assert "error" not in listing, listing
        return [tool["name"] for tool in listing["result"]["tools"]]
    finally:
        process.kill()
        process.wait(timeout=10)


def test_trusted_identity_is_not_assertable_on_the_model_plane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No **model-visible input** can reach or identify the private route.

    Three separate obstructions, each observed on the real process:

    1. OBSERVED: the session's own wire log records only the explicit calls —
       the ``initialize`` frame carrying the client-asserted ``clientInfo``
       constant is written straight to the pipe and never recorded. Fin's only
       identity-shaped field is not even observable in the session log.
    2. OBSERVED on the real process: a client that asserts a completely
       different ``clientInfo`` still receives the identical full tool
       listing, so ``clientInfo`` carries no authority. It names the program,
       not a per-connection consumer, and cannot be the trusted identity
       source.
    3. OBSERVED: an identity-shaped extra key on a private-method request is
       refused as a method error and is never echoed back.

    None of these depends on a dispatcher existing; all three hold at this pin
    and must keep holding once T13/T14 add one.
    """

    with _live_private_route(monkeypatch) as live:
        # (1) No initialize frame in the recorded request log.
        _tools_list(live, 300)
        methods = [request.get("method") for request in live["session"].requests]
        assert "initialize" not in methods, methods
        assert methods, "the session recorded no requests at all"

        # (2) A forged clientInfo changes nothing the server returns.
        assert _tools_list_for_forged_client() == list(EXPECTED_TOOL_NAMES)

        # (3) A model-shaped attempt to smuggle an identity onto the private
        # route is refused, and the supplied identity is never echoed.
        response = live["session"].call(
            {
                "jsonrpc": "2.0",
                "id": 301,
                "method": PRIVATE_METHOD,
                "params": {
                    "action": "read",
                    "evidence_ref": "t39-probe",
                    "trusted_consumer": "fin-host",
                },
            },
            _REQUEST_TIMEOUT_SECONDS,
        )
        _assert_private_dispatch_refused(response, 301)
        assert "fin-host" not in json.dumps(response), response

        # The listing is still untouched after the attempt.
        assert tuple(tool["name"] for tool in _tools_list(live, 302)) == (
            EXPECTED_TOOL_NAMES
        )


def test_postgres_is_real_and_isolated_and_schema_is_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The retained-record backing store is a real PostgreSQL with pgvector,
    in a per-run schema this driver owns and removes.

    Nothing here is a fixture or an in-memory substitute: the live process was
    booted against this database in ``pgvector`` mode, so its tables live in
    this run's schema.
    """

    with _live_private_route(monkeypatch) as live:
        schema = live["schema"]
        # The IK process is spawned lazily on the first call, and its DDL runs
        # at boot, so the leg must actually run before the schema is inspected.
        _tools_list(live, 500)
        with _pg_connect() as connection:
            version = connection.execute("show server_version").fetchone()[0]
            extension = connection.execute(
                "select extversion from pg_extension where extname = 'vector'"
            ).fetchone()
            assert version is not None and version.split(".")[0] == "16", version
            assert extension is not None, (
                "pgvector extension is absent; T39 requires a real pgvector "
                "instance, not a substitute"
            )

            present = connection.execute(
                "select count(*) from information_schema.schemata "
                "where schema_name = %s",
                (schema,),
            ).fetchone()[0]
            assert present == 1, schema

            # Tables the booted IK process created inside this run's schema.
            owned = connection.execute(
                "select count(*) from information_schema.tables "
                "where table_schema = %s",
                (schema,),
            ).fetchone()[0]
            assert owned > 0, (
                "the real IK process created no table in the isolated schema; "
                "the pgvector backing store is not actually live"
            )

    # After the context exits, cleanup must be complete.
    with _pg_connect() as connection:
        leftover = connection.execute(
            "select count(*) from information_schema.schemata "
            "where schema_name = %s",
            (schema,),
        ).fetchone()[0]
    assert leftover == 0, f"schema {schema} survived teardown"


def test_session_close_is_the_owner_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Closing the shared session terminates the real child process, and the
    session refuses further calls afterwards.

    D3 froze "the owner closes, on every path". The observable half of that
    contract at this layer is: ``close()`` reaps the stdio child, and a closed
    session fails by name instead of silently spawning a replacement.
    """

    session_holder: dict[str, MCPStdioSession] = {}
    with _live_private_route(monkeypatch) as live:
        session = live["session"]
        session_holder["session"] = session
        _tools_list(live, 400)
        process = session._process
        assert process is not None
        assert process.poll() is None, "the real IK child died before close"

        session.close()

        assert process.poll() is not None, (
            "close() left the stdio child process running"
        )
        with pytest.raises(MCPStdioTransportError):
            session.call(
                {"jsonrpc": "2.0", "id": 401, "method": "tools/list"},
                _REQUEST_TIMEOUT_SECONDS,
            )

    # Teardown calls close() again; that must stay idempotent.
    session_holder["session"].close()