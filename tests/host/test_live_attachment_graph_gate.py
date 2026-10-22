"""T2f's durable graph gate, crossed over a real process boundary.

Why this file exists
--------------------
``tests/host/test_attachment_send_wait.py`` and
``tests/host/test_attachment_resume_worker.py`` prove the gate's contract on
``InMemorySaver`` + ``ScriptedChatModel`` inside one test process. That is the
right shape for the branch matrix, but the ticket's claims are about
*surviving a death*, and an in-process graph cannot answer them:

* does a run suspended on the preparation gate still exist, with the SAME
  server-minted run id, in a process that never saw the send?
* does a fresh process's boot drain + auto-resumer finish that run with no
  operator resend?
* can a deleted attachment publish late, once a drain in another process is
  still holding its job?

So this module runs the real composition root in a real ``uvicorn`` process on
a real socket against the real Postgres at ``FIN_CHECKPOINT_URL`` (thread
store, attachment store, durable job store, checkpointer), with the real
compiled graph, the real gate node, the real SSE wire, the real prep drain and
the real auto-resumer, and reads every result back over a SECOND SQL
connection. ``settings.example.yaml`` supplies the deployed attachment bounds, as
``tests/host/test_live_attachment_upload_door.py`` does.

What is real here, and what is not
----------------------------------
Real: the process, the socket, every HTTP door under test, the Postgres
thread / attachment / job tables, the ``PostgresSaver`` checkpoints the pause
and resume ride, the compiled host graph including
``ensure_attachment_ready_node`` and its ``interrupt``, the send route's
``attachment_wait`` frame, :func:`AttachmentPreparationService
.drain_attachment_jobs` (the boot drain), ``start_attachment_resume_worker``
(the boot auto-resumer), the restart sweep in ``service.recover()``, the
object-store adapter the product selects from the environment, and the
committed corpus under ``data/corpus/``.

Substituted, in exactly two places, both because this host has no provider
credential and no knowledge MCP deployed (the ticket's own instruction):

* **model transport** — the child builds the product's own
  :class:`ChatOpenAI` (``fin.host.compile._build_default_model``, with the
  product's reasoning-preserving stream mapping) and points its ``base_url``
  at a scripted OpenAI-compatible server started inside this test. The child
  never reads ``FIN_API_KEY``; no credential is loaded, printed, or needed.
  Every gate decision is taken BEFORE the model leaf, and the scripted server
  records the calls it received, so "the paused run never reached the model"
  and "no operator resend was needed" are measured, not asserted.
* **evidence source** — the grounded leaf's ``read_source`` bodies come from
  the committed extracted tree through the factory's own documented seams
  (``known_document_ids`` + a retrieve adapter carrying ``pages_by_doc``),
  because the product MCP retrieve (``iknow-memory``, ADR 0030) is not
  deployed on this host and ``compile_for_runtime`` cannot be handed a
  retrieve adapter. This is what lets the success leg reach a
  citation-gate-passing ``done`` instead of a refusal about infrastructure.
  The citation gate, the evidence channel, and the attachment-hydration path
  it guards are all the real ones.

The page lane is deliberately untouched: every case here is a text-lane
payload, so no Heron harness or docker image is required.

Opt-in
------
``pytestmark = pytest.mark.mcp`` keeps this out of the default hermetic run
(``tests/host/conftest.py`` deselects it). Run it deliberately:

    FIN_CHECKPOINT_URL=… uv run --no-sync pytest -q -m mcp \\
        tests/host/test_live_attachment_graph_gate.py

It follows the repo's ``FIN_CHECKPOINT_URL`` skip convention, and the
durable-byte leg additionally skips when ``FIN_OBJECT_STORE_URL`` /
``FIN_OBJECT_STORE_ENDPOINT`` + ``FIN_OBJECT_STORE_BUCKET`` are unset. Because
the mark already requires a deliberate opt-in, a skip here names a missing
prerequisite rather than standing as a green pass; the validation record logs
it as blocked evidence when it happens.

How this run is isolated from everyone else's rows
--------------------------------------------------
The operator-supplied database and bucket MAY be shared, non-disposable, and
outlive any one run, so "clean" cannot mean "the next process sees an empty
world" — and the acceptance runs for this module additionally minted a per-run
disposable database and bucket on a session-owned container. Either way the
isolation below cannot rely on an empty world, because every child's boot
runs ``service.recover()``, and that sweep has no ownership
predicate of its own: ``jobs.requeue_running()`` re-adopts every ``running``
row in the database and ``store.incomplete()`` prepares every ``parsing`` row
in it, and the auto-resumer walks ``store.list_threads()`` in full. A process
that is deliberately NOT executing (this module's ``workers=False`` posture)
still performed the global sweep at construction, so residue from a prior
partial run — or a sibling opt-in module, or an orphaned child left by either
— was requeued and claimed out from under the run under test.

So the isolation lives in the DSN. ``postgres_url`` yields the operator's
URL with ``search_path`` pinned to a schema minted for THIS module run and
dropped at teardown, and every probe, cleanup statement, and child's
``FIN_CHECKPOINT_URL`` resolves unqualified table names through it. Nothing
in the product is reconfigured or stubbed to get there: the sweep, the
claim, the resume, and the doors are the shipped ones, pointed at a namespace
that contains only the rows this run minted. A sibling module keeps the
default ``search_path`` and a prior run's schema is dropped before the next
one starts, so neither can reach into this run's work.

Isolation never depends on bucket exclusivity, so the object store may stay
shared (acceptance runs additionally used per-run disposable buckets). Its keys
are are
``thread-attachments/{thread_id}/{attachment_id}`` over server-minted uuid4
ids, and every read is addressed by an ``object_key`` that came out of a row
in this run's schema, so a foreign or abandoned key is unreachable rather
than contended: no code path enumerates the bucket. Per-test ``$HOME`` (and
so the ``$HOME/.fin/default`` workspace root) is separate and disposable;
the hygiene case proves it is gone.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from fin.host.attachment.attachment_budgets import _ENV_BOUNDS as _PRODUCT_ENV_BOUNDS

REPO_ROOT = Path(__file__).resolve().parents[2]
GOLD_DIR = REPO_ROOT / "data" / "corpus" / "g4_attachment_gold"
CHART_PNG = GOLD_DIR / "g4-image-chart-001.png"
TEXT_FIXTURE = REPO_ROOT / "data" / "corpus" / "dataset" / "t1" / "compliance-001.md"

MANIFEST_PATH = REPO_ROOT / "data" / "corpus" / "manifest.json"
EXTRACTED_ROOT = REPO_ROOT / "data" / "corpus" / "extracted"

# The one committed document the scripted leaf reads, so the done answer is
# extractive evidence and not prose. Pinned by name only; its version and text
# are read off the committed tree at run time.
AAPL_2022Q3 = "AAPL-2022Q3"

# Grounded by the Soft-Track rules tier (same cue the hermetic gate tests use):
# naming "retrieve" routes the turn to the research leaf, which is the only
# route that passes through the attachment preparation gate.
GROUNDED_QUESTION = "retrieve a summary of the attachment"

# An open 档 named exactly once: the rules tier assigns it without opening the
# choice window, so the only pause this suite can observe is T2f's.
PINNED_TIER = "lexical"

# The named text-lane refusal for bytes that do not decode as UTF-8.
ATTACHMENT_NOT_TEXT = "attachment_not_text"

# Every case is one attachment preparation job; a second row is the bug the
# durable gate is supposed to make impossible.
JOB_TABLE = "fin_thread_attachment_job"

#: Seconds between the child's boot sweeps. Shorter than the production
#: interval so a resume is observed inside the polling deadline; the loop
#: itself, the startup drain, and the singleton guard are the shipped ones.
CHILD_SWEEP_INTERVAL_S = "2.0"

#: A restart landing a resume in a fresh process does real work twice over:
#: each child boots the real composition root, the prep drain's own prepare
#: runs a page-extraction pass, and the resumed remainder then walks the real
#: graph to a citation-gate-passing answer. Measured after the isolation
#: fixes below, the slowest case in this module settles in 8.9s and the whole
#: module in 34-41s across three consecutive runs, so 120s was already 13x
#: the observed worst case; 60s keeps that margin on a loaded host without
#: letting a genuinely stuck resume sit for minutes. Budget only — the
#: isolation fixes are what make a run succeed at all.
RESUME_DEADLINE_S = 60.0

#: ``uvicorn``'s own boot-to-first-answer budget, sized the same way.
SERVER_BOOT_DEADLINE_S = 60.0

#: Prefix of the Postgres schema this run owns. Lower-case and unquoted-safe,
#: so the name can never need a case-folding or quoting rule to resolve.
_SCHEMA_PREFIX = "fin_t2f_gate_"

BOUND_KEYS = tuple(key for key, _ in _PRODUCT_ENV_BOUNDS)

pytestmark = pytest.mark.mcp


# ----- preconditions -----


@pytest.fixture(scope="module")
def deployed_bounds() -> dict[str, str]:
    """The attachment bounds exactly as ``settings.example.yaml`` ships them."""
    from fin.host.operator_settings import shipped_attachment_bounds

    values = shipped_attachment_bounds(repo_root=REPO_ROOT)
    missing = [key for key in BOUND_KEYS if key not in values]
    assert not missing, f"settings.example.yaml lost {missing}"
    return {key: values[key] for key in BOUND_KEYS}


@pytest.fixture(scope="module")
def postgres_url() -> Iterator[str]:
    """The live DSN, narrowed to a schema this module owns, or the named skip.

    The narrowing is the run's isolation boundary. ``service.recover()`` runs
    in every child and has no ownership predicate — ``requeue_running()``
    touches every ``running`` job row in the database and
    ``store.incomplete()`` prepares every ``parsing`` attachment row in it —
    so on the shared database a child this module starts would adopt and
    execute rows belonging to a prior partial run, a sibling opt-in module,
    or an orphaned child. Pinning ``search_path`` to a schema minted here
    makes every unqualified statement the product issues land in a namespace
    that holds only what this run minted, with no change to the product and
    no stubbed sweep. The schema is dropped at teardown, so the next run
    starts clean and a crash leaves an inert namespace rather than live work
    for somebody else's boot to claim.
    """
    url = (os.environ.get("FIN_CHECKPOINT_URL") or "").strip()
    if not url:
        pytest.skip(
            "FIN_CHECKPOINT_URL is not set; this module is the real-storage "
            "evidence and cannot run against an in-memory stand-in"
        )
    import psycopg

    schema = f"{_SCHEMA_PREFIX}{uuid.uuid4().hex[:12]}"
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')
    scoped = _scoped_url(url, schema)
    # WHY: PostgresAttachmentStore creates its own tables with
    # CREATE TABLE IF NOT EXISTS but references fin_thread without creating
    # it, and raises AttachmentStoreError when that table is absent. On the
    # shared database fin_thread already exists from an earlier run, so the
    # dependency is satisfied by accident; a schema minted here starts empty
    # and every child would fail to boot. PostgresThreadStore's constructor
    # runs its own idempotent schema DDL, which restores the ordering the
    # product documents, inside this run's namespace only.
    from fin.host.thread_store import PostgresThreadStore

    PostgresThreadStore(scoped)
    try:
        yield scoped
    finally:
        # WHY CASCADE: the children create their own tables inside this
        # schema, and a dropped namespace must take the whole run's rows
        # with it even when a test failed before its own cleanup ran.
        # WHY suppressed: a schema this run can no longer reach (the DSN
        # rotated, the database went away) is not a second failure worth
        # replacing the run's result with — the cases already reported.
        with contextlib.suppress(psycopg.Error), psycopg.connect(
            url, autocommit=True
        ) as conn:
            conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


def _scoped_url(url: str, schema: str) -> str:
    """The operator's DSN with ``search_path`` pinned to ``schema``.

    Built through psycopg's own conninfo round trip so the operator's
    password-bearing DSN is re-encoded by the library rather than by string
    surgery here, and so an ``options`` the operator already set is replaced
    rather than appended to (two ``-c`` flags would take the last one).
    """
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    params = conninfo_to_dict(url)
    params["options"] = f"-c search_path={schema}"
    return make_conninfo(**params)


@pytest.fixture
def durable_object_store() -> None:
    """Require a configured byte store without exposing its values."""
    if not _s3_object_store_configured():
        pytest.skip(
            "FIN_OBJECT_STORE_URL/FIN_OBJECT_STORE_ENDPOINT and "
            "FIN_OBJECT_STORE_BUCKET are not configured; the durable-byte "
            "leg was not run against a real object store"
        )
    pytest.importorskip(
        "boto3", reason="the configured byte adapter needs the object-store extra"
    )


@pytest.fixture
def live_home(registry: dict[str, list[Any]]) -> Iterator[Path]:
    """A private ``$HOME`` per test, for every child that test starts.

    The workspace-root dependency resolves ``$HOME/.fin/default`` at request
    time, which is the production code path; pointing it at a throwaway
    directory keeps the real home untouched and still lets a send and a
    restart resolve the SAME root — within one test. Scoping it to the test
    rather than the module is what stops two cases from sharing one on-disk
    workspace root and one case's artifacts from reaching the next case's
    resumed run, and it is what makes the "no state left behind" claim
    checkable: the directory this test used is deleted at teardown, so the
    hygiene case can assert it is gone rather than merely empty.
    """
    root = Path(tempfile.mkdtemp(prefix="fin-t2f-gate-home-"))
    registry["homes"].append(root)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture(scope="module")
def registry() -> dict[str, list[Any]]:
    """Every id, home, and child process this module created, for the
    hygiene case."""
    return {"threads": [], "attachments": [], "children": [], "homes": []}


class _Owned:
    """One test's minted identities, recorded the moment they exist."""

    def __init__(self, registry: dict[str, list[Any]]) -> None:
        self._registry = registry
        self.threads: list[str] = []
        self.attachments: list[str] = []

    def thread(self, thread_id: str) -> str:
        self.threads.append(thread_id)
        self._registry["threads"].append(thread_id)
        return thread_id

    def attachment(self, attachment_id: str) -> str:
        self.attachments.append(attachment_id)
        self._registry["attachments"].append(attachment_id)
        return attachment_id


