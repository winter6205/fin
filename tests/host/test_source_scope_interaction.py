"""REAL interaction acceptance for the source-scope enforcement gate.

Spec ``specs/question-source-scope.md`` (decisions 4, 5, 7; SC-2, SC-3,
SC-4) and ADR 0088. Every expected value here is derived from the spec,
NOT read off ``fin.host.source_scope`` or the wiring in ``graph.py``:

* a known locator outside the effective scope is refused BEFORE the read
  (SC-3, decision 5) — proven on the legs, not merely on the result;
* a cross-thread locator cannot bypass the scope (ADR 0088 core);
* a not-ready-but-authorized attachment is ``unavailable``, distinct from
  ``denied`` (decision 1 / SC-4: readiness is not permission);
* a restriction that merely NAMES attachments narrows the attachment branch
  only and leaves the company corpus eligible (ADR 0088:10);
* a restriction the operator declared exclusive puts the company corpus outside
  the effective scope, and the corpus is refused BEFORE it is consulted with
  that branch's own denial code rather than a capability or dependency verdict
  (decision 5, SC-4);
* a search that looked inside the scope and matched nothing is a successful
  empty, never a scope error (SC-4).

What makes this an INTERACTION test, not a unit test: it drives the ACTUAL
shipped graph through ``compile_graph`` — the same factory the server uses —
with a ``ScriptedChatModel`` for the plan, behind a REAL
:class:`~fin.host.attachment_store.InMemoryAttachmentStore` as the store the
attachment seam is wired to. Nothing about the gate, the store, or the tools
is stubbed: the object under test is the enforcement path exactly as wired,
and the assertions are on the observable end-of-run channels — the
``ToolMessage`` the model receives, ``AgentState``'s evidence / citation
channels, and what the wrapped legs recorded.

Hermetic: in-memory thread / attachment stores and an in-process fake
company port only. It needs no live service, so it is deliberately UNMARKED
and runs in the default ``uv run pytest -q`` suite.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from fin.corpus.extracted import Locator
from fin.host import ScriptedChatModel, compile_graph
from fin.host.attachment_index import build_postings
from fin.host.attachment_retrieval import AttachmentChunk, AttachmentLocator
from fin.host.attachment_source_read import AttachmentSourceReader
from fin.host.attachment_store import (
    InMemoryAttachmentStore,
    ParsedAttachmentUnit,
)
from fin.host.source_scope import (
    SCOPE_ATTACHMENT_NOT_PERMITTED,
    SCOPE_ATTACHMENT_NOT_READY,
    SCOPE_COMPANY_NOT_PERMITTED,
    ScopeRestriction,
)
from fin.host.state import reset_input_state
from fin.host.thread_store import InMemoryThreadStore
from fin.retrieve import Chunk
from fin.retrieve.fake_port import FakeRetrievePort
from fin.retrieve.source_read import ATTACHMENT_KIND

# A research-track question, so the grounded tools really run (mirrors the
# existing attachment suites).
GROUNDED = "What was AAPL net sales in 2023 Q3?"

COMPANY_TEXT = "Apple net sales for the fiscal 2023 fourth quarter were 89,493 million."
ATTACHMENT_TEXT = "The term sheet lists a purchase price of 12,000 thousand dollars."
CROSS_THREAD_SECRET = "Thread A only: renegotiation floor of 9,750 thousand."


# ---------------------------------------------------------------------------
# harness — the real store behind the real shipped graph
# ---------------------------------------------------------------------------


class _Service:
    """The wired attachment seam, shaped like the deployed service.

    The scope facts and the derived reader are built from ``store`` alone, so
    this is the minimal real seam: a store the host trusts, with no turn-time
    preparation involved (the runs below name no incoming attachment).
    """

    def __init__(self, store: InMemoryAttachmentStore) -> None:
        self.store = store


class _RecordingReader:
    """The real attachment leg, wrapped so its reads stay observable.

    The wrapper changes no behavior: it forwards ``read_slice`` unchanged and
    records the call. That record is the "before, not after" proof — a
    refused locator must leave ``reads`` empty, which is what distinguishes a
    gate that ran before the read from a filter that ran after it.
    """

    def __init__(self, inner: AttachmentSourceReader, reads: list[Any]) -> None:
        self._inner = inner
        self._reads = reads

    def read_slice(
        self, source_ref: Mapping[str, Any], position: int = 0
    ) -> dict[str, Any]:
        self._reads.append((dict(source_ref), position))
        return self._inner.read_slice(source_ref, position)

    # The identity faces the citation builder asks of the run's own leg; they
    # are forwarded untouched so a permitted read earns evidence exactly as
    # the unwrapped leg would.
    def owned_record(self, attachment_id: str) -> Any:
        return self._inner.owned_record(attachment_id)

    def identity_is_current(
        self, attachment_id: str, version_int: int | None
    ) -> bool:
        return self._inner.identity_is_current(attachment_id, version_int)


class _RecordingReaderFactory:
    """A reader factory that records construction and every leg read.

    Only ``reads`` carries the enforcement proof: ``read_source`` builds the
    leg BEFORE it consults the gate (the reader is constructed up front and
    the slate is handed to the leg only when permitted), so a factory-call
    count alone cannot show "gate before read". ``read_slice`` can.
    """

    def __init__(self, store: InMemoryAttachmentStore) -> None:
        self._store = store
        self.built: list[str] = []
        self.reads: list[Any] = []

    def __call__(self, thread_id: str) -> _RecordingReader:
        self.built.append(thread_id)
        return _RecordingReader(
            AttachmentSourceReader(self._store, thread_id), self.reads
        )


class _RecordingSearchPort:
    """The attachment search seam: records every call, answers from a table."""

    def __init__(
        self, by_thread: dict[str, list[AttachmentChunk]] | None = None
    ) -> None:
        self.by_thread = by_thread or {}
        self.calls: list[dict[str, Any]] = []

    def search(
        self,
        *,
        thread_id: str,
        query: str,
        attachment_ids: list[str] | None = None,
        limit: int | None = None,
    ) -> list[AttachmentChunk]:
        self.calls.append(
            {
                "thread_id": thread_id,
                "query": query,
                "attachment_ids": None
                if attachment_ids is None
                else list(attachment_ids),
                "limit": limit,
            }
        )
        chunks = list(self.by_thread.get(thread_id, []))
        if attachment_ids is not None:
            allowed = set(attachment_ids)
            chunks = [c for c in chunks if c.source_ref.attachment_id in allowed]
        return chunks[: (8 if limit is None else limit)]


class _World:
    """One thread store, its attachment store, and the created thread id."""

    def __init__(self) -> None:
        self.threads = InMemoryThreadStore()
        self.store = InMemoryAttachmentStore(self.threads)
        self.thread_id = self.threads.create_thread(title="Thread").id

    def thread(self, title: str = "Other") -> str:
        return self.threads.create_thread(title=title).id


def _seed_ready(
    world: _World,
    attachment_id: str,
    *,
    text: str = ATTACHMENT_TEXT,
    version: int = 1,
    thread_id: str | None = None,
    filename: str = "memo.txt",
) -> str:
    """Create one READY, indexed attachment owned by ``thread_id``.

    The row is written through the store's own doors in the order preparation
    uses them, so the record the gate trusts is the record an upload really
    produces.
    """
    owner = thread_id or world.thread_id
    world.store.create(
        thread_id=owner,
        attachment_id=attachment_id,
        filename=filename,
        size_bytes=1_024,
        sha256="ab" * 32,
        object_key=f"threads/{owner}/{attachment_id}",
    )
    units = (ParsedAttachmentUnit(unit=1, text=text),)
    world.store.record_units(attachment_id=attachment_id, version=version, units=units)
    world.store.put_index(
        attachment_id=attachment_id, version=version, postings=build_postings(units)
    )
    world.store.mark_ready(attachment_id=attachment_id, version=version, unit_count=1)
    return owner


def _company_port() -> FakeRetrievePort:
    return FakeRetrievePort(
        hits_by_query={
            GROUNDED: [
                Chunk(
                    text=COMPANY_TEXT,
                    source_ref=Locator(
                        document_id="AAPL-2023Q3", version="d" * 64, unit=3
                    ),
                    score=1.5,
                )
            ]
        },
        known_document_ids={"AAPL-2023Q3"},
    )


def _attachment_ref(attachment_id: str, version: object = "1") -> dict[str, Any]:
    """The locator the model would copy from a retrieve hit for the id."""
    return {
        "kind": ATTACHMENT_KIND,
        "attachment_id": attachment_id,
        "version": version,
        "unit": 1,
    }


def _compile(
    world: _World,
    *,
    script: list[AIMessage],
    service: Any,
    reader_factory: Any = None,
    search_port: Any = None,
    company_port: FakeRetrievePort | None = None,
    scope_restriction: ScopeRestriction | None = None,
) -> Any:
    return compile_graph(
        checkpointer=InMemorySaver(),
        model=ScriptedChatModel(script=script),
        adapter=company_port if company_port is not None else _company_port(),
        known_document_ids={"AAPL-2023Q3"},
        attachment_service=service,
        attachment_source_reader_factory=reader_factory,
        attachment_retriever=search_port,
        scope_restriction=scope_restriction,
    )


def _run(graph: Any, thread_id: str) -> dict[str, Any]:
    return graph.invoke(
        reset_input_state(GROUNDED),
        config={"configurable": {"thread_id": thread_id}},
    )


def _tool_payloads(out: Mapping[str, Any], name: str) -> list[dict[str, Any]]:
    messages = [
        m
        for m in out["messages"]
        if isinstance(m, ToolMessage) and getattr(m, "name", None) == name
    ]
    return [json.loads(str(m.content)) for m in messages]


def _single_tool_payload(out: Mapping[str, Any], name: str) -> dict[str, Any]:
    payloads = _tool_payloads(out, name)
    assert len(payloads) == 1, f"expected one {name} result: {payloads}"
    return payloads[0]


def _read_call(attachment_id: str, *, version: object = "1") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "read_source",
                "args": {"source_ref": _attachment_ref(attachment_id, version), "position": 0},
                "id": "c-read",
                "type": "tool_call",
            }
        ],
    )


def _retrieve_call() -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {"name": "retrieve", "args": {"query": GROUNDED}, "id": "c-retrieve", "type": "tool_call"}
        ],
    )


def _attachment_chunk(attachment_id: str, *, version: int = 1, unit: int = 1) -> AttachmentChunk:
    return AttachmentChunk(
        text=ATTACHMENT_TEXT,
        source_ref=AttachmentLocator(
            attachment_id=attachment_id, version=version, unit=unit
        ),
        score=0.75,
    )


# ---------------------------------------------------------------------------
# 1. in-scope read succeeds — the no-regression baseline
# ---------------------------------------------------------------------------


def test_in_scope_read_returns_text_and_admits_evidence() -> None:
    world = _World()
    _seed_ready(world, "att-1")
    factory = _RecordingReaderFactory(world.store)
    graph = _compile(
        world,
        service=_Service(world.store),
        reader_factory=factory,
        script=[
            _read_call("att-1"),
            AIMessage(content=f"Verbatim: {ATTACHMENT_TEXT}"),
        ],
    )

    out = _run(graph, world.thread_id)

    # The text really comes back through the shipped graph.
    payload = _single_tool_payload(out, "read_source")
    assert payload["text"] == ATTACHMENT_TEXT
    assert "error" not in payload

    # And the read earned exactly one attachment-typed evidence item.
    attachments = [item for item in out["evidence"] if item.kind == ATTACHMENT_KIND]
    assert [item.attachment_id for item in attachments] == ["att-1"]
    assert attachments[0].unit_text == ATTACHMENT_TEXT
    # The leg really ran once — this is the permitted baseline the refusals
    # below are measured against. (``built`` counts reader CONSTRUCTIONS,
    # which the graph may also make for the citation identity re-check, so
    # only its thread binding is asserted; ``reads`` is the read proof.)
    assert len(factory.reads) == 1
    assert set(factory.built) == {world.thread_id}

    # A verbatim quote grounds an attachment citation (end-to-end).
    assert out["status"] == "done", out.get("status_reason")
    assert [c.kind for c in out["citations"]] == [ATTACHMENT_KIND]
    assert out["citations"][0].attachment_id == "att-1"


# ---------------------------------------------------------------------------
# 2. known locator outside the effective scope is refused BEFORE the read
# ---------------------------------------------------------------------------


def test_known_out_of_scope_locator_is_refused_before_the_read_leg_runs() -> None:
    world = _World()
    _seed_ready(world, "att-allowed", text="The allowed body.")
    _seed_ready(world, "att-other", text="Another body the operator did not select.")
    factory = _RecordingReaderFactory(world.store)
    graph = _compile(
        world,
        service=_Service(world.store),
        reader_factory=factory,
        scope_restriction=ScopeRestriction(("att-allowed",), "explicit"),
        script=[_read_call("att-other"), AIMessage(content="Reviewed.")],
    )

    out = _run(graph, world.thread_id)

    # The gate's typed code, NOT a slice, NOT empty text.
    payload = _single_tool_payload(out, "read_source")
    assert payload == {"error": SCOPE_ATTACHMENT_NOT_PERMITTED}
    assert "text" not in payload

    # The "before, not after" proof: the read leg was never reached, so no
    # out-of-scope text was ever fetched and then filtered.
    assert factory.reads == []
    assert out["evidence"] == []
    # The refused attachment's text never appeared on the wire.
    serialized = json.dumps([str(m.content) for m in out["messages"]])
    assert "Another body the operator did not select." not in serialized


def test_the_permitted_attachment_of_the_same_restriction_still_reads() -> None:
    """The restriction narrows the branch; it does not disable it."""
    world = _World()
    _seed_ready(world, "att-allowed", text=ATTACHMENT_TEXT)
    factory = _RecordingReaderFactory(world.store)
    graph = _compile(
        world,
        service=_Service(world.store),
        reader_factory=factory,
        scope_restriction=ScopeRestriction(("att-allowed",), "explicit"),
        script=[_read_call("att-allowed"), AIMessage(content="Reviewed.")],
    )

    out = _run(graph, world.thread_id)

    assert _single_tool_payload(out, "read_source")["text"] == ATTACHMENT_TEXT
    assert len(factory.reads) == 1


# ---------------------------------------------------------------------------
# 3. cross-thread locator cannot bypass the scope (ADR 0088 core)
# ---------------------------------------------------------------------------


def test_a_cross_thread_locator_cannot_bypass_the_scope() -> None:
    world = _World()
    # Thread A owns a real, ready attachment with a distinctive body.
    thread_a = world.thread("Thread A")
    _seed_ready(world, "att-a", text=CROSS_THREAD_SECRET, thread_id=thread_a)
    # Thread B owns nothing; the run below is bound to thread B.
    thread_b = world.thread("Thread B")
    factory = _RecordingReaderFactory(world.store)
    graph = _compile(
        world,
        service=_Service(world.store),
        reader_factory=factory,
        # No restriction: thread B's default scope is simply its own (empty)
        # attachment set, so A's id is out of scope regardless.
        script=[_read_call("att-a"), AIMessage(content="Reviewed.")],
    )

    out = _run(graph, thread_b)

    payload = _single_tool_payload(out, "read_source")
    assert payload == {"error": SCOPE_ATTACHMENT_NOT_PERMITTED}
    assert factory.reads == []
    assert out["evidence"] == []
    # None of thread A's attachment body reached thread B.
    serialized = json.dumps([str(m.content) for m in out["messages"]])
    assert CROSS_THREAD_SECRET not in serialized
    assert "att-a" not in serialized


# ---------------------------------------------------------------------------
# 4. the company branch under a restriction that only names attachments
# ---------------------------------------------------------------------------


def test_company_branch_still_works_when_a_restriction_only_names_attachments() -> None:
    """Naming attachments narrows the attachment branch, never the corpus.

    A restriction with no declared exclusion leaves the company branch eligible
    (ADR 0088:10), so a comparison question still gets corpus material while the
    attachment branch is narrowed to the named id.
    """
    world = _World()
    _seed_ready(world, "att-a")
    _seed_ready(world, "att-b", text="restricted-out body")
    search = _RecordingSearchPort({world.thread_id: [_attachment_chunk("att-a")]})
    factory = _RecordingReaderFactory(world.store)
    graph = _compile(
        world,
        service=_Service(world.store),
        reader_factory=factory,
        search_port=search,
        scope_restriction=ScopeRestriction(("att-a",), "explicit"),
        script=[_retrieve_call(), AIMessage(content="Reviewed.")],
    )

    out = _run(graph, world.thread_id)

    envelope = _single_tool_payload(out, "retrieve")
    # A restriction that names attachments leaves company material eligible
    # (ADR 0088:10); only a declared exclusion removes it.
    assert [c["source_ref"]["document_id"] for c in envelope["chunks"]] == [
        "AAPL-2023Q3"
    ]
    company_evidence = [item for item in out["evidence"] if item.kind == "company"]
    assert [item.locator.document_id for item in company_evidence] == ["AAPL-2023Q3"]
    # And the branch itself narrowed to the restriction rather than widening:
    # the search never saw the restricted-out id.
    assert search.calls[0]["attachment_ids"] == ["att-a"]
    assert "attachment_error" not in envelope


# ---------------------------------------------------------------------------
# 5. a declared exclusion removes the company branch before the corpus read
# ---------------------------------------------------------------------------


def test_an_exclusive_restriction_refuses_the_company_branch_before_the_read() -> None:
    """``company_excluded`` closes the corpus; a capability answer cannot reopen it.

    The same wiring and the same question as the naming case above, with the
    exclusion declared: the corpus is refused with its own denial code, the port
    is never reached (the "before, not after" proof on this leg), and the
    attachment branch keeps answering — an exclusion removes a branch, it does
    not stop the run.
    """
    world = _World()
    _seed_ready(world, "att-a")
    search = _RecordingSearchPort({world.thread_id: [_attachment_chunk("att-a")]})
    corpus = _company_port()
    graph = _compile(
        world,
        service=_Service(world.store),
        search_port=search,
        company_port=corpus,
        scope_restriction=ScopeRestriction(
            ("att-a",), "explicit", company_excluded=True
        ),
        script=[_retrieve_call(), AIMessage(content="Reviewed.")],
    )

    out = _run(graph, world.thread_id)

    envelope = _single_tool_payload(out, "retrieve")
    # The branch ruling, named as itself: the wired adapter is fully capable of
    # this operation, so a refusal here can only be the exclusion.
    assert envelope["error"] == SCOPE_COMPANY_NOT_PERMITTED
    # Refused before the read: the corpus port was never asked, so no company
    # text crossed the boundary to be filtered afterwards.
    assert corpus.calls == []
    assert "chunks" not in envelope
    serialized = json.dumps([str(m.content) for m in out["messages"]])
    assert COMPANY_TEXT not in serialized
    assert all(item.kind != "company" for item in out["evidence"])
    # The attachment branch answers independently of the refusal.
    assert [
        c["source_ref"]["attachment_id"] for c in envelope["attachment_chunks"]
    ] == ["att-a"]
    assert "attachment_error" not in envelope


# ---------------------------------------------------------------------------
# 6. a not-ready attachment reports `unavailable`, never `denied`
# ---------------------------------------------------------------------------


def test_a_not_ready_attachment_reports_unavailable_not_denied() -> None:
    world = _World()
    # Ready + indexed first, so the record has a real current version (1)...
    _seed_ready(world, "att-1")
    # ...then the preparation lifecycle leaves it not-ready (a failure does
    # not mint a new version, so the identity is still "current" yet unusable).
    world.store.mark_failed(attachment_id="att-1", version=1, reason="parse failed")

    factory = _RecordingReaderFactory(world.store)
    graph = _compile(
        world,
        service=_Service(world.store),
        reader_factory=factory,
        # No restriction: the id is fully eligible, so only readiness can
        # stand between it and a read.
        script=[_read_call("att-1", version="1"), AIMessage(content="Reviewed.")],
    )

    out = _run(graph, world.thread_id)

    payload = _single_tool_payload(out, "read_source")
    # Availability, not permission: "cannot answer yet", distinct from denial.
    assert payload == {"error": SCOPE_ATTACHMENT_NOT_READY}
    assert payload["error"] != SCOPE_ATTACHMENT_NOT_PERMITTED
    # Readiness never grants access and never reaches the leg.
    assert factory.reads == []
    assert out["evidence"] == []


def test_an_unprepared_attachment_is_also_unavailable() -> None:
    """The clean "uploaded but never prepared" shape is the same outcome."""
    world = _World()
    world.store.create(
        thread_id=world.thread_id,
        attachment_id="att-raw",
        filename="memo.txt",
        size_bytes=1_024,
        sha256="cd" * 32,
        object_key=f"threads/{world.thread_id}/att-raw",
    )
    factory = _RecordingReaderFactory(world.store)
    graph = _compile(
        world,
        service=_Service(world.store),
        reader_factory=factory,
        # An unprepared record is at version 0; the model names that version.
        script=[_read_call("att-raw", version="0"), AIMessage(content="Reviewed.")],
    )

    out = _run(graph, world.thread_id)

    assert _single_tool_payload(out, "read_source") == {
        "error": SCOPE_ATTACHMENT_NOT_READY
    }
    assert factory.reads == []
    assert out["evidence"] == []


# ---------------------------------------------------------------------------
# 7. successful-empty stays successful-empty (SC-4)
# ---------------------------------------------------------------------------


def test_a_scoped_retrieve_with_no_matching_content_is_a_successful_empty() -> None:
    world = _World()
    _seed_ready(world, "att-1")
    # The index is healthy and answers — with nothing matching.
    search = _RecordingSearchPort({world.thread_id: []})
    graph = _compile(
        world,
        service=_Service(world.store),
        search_port=search,
        scope_restriction=ScopeRestriction(("att-1",), "explicit"),
        script=[_retrieve_call(), AIMessage(content="Reviewed.")],
    )

    out = _run(graph, world.thread_id)

    envelope = _single_tool_payload(out, "retrieve")
    # Looked, found nothing: an empty success, never a scope error.
    assert envelope["attachment_chunks"] == []
    assert "attachment_error" not in envelope
    # The search really ran, narrowed to the effective selection.
    assert search.calls[0]["attachment_ids"] == ["att-1"]
