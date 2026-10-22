"""G4 T3's thread-bound retrieval, crossed over the REAL boundaries.

What this module is
-------------------
``plans/thread-attachment-source-read.md`` item 8 makes T3's contract
observable at three places: the model-facing ``retrieve`` envelope (the
``attachment_chunks`` / ``attachment_error`` keys), the thread-private
index rows behind it, and the live company port next to it. The hermetic
suites pin the branch matrix on in-memory stores and a Fake retrieve;
they cannot answer the ticket's real claims — that a grounded turn over
the shipped Postgres stores and the shipped object store returns a hit
whose ``source_ref`` addresses a frozen unit row, that another thread's
run cannot reach it, that a corrupt derived layer surfaces as a typed
unavailability instead of a silent empty success, and that the
attachment branch and the company branch answer the SAME live
``retrieve`` call side by side.

So this module runs the real composition in-process: the shipped
``default_attachment_service()`` (Postgres thread / attachment / durable
job stores, the environment-selected object store, the real
``request_preparation`` -> ``drain_attachment_jobs`` execution path), the
real ``compile_graph`` with the real attachment branch
(``attachment_retriever``) and the product ``MCPRetrieveAdapter`` wired
to a live iknow-memory stdio process, and it reads every stored result
back through a SECOND SQL connection, asserting only the observable
envelope and rows.

What is real here, and what is not
----------------------------------
Real: every Postgres row and table (thread, attachment, frozen units,
derived postings, durable jobs), the object store the product selects
from ``FIN_OBJECT_STORE_URL``/``FIN_OBJECT_STORE_ENDPOINT`` +
``FIN_OBJECT_STORE_BUCKET`` (the uploaded bytes are written there, read
back by preparation, and their sha256 is verified from this process over
a fresh client), preparation executed to ``ready`` through the durable
job, the compiled host graph including the ``retrieve`` tool's
attachment branch, the thread scope taken from the run config, and the
company leg — a real iknow-memory process over a real stdio MCP session,
pushed through the product ``IngestEndpoint``/``MCPPushPort`` path exactly
as ``tests/retrieve/test_live_research_cite_on_ingest.py`` does.

Substituted, in two disclosed places:

* **model transport** — the scripted ``BaseChatModel`` below stands in
  for the provider ``ChatOpenAI`` (this host has no credential, the same
  substitution and for the same reason as ``_ScriptedModelServer`` in
  ``tests/host/test_live_attachment_graph_gate.py``). The model exists
  only to emit one ``retrieve`` tool call with frozen args and then to
  answer; every attachment/company assertion is taken on the tool
  ENVELOPE and the SQL rows, never on model prose. Where case 1 asserts
  a ``done`` citation, the quoted span is extracted at run time from
  THIS run's live company payload (the SC8 technique), so a store that
  stopped returning that text fails the case instead of passing on a
  stale literal.
* **company backing store** — the live iknow process runs its
  ``STORAGE=memory`` harness mode with the deterministic embedding
  endpoint from ``tests/retrieve/live_iknow_support.py``. The MCP wire,
  the adapter, the ranking path inside the server, and the pushed slice
  are the real ones; the PG/ES cluster is not required for a company
  leg that the retrieve suite's own ``pg_es`` drivers already cover.

The checkpointer is ``InMemorySaver`` on purpose: this module's claims
are the retrieval envelope and thread isolation, not pause/resume
durability, which ``test_live_attachment_graph_gate.py`` already proves
across real processes with ``PostgresSaver``.

The page lane is irrelevant to every case here: all payloads are
committed UTF-8 text fixtures under ``data/corpus/dataset/t1/``, so no
bounds env is needed for a text-only deployment, and with the deployed
``FIN_ATTACHMENT_*`` bounds present the same cases still run (the text
lane never crosses the page port).

Opt-in
------
``pytestmark = pytest.mark.mcp`` keeps this out of the default hermetic
run (``tests/host/conftest.py`` deselects it). The OPERATOR runs it
deliberately, with the live stack up (``docker-compose.yml`` for
SeaweedFS, the Postgres at ``FIN_CHECKPOINT_URL``, the built iknow
``dist``):

    FIN_CHECKPOINT_URL=… FIN_OBJECT_STORE_URL=… FIN_OBJECT_STORE_BUCKET=… \\
        uv run --no-sync pytest -q -m mcp \\
        tests/host/test_live_attachment_retrieve_mcp.py

A missing ``FIN_CHECKPOINT_URL``, a missing object-store configuration,
or a missing iknow ``dist/index.js`` skips each case WITH THE NAMED
REASON — under this mark a skip is blocked evidence for the validation
record, never a green pass.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver

from fin.corpus.object_store import InMemoryObjectStore, create_object_store
from fin.corpus.object_store_errors import UnknownObjectKeyError
from fin.host.attachment.attachment_job_store import PostgresThreadAttachmentJobStore
from fin.host.attachment.attachment_store import PostgresAttachmentStore
from fin.host.grounded.citation_gate import (
    _LOCATOR_REGEX,  # noqa: PLC2701 - the gate's own token shape is the SSOT
)
from fin.host.graph import compile_graph
from fin.host.state import reset_input_state
from fin.host.thread_store import PostgresThreadStore
from fin.host.workspace_http import default_attachment_service

# tests/host modules get tests/host on sys.path, not tests/retrieve; the
# shared live-iknow harness lives there by convention (every retrieve live
# suite imports it by bare name), so this module extends the path itself
# rather than forking a second copy of the spawn/env logic.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "retrieve"))

from live_iknow_support import IKNOW_ROOT, live_iknow  # noqa: E402

from fin.corpus.ingest_endpoint import IngestEndpoint  # noqa: E402
from fin.retrieve.mcp_adapter import MCPRetrieveAdapter  # noqa: E402
from fin.retrieve.mcp_push_port import MCPPushPort  # noqa: E402


pytestmark = pytest.mark.mcp

REPO_ROOT = Path(__file__).resolve().parents[2]

# The committed text-lane fixtures. A is the GDPR excerpt (its "personal
# data" term is what the narrowed queries must hit); B shares only the
# corpus boilerplate phrase, which is exactly the overlap the naming case
# needs: one query, two ready attachments, one named.
FIXTURE_A = REPO_ROOT / "data" / "corpus" / "dataset" / "t1" / "compliance-001.md"
FIXTURE_B = REPO_ROOT / "data" / "corpus" / "dataset" / "t1" / "engineering-001.md"
FIXTURE_A_PHRASE = "Lawfulness, fairness and transparency"
FIXTURE_B_PHRASE = "The interview gap is orchestration"

# The same company fixture, doc id, and doc type the SC8 live suite pushed
# (tests/retrieve/test_live_research_cite_on_ingest.py): reusing them keeps
# this module downstream of a proven ingest path.
COMPANY_FIXTURE = (
    REPO_ROOT
    / "data" / "corpus" / "dataset" / "documents" / "md"
    / "AAPL-2022Q3__table_dense__md__md.md"
)
COMPANY_DOC_ID = "AAPL-2022Q3"
COMPANY_DOC_TYPE = "narrative"

# Questions proven to route to the grounded research leaf (SC8's exact
# shape); the retrieve tool args are scripted independently, so the
# question only carries track routing.
GROUNDED_QUESTION = (
    "grounded: what did AAPL report for operating revenue in 2022 Q3?"
)

# One query that the attachment branch matches (GDPR "personal data") and
# the live company store answers against the pushed AAPL filing slice:
# the comparison shape of ticket case 1 in a single retrieve.
MIXED_QUERY = "personal data operating revenue"
# Shared only by the corpus-boilerplate lines of both t1 fixtures, so the
# unnamed search must return both and naming one must drop the other.
NARROWING_QUERY = "evaluation wave"

# The frozen T3 wire contract, restated as literals on purpose: the test
# asserts the promised codes, not whatever constant the implementation
# currently spells.
ERROR_UNKNOWN_LOCATOR = "unknown_locator"
ERROR_NOT_READY = "attachment_not_ready"
ERROR_INDEX_UNAVAILABLE = "attachment_index_unavailable"
# The reason a failed-row case stores through the real store's failure
# door: the SEARCH surface must re-publish it unchanged.
ERROR_STORED_PARSE_FAILURE = "attachment_empty_text"

JOB_TABLE = "fin_thread_attachment_job"

# An answer proven to hold no >=32-char evidence span and no locator
# token: the verifier's ordinary bounce path (same copy the hermetic
# named-failure suites use). Envelope-focused cases answer this; their
# assertions are on the envelope, not on the run status.
NEUTRAL_ANSWER = (
    "Management expects the pace of hiring to slow through the "
    "remainder of the fiscal year."
)

_MIN_SPAN_LEN = 32
_QUOTE_LEN = 96


# ----- preconditions (each names its missing prerequisite) -----


@pytest.fixture(scope="module")
def live_env() -> str:
    """One guard for all three real dependencies, checked before any boot.

    Returns the DSN. A missing piece is a NAMED skip, per the module
    docstring: the operator's later run is the evidence, and this module
    never fakes across a store boundary.
    """
    url = (os.environ.get("FIN_CHECKPOINT_URL") or "").strip()
    if not url:
        pytest.skip(
            "FIN_CHECKPOINT_URL is not set; the thread/attachment/job stores "
            "under test are the real Postgres ones and cannot stand in "
            "in-memory"
        )
    endpoint = (
        os.environ.get("FIN_OBJECT_STORE_URL")
        or os.environ.get("FIN_OBJECT_STORE_ENDPOINT")
        or ""
    ).strip()
    bucket = (os.environ.get("FIN_OBJECT_STORE_BUCKET") or "").strip()
    if not (endpoint and bucket):
        pytest.skip(
            "FIN_OBJECT_STORE_URL/FIN_OBJECT_STORE_ENDPOINT and "
            "FIN_OBJECT_STORE_BUCKET are not configured; the attachment "
            "bytes must live in the real object store for this acceptance"
        )
    pytest.importorskip(
        "boto3", reason="the configured byte adapter needs the object-store extra"
    )
    entry = IKNOW_ROOT / "dist" / "index.js"
    if not entry.is_file():
        pytest.skip(
            f"{entry} is missing; run `npm run build` in {IKNOW_ROOT} to "
            "exercise the live company leg"
        )
    return url


@pytest.fixture(scope="module")
def service(live_env: str) -> Any:
    """The shipped composition: real stores, real byte adapter, no stand-in.

    ``default_attachment_service`` is the same root the production boot
    builds (Postgres attachment store, Postgres job store, the object
    store selected from the environment) and its construction runs
    ``service.recover()`` exactly as ``serve()`` does. The isinstance
    guards make a silently degraded singleton a loud failure instead of a
    vacuous "live" run.
    """
    svc = default_attachment_service()
    assert isinstance(svc.store, PostgresAttachmentStore), type(svc.store)
    assert isinstance(svc.jobs, PostgresThreadAttachmentJobStore), type(svc.jobs)
    assert not isinstance(svc.objects, InMemoryObjectStore), (
        "the byte layer degraded to process-local storage; this suite's "
        "object-store boundary requires the configured S3-compatible store"
    )
    return svc


@pytest.fixture(scope="module")
def threads(live_env: str) -> PostgresThreadStore:
    """The real thread store; attachment rows hang off these rows by FK."""
    return PostgresThreadStore(live_env)


@pytest.fixture(scope="module")
def company_live(live_env: str) -> Any:
    """One live iknow session with the committed company fixture pushed.

    Same chain as the SC8 suite: the real compiled server over stdio, the
    product ``IngestEndpoint`` + ``MCPPushPort`` writing the fixture, and
    the graph retrieving from the same session. Module scope: every case
    asks the same store the same kind of question, and the session is
    expensive.
    """
    mpatch = pytest.MonkeyPatch()
    try:
        with live_iknow(mpatch, root=IKNOW_ROOT) as live:
            endpoint = IngestEndpoint(
                push_port=MCPPushPort(session=live.session, timeout_seconds=20.0),
                invalidation_port=live.invalidation,
            )
            result = endpoint.ingest(
                COMPANY_FIXTURE.read_bytes(),
                filename=COMPANY_FIXTURE.name,
                document_id=COMPANY_DOC_ID,
                doc_type=COMPANY_DOC_TYPE,
            )
            assert result.written >= 1, "the fixture pushed no chunks"
            yield live, result
    finally:
        mpatch.undo()


# ----- identity registry / cleanup (mirrors the T2f live module) -----


@pytest.fixture(scope="module")
def registry() -> dict[str, list[Any]]:
    """Every id this module mints, for the hygiene case."""
    return {"threads": [], "attachments": []}


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


def _corrupt_postings(url: str, attachment_id: str) -> int:
    """Delete one attachment's derived postings by hand; the row stays ready.

    This is the ticket's corruption premise, written the way an operator's
    broken rebuild or a partially swept index version looks on disk: the
    attachment row still says ``ready``/``indexed``, the frozen units are
    still there, and only the term->unit postings are gone.
    """
    import psycopg

    with psycopg.connect(url) as conn:
        result = conn.execute(
            "DELETE FROM fin_thread_attachment_index WHERE attachment_id = %s",
            (attachment_id,),
        )
        conn.commit()
        return int(result.rowcount or 0)


def _thread_count(url: str, thread_ids: list[str]) -> int:
    if not thread_ids:
        return 0
    return int(
        _sql_rows(
            url, "SELECT count(*) FROM fin_thread WHERE id = ANY(%s)", (thread_ids,)
        )[0][0]
    )


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


def _job_rows_count(url: str, attachment_ids: list[str]) -> int:
    if not attachment_ids:
        return 0
    return int(
        _sql_rows(
            url,
            f"SELECT count(*) FROM {JOB_TABLE} WHERE attachment_id = ANY(%s)",
            (attachment_ids,),
        )[0][0]
    )


def _derived_counts_total(url: str, attachment_ids: list[str]) -> int:
    if not attachment_ids:
        return 0
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


def _cleanup_live(
    url: str, owned: _Owned, objects: Any
) -> None:
    """Remove everything one test created, over the real stores.

    The byte removal goes through the same adapter the product used to
    write (a delete-then-read-back proves the key is really gone), and the
    SQL sweep covers what no FK reaches: ``fin_thread_attachment_job`` has
    no foreign key and therefore survives a raw thread delete by design
    (the T2f module documents the same belt).
    """
    threads = list(owned.threads)
    failures: list[str] = []

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
            conn.execute("DELETE FROM fin_thread WHERE id = ANY(%s)", (threads,))
        conn.commit()

    if failures:
        raise AssertionError(
            f"live-test cleanup left {len(failures)} problem(s): "
            + "; ".join(failures[:10])
        )


# ----- real preparation to ready, through the durable job -----


def _mint_thread(owned: _Owned, threads: PostgresThreadStore) -> str:
    return owned.thread(threads.create_thread(title="t3-live-retrieve").id)


def _prepare_to_ready(
    url: str,
    service: Any,
    threads: PostgresThreadStore,
    owned: _Owned,
    *,
    path: Path,
    thread_id: str | None = None,
    filename: str,
) -> tuple[str, str]:
    """Upload a committed text fixture and run it to ``ready`` for real.

    Crossed boundaries, all the product's own: the object store write in
    ``accept``, the durable job enqueue in ``request_preparation``, the
    executor in ``drain_attachment_jobs``, and the frozen-unit + posting
    publishes. The assertions are read back on a second SQL connection
    and by re-fetching the bytes from the object store, so "ready" here is
    a measured state, not a returned dataclass.

    Returns ``(thread_id, attachment_id)``.
    """
    assert path.is_file(), f"committed text fixture missing: {path}"
    payload = path.read_bytes()
    if thread_id is None:
        thread_id = _mint_thread(owned, threads)

    record = service.accept(thread_id=thread_id, filename=filename, payload=payload)
    assert record.status == "uploaded", record
    assert record.thread_id == thread_id, record
    attachment_id = owned.attachment(record.attachment_id)

    request = service.request_preparation(thread_id, attachment_id)
    assert request.state == "pending", request
    assert service.drain_attachment_jobs() >= 1, "the drain claimed no work"

    row = _attachment_row(url, attachment_id)
    assert row is not None, attachment_id
    assert row["thread_id"] == thread_id, row
    assert row["status"] == "ready", row
    assert row["failure_reason"] is None, row
    assert row["indexed"] is True, row
    assert int(row["unit_count"]) >= 1, row
    assert int(row["version"]) >= 1, row
    assert row["sha256"] == hashlib.sha256(payload).hexdigest(), row
    units, postings = _derived_counts(url, attachment_id)
    assert units == int(row["unit_count"]) >= 1, row
    assert postings >= 1, row

    jobs = _job_rows(url, attachment_id)
    assert len(jobs) == 1, jobs
    assert jobs[0]["state"] == "succeeded", jobs[0]
    assert jobs[0]["failure_reason"] is None, jobs[0]

    # The bytes are really in the deployed bucket: read them back over a
    # fresh product client and match the stored sha256, so a degraded or
    # in-memory byte layer could never carry this suite.
    fresh_objects = create_object_store()
    stored = fresh_objects.get(str(row["object_key"]))
    assert hashlib.sha256(stored).hexdigest() == row["sha256"], row
    assert stored == payload, row

    return thread_id, attachment_id


# ----- the scripted retrieve caller (the substituted model transport) -----


def _retrieve_envelope(messages: Any) -> dict[str, Any]:
    """The LAST ``retrieve`` ToolMessage's parsed envelope for this run.

    One call is what every case scripts for; taking the last rather than
    pinning the count keeps a future repair-loop second call from
    changing which payload the contract assertions read.
    """
    envelopes = []
    for message in messages:
        if getattr(message, "name", None) == "retrieve" and isinstance(
            message.content, str
        ):
            try:
                envelopes.append(json.loads(message.content))
            except json.JSONDecodeError:
                raise AssertionError(
                    f"retrieve ToolMessage is not JSON: {message.content[:400]!r}"
                )
    assert envelopes, [str(m)[:200] for m in messages]
    return envelopes[-1]


def _company_payload(envelope: dict[str, Any]) -> list[dict[str, Any]]:
    chunks = envelope.get("chunks")
    return list(chunks) if isinstance(chunks, list) else []


def _attachment_payload(envelope: dict[str, Any]) -> list[dict[str, Any]]:
    chunks = envelope.get("attachment_chunks")
    return list(chunks) if isinstance(chunks, list) else []


def _quote_span(chunks: Any) -> str | None:
    """A real prefix of a real company chunk, locator-token free.

    The SC8 rule: quoting text that carries a locator-shaped token the
    run cannot vouch for would fail the gate for a reason unrelated to
    this ticket. ``None`` means the live store handed back nothing
    quotable, and the caller's answer goes ungrounded on purpose so the
    case fails loudly instead of citing a pasted literal.
    """
    for chunk in chunks:
        text = str(chunk.get("text", ""))
        if len(text) < _MIN_SPAN_LEN:
            continue
        locator = chunk.get("source_ref") or {}
        known = {str(locator.get("document_id", ""))}
        if set(_LOCATOR_REGEX.findall(text)) - known:
            continue
        span = text.strip()[:_QUOTE_LEN].rstrip()
        if len(span) >= _MIN_SPAN_LEN:
            return span
    return None


class _ScriptedRetrieveModel(BaseChatModel):
    """Turn 1: one ``retrieve`` call with frozen args. Later turns: answer.

    ``quote_company`` (case 1) answers extractively from THIS run's live
    company payload — the citation-gate path the SC8 suite proved. The
    other cases answer the neutral prose: their assertions are on the
    envelope and the rows, and a refusal after them is the verifier's
    honest verdict about a non-quoting script, never about retrieval.
    """

    tool_args: dict[str, Any] = {}
    quote_company: bool = False

    @property
    def _llm_type(self) -> str:
        return "t3-live-retrieve"

    def bind_tools(self, tools: list[Any], **kwargs: Any) -> "_ScriptedRetrieveModel":
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        already_retrieved = any(
            getattr(message, "name", None) == "retrieve" for message in messages
        )
        if not already_retrieved:
            return self._result(
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "retrieve",
                            "args": dict(self.tool_args),
                            "id": "c-t3-live-retrieve",
                            "type": "tool_call",
                        }
                    ],
                )
            )
        if self.quote_company:
            try:
                envelope = _retrieve_envelope(messages)
            except AssertionError:
                envelope = {}
            span = _quote_span(_company_payload(envelope))
            if span is not None:
                return self._result(
                    AIMessage(content=f"According to the company source: {span}")
                )
        return self._result(AIMessage(content=NEUTRAL_ANSWER))

    @staticmethod
    def _result(message: AIMessage) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=message)])


def _run_grounded_retrieve(
    *,
    service: Any,
    company: Any,
    thread_id: str,
    tool_args: dict[str, Any],
    quote_company: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """One grounded turn through the real compiled graph.

    The graph is the production composition root: the retrieve tool's
    attachment branch is fed by the shipped service (whose ``search`` is
    the ``AttachmentSearchPort`` the T3 contract freezes, delegating to
    ``ThreadAttachmentRetriever`` over the same Postgres store), the
    company branch by the live ``MCPRetrieveAdapter``, and the thread
    scope by the run config — there is no model-facing thread argument to
    widen, which is what cases 2 and 3 measure.
    """
    live, result = company
    model = _ScriptedRetrieveModel(
        tool_args=dict(tool_args), quote_company=quote_company
    )
    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        adapter=live.retrieve,
        known_document_ids={result.document_id},
        attachment_service=service,
        attachment_retriever=service,
    )
    out = graph.invoke(
        reset_input_state(GROUNDED_QUESTION),
        config={"configurable": {"thread_id": thread_id}},
    )
    return out, _retrieve_envelope(out["messages"])


# =========================================================================
# Case 1 (plan item 8): a grounded turn retrieves the thread's ready
# attachment alongside the live company branch, in ONE envelope.
# =========================================================================


def test_a_ready_attachment_and_the_live_company_leg_answer_one_retrieve(
    live_env: str,
    service: Any,
    threads: PostgresThreadStore,
    company_live: Any,
    registry: dict[str, list[Any]],
) -> None:
    """Ticket case 1: typed attachment locator + company hits, real stores.

    The attachment was prepared to ``ready`` through the durable job over
    the real Postgres and object stores; the company slice was pushed
    through the product ingest path into the live iknow process. One
    scripted retrieve has to answer BOTH from those rows: the envelope's
    attachment hit carries ``source_ref={kind:"attachment",
    attachment_id, version, unit}`` addressing the frozen unit row (unit
    ordinal 1), its chunks carry the pushed company text, the live server
    really received the request, and the run ends ``done`` citing a span
    extracted from this run's company payload.
    """
    live, result = company_live
    owned = _Owned(registry)
    try:
        thread_id, attachment_id = _prepare_to_ready(
            live_env, service, threads, owned, path=FIXTURE_A, filename="compliance-001.md"
        )
        row = _attachment_row(live_env, attachment_id)
        assert row is not None, attachment_id

        out, envelope = _run_grounded_retrieve(
            service=service,
            company=company_live,
            thread_id=thread_id,
            tool_args={"query": MIXED_QUERY, "attachment_ids": [attachment_id]},
            quote_company=True,
        )

        # The attachment branch answered with the typed locator.
        hits = _attachment_payload(envelope)
        assert hits, envelope
        assert "attachment_error" not in envelope, envelope
        locators = [hit["source_ref"] for hit in hits]
        for locator in locators:
            assert locator["kind"] == "attachment", locator
            assert locator["attachment_id"] == attachment_id, locator
            assert locator["version"] == int(row["version"]), locator
            assert int(locator["unit"]) >= 1, locator
        assert 1 in [int(locator["unit"]) for locator in locators], locators

        # The hit text IS the frozen unit row the ordinal addresses —
        # read off the same service the search delegated to, so a hit
        # promising text no row carries fails here, not in a later read.
        unit_rows = service.units(attachment_id)
        by_unit = {parsed.unit: parsed.text for parsed in unit_rows}
        for hit in hits:
            unit = int(hit["source_ref"]["unit"])
            assert hit["text"] == by_unit[unit], (unit, str(hit["text"])[:120])
        assert any(
            FIXTURE_A_PHRASE in str(hit["text"]) for hit in hits
        ), "the returned attachment text is not the committed fixture's"
        assert any(
            "personal data" in str(hit["text"]).lower() for hit in hits
        ), hits

        # The company branch answered, from the live store, same envelope.
        company_chunks = _company_payload(envelope)
        assert company_chunks, envelope
        for chunk in company_chunks:
            assert chunk["source_ref"]["document_id"] == result.document_id, chunk
            assert chunk["source_ref"]["version"] == result.version, chunk
        assert any(
            "apple inc." in str(chunk["text"]).lower()
            for chunk in company_chunks
        ), "the live store returned no company text for the pushed fixture"

        # The request actually crossed the MCP wire: the live server saw
        # this run's retrieve (SC8's standard for "live").
        retrieve_calls = [
            request
            for request in live.session.requests
            if request.get("method") == "tools/call"
            and request.get("params", {}).get("name") == "retrieve"
        ]
        assert retrieve_calls, "no retrieve tools/call reached the live server"
        assert any(
            call["params"]["arguments"].get("query") == MIXED_QUERY
            for call in retrieve_calls
        ), [call["params"]["arguments"] for call in retrieve_calls]
        assert isinstance(live.retrieve, MCPRetrieveAdapter)

        # The grounded turn completes and cites THIS run's company text —
        # the comparison is an answer, not just two payload dumps.
        assert out["track"] == "grounded", out["track"]
        assert out["status"] == "done", (
            out["status"],
            out.get("status_reason"),
            out.get("answer"),
        )
        citations = list(out["citations"])
        assert citations, "a done grounded run must carry >= 1 citation"
        company_texts = [str(chunk["text"]) for chunk in company_chunks]
        for citation in citations:
            assert len(citation.quote) >= _MIN_SPAN_LEN, citation
            assert any(citation.quote in text for text in company_texts), citation
            assert citation.document_id == result.document_id, citation
    finally:
        _cleanup_live(live_env, owned, service.objects)


# =========================================================================
# Case 2: another thread's run cannot reach thread A's attachment.
# =========================================================================


def test_another_threads_run_retrieves_nothing_from_the_first_threads_attachment(
    live_env: str,
    service: Any,
    threads: PostgresThreadStore,
    company_live: Any,
    registry: dict[str, list[Any]],
) -> None:
    """Ticket case 2: scope is the CONFIG's thread, not the corpus.

    Thread A holds the ready attachment (so its content exists somewhere
    searchable); the run happens under thread B's config, naming nothing.
    B has no attachments, so an empty attachment branch is the LEGAL
    answer here — and it must be an empty success, not an error — while
    none of A's content or identity may appear in the envelope.
    """
    owned = _Owned(registry)
    try:
        a_thread, a_attachment = _prepare_to_ready(
            live_env, service, threads, owned, path=FIXTURE_A, filename="compliance-001.md"
        )
        b_thread = _mint_thread(owned, threads)

        out, envelope = _run_grounded_retrieve(
            service=service,
            company=company_live,
            thread_id=b_thread,
            tool_args={"query": MIXED_QUERY},
        )

        hits = _attachment_payload(envelope)
        assert all(
            hit["source_ref"]["attachment_id"] != a_attachment for hit in hits
        ), hits
        assert FIXTURE_A_PHRASE.casefold() not in json.dumps(envelope).casefold(), (
            "thread A's attachment text leaked into thread B's envelope"
        )
        # B genuinely has nothing: that is a legal empty, never a typed
        # failure and never a borrowed hit list.
        assert "attachment_error" not in envelope, envelope
        assert out["track"] == "grounded", out["track"]
    finally:
        _cleanup_live(live_env, owned, service.objects)


# =========================================================================
# Case 3: a model-named locator from another thread is unknown_locator.
# =========================================================================


def test_a_model_named_locator_from_another_thread_is_typed_and_reveals_nothing(
    live_env: str,
    service: Any,
    threads: PostgresThreadStore,
    company_live: Any,
    registry: dict[str, list[Any]],
) -> None:
    """Ticket case 3: the model can name an id; it cannot reach it.

    The run happens under thread B's config while the script's tool args
    name thread A's real attachment id. The envelope must carry the typed
    ``{"error": "unknown_locator"}`` on ``attachment_error`` — the
    reveal-nothing identity exit — with no attachment hits, no fixture
    text, and nothing in the error dict echoing the id beyond the args the
    model already had.
    """
    owned = _Owned(registry)
    try:
        a_thread, a_attachment = _prepare_to_ready(
            live_env, service, threads, owned, path=FIXTURE_A, filename="compliance-001.md"
        )
        b_thread = _mint_thread(owned, threads)

        _out, envelope = _run_grounded_retrieve(
            service=service,
            company=company_live,
            thread_id=b_thread,
            tool_args={"query": MIXED_QUERY, "attachment_ids": [a_attachment]},
        )

        assert envelope.get("attachment_error") == {
            "error": ERROR_UNKNOWN_LOCATOR
        }, envelope
        assert _attachment_payload(envelope) == [], envelope
        assert FIXTURE_A_PHRASE.casefold() not in json.dumps(envelope).casefold(), (
            "a cross-thread probe must reveal no source text"
        )
    finally:
        _cleanup_live(live_env, owned, service.objects)


# =========================================================================
# Case 3b: a same-thread locator that is not searchable yet is typed too
# (the contract names attachment_not_ready alongside unknown_locator; a
# not-yet-ready row is neither a hit set nor an unknown identity).
# =========================================================================


def test_naming_an_uploaded_attachment_answers_attachment_not_ready(
    live_env: str,
    service: Any,
    threads: PostgresThreadStore,
    company_live: Any,
    registry: dict[str, list[Any]],
) -> None:
    """Raw-only bytes are an honest ``attachment_not_ready``, never a hit.

    The row is uploaded (durable bytes, no preparation asked) on the SAME
    thread the run belongs to, so this is not the identity failure of
    case 3 — it is the lifecycle one the frozen envelope names separately.
    """
    assert FIXTURE_A.is_file(), FIXTURE_A
    owned = _Owned(registry)
    try:
        thread_id = _mint_thread(owned, threads)
        record = service.accept(
            thread_id=thread_id,
            filename="compliance-001.md",
            payload=FIXTURE_A.read_bytes(),
        )
        assert record.status == "uploaded", record
        attachment_id = owned.attachment(record.attachment_id)

        _out, envelope = _run_grounded_retrieve(
            service=service,
            company=company_live,
            thread_id=thread_id,
            tool_args={"query": MIXED_QUERY, "attachment_ids": [attachment_id]},
        )

        assert envelope.get("attachment_error") == {
            "error": ERROR_NOT_READY
        }, envelope
        assert _attachment_payload(envelope) == [], envelope
    finally:
        _cleanup_live(live_env, owned, service.objects)


# =========================================================================
# Case 3c: a durable terminal ``failed`` row answers with its STORED
# failure code, never the transient not-ready code — the search-surface
# exit for a parse failure is observable across the real MCP/storage
# boundary and must not invite a wait-and-retry.
# =========================================================================


def test_a_durable_failed_row_answers_with_its_stored_failure_code(
    live_env: str,
    service: Any,
    threads: PostgresThreadStore,
    company_live: Any,
    registry: dict[str, list[Any]],
) -> None:
    """A named ``failed`` row re-publishes its stored reason on the envelope.

    The row reaches terminal ``failed`` durably in the real Postgres store
    through the store's own ``mark_failed`` door (the same door the
    preparation executor writes through); the seeding deliberately skips
    the preparation job because this case pins the SEARCH surface's exit,
    not the preparation door's path to that state. The stored shape is
    re-read on a second SQL connection before the run, and the envelope
    must answer the stored code — an ``attachment_not_ready`` here would
    tell the model to wait for a parse that already terminated.
    """
    assert FIXTURE_A.is_file(), FIXTURE_A
    owned = _Owned(registry)
    try:
        thread_id = _mint_thread(owned, threads)
        record = service.accept(
            thread_id=thread_id,
            filename="compliance-001.md",
            payload=FIXTURE_A.read_bytes(),
        )
        assert record.status == "uploaded", record
        attachment_id = owned.attachment(record.attachment_id)

        service.store.mark_failed(
            attachment_id=attachment_id,
            version=0,
            reason=ERROR_STORED_PARSE_FAILURE,
        )
        row = _attachment_row(live_env, attachment_id)
        assert row is not None and row["status"] == "failed", row
        assert row["failure_reason"] == ERROR_STORED_PARSE_FAILURE, row

        _out, envelope = _run_grounded_retrieve(
            service=service,
            company=company_live,
            thread_id=thread_id,
            tool_args={"query": MIXED_QUERY, "attachment_ids": [attachment_id]},
        )

        # The stored code rides the envelope verbatim, never the wait code.
        assert envelope.get("attachment_error") == {
            "error": ERROR_STORED_PARSE_FAILURE
        }, envelope
        assert envelope["attachment_error"]["error"] != ERROR_NOT_READY, envelope
        # No text channel: a failed exit is never an empty success either.
        assert "attachment_chunks" not in envelope, envelope
        assert FIXTURE_A_PHRASE.casefold() not in json.dumps(envelope).casefold(), (
            "a failed-row exit must reveal no source text"
        )
    finally:
        _cleanup_live(live_env, owned, service.objects)


# =========================================================================
# Case 4: a ready row whose postings are gone is typed unavailable.
# =========================================================================

def test_a_ready_attachment_with_a_broken_index_answers_typed_unavailable(
    live_env: str,
    service: Any,
    threads: PostgresThreadStore,
    company_live: Any,
    registry: dict[str, list[Any]],
) -> None:
    """Ticket case 4: an index outage is named, never an empty success.

    After real preparation, the derived postings are deleted by direct
    SQL while the attachment row still reads ``ready``/``indexed`` — the
    shape a partially swept rebuild or a lost derived table leaves behind.
    A retrieve naming that attachment must answer
    ``attachment_index_unavailable`` on the envelope; answering
    ``{"attachment_chunks": []}`` with no error would tell the model the
    text is not in there, which is the exact lie T3 forbids.
    """
    owned = _Owned(registry)
    try:
        thread_id, attachment_id = _prepare_to_ready(
            live_env, service, threads, owned, path=FIXTURE_A, filename="compliance-001.md"
        )
        units_before, postings_before = _derived_counts(live_env, attachment_id)
        assert postings_before >= 1, (units_before, postings_before)

        deleted = _corrupt_postings(live_env, attachment_id)
        assert deleted == postings_before, (deleted, postings_before)
        # The premise, re-read after the corruption: the row still claims
        # ready/indexed and the frozen units are untouched. Only the
        # derived postings are gone.
        row = _attachment_row(live_env, attachment_id)
        assert row is not None and row["status"] == "ready", row
        assert row["indexed"] is True, row
        units_after, postings_after = _derived_counts(live_env, attachment_id)
        assert (units_after, postings_after) == (units_before, 0), row

        _out, envelope = _run_grounded_retrieve(
            service=service,
            company=company_live,
            thread_id=thread_id,
            tool_args={"query": MIXED_QUERY, "attachment_ids": [attachment_id]},
        )

        assert envelope.get("attachment_error") == {
            "error": ERROR_INDEX_UNAVAILABLE
        }, envelope
        assert _attachment_payload(envelope) == [], envelope
        assert FIXTURE_A_PHRASE.casefold() not in json.dumps(envelope).casefold(), (
            "an unavailable index must not hand back text it never served"
        )
    finally:
        _cleanup_live(live_env, owned, service.objects)


# =========================================================================
# Case 5: naming one attachment narrows the branch to that id only.
# =========================================================================


def test_naming_one_of_two_ready_attachments_excludes_the_other(
    live_env: str,
    service: Any,
    threads: PostgresThreadStore,
    company_live: Any,
    registry: dict[str, list[Any]],
) -> None:
    """Ticket case 5: selection narrows the attachment branch, nothing else.

    One thread, two ready attachments whose committed texts share exactly
    one searchable phrase. Unnamed, the branch owes both — that is what
    makes the narrowed run's single-id answer a fact about naming rather
    than about ranking. Named, every attachment hit carries the named id
    and the other attachment's text is absent from the branch.
    """
    owned = _Owned(registry)
    try:
        thread_id, first = _prepare_to_ready(
            live_env, service, threads, owned, path=FIXTURE_A, filename="compliance-001.md"
        )
        _same_thread, second = _prepare_to_ready(
            live_env,
            service,
            threads,
            owned,
            path=FIXTURE_B,
            filename="engineering-001.md",
            thread_id=thread_id,
        )
        # Both belong to the one thread and both are searchable; the
        # shared boilerplate term must therefore reach both unnamed.
        assert _attachment_row(live_env, second) is not None

        _out, wide = _run_grounded_retrieve(
            service=service,
            company=company_live,
            thread_id=thread_id,
            tool_args={"query": NARROWING_QUERY},
        )
        wide_ids = {hit["source_ref"]["attachment_id"] for hit in _attachment_payload(wide)}
        assert wide_ids == {first, second}, wide

        _out, narrowed = _run_grounded_retrieve(
            service=service,
            company=company_live,
            thread_id=thread_id,
            tool_args={"query": NARROWING_QUERY, "attachment_ids": [first]},
        )
        narrow_hits = _attachment_payload(narrowed)
        assert narrow_hits, narrowed
        assert "attachment_error" not in narrowed, narrowed
        assert {
            hit["source_ref"]["attachment_id"] for hit in narrow_hits
        } == {first}, narrowed
        assert FIXTURE_B_PHRASE.casefold() not in json.dumps(
            [
                {
                    "text": hit["text"],
                    "source_ref": hit["source_ref"],
                }
                for hit in narrow_hits
            ]
        ).casefold(), "the excluded attachment's text still reached the branch"
    finally:
        _cleanup_live(live_env, owned, service.objects)


# =========================================================================
# Re-runnability (same contract the T2f live module pins for itself).
# =========================================================================


def test_module_leaves_no_live_state_behind(
    live_env: str, registry: dict[str, list[Any]]
) -> None:
    """Every identity this module minted is gone from every real store.

    A live suite that leaves ready rows, durable jobs, postings, or bytes
    behind hands the next boot's recover sweep someone else's work to
    re-adopt, and it turns the object-store bucket into a leak. The byte
    leg is measured before the rows drop (the key is gone below).
    """
    threads = list(registry["threads"])
    attachments = list(registry["attachments"])
    assert threads, "the registry lost the threads this module created"
    assert attachments, "the registry lost the attachments this module created"

    # ``_cleanup_live`` already verified each key raises
    # UnknownObjectKeyError after its delete; assert the rows are gone
    # and that no key this module's threads owned survives in the tables.
    assert _thread_count(live_env, threads) == 0, threads
    assert int(
        _sql_rows(
            live_env,
            "SELECT count(*) FROM fin_thread_attachment WHERE thread_id = ANY(%s)",
            (threads,),
        )[0][0]
    ) == 0, threads
    assert _job_rows_count(live_env, attachments) == 0, attachments
    assert _derived_counts_total(live_env, attachments) == 0, attachments
    assert _object_keys(live_env, threads) == [], threads