# ----- real-storage probes (a second SQL connection, never the app's) -----


def _sql_rows(url: str, statement: str, params: tuple[Any, ...] = ()) -> list[Any]:
    import psycopg

    with psycopg.connect(url) as conn:
        return conn.execute(statement, params).fetchall()


_ATTACHMENT_COLUMNS = (
    "attachment_id",
    "thread_id",
    "filename",
    "size_bytes",
    "sha256",
    "object_key",
    "status",
    "version",
    "unit_count",
    "indexed",
    "failure_reason",
)


def _attachment_row(url: str, attachment_id: str) -> dict[str, Any] | None:
    rows = _sql_rows(
        url,
        "SELECT attachment_id, thread_id, filename, size_bytes, sha256, "
        "object_key, status, version, unit_count, indexed, failure_reason "
        "FROM fin_thread_attachment WHERE attachment_id = %s",
        (attachment_id,),
    )
    if not rows:
        return None
    return dict(zip(_ATTACHMENT_COLUMNS, rows[0]))


_JOB_COLUMNS = (
    "attachment_id",
    "content_sha256",
    "target_version",
    "state",
    "failure_reason",
)


def _job_rows(url: str, attachment_id: str) -> list[dict[str, Any]]:
    """The durable work one attachment owns, as a different connection sees it."""
    rows = _sql_rows(
        url,
        f"SELECT {', '.join(_JOB_COLUMNS)} FROM {JOB_TABLE} "
        "WHERE attachment_id = %s ORDER BY created_at",
        (attachment_id,),
    )
    return [dict(zip(_JOB_COLUMNS, row)) for row in rows]


_RUN_COLUMNS = (
    "id",
    "thread_id",
    "question",
    "status",
    "status_reason",
    "answer",
    "tool_summaries",
    "evidence_locators",
)


