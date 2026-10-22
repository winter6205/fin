"""The ACCEPTED source-scope snapshot must constrain every retrieval read.

The document-choice resume commits a versioned selection to the
thread checkpoint (``graph.py``'s document-choice node writes
``update["source_scope"]``) and ``_LockedGraph.source_scope`` reads it back
off that same channel. That committed snapshot — not the COMPILE-TIME
``scope_restriction`` — is the run-dependent input every read boundary
resolves its scope from, so a user who picked one document cannot have
retrieval read the whole authorized corpus.

What is asserted here, all through the REAL shipped graph
(``compile_graph``, ``InMemorySaver``):

* a committed restricted snapshot narrows ``read_source``, the attachment
  retrieval branch, and the company text-admission boundary — one selection,
  four boundaries, no bypass;
* an INVALID snapshot (malformed, unsupported version, an id the current
  authority does not stand behind) NEVER widens to the whole corpus: it
  produces the module's typed code instead of a broad pass;
* a thread with NO committed snapshot behaves exactly as before — the
  compile-time restriction path is untouched and stays the only input.

A snapshot is only ever written by the host (a Runtime-accepted reply), so
every test seeds it through the graph's own state channel rather than through
model output: the snapshot is not a value the model can reach.

Hermetic: in-memory thread / attachment stores and the in-process fake
company port. No database, no network.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from fin.corpus.extracted import Locator
from fin.host import ScriptedChatModel, compile_graph
from fin.host.attachment.attachment_index import build_postings
from fin.host.attachment.attachment_retrieval import AttachmentChunk, AttachmentLocator
from fin.host.attachment.attachment_source_read import AttachmentSourceReader
from fin.host.attachment.attachment_store import (
    InMemoryAttachmentStore,
    ParsedAttachmentUnit,
)
from fin.host.source_scope import (
    SCOPE_ATTACHMENT_NOT_PERMITTED,
    SCOPE_ATTACHMENT_VERSION_STALE,
    SCOPE_COMPANY_NOT_PERMITTED,
    SCOPE_SNAPSHOT_UNREADABLE,
    ScopeRestriction,
    SourceScopeSnapshot,
)
from fin.host.state import reset_input_state
from fin.host.thread_store import InMemoryThreadStore
from fin.retrieve import Chunk
from fin.retrieve.fake_port import FakeRetrievePort
from fin.retrieve.port import (
    DATABASE_ROWS_KEY,
    STRUCTURED_RESULT_KEY,
    DocumentScopeEntry,
    UNSUPPORTED_SOURCE_SCOPE_CODE,
)
from fin.retrieve.source_read import ATTACHMENT_KIND

GROUNDED = "What was AAPL net sales in 2023 Q3?"

# Two corpus documents, so a document-level narrowing is observable: without
# one the whole corpus answers, with one only the named document does.
DOC_A = "AAPL-2023Q3"
DOC_B = "AAPL-2022Q4"
DOC_A_TEXT = "Apple net sales for the fiscal 2023 fourth quarter were 89,493 million."
DOC_B_TEXT = "Apple net sales for the fiscal 2022 fourth quarter were 82,959 million."
CORPUS_VERSION = "v" * 8

ATTACHMENT_TEXT = "The term sheet lists a purchase price of 12,000 thousand dollars."


# ---------------------------------------------------------------------------
# harness — the real store behind the real shipped graph
# ---------------------------------------------------------------------------


class _Service:
    """The wired attachment seam, shaped like the deployed service.

    Built from ``store`` alone: the minimal real seam, with no turn-time
    preparation involved (the runs below name no incoming attachment).
    """

    def __init__(self, store: InMemoryAttachmentStore) -> None:
        self.store = store


class _RecordingReaderFactory:
    """A reader factory whose ``read_slice`` calls stay observable.

    ``reads`` is the "before, not after" proof: a refused locator must leave
    it empty, which is what distinguishes a gate that ran before the read from
    a filter that ran after it.
    """

    def __init__(self, store: InMemoryAttachmentStore) -> None:
        self._store = store
        self.reads: list[Any] = []

    def __call__(self, thread_id: str) -> Any:
        reader = AttachmentSourceReader(self._store, thread_id)
        inner_read_slice = reader.read_slice

        def read_slice(source_ref: Mapping[str, Any], position: int = 0) -> dict:
            self.reads.append((dict(source_ref), position))
            return inner_read_slice(source_ref, position)

        reader.read_slice = read_slice  # type: ignore[method-assign]
        return reader


class _RecordingSearchPort:
    """The attachment search seam: records every call, answers from a table."""

    def __init__(self, by_thread: dict[str, list[AttachmentChunk]] | None = None) -> None:
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
) -> str:
    """Create one READY, indexed attachment owned by ``thread_id``.

    The row is written through the store's own doors in the order preparation
    uses them, so the record the gate trusts is the record an upload produces.
    """
    owner = thread_id or world.thread_id
    world.store.create(
        thread_id=owner,
        attachment_id=attachment_id,
        filename="memo.txt",
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
                    text=DOC_A_TEXT,
                    source_ref=Locator(
                        document_id=DOC_A, version=CORPUS_VERSION, unit=3
                    ),
                    score=1.5,
                ),
                Chunk(
                    text=DOC_B_TEXT,
                    source_ref=Locator(
                        document_id=DOC_B, version=CORPUS_VERSION, unit=3
                    ),
                    score=1.4,
                ),
            ]
        },
        known_document_ids={DOC_A, DOC_B},
        # The corpus authority the recheck consults: ``(document_id, version)``
        # pairs the catalog-equivalent table actually holds.
        manifest_docs={
            DOC_A: {"version": CORPUS_VERSION, "text": DOC_A_TEXT},
            DOC_B: {"version": CORPUS_VERSION, "text": DOC_B_TEXT},
        },
    )


def _snapshot(
    *sources: tuple[str, str, str], mode: str = "restricted", version: int = 1
) -> dict[str, Any]:
    """The wire shape a Runtime-accepted reply commits to the checkpoint.

    ``sources`` are ``(source_kind, source_id, version)`` triples. Built
    through the module's own validation so a test cannot seed a snapshot the
    writer would have refused.
    """
    if mode == "unrestricted":
        raw: dict[str, Any] = {
            "schema_version": 1,
            "mode": "unrestricted",
            "sources": [],
            "accepted_input_version": version,
        }
    else:
        raw = {
            "schema_version": 1,
            "mode": "restricted",
            "sources": [
                {"source_kind": kind, "source_id": sid, "version": ver}
                for kind, sid, ver in sources
            ],
            "accepted_input_version": version,
        }
    SourceScopeSnapshot.from_state_dict(raw)
    return raw


def _compile(
    world: _World,
    *,
    script: list[AIMessage],
    service: Any = None,
    reader_factory: Any = None,
    search_port: Any = None,
    company_port: FakeRetrievePort | None = None,
    scope_restriction: ScopeRestriction | None = None,
) -> Any:
    return compile_graph(
        checkpointer=InMemorySaver(),
        model=ScriptedChatModel(script=script),
        adapter=company_port if company_port is not None else _company_port(),
        known_document_ids={DOC_A, DOC_B},
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


def _commit(graph: Any, thread_id: str, raw: Mapping[str, Any]) -> None:
    """Write an accepted snapshot onto the thread's own state channel.

    Seeding through ``AgentState`` is the only way a test can present a
    snapshot to the read path: the channel is the same one the
    document-choice resume commits to, and nothing the model says reaches it.

    The write is a plain ``update_state`` with no ``as_node`` so the run's
    messages are left untouched — seeding a snapshot must not itself model a
    turn, and a thread with no checkpoint yet is exactly the state a
    just-accepted reply writes into.
    """
    graph._graph.update_state(
        {"configurable": {"thread_id": thread_id}},
        {"source_scope": dict(raw)},
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


def _attachment_ref(attachment_id: str, version: object = "1") -> dict[str, Any]:
    return {
        "kind": ATTACHMENT_KIND,
        "attachment_id": attachment_id,
        "version": version,
        "unit": 1,
    }


def _company_ref(document_id: str, *, version: str = CORPUS_VERSION) -> dict[str, Any]:
    return {"document_id": document_id, "version": version, "unit": 3}


def _read_call(ref: dict[str, Any]) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "read_source",
                "args": {"source_ref": ref, "position": 0},
                "id": "c-read",
                "type": "tool_call",
            }
        ],
    )


def _retrieve_call(
    document_id: str | None = None,
    structured_request: dict[str, Any] | None = None,
) -> AIMessage:
    arguments: dict[str, Any] = {"query": GROUNDED}
    if document_id is not None:
        arguments["document_id"] = document_id
    if structured_request is not None:
        arguments["structured_request"] = structured_request
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "retrieve",
                "args": arguments,
                "id": "c-retrieve",
                "type": "tool_call",
            }
        ],
    )


def _attachment_chunk(attachment_id: str) -> AttachmentChunk:
    return AttachmentChunk(
        text=ATTACHMENT_TEXT,
        source_ref=AttachmentLocator(attachment_id=attachment_id, version=1, unit=1),
        score=0.75,
    )


# ---------------------------------------------------------------------------
# 1. the committed snapshot reaches the read boundaries
# ---------------------------------------------------------------------------


def test_a_committed_attachment_snapshot_narrows_the_read_leg() -> None:
    """An accepted selection names one attachment; the other is refused.

    No compile-time restriction is supplied at all, so every narrowing here
    comes from the checkpointed snapshot alone.
    """
    world = _World()
    _seed_ready(world, "att-picked", text="The body the user picked.")
    _seed_ready(world, "att-other", text="Another body nobody selected.")
    factory = _RecordingReaderFactory(world.store)
    graph = _compile(
        world,
        service=_Service(world.store),
        reader_factory=factory,
        script=[_read_call(_attachment_ref("att-other")), AIMessage(content="Reviewed.")],
    )
    _commit(graph, world.thread_id, _snapshot(("attachment", "att-picked", "1")))

    out = _run(graph, world.thread_id)

    payload = _single_tool_payload(out, "read_source")
    assert payload == {"error": SCOPE_ATTACHMENT_NOT_PERMITTED}
    assert "text" not in payload
    # Gated BEFORE the leg: no out-of-scope text was fetched and then filtered.
    assert factory.reads == []
    serialized = json.dumps([str(m.content) for m in out["messages"]])
    assert "Another body nobody selected." not in serialized


def test_the_attachment_the_snapshot_names_still_reads() -> None:
    """The snapshot narrows the branch; it does not disable it."""
    world = _World()
    _seed_ready(world, "att-picked", text="The body the user picked.")
    _seed_ready(world, "att-other", text="Another body nobody selected.")
    factory = _RecordingReaderFactory(world.store)
    graph = _compile(
        world,
        service=_Service(world.store),
        reader_factory=factory,
        script=[_read_call(_attachment_ref("att-picked")), AIMessage(content="Reviewed.")],
    )
    _commit(graph, world.thread_id, _snapshot(("attachment", "att-picked", "1")))

    out = _run(graph, world.thread_id)

    payload = _single_tool_payload(out, "read_source")
    assert payload["text"] == "The body the user picked."
    assert "error" not in payload
    assert len(factory.reads) == 1
    assert out["evidence"][0].attachment_id == "att-picked"


def test_a_committed_snapshot_narrows_the_attachment_search_branch() -> None:
    """The retrieval branch searches the selection, not the whole thread."""
    world = _World()
    _seed_ready(world, "att-picked")
    _seed_ready(world, "att-other")
    search = _RecordingSearchPort(
        {
            world.thread_id: [
                _attachment_chunk("att-picked"),
                _attachment_chunk("att-other"),
            ]
        }
    )
    graph = _compile(
        world,
        service=_Service(world.store),
        search_port=search,
        script=[_retrieve_call(), AIMessage(content="Reviewed.")],
    )
    _commit(graph, world.thread_id, _snapshot(("attachment", "att-picked", "1")))

    out = _run(graph, world.thread_id)

    envelope = _single_tool_payload(out, "retrieve")
    # The search never saw the id the snapshot does not name.
    assert search.calls[0]["attachment_ids"] == ["att-picked"]
    assert [
        chunk["source_ref"]["attachment_id"] for chunk in envelope["attachment_chunks"]
    ] == ["att-picked"]
    assert "attachment_error" not in envelope


def test_a_committed_corpus_snapshot_narrows_the_company_branch() -> None:
    """A corpus selection admits only the named document's text.

    The company branch stays ELIGIBLE (an explicit selection is not the
    ``company_excluded`` case), but only the named document is admitted — the
    other hit is never admitted as evidence and never reaches the model.
    """
    world = _World()
    corpus = _company_port()
    graph = _compile(
        world,
        company_port=corpus,
        script=[_retrieve_call(), AIMessage(content="Reviewed.")],
    )
    _commit(
        graph, world.thread_id, _snapshot(("corpus", DOC_A, CORPUS_VERSION))
    )

    out = _run(graph, world.thread_id)

    envelope = _single_tool_payload(out, "retrieve")
    assert [chunk["source_ref"]["document_id"] for chunk in envelope["chunks"]] == [
        DOC_A
    ]
    assert [item.locator.document_id for item in out["evidence"]] == [DOC_A]
    assert corpus.calls[0]["document_scope"] == (
        DocumentScopeEntry(DOC_A, CORPUS_VERSION),
    )
    serialized = json.dumps([str(m.content) for m in out["messages"]])
    assert DOC_B_TEXT not in serialized


def test_a_corpus_snapshot_refuses_structured_retrieval_before_the_port() -> None:
    world = _World()
    corpus = _company_port()
    graph = _compile(
        world,
        company_port=corpus,
        script=[
            _retrieve_call(
                structured_request={
                    "entity_type": "Organization",
                    "select": ["name"],
                }
            ),
            AIMessage(content="Reviewed."),
        ],
    )
    _commit(graph, world.thread_id, _snapshot(("corpus", DOC_A, CORPUS_VERSION)))

    out = _run(graph, world.thread_id)

    assert _single_tool_payload(out, "retrieve") == {
        "error": UNSUPPORTED_SOURCE_SCOPE_CODE
    }
    assert corpus.calls == []


def test_blank_document_id_with_a_corpus_snapshot_keeps_the_miss_shape() -> None:
    world = _World()
    corpus = _company_port()
    graph = _compile(
        world,
        company_port=corpus,
        script=[_retrieve_call("  "), AIMessage(content="Reviewed.")],
    )
    _commit(graph, world.thread_id, _snapshot(("corpus", DOC_A, CORPUS_VERSION)))

    out = _run(graph, world.thread_id)

    assert _single_tool_payload(out, "retrieve") == {"chunks": []}
    assert corpus.calls[0]["document_scope"] == (
        DocumentScopeEntry(DOC_A, CORPUS_VERSION),
    )


def test_no_corpus_snapshot_preserves_unrestricted_retrieve_call_shape() -> None:
    world = _World()
    corpus = _company_port()
    graph = _compile(
        world,
        company_port=corpus,
        script=[_retrieve_call(), AIMessage(content="Reviewed.")],
    )

    out = _run(graph, world.thread_id)

    envelope = _single_tool_payload(out, "retrieve")
    assert [chunk["source_ref"]["document_id"] for chunk in envelope["chunks"]] == [
        DOC_A,
        DOC_B,
    ]
    assert "document_scope" not in corpus.calls[0]


def test_an_explicit_document_id_intersects_the_committed_corpus_scope() -> None:
    world = _World()
    corpus = _company_port()
    graph = _compile(
        world,
        company_port=corpus,
        script=[_retrieve_call(DOC_B), AIMessage(content="Reviewed.")],
    )
    _commit(graph, world.thread_id, _snapshot(("corpus", DOC_A, CORPUS_VERSION)))

    out = _run(graph, world.thread_id)

    envelope = _single_tool_payload(out, "retrieve")
    assert envelope == {"error": UNSUPPORTED_SOURCE_SCOPE_CODE}
    assert corpus.calls == []


class _ScopeIgnoringFake(FakeRetrievePort):
    """Return all scripted hits so the host's version post-filter is exercised."""

    def __init__(self, hits: list[Chunk]) -> None:
        super().__init__(
            hits_by_query={GROUNDED: hits},
            known_document_ids={DOC_A, DOC_B},
            manifest_docs={
                DOC_A: {"version": CORPUS_VERSION, "text": DOC_A_TEXT},
                DOC_B: {"version": CORPUS_VERSION, "text": DOC_B_TEXT},
            },
        )
        self.received_scope: tuple[DocumentScopeEntry, ...] | None = None

    def retrieve(self, query: str, **kwargs: Any) -> dict[str, Any]:
        self.received_scope = kwargs.pop("document_scope", None)
        return super().retrieve(query, **kwargs)