def _run_row(url: str, run_id: str) -> dict[str, Any] | None:
    rows = _sql_rows(
        url,
        f"SELECT {', '.join(_RUN_COLUMNS)} FROM fin_run WHERE id = %s",
        (run_id,),
    )
    if not rows:
        return None
    return dict(zip(_RUN_COLUMNS, rows[0]))


def _run_rows_for_thread(url: str, thread_id: str) -> list[dict[str, Any]]:
    rows = _sql_rows(
        url,
        f"SELECT {', '.join(_RUN_COLUMNS)} FROM fin_run WHERE thread_id = %s "
        "ORDER BY created_at",
        (thread_id,),
    )
    return [dict(zip(_RUN_COLUMNS, row)) for row in rows]


def _derived_counts(url: str, attachment_id: str) -> tuple[int, int]:
    """(frozen unit rows, index postings) for one attachment, all versions."""
    units = _sql_rows(
        url,
        "SELECT count(*) FROM fin_thread_attachment_unit WHERE attachment_id = %s",
        (attachment_id,),
    )[0][0]
    postings = _sql_rows(
        url,
        "SELECT count(*) FROM fin_thread_attachment_index WHERE attachment_id = %s",
        (attachment_id,),
    )[0][0]
    return int(units), int(postings)


def _thread_count(url: str, thread_ids: list[str]) -> int:
    if not thread_ids:
        return 0
    return int(
        _sql_rows(url, "SELECT count(*) FROM fin_thread WHERE id = ANY(%s)",
                  (thread_ids,))[0][0]
    )


def _checkpoint_count(url: str, thread_ids: list[str]) -> int:
    """How many checkpoint rows one set of threads still carries."""
    if not thread_ids:
        return 0
    tables = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")
    total = 0
    for table in tables:
        total += int(
            _sql_rows(
                url,
                f"SELECT count(*) FROM {table} WHERE thread_id = ANY(%s)",
                (thread_ids,),
            )[0][0]
        )
    return total


def _object_keys(url: str, thread_ids: list[str]) -> list[str]:
    if not thread_ids:
        return []
    rows = _sql_rows(
        url,
        "SELECT object_key FROM fin_thread_attachment WHERE thread_id = ANY(%s) "
        "UNION SELECT object_key FROM fin_thread_attachment_tombstone "
        "WHERE thread_id = ANY(%s)",
        (thread_ids, thread_ids),
    )
    return [str(row[0]) for row in rows]


# ----- deterministic waiting -----


def _wait_until(
    observe: Callable[[], Any],
    settled: Callable[[Any], bool],
    *,
    what: str,
    timeout_s: float = 120.0,
) -> Any:
    """Poll ``observe`` until ``settled``; no fixed sleeps, a named failure.

    Every wait in this module is a restart landing work in another process, so
    the deadline is generous and the failure message names what was watched
    for and what was last seen.
    """
    deadline = time.monotonic() + timeout_s
    last: Any = None
    while time.monotonic() < deadline:
        last = observe()
        if settled(last):
            return last
        time.sleep(0.2)
    raise AssertionError(f"{what}; last observed: {last!r} (after {timeout_s}s)")


# ----- the scripted model transport (the only substituted boundary) -----


class _ScriptedModelServer:
    """A local OpenAI-compatible ``/chat/completions`` endpoint with one script.

    The script is the smallest thing that can produce a citation-gate-passing
    grounded answer: while no tool result is in the prompt it asks for one
    ``read_source`` hop, and once a tool result is present it answers
    extractively from the committed unit text. Every request body is recorded,
    which is how the suite measures (a) that a paused run never reached the
    model leaf and (b) that a resumed run reached it without an operator
    resend.
    """

    def __init__(self, *, tool_args: dict[str, Any], answer: str) -> None:
        self._tool_args = dict(tool_args)
        self._answer = answer
        self.seen: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port = 0

    # -- lifecycle --

    def start(self) -> str:
        harness = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                return None

            def do_POST(self) -> None:  # noqa: N802 - http.server's name
                length = int(self.headers.get("content-length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                with harness._lock:
                    harness.seen.append(body)
                if body.get("stream"):
                    self._sse(body)
                else:
                    self._json(body)

            # -- the two wire shapes langchain's ChatOpenAI can ask for --

            def _needs_tool(self, body: dict[str, Any]) -> bool:
                return any(
                    message.get("role") == "tool"
                    for message in body.get("messages") or []
                )

            def _payload(self, body: dict[str, Any]) -> tuple[list[dict], str]:
                if self._needs_tool(body):
                    return ([{"content": harness._answer}], "stop")
                call = [
                    {
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call-live-1",
                                "type": "function",
                                "function": {
                                    "name": "read_source",
                                    "arguments": json.dumps(harness._tool_args),
                                },
                            }
                        ],
                    }
                ]
                return (call, "tool_calls")

            def _sse(self, body: dict[str, Any]) -> None:
                payload, finish = self._payload(body)
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.end_headers()
                first = True
                for item in payload:
                    delta = dict(item)
                    if first:
                        delta = {"role": "assistant", **delta}
                        first = False
                    chunk = {
                        "id": "chatcmpl-live",
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": body.get("model", "fixture"),
                        "choices": [
                            {"index": 0, "delta": delta, "finish_reason": None}
                        ],
                    }
                    self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                final = {
                    "id": "chatcmpl-live",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": body.get("model", "fixture"),
                    "choices": [
                        {"index": 0, "delta": {}, "finish_reason": finish}
                    ],
                }
                self.wfile.write(f"data: {json.dumps(final)}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")

            def _json(self, body: dict[str, Any]) -> None:
                payload, finish = self._payload(body)
                completion = {
                    "id": "chatcmpl-live",
                    "object": "chat.completion",
                    "created": 1,
                    "model": body.get("model", "fixture"),
                    "choices": [
                        {
                            "index": 0,
                            "message": payload[0],
                            "finish_reason": finish,
                        }
                    ],
                }
                raw = json.dumps(completion).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        # WHY port 0 and no probe socket: asking the OS for port 0 and
        # reading back the port it actually assigned means the listening
        # socket never stops existing, so nothing can take the port between
        # the number being known and the server being bound. A probe that
        # bound port 0, read the number, closed, and then let the real server
        # rebind left a window in which any other process could win, and the
        # collision surfaced as a failed case rather than a retry.
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.port = int(self._server.server_address[1])
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True
        )
        self._thread.start()
        return self.base_url

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    @property
    def call_count(self) -> int:
        with self._lock:
            return len(self.seen)


@pytest.fixture
def scripted_model(corpus_unit: dict[str, Any]) -> Iterator[_ScriptedModelServer]:
    """One scripted transport per test, so its call log counts one story."""
    server = _ScriptedModelServer(
        tool_args=corpus_unit["tool_args"], answer=corpus_unit["answer"]
    )
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture(scope="module")
def corpus_unit() -> dict[str, Any]:
    """The committed evidence the scripted leaf quotes.

    Read off the extracted tree rather than restated here, so the answer span
    the citation gate must match is the corpus's own text and the
    ``read_source`` locator carries the manifest's own sha256.
    """
    from fin.corpus.extracted import load_extracted_tree

    manifest = json.loads(MANIFEST_PATH.read_text())
    versions = {
        str(document["document_id"]): str(document["sha256"])
        for document in manifest["documents"]
    }
    assert AAPL_2022Q3 in versions, f"{AAPL_2022Q3} left the committed manifest"
    tree = load_extracted_tree(MANIFEST_PATH, EXTRACTED_ROOT)
    pages = tree[AAPL_2022Q3]
    version = str(pages[0].locator.version)
    assert version == versions[AAPL_2022Q3], (version, versions[AAPL_2022Q3])
    span = pages[0].text[:80]
    assert len(span) >= 32, len(span)
    source_ref = {"document_id": AAPL_2022Q3, "version": version, "unit": 1}
    return {
        "source_ref": source_ref,
        "tool_args": {"source_ref": source_ref, "position": 0},
        "answer": f"Per the filing: {span}",
        "known_document_ids": sorted(versions),
    }


# ----- the live child: the real app, the real gate, on a real socket -----

_CHILD_SOURCE = """
import contextlib
import json
import os
import sys
from pathlib import Path

import uvicorn
from langgraph.checkpoint.postgres import PostgresSaver

from fin.corpus.extracted import load_extracted_tree
from fin.host import http as http_module
from fin.host.compile import _build_default_model
from fin.host.graph import compile_graph
from fin.host.thread_store import default_thread_store
from fin.host.workspace_http import (
    default_workspace_root_for_process,
    start_attachment_prep_worker,
    start_attachment_resume_worker,
)
from fin.retrieve import FakeRetrievePort

CHECKPOINT_URL = os.environ["FIN_CHECKPOINT_URL"]
MODEL_BASE_URL = os.environ["FIN_LIVE_MODEL_BASE_URL"]
MANIFEST = Path(os.environ["FIN_LIVE_CORPUS_MANIFEST"])
EXTRACTED = Path(os.environ["FIN_LIVE_CORPUS_EXTRACTED"])
SWEEP_INTERVAL_S = float(os.environ.get("FIN_LIVE_SWEEP_INTERVAL_S", "2.0"))
# A process without the two boot workers is the state a crash leaves behind:
# the gate still queues durable work and still suspends the run, and nothing
# in this process will ever run or resume it. That is the half of the ticket
# that says the work is durable, so the harness needs a server that is real
# but deliberately un-executing.
#
# WHY the absent workers are now a real posture and not a fiction: the sweep
# below is NOT conditional on this flag — ``recover()`` runs inside
# ``default_attachment_service()``, so an un-executing child still requeues
# every ``running`` job row and prepares every ``parsing`` attachment row it
# can see. What makes the posture hold is that CHECKPOINT_URL below resolves
# unqualified table names through a ``search_path`` pinned to the schema this
# run minted, so the sweep can only ever reach this run's own rows. A
# deliberate no-worker child therefore reclaims only work this run left
# running, which is exactly what a crashed predecessor's boot would do.
WORKERS = os.environ.get("FIN_LIVE_ATTACHMENT_WORKERS") == "1"

# One attachment service for the whole process, the way serve() wires it: the
# HTTP doors, the graph gate, the drain, and the restart sweep all see the same
# Postgres-backed stores. Construction is also what runs service.recover().
service = http_module.default_attachment_service()

stack = contextlib.ExitStack()
saver = stack.enter_context(PostgresSaver.from_conn_string(CHECKPOINT_URL))
saver.setup()

# The product's own ChatOpenAI, with its product stream mapping; only the
# transport is a local scripted endpoint, and no credential is read at all.
model = _build_default_model(
    model_name=os.environ.get("FIN_LIVE_MODEL_NAME", "fin-live-gate-model"),
    base_url=MODEL_BASE_URL,
    env={},
)

graph = compile_graph(
    checkpointer=saver,
    model=model,
    known_document_ids={
        str(document["document_id"])
        for document in json.loads(MANIFEST.read_text())["documents"]
    },
    # The committed corpus units serve the company leg of ``read_source``:
    # the bound retrieve adapter's own ``read_slice``, so the graph still
    # never walks the tree on its own (ADR 0046 / 0047).
    adapter=FakeRetrievePort(
        pages_by_doc=load_extracted_tree(MANIFEST, EXTRACTED)
    ),
    # The production boot ships the HITL lane; the pinned 档 keeps it closed,
    # so the only pause this suite can observe is the attachment gate's.
    enable_choice_window=True,
    attachment_service=service,
)

http_module.app.dependency_overrides[http_module.get_graph] = lambda: graph

if WORKERS:
    start_attachment_prep_worker(service, interval_s=SWEEP_INTERVAL_S)
    start_attachment_resume_worker(
        graph=graph,
        store=default_thread_store(),
        service=service,
        workspace_root=default_workspace_root_for_process(),
        check_interval_seconds=SWEEP_INTERVAL_S,
    )

uvicorn.run(http_module.app, fd=int(sys.argv[1]), log_level="warning")
"""

_READBACK_CHILD_SOURCE = """
import hashlib
import json
import sys
from pathlib import Path

from fin.corpus.object_store import create_object_store


key, payload_path, expected_sha256 = sys.argv[1:4]
try:
    expected = Path(payload_path).read_bytes()
    actual = create_object_store().get(key)
except Exception as error:
    print(json.dumps({"ok": False, "error_type": type(error).__name__}))
    raise SystemExit(1)

actual_sha256 = hashlib.sha256(actual).hexdigest()
if actual != expected or actual_sha256 != expected_sha256:
    print(json.dumps({"ok": False, "error_type": "PayloadMismatch"}))
    raise SystemExit(1)

print(json.dumps({"ok": True, "size_bytes": len(actual), "sha256": actual_sha256}))
"""


class _LiveServerError(AssertionError):
    """A child that could not boot: a failed prerequisite, never a skip."""


class _LiveServer:
    """One real uvicorn process serving the real app on a real socket."""

    def __init__(self, port: int, process: subprocess.Popen[str]) -> None:
        self.port = port
        self.process = process
        self._redaction: tuple[str, str] = ("", "")

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def redact(self, text: str) -> str:
        """Keep the DSN (which carries a password) out of every failure message."""
        needle, name = self._redaction
        return text.replace(needle, name) if needle else text

    def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        content_type: str | None = None,
    ) -> tuple[int, Any]:
        headers = {"content-type": content_type} if content_type else {}
        request = urllib.request.Request(
            f"{self.base}{path}", data=body, method=method, headers=headers
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return response.status, _load(response.read())
        except urllib.error.HTTPError as error:
            return error.code, _load(error.read())

    def create_thread(self) -> str:
        status, payload = self.request(
            "POST", "/api/threads", body=b"{}", content_type="application/json"
        )
        assert status == 200, payload
        assert isinstance(payload, dict), payload
        return str(payload["id"])

    def attach(self, thread_id: str, payload: bytes, filename: str) -> tuple[int, Any]:
        query = urllib.parse.urlencode({"filename": filename})
        return self.request(
            "POST",
            f"/api/threads/{thread_id}/attachments?{query}",
            body=payload,
            content_type="application/octet-stream",
        )

    def retry(self, thread_id: str, attachment_id: str) -> tuple[int, Any]:
        return self.request(
            "POST",
            f"/api/threads/{thread_id}/attachments/{attachment_id}/retry",
            body=b"",
            content_type="application/json",
        )

    def attachment_status(self, thread_id: str, attachment_id: str) -> tuple[int, Any]:
        return self.request(
            "GET", f"/api/threads/{thread_id}/attachments/{attachment_id}"
        )

    def delete_attachment(self, thread_id: str, attachment_id: str) -> tuple[int, Any]:
        return self.request(
            "DELETE", f"/api/threads/{thread_id}/attachments/{attachment_id}"
        )

    def thread_detail(self, thread_id: str) -> tuple[int, Any]:
        return self.request("GET", f"/api/threads/{thread_id}")

    def delete_thread(self, thread_id: str) -> tuple[int, Any]:
        return self.request("DELETE", f"/api/threads/{thread_id}")

    def send(
        self,
        thread_id: str,
        *,
        question: str = GROUNDED_QUESTION,
        attachment_ids: list[str] | None = None,
        tiers: list[str] | None = None,
        read_timeout_s: float = 120.0,
    ) -> list[tuple[str, Any]]:
        """One real SSE send, returned as its ``(event, data)`` frames.

        The stream is the run: a pause closes it (the send route persists
        nothing), so EOF is the answer and a read timeout means the run never
        ended on its own. The partial frames captured before a timeout are
        returned rather than dropped, so the failure report says what did
        arrive.
        """
        import httpx

        body: dict[str, Any] = {"question": question, "thinking": False}
        if attachment_ids is not None:
            body["attachment_ids"] = list(attachment_ids)
        if tiers is not None:
            body["tiers"] = list(tiers)
        events: list[tuple[str, Any]] = []
        timeout = httpx.Timeout(
            connect=10.0, read=read_timeout_s, write=10.0, pool=10.0
        )
        with httpx.Client(timeout=timeout) as client:
            try:
                with client.stream(
                    "POST",
                    f"{self.base}/api/threads/{thread_id}/messages",
                    json=body,
                    headers={"accept": "text/event-stream", "connection": "close"},
                ) as response:
                    if response.status_code != 200:
                        raise AssertionError(
                            f"send answered {response.status_code}: "
                            f"{response.read().decode('utf-8', 'replace')}"
                        )
                    for name, data in _sse_frames(response.iter_lines()):
                        events.append((name, data))
            except httpx.TimeoutException:
                return events
        return events

    def stop(self) -> None:
        """SIGTERM, then wait for the exit — the death every case is modelling."""
        if self.process.poll() is not None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=60)
        except subprocess.TimeoutExpired:  # pragma: no cover - child misbehaving
            self.process.kill()
            self.process.wait(timeout=60)