class _ScopeIgnoringFactPort(FakeRetrievePort):
    """Return fact siblings without a document/version locator channel."""

    def __init__(self, result_key: str) -> None:
        super().__init__(hits_by_query={GROUNDED: []})
        self.result_key = result_key
        self.marker = "unscoped fact must stay private"

    def retrieve(self, query: str, **kwargs: Any) -> dict[str, Any]:
        kwargs.pop("document_scope", None)
        super().retrieve(query, **kwargs)
        return {"chunks": [], self.result_key: {"rows": [{"value": self.marker}]}}


@pytest.mark.parametrize(
    "result_key",
    [DATABASE_ROWS_KEY, STRUCTURED_RESULT_KEY],
    ids=["database-facts", "structured-report"],
)
def test_host_refuses_fact_siblings_without_document_version_locators(
    result_key: str,
) -> None:
    world = _World()
    corpus = _ScopeIgnoringFactPort(result_key)
    graph = _compile(
        world,
        company_port=corpus,
        script=[_retrieve_call(), AIMessage(content="Reviewed.")],
    )
    _commit(graph, world.thread_id, _snapshot(("corpus", DOC_A, CORPUS_VERSION)))

    out = _run(graph, world.thread_id)

    assert _single_tool_payload(out, "retrieve") == {
        "error": UNSUPPORTED_SOURCE_SCOPE_CODE
    }
    serialized = json.dumps([str(message.content) for message in out["messages"]])
    assert corpus.marker not in serialized