def _load(raw: bytes) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        # EXIT: a body that is not JSON is returned as bytes so the failing
        # assertion reports what actually arrived instead of a decode error.
        return raw


def _sse_frames(lines: Any) -> Iterator[tuple[str, Any]]:
    """Parse the SSE wire: ``event:`` + ``data:`` blocks separated by blanks."""
    current: dict[str, str] = {}
    for line in lines:
        if not line:
            if "event" in current and "data" in current:
                try:
                    parsed: Any = json.loads(current["data"])
                except json.JSONDecodeError:
                    parsed = {"raw": current["data"]}
                yield current["event"], parsed
            current = {}
            continue
        if line.startswith("event:"):
            current["event"] = line[len("event:"):].strip()
        elif line.startswith("data:"):
            current["data"] = line[len("data:"):].strip()
    if "event" in current and "data" in current:
        try:
            parsed = json.loads(current["data"])
        except json.JSONDecodeError:
            parsed = {"raw": current["data"]}
        yield current["event"], parsed


def _start_live_server(
    postgres_url: str,
    deployed_bounds: dict[str, str],
    *,
    model_base_url: str,
    workers: bool,
    live_home: Path,
    registry: dict[str, list[Any]],
) -> _LiveServer:
    """Boot an independent real process: real stores, real graph, real gate.

    ``workers`` selects the posture: ``True`` reproduces ``serve()``'s boot
    (startup drain + auto-resumer), ``False`` reproduces a process that queued
    durable work and then died before any executor ran.
    """
    environ = dict(os.environ)
    environ["FIN_CHECKPOINT_URL"] = postgres_url
    environ["HOME"] = str(live_home)
    environ["FIN_HOME"] = str(live_home / ".fin")
    environ.update(deployed_bounds)
    environ["FIN_LIVE_MODEL_BASE_URL"] = model_base_url
    environ["FIN_LIVE_CORPUS_MANIFEST"] = str(MANIFEST_PATH)
    environ["FIN_LIVE_CORPUS_EXTRACTED"] = str(EXTRACTED_ROOT)
    environ["FIN_LIVE_SWEEP_INTERVAL_S"] = CHILD_SWEEP_INTERVAL_S
    environ["FIN_LIVE_ATTACHMENT_WORKERS"] = "1" if workers else "0"

    # WHY the listener is bound HERE and handed over as an fd: the socket
    # that owns the port is the one the child serves on, so the port cannot
    # be taken between learning it and binding it. Reserving with a probe
    # socket and closing it first (the old shape) left that window open, and a
    # collision inside it killed the child at startup and surfaced as a case
    # failure. A bounded retry is unnecessary when the reservation IS the
    # server, so there is nothing to retry and no half-started child to reap.
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        listener.set_inheritable(True)
        port = int(listener.getsockname()[1])
        process = subprocess.Popen(
            [sys.executable, "-c", _CHILD_SOURCE, str(listener.fileno())],
            cwd=str(REPO_ROOT),
            env=environ,
            pass_fds=(listener.fileno(),),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    finally:
        # The child holds the only copy now. Dropping this one is what lets
        # the port be released the moment the child dies.
        listener.close()

    server = _LiveServer(port, process)
    server._redaction = (postgres_url, "<FIN_CHECKPOINT_URL>")
    registry["children"].append(process)
    try:
        _wait_for_server(server)
    except Exception:
        server.stop()
        raise
    return server


def _wait_for_server(server: _LiveServer) -> None:
    """Block until the child answers, or fail with the child's own output."""
    deadline = time.monotonic() + SERVER_BOOT_DEADLINE_S
    while time.monotonic() < deadline:
        if server.process.poll() is not None:
            tail = server.process.stdout.read() if server.process.stdout else ""
            raise _LiveServerError(
                "the live server exited "
                f"{server.process.returncode}: {server.redact(tail[-3000:])}"
            )
        try:
            with urllib.request.urlopen(f"{server.base}/docs", timeout=3) as probe:
                if probe.status == 200:
                    return
        except (urllib.error.URLError, TimeoutError, OSError):
            time.sleep(0.5)
    raise _LiveServerError(f"the live server never answered on :{server.port}")


# ----- byte store / thread cleanup -----


def _s3_object_store_configured() -> bool:
    endpoint = (
        os.environ.get("FIN_OBJECT_STORE_URL")
        or os.environ.get("FIN_OBJECT_STORE_ENDPOINT")
        or ""
    ).strip()
    bucket = (os.environ.get("FIN_OBJECT_STORE_BUCKET") or "").strip()
    return bool(endpoint and bucket)


def _fresh_object_read(key: str) -> tuple[bytes | None, str]:
    """Read one key through a NEW object-store client in this process."""
    from fin.corpus.object_store import create_object_store
    from fin.corpus.object_store_errors import UnknownObjectKeyError

    try:
        return create_object_store().get(key), "present"
    except UnknownObjectKeyError:
        return None, "absent"
    except Exception as error:  # noqa: BLE001 - the type name IS the result
        return None, type(error).__name__


def _cleanup_live(
    url: str,
    owned: _Owned,
    *servers: _LiveServer | None,
) -> None:
    """Remove everything one test created, then stop its processes.

    The order is deliberate. While a process is still listening the shipped
    thread-delete door runs first: it voids attachments, removes their bytes,
    retires their durable jobs, and drops the thread's checkpoints, so a
    cleanup through it is itself evidence about the product. The SQL sweep is
    the belt for the two shapes no door reaches once the process is gone — a
    thread whose process died before the door ran, and
    ``fin_thread_attachment_job``, which has no foreign key and therefore
    survives a raw thread delete by design.

    Servers are stopped last, and only after the sweep, so a failure inside the
    cleanup never leaves an orphaned uvicorn child behind — and only PIDs this
    test started are ever signalled.
    """
    threads = list(owned.threads)
    failures: list[str] = []

    live = next(
        (
            server
            for server in servers
            if server is not None and server.process.poll() is None
        ),
        None,
    )
    if live is not None:
        for thread_id in threads:
            try:
                status, _body = live.delete_thread(thread_id)
            except (urllib.error.URLError, OSError) as error:
                failures.append(f"DELETE /threads/{thread_id}: {type(error).__name__}")
                break
            if status not in (200, 404):
                failures.append(f"DELETE /threads/{thread_id} -> {status}")

    if threads and _s3_object_store_configured():
        from fin.corpus.object_store import create_object_store
        from fin.corpus.object_store_errors import UnknownObjectKeyError

        objects = create_object_store()
        for key in _object_keys(url, threads):
            try:
                objects.delete(key)
                objects.get(key)
            except UnknownObjectKeyError:
                continue
            except Exception as error:  # noqa: BLE001 - reported by name
                failures.append(f"{key}: {type(error).__name__}")
            else:
                failures.append(f"{key}: still present after delete")

    import psycopg

    with psycopg.connect(url) as conn:
        if owned.attachments:
            conn.execute(
                f"DELETE FROM {JOB_TABLE} WHERE attachment_id = ANY(%s)",
                (owned.attachments,),
            )
        if threads:
            for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
                conn.execute(
                    f"DELETE FROM {table} WHERE thread_id = ANY(%s)", (threads,)
                )
            conn.execute("DELETE FROM fin_thread WHERE id = ANY(%s)", (threads,))
        conn.commit()

    for server in servers:
        if server is not None:
            server.stop()

    if failures:
        raise AssertionError(
            f"live-test cleanup left {len(failures)} problem(s): "
            + "; ".join(failures[:10])
        )


class _LiveCase:
    """One live case's owned servers, started through one recorded path.

    Registration is the point: a case names the child it wants with
    ``start()`` and the case owns every PID it ever started, so teardown has a
    complete list without any test having to remember to append to it. A
    child that fails to boot is already recorded before the wait, so a boot
    failure is still reaped.
    """

    def __init__(
        self,
        url: str,
        owned: _Owned,
        *,
        deployed_bounds: dict[str, str],
        model_base_url: str,
        live_home: Path,
        registry: dict[str, list[Any]],
    ) -> None:
        self._url = url
        self._deployed_bounds = deployed_bounds
        self._model_base_url = model_base_url
        self._live_home = live_home
        self._registry = registry
        self.owned = owned
        self.servers: list[_LiveServer | None] = []

    def start(self, *, workers: bool) -> _LiveServer:
        server = _start_live_server(
            self._url,
            self._deployed_bounds,
            model_base_url=self._model_base_url,
            workers=workers,
            live_home=self._live_home,
            registry=self._registry,
        )
        self.servers.append(server)
        return server


@contextlib.contextmanager
def _live_case(case: _LiveCase) -> Iterator[_LiveCase]:
    """Run one live case, then clean up without ever masking its failure.

    Cleanup used to be a bare ``finally: _cleanup_live(...)``, so a cleanup
    problem raised out of the ``finally`` and REPLACED the traceback of the
    assertion that actually failed. On a suite whose failures are timing
    races, that inversion is the difference between a diagnosable report and
    an unrelated-looking one: the case that failed on a resume deadline
    reported as a cleanup complaint. So a cleanup problem is only raised when
    the case itself passed; when the case failed, it is attached to that
    failure as a note and the original traceback is what the report shows.
    """
    try:
        yield case
    except BaseException as failure:  # noqa: BLE001 - re-raised below
        _cleanup_for_exit(case, failure)
        raise
    _cleanup_for_exit(case, None)


def _cleanup_for_exit(
    case: _LiveCase, failure: BaseException | None
) -> None:
    try:
        _cleanup_live(case._url, case.owned, *case.servers)
    except BaseException as cleanup:  # noqa: BLE001 - reported, never replaces
        if failure is None:
            raise cleanup
        failure.add_note(f"live-test cleanup also failed: {cleanup}")


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _text_payload() -> bytes:
    """A committed text-lane body: real UTF-8 prose, no page lane involved."""
    assert TEXT_FIXTURE.is_file(), f"committed text fixture missing: {TEXT_FIXTURE}"
    return TEXT_FIXTURE.read_bytes()


def _binary_payload() -> bytes:
    """Committed bytes that cannot decode as UTF-8, named as a text file."""
    assert CHART_PNG.is_file(), f"committed gold payload missing: {CHART_PNG}"
    payload = CHART_PNG.read_bytes()
    with pytest.raises(UnicodeDecodeError):
        payload.decode("utf-8")
    return payload


def _mint_send_and_pause(
    server: _LiveServer,
    owned: _Owned,
    *,
    payload: bytes,
    filename: str,
) -> tuple[str, str, str, list[tuple[str, Any]]]:
    """Upload, groundedly send, and settle on the durable pause.

    Returns ``(thread_id, attachment_id, run_id, frames)`` after the gate has
    actually suspended the run and the durable state behind the pause has
    landed — the moment a SIGTERM is evidence rather than a race.
    """
    thread_id = owned.thread(server.create_thread())
    status, uploaded = server.attach(thread_id, payload, filename)
    assert status == 200, (status, uploaded)
    assert isinstance(uploaded, dict), uploaded
    assert uploaded["status"] == "uploaded", uploaded
    attachment_id = owned.attachment(str(uploaded["attachment_id"]))

    # The 档 is pinned, so the choice window can never be the pause this suite
    # observes: only T2f's preparation gate can stop this run.
    frames = server.send(
        thread_id, attachment_ids=[attachment_id], tiers=[PINNED_TIER]
    )
    waits = [data for name, data in frames if name == "attachment_wait"]
    assert len(waits) == 1, frames
    wait = waits[0]
    assert wait["kind"] == "attachment_preparation", wait
    assert wait["attachment_ids"] == [attachment_id], wait
    run_id = str(wait["run_id"])
    assert run_id, wait
    assert frames[-1][0] == "attachment_wait", frames
    return thread_id, attachment_id, run_id, frames


# =========================================================================
# Ticket cases 1 + 2: a killed sender, a fresh finisher, no resend.
# =========================================================================


def test_a_run_suspended_on_the_gate_completes_under_a_fresh_process(
    postgres_url: str,
    deployed_bounds: dict[str, str],
    live_home: Path,
    registry: dict[str, list[Any]],
    scripted_model: _ScriptedModelServer,
    corpus_unit: dict[str, Any],
) -> None:
    """Case 1 + 2: the pause survives the death and the restart finishes it.

    Process A (real gate, real durable queue, no boot workers) is sent a
    grounded turn naming an ``uploaded`` attachment: the SSE stream carries the
    server-minted ``attachment_wait`` frame, the attachment row reads
    ``parsing``, exactly one durable job row exists, and NOTHING reached the
    model leaf. A is then SIGTERMed.

    Process B — fresh, same environment, same database, boot drain and
    auto-resumer enabled — completes the SAME run id with no operator resend:
    the turn row lands ``done`` with a non-empty answer, the attachment row is
    ``ready`` and searchable, and the job row is ``succeeded``.
    """
    payload = _text_payload()
    case = _LiveCase(
        postgres_url,
        _Owned(registry),
        deployed_bounds=deployed_bounds,
        model_base_url=scripted_model.base_url,
        live_home=live_home,
        registry=registry,
    )
    with _live_case(case):
        sender = case.start(workers=False)
        thread_id, attachment_id, run_id, frames = _mint_send_and_pause(
            sender, case.owned, payload=payload, filename="gate-note.md"
        )
        assert frames[-1][1]["run_id"] == run_id, frames
        # A paused run is not a finished turn: the send route persists nothing.
        assert _run_row(postgres_url, run_id) is None, frames
        assert scripted_model.call_count == 0, scripted_model.seen

        row = _wait_until(
            lambda: _attachment_row(postgres_url, attachment_id),
            lambda current: current is not None
            and current["status"] == "parsing",
            what="the attachment row never reached 'parsing' behind the pause",
            timeout_s=30.0,
        )
        assert row["thread_id"] == thread_id, row
        assert row["sha256"] == _sha(payload), row
        assert _derived_counts(postgres_url, attachment_id) == (0, 0), row

        jobs = _job_rows(postgres_url, attachment_id)
        assert len(jobs) == 1, jobs
        assert jobs[0]["state"] in ("pending", "running"), jobs[0]
        assert jobs[0]["failure_reason"] is None, jobs[0]

        sender.stop()
        assert sender.process.poll() is not None
        assert scripted_model.call_count == 0, (
            "a dead process must not be reaching the model leaf"
        )
        assert _run_row(postgres_url, run_id) is None, (
            "the killed sender left no turn row behind"
        )

        finisher = case.start(workers=True)

        turn = _wait_until(
            lambda: _run_row(postgres_url, run_id),
            lambda current: current is not None,
            what="the restarted boot drain + auto-resumer never produced the "
                 "turn row for the run id the gate minted",
            timeout_s=RESUME_DEADLINE_S,
        )
        assert turn["thread_id"] == thread_id, turn
        assert turn["status"] == "done", (
            turn["status"],
            turn["status_reason"],
            turn["answer"],
        )
        assert turn["answer"].strip(), turn
        assert turn["question"] == GROUNDED_QUESTION, turn
        # The answer is the extractive span the scripted leaf quoted from the
        # committed unit, so the resumed remainder really ran the research
        # leaf and cleared the citation gate — not a filler success.
        assert corpus_unit["answer"] in turn["answer"], turn
        # The turn row's own audit column carries the locator the run read
        # with: the frozen company triple keyed by ``unit``, and no generic
        # ``page`` key anywhere on the persisted evidence (spec SC11).
        assert corpus_unit["source_ref"] in turn["evidence_locators"], turn
        assert all(
            "page" not in locator for locator in turn["evidence_locators"]
        ), turn

        row = _wait_until(
            lambda: _attachment_row(postgres_url, attachment_id),
            lambda current: current is not None and current["status"] == "ready",
            what="the attachment never became ready",
            timeout_s=RESUME_DEADLINE_S,
        )
        assert row["indexed"] is True, row
        assert int(row["unit_count"]) > 0, row
        assert int(row["version"]) >= 1, row
        assert row["failure_reason"] is None, row
        assert _derived_counts(postgres_url, attachment_id)[0] > 0, row

        jobs = _wait_until(
            lambda: _job_rows(postgres_url, attachment_id),
            lambda current: len(current) == 1
            and current[0]["state"] == "succeeded",
            what="the durable job never settled as exactly one 'succeeded' row",
            timeout_s=RESUME_DEADLINE_S,
        )
        assert jobs[0]["failure_reason"] is None, jobs[0]

        # No operator resend: one turn row on the thread, and exactly the two
        # model calls the resumed remainder needed (the read_source hop, then
        # the answer). A second send would have minted a second run id.
        assert _run_rows_for_thread(postgres_url, thread_id) == [turn], thread_id
        assert scripted_model.call_count == 2, scripted_model.seen

        status, detail = finisher.thread_detail(thread_id)
        assert status == 200, detail
        turns = detail["turns"]
        assert [entry["run_id"] for entry in turns] == [run_id], turns
        assert turns[0]["status"] == "done", turns[0]
        assert turns[0]["answer"].strip(), turns[0]
        assert turns[0]["in_flight"] is False, turns[0]


# =========================================================================
# Ticket case 3: the bytes leg, read back from the real object store.
# =========================================================================


def test_the_uploaded_bytes_are_whole_in_the_real_object_store_after_the_sender_dies(
    durable_object_store: None,
    postgres_url: str,
    deployed_bounds: dict[str, str],
    live_home: Path,
    registry: dict[str, list[Any]],
    scripted_model: _ScriptedModelServer,
) -> None:
    """Case 3: a second process reads the raw bytes the first one wrote.

    The attachment the gate paused on is addressed by its stored object key;
    after the sending process is gone, a fresh product adapter (its own boto3
    client, built from the environment) must fetch exactly the uploaded
    payload, byte for byte and sha256 for sha256. On a host with no byte store
    configured this case skips by name, because the degraded adapter lives in
    the dying process's memory and the claim would be vacuous.
    """
    payload = _text_payload()
    expected_sha256 = _sha(payload)
    case = _LiveCase(
        postgres_url,
        _Owned(registry),
        deployed_bounds=deployed_bounds,
        model_base_url=scripted_model.base_url,
        live_home=live_home,
        registry=registry,
    )
    with _live_case(case):
        server = case.start(workers=False)
        thread_id, attachment_id, run_id, _frames = _mint_send_and_pause(
            server, case.owned, payload=payload, filename="bytes-leg.md"
        )
        row = _attachment_row(postgres_url, attachment_id)
        assert row is not None and row["status"] == "parsing", row
        assert row["sha256"] == expected_sha256, row
        object_key = str(row["object_key"])
        assert object_key == f"thread-attachments/{thread_id}/{attachment_id}", row

        server.stop()

        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                _READBACK_CHILD_SOURCE,
                object_key,
                str(TEXT_FIXTURE),
                expected_sha256,
            ],
            cwd=str(REPO_ROOT),
            env=dict(os.environ),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert completed.returncode == 0, (
            "a fresh product adapter could not verify the uploaded bytes; "
            f"exit={completed.returncode}, "
            f"result={completed.stdout.strip()[:400]}"
        )
        result = json.loads(completed.stdout)
        assert result == {
            "ok": True,
            "size_bytes": len(payload),
            "sha256": expected_sha256,
        }, result


# =========================================================================
# Ticket case 4: a failure lands as a typed refusal, never an empty success.
# =========================================================================


def test_bytes_that_cannot_be_read_land_as_a_named_refusal_turn(
    postgres_url: str,
    deployed_bounds: dict[str, str],
    live_home: Path,
    registry: dict[str, list[Any]],
    scripted_model: _ScriptedModelServer,
) -> None:
    """Case 4: the gate waits, the job fails, the run still answers honestly.

    Undecodable bytes named as a text file are accepted by the upload door and
    refused by preparation. The paused run must not hang and must not be
    granted an empty ``done``: the settled sweep re-evaluates the gate, whose
    typed refusal becomes the turn row, carrying the job's own reason code.
    """
    payload = _binary_payload()
    case = _LiveCase(
        postgres_url,
        _Owned(registry),
        deployed_bounds=deployed_bounds,
        model_base_url=scripted_model.base_url,
        live_home=live_home,
        registry=registry,
    )
    with _live_case(case):
        server = case.start(workers=True)
        thread_id, attachment_id, run_id, frames = _mint_send_and_pause(
            server, case.owned, payload=payload, filename="chart-not-text.txt"
        )
        assert len(frames) >= 1, frames

        jobs = _wait_until(
            lambda: _job_rows(postgres_url, attachment_id),
            lambda current: len(current) == 1
            and current[0]["state"] in ("failed", "succeeded"),
            what="the boot drain never finished the unreadable attachment's job",
            timeout_s=RESUME_DEADLINE_S,
        )
        assert jobs[0]["state"] == "failed", jobs[0]
        assert jobs[0]["failure_reason"] == ATTACHMENT_NOT_TEXT, jobs[0]

        turn = _wait_until(
            lambda: _run_row(postgres_url, run_id),
            lambda current: current is not None,
            what="the settled failure never reached a turn row",
            timeout_s=RESUME_DEADLINE_S,
        )
        assert turn["status"] == "refuse", (
            turn["status"],
            turn["status_reason"],
            turn["answer"],
        )
        assert turn["status_reason"] == ATTACHMENT_NOT_TEXT, turn["status_reason"]
        assert turn["answer"] == "", turn
        assert scripted_model.call_count == 0, (
            "a failed preparation must never hand the refusal to the model"
        )

        row = _attachment_row(postgres_url, attachment_id)
        assert row is not None and row["status"] == "failed", row
        assert row["failure_reason"] == ATTACHMENT_NOT_TEXT, row
        assert row["indexed"] is False, row
        assert _derived_counts(postgres_url, attachment_id) == (0, 0), row

        # Never an empty success, anywhere on this thread.
        assert not [
            entry
            for entry in _run_rows_for_thread(postgres_url, thread_id)
            if entry["status"] == "done"
        ], thread_id