def test_host_return_defense_checks_the_selected_document_version() -> None:
    world = _World()
    stale = Chunk(
        text="Old version text must not reach the model.",
        source_ref=Locator(document_id=DOC_A, version="old-version", unit=3),
        score=2.0,
    )
    corpus = _ScopeIgnoringFake(
        [
            Chunk(
                text=DOC_A_TEXT,
                source_ref=Locator(
                    document_id=DOC_A, version=CORPUS_VERSION, unit=3
                ),
                score=1.5,
            ),
            stale,
            Chunk(
                text=DOC_B_TEXT,
                source_ref=Locator(
                    document_id=DOC_B, version=CORPUS_VERSION, unit=3
                ),
                score=1.4,
            ),
        ]
    )
    graph = _compile(
        world,
        company_port=corpus,
        script=[_retrieve_call(), AIMessage(content="Reviewed.")],
    )
    _commit(graph, world.thread_id, _snapshot(("corpus", DOC_A, CORPUS_VERSION)))

    out = _run(graph, world.thread_id)

    envelope = _single_tool_payload(out, "retrieve")
    assert [chunk["source_ref"] for chunk in envelope["chunks"]] == [
        {"document_id": DOC_A, "version": CORPUS_VERSION, "unit": 3}
    ]
    assert corpus.received_scope == (DocumentScopeEntry(DOC_A, CORPUS_VERSION),)
    assert [item.locator.version for item in out["evidence"]] == [CORPUS_VERSION]