# =========================================================================
# Ticket case 5: a deleted attachment can never publish late.
# =========================================================================


def test_a_deleted_attachment_can_never_publish_late_to_its_paused_run(
    postgres_url: str,
    deployed_bounds: dict[str, str],
    live_home: Path,
    registry: dict[str, list[Any]],
    scripted_model: _ScriptedModelServer,
) -> None:
    """Case 5: the tombstone retires the job, and the paused run refuses.

    The deletion happens while the run is still suspended on the gate. The
    attachment's durable job is retired with it, so no drain — not this
    process's, not a restart's — has anything left to publish; every read door
    answers the typed absence; and the suspended run, driven by a fresh
    process's auto-resumer, reaches the turn row as the gate's
    ``unknown_locator`` refusal rather than an answer built on deleted bytes.
    """
    payload = _text_payload()
    case = _LiveCase(
        postgres_url,
        _Owned(registry),
        deployed_bounds=deployed_bounds,
        model_base_url=scripted_model.base_url,
        live_home=live_home,
        registry=registry,
    )
    with _live_case(case):
        deleter = case.start(workers=False)
        thread_id, attachment_id, run_id, _frames = _mint_send_and_pause(
            deleter, case.owned, payload=payload, filename="tombstone-leg.md"
        )
        assert len(_job_rows(postgres_url, attachment_id)) == 1, (
            "the pause queued no durable work to void"
        )

        status, body = deleter.delete_attachment(thread_id, attachment_id)
        assert status == 200, (status, body)
        assert body == {"ok": True, "attachment_id": attachment_id}, body

        # Late publication is impossible: the job row is gone with the row.
        assert _job_rows(postgres_url, attachment_id) == []
        assert _attachment_row(postgres_url, attachment_id) is None
        assert _derived_counts(postgres_url, attachment_id) == (0, 0)
        assert _sql_rows(
            postgres_url,
            "SELECT count(*) FROM fin_thread_attachment_tombstone "
            "WHERE attachment_id = %s",
            (attachment_id,),
        )[0][0] == 0

        # Every read door answers the typed absence, never a stale record.
        assert deleter.attachment_status(thread_id, attachment_id) == (
            400,
            {"error": "attachment_not_found"},
        )
        assert deleter.retry(thread_id, attachment_id) == (
            400,
            {"error": "attachment_not_found"},
        )
        assert _job_rows(postgres_url, attachment_id) == []

        deleter.stop()

        finisher = case.start(workers=True)
        turn = _wait_until(
            lambda: _run_row(postgres_url, run_id),
            lambda current: current is not None,
            what="the restart never drove the paused run to its typed absence",
            timeout_s=RESUME_DEADLINE_S,
        )
        assert turn["status"] == "refuse", (
            turn["status"],
            turn["status_reason"],
            turn["answer"],
        )
        assert turn["status_reason"] == "unknown_locator", turn["status_reason"]
        assert turn["answer"] == "", turn
        assert scripted_model.call_count == 0, (
            "a voided attachment must never reach the model leaf"
        )

        # Nothing came back from the void, in this process or the next.
        assert _attachment_row(postgres_url, attachment_id) is None
        assert _job_rows(postgres_url, attachment_id) == []
        assert _derived_counts(postgres_url, attachment_id) == (0, 0)