# ---------------------------------------------------------------------------
# 2. an invalid snapshot NEVER widens — the mandatory no-widening proof
# ---------------------------------------------------------------------------


def test_a_malformed_snapshot_refuses_instead_of_widening() -> None:
    """An unreadable snapshot is a typed refusal, never a whole-corpus pass.

    The wire value is committed raw (the writer validated it; this test seeds
    a value the writer would have refused, to pin the READER's behaviour). A
    reader that ignored it would answer with both documents — the widening
    this assertion exists to forbid.
    """
    world = _World()
    corpus = _company_port()
    graph = _compile(
        world,
        company_port=corpus,
        script=[_read_call(_company_ref(DOC_B)), AIMessage(content="Reviewed.")],
    )
    _commit(graph, world.thread_id, {"mode": "restricted", "sources": "nonsense"})

    out = _run(graph, world.thread_id)

    payload = _single_tool_payload(out, "read_source")
    assert payload == {"error": SCOPE_SNAPSHOT_UNREADABLE}
    assert "text" not in payload
    # The whole-corpus read this would have been is the thing being refused.
    assert DOC_B_TEXT not in json.dumps([str(m.content) for m in out["messages"]])
    assert out["evidence"] == []


def test_a_malformed_snapshot_refuses_the_company_branch_too() -> None:
    """The same unreadable value refuses the corpus, not just attachments.

    An unreadable selection is a named cause on every boundary. Refusing only
    the attachment branch would leave the corpus — the branch a document choice
    most often names — reading the whole thing, which is the widening this
    code exists to prevent.
    """
    world = _World()
    corpus = _company_port()
    graph = _compile(
        world,
        company_port=corpus,
        script=[_retrieve_call(), AIMessage(content="Reviewed.")],
    )
    _commit(graph, world.thread_id, {"schema_version": 99, "mode": "restricted"})

    out = _run(graph, world.thread_id)

    envelope = _single_tool_payload(out, "retrieve")
    assert envelope == {"error": SCOPE_SNAPSHOT_UNREADABLE}
    assert "chunks" not in envelope
    assert DOC_A_TEXT not in json.dumps([str(m.content) for m in out["messages"]])
    assert DOC_B_TEXT not in json.dumps([str(m.content) for m in out["messages"]])
    assert [item for item in out["evidence"] if item.kind == "company"] == []


def test_a_corpus_read_outside_the_selection_never_returns_its_text() -> None:
    """A document the selection does not name yields no text, by either route.

    Two things are pinned together, because both are widening and only one of
    them is a gate decision:

    * the ``retrieve`` branch drops the hit from an unnamed document before
      admission, so it never becomes evidence or reaches the model;
    * ``read_source`` on an unnamed document resolves through the company leg
      and returns nothing, because the leg answers for the exact
      ``(document, version, unit)`` it was given.

    The recheck deliberately does NOT answer for a corpus identity — nothing
    in this wiring can confirm one — so this test is what proves that gap is
    closed by narrowing rather than left open.
    """
    world = _World()
    corpus = _company_port()
    graph = _compile(
        world,
        company_port=corpus,
        script=[_read_call(_company_ref(DOC_B)), AIMessage(content="Reviewed.")],
    )
    _commit(
        graph,
        world.thread_id,
        _snapshot(("corpus", DOC_A, CORPUS_VERSION)),
    )

    out = _run(graph, world.thread_id)

    payload = _single_tool_payload(out, "read_source")
    assert "text" not in payload
    assert DOC_B_TEXT not in json.dumps([str(m.content) for m in out["messages"]])
    assert out["evidence"] == []


def test_a_stale_corpus_version_reads_no_text_rather_than_a_substitute() -> None:
    """A version the corpus does not hold is answered as a miss, never repaired.

    The model asked for a superseded version of a document the selection DID
    name. The read leg refuses the triple instead of serving the version that
    happens to be current, which is what "never widen, never silently repair"
    means at this boundary.
    """
    world = _World()
    corpus = _company_port()
    graph = _compile(
        world,
        company_port=corpus,
        script=[
            _read_call(_company_ref(DOC_A, version="a-superseded-version")),
            AIMessage(content="Reviewed."),
        ],
    )
    _commit(
        graph,
        world.thread_id,
        _snapshot(("corpus", DOC_A, CORPUS_VERSION)),
    )

    out = _run(graph, world.thread_id)

    payload = _single_tool_payload(out, "read_source")
    assert payload == {"error": "unknown_locator"}
    assert DOC_A_TEXT not in json.dumps([str(m.content) for m in out["messages"]])
    assert out["evidence"] == []