# =========================================================================
# Ticket case 6: concurrent triggers join one job.
# =========================================================================


def test_two_parallel_retry_doors_join_one_durable_job(
    postgres_url: str,
    deployed_bounds: dict[str, str],
    live_home: Path,
    registry: dict[str, list[Any]],
    scripted_model: _ScriptedModelServer,
) -> None:
    """Case 6: two real concurrent requests, one row of work.

    Both POSTs go over the socket of a process with no drain, so the join is
    decided by the job store's primary key rather than by a race that happened
    to finish first. A second row would mean two executors preparing the same
    content version, which is exactly what the durable gate exists to prevent.
    """
    import concurrent.futures

    payload = _text_payload()
    case = _LiveCase(
        postgres_url,
        _Owned(registry),
        deployed_bounds=deployed_bounds,
        model_base_url=scripted_model.base_url,
        live_home=live_home,
        registry=registry,
    )
    with _live_case(case):
        server = case.start(workers=False)
        owned = case.owned
        thread_id = owned.thread(server.create_thread())
        status, uploaded = server.attach(thread_id, payload, "one-job.md")
        assert status == 200, (status, uploaded)
        attachment_id = owned.attachment(str(uploaded["attachment_id"]))
        assert uploaded["status"] == "uploaded", uploaded

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(
                pool.map(
                    lambda _: server.retry(thread_id, attachment_id),
                    range(2),
                )
            )
        assert [response[0] for response in responses] == [200, 200], responses
        bodies = [response[1] for response in responses]
        assert all(isinstance(body, dict) for body in bodies), bodies
        # Work was queued, not performed in the request: the door never waits.
        assert [body["status"] for body in bodies] == ["parsing", "parsing"], bodies
        assert all(body["searchable"] is False for body in bodies), bodies

        jobs = _job_rows(postgres_url, attachment_id)
        assert len(jobs) == 1, jobs
        assert jobs[0]["state"] == "pending", jobs[0]
        assert jobs[0]["content_sha256"] == _sha(payload), jobs[0]
        assert int(jobs[0]["target_version"]) == 1, jobs[0]
        assert _derived_counts(postgres_url, attachment_id) == (0, 0), jobs

        # A third, sequential trigger joins the same row rather than minting one.
        assert server.retry(thread_id, attachment_id)[0] == 200
        assert len(_job_rows(postgres_url, attachment_id)) == 1, jobs


# =========================================================================
# Re-runnability.
# =========================================================================


def test_module_leaves_no_live_state_behind(
    postgres_url: str,
    registry: dict[str, list[Any]],
) -> None:
    """Every identity this module minted is gone, and so is every child.

    A real-storage suite that leaves a paused checkpoint behind hands the next
    boot's auto-resumer someone else's work to drive, and that is precisely the
    sweep this module claims to have exercised.

    The filesystem is in scope for the same reason. Every child resolved its
    ``$HOME/.fin/default`` workspace root from the home this module minted, so
    a directory left under one of them is exactly the kind of residue that
    survives the database sweep and reaches the next case.
    """
    threads = list(registry["threads"])
    attachments = list(registry["attachments"])
    homes = list(registry["homes"])
    assert threads, "the registry lost the threads this module created"

    assert _thread_count(postgres_url, threads) == 0, threads
    assert int(
        _sql_rows(
            postgres_url,
            "SELECT count(*) FROM fin_run WHERE thread_id = ANY(%s)",
            (threads,),
        )[0][0]
    ) == 0, threads
    assert int(
        _sql_rows(
            postgres_url,
            "SELECT count(*) FROM fin_thread_attachment WHERE thread_id = ANY(%s)",
            (threads,),
        )[0][0]
    ) == 0, threads
    if attachments:
        assert _job_rows_count(postgres_url, attachments) == 0, attachments
        assert _derived_counts_total(postgres_url, attachments) == 0, attachments
    assert _checkpoint_count(postgres_url, threads) == 0, threads
    assert _object_keys(postgres_url, threads) == [], threads

    assert homes, "the module recorded no home to check for residue"
    assert [path for path in homes if path.exists()] == [], (
        "workspace homes left on disk: "
        f"{[str(path) for path in homes if path.exists()]}"
    )

    # The acceptance clause is "record process IDs and prove all processes
    # created by the test are stopped", so enumerate every child PID this run
    # started with the exit status its Popen reports. ``poll()`` is called once
    # here and its result reused by the assertion, so the recorded list and the
    # proof observe the same snapshot and cannot diverge; the print makes the
    # id list part of the gated run's own output, not just the methodology.
    outcomes = [(process.pid, process.poll()) for process in registry["children"]]
    print(
        f"live-gate children created by this run ({len(outcomes)}): "
        + ", ".join(
            f"pid={pid} exit="
            + ("STILL RUNNING" if exit_status is None else str(exit_status))
            for pid, exit_status in outcomes
        )
    )
    still_alive = [pid for pid, exit_status in outcomes if exit_status is None]
    assert still_alive == [], f"uvicorn children left running: {still_alive}"


def _job_rows_count(url: str, attachment_ids: list[str]) -> int:
    return int(
        _sql_rows(
            url,
            f"SELECT count(*) FROM {JOB_TABLE} WHERE attachment_id = ANY(%s)",
            (attachment_ids,),
        )[0][0]
    )


def _derived_counts_total(url: str, attachment_ids: list[str]) -> int:
    return int(
        _sql_rows(
            url,
            "SELECT (SELECT count(*) FROM fin_thread_attachment_unit "
            "WHERE attachment_id = ANY(%s)) + "
            "(SELECT count(*) FROM fin_thread_attachment_index "
            "WHERE attachment_id = ANY(%s))",
            (attachment_ids, attachment_ids),
        )[0][0]
    )