def test_a_snapshot_naming_a_stale_attachment_version_refuses() -> None:
    """A selection whose saved version is no longer current is refused.

    The accepted snapshot named version "9" of an attachment the store now
    holds at version 1. Reading on would mean honouring a DIFFERENT version
    than the one the user accepted, so the run answers the module's typed
    stale-version code and the leg is never reached.
    """
    world = _World()
    _seed_ready(world, "att-1", text="The real body.")
    factory = _RecordingReaderFactory(world.store)
    graph = _compile(
        world,
        service=_Service(world.store),
        reader_factory=factory,
        script=[_read_call(_attachment_ref("att-1")), AIMessage(content="Reviewed.")],
    )
    _commit(graph, world.thread_id, _snapshot(("attachment", "att-1", "9")))

    out = _run(graph, world.thread_id)

    payload = _single_tool_payload(out, "read_source")
    assert payload == {"error": SCOPE_ATTACHMENT_VERSION_STALE}
    assert factory.reads == []
    assert out["evidence"] == []


def test_a_snapshot_naming_an_unknown_attachment_refuses_before_the_leg() -> None:
    """An attachment the current store does not hold is refused, not widened."""
    world = _World()
    _seed_ready(world, "att-real", text="The real body.")
    factory = _RecordingReaderFactory(world.store)
    graph = _compile(
        world,
        service=_Service(world.store),
        reader_factory=factory,
        script=[_read_call(_attachment_ref("att-gone")), AIMessage(content="Reviewed.")],
    )
    _commit(graph, world.thread_id, _snapshot(("attachment", "att-gone", "1")))

    out = _run(graph, world.thread_id)

    payload = _single_tool_payload(out, "read_source")
    assert payload == {"error": SCOPE_ATTACHMENT_VERSION_STALE}
    assert factory.reads == []


# ---------------------------------------------------------------------------
# 3. no snapshot — byte-for-byte the pre-existing behaviour
# ---------------------------------------------------------------------------


def test_no_snapshot_leaves_the_compile_time_restriction_the_only_input() -> None:
    """The regression guard: with no committed snapshot nothing changes.

    Same wiring, same compile-time restriction as the interaction suite's
    baseline — the out-of-scope locator is refused and the in-scope one reads,
    which is exactly the behaviour that exists without any snapshot.
    """
    world = _World()
    _seed_ready(world, "att-allowed", text="The allowed body.")
    _seed_ready(world, "att-other", text="restricted-out body")
    factory = _RecordingReaderFactory(world.store)
    graph = _compile(
        world,
        service=_Service(world.store),
        reader_factory=factory,
        scope_restriction=ScopeRestriction(("att-allowed",), "explicit"),
        script=[_read_call(_attachment_ref("att-other")), AIMessage(content="Reviewed.")],
    )
    # Deliberately NO ``source_scope`` committed.

    out = _run(graph, world.thread_id)

    payload = _single_tool_payload(out, "read_source")
    assert payload == {"error": SCOPE_ATTACHMENT_NOT_PERMITTED}
    assert factory.reads == []


def test_no_snapshot_and_no_restriction_reads_everything_as_before() -> None:
    """With nothing in force at all, the read succeeds unchanged."""
    world = _World()
    _seed_ready(world, "att-1", text="The allowed body.")
    factory = _RecordingReaderFactory(world.store)
    graph = _compile(
        world,
        service=_Service(world.store),
        reader_factory=factory,
        script=[_read_call(_attachment_ref("att-1")), AIMessage(content="Reviewed.")],
    )

    out = _run(graph, world.thread_id)

    payload = _single_tool_payload(out, "read_source")
    assert payload["text"] == "The allowed body."
    assert len(factory.reads) == 1


def test_an_unrestricted_snapshot_is_not_a_narrowing() -> None:
    """``mode="unrestricted"`` is the explicit whole-corpus posture, not a deny-all.

    The clear is a real snapshot and must be READ as one: a reader that
    treated any present snapshot as a narrowing would refuse everything here.
    """
    world = _World()
    _seed_ready(world, "att-1", text="The allowed body.")
    factory = _RecordingReaderFactory(world.store)
    graph = _compile(
        world,
        service=_Service(world.store),
        reader_factory=factory,
        script=[_read_call(_attachment_ref("att-1")), AIMessage(content="Reviewed.")],
    )
    _commit(graph, world.thread_id, _snapshot(mode="unrestricted"))

    out = _run(graph, world.thread_id)

    payload = _single_tool_payload(out, "read_source")
    assert payload["text"] == "The allowed body."
    assert len(factory.reads) == 1


def test_the_company_branch_stays_eligible_under_an_attachment_only_snapshot() -> None:
    """Naming attachments narrows the attachment branch, never the corpus.

    ADR 0088:10 — the selection is not the ``company_excluded`` case, so the
    company branch stays eligible and a comparison question still gets corpus
    material.
    """
    world = _World()
    _seed_ready(world, "att-picked")
    search = _RecordingSearchPort({world.thread_id: [_attachment_chunk("att-picked")]})
    graph = _compile(
        world,
        service=_Service(world.store),
        search_port=search,
        script=[_retrieve_call(), AIMessage(content="Reviewed.")],
    )
    _commit(graph, world.thread_id, _snapshot(("attachment", "att-picked", "1")))

    out = _run(graph, world.thread_id)

    envelope = _single_tool_payload(out, "retrieve")
    assert [chunk["source_ref"]["document_id"] for chunk in envelope["chunks"]] == [
        DOC_A,
        DOC_B,
    ]
    assert envelope.get("error") != SCOPE_COMPANY_NOT_PERMITTED
