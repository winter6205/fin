"""The retrieve scope narrows by CANONICAL document identity.

One corpus document can be reached under more than one identifier, so a
selection that names the ALIAS id and a hit that carries the CANONICAL id
(or the other way round) name the same document and must meet. These tests
pin that through the helpers the retrieve tool now delegates to — the
versioned document-scope narrowing, the corpus-id narrowing, and the scope
projection — and once end to end through the real shipped graph with an
accepted snapshot that names the alias.

Hermetic: the in-process identity store, ``InMemorySaver`` and the fake
company port. No database, no network.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from fin.corpus.document_identity import (
    InMemoryDocumentIdentityStore,
    reset_default_document_identity_store,
    set_default_document_identity_store,
)
from fin.corpus.extracted import Locator
from fin.host import ScriptedChatModel, compile_graph
from fin.host.graph import (
    _canonical,
    _narrow_chunks_to_corpus_ids,
    _narrow_chunks_to_document_scope,
    _snapshot_corpus_document_scope,
)
from fin.host.source_scope import SourceScopeSnapshot
from fin.host.state import reset_input_state
from fin.host.thread_store import InMemoryThreadStore
from fin.retrieve import Chunk
from fin.retrieve.fake_port import FakeRetrievePort
from fin.retrieve.port import DocumentScopeEntry

GROUNDED = "What was AAPL net sales in 2023 Q3?"

# Two identifiers for ONE document: the id it was first accepted under (the
# canonical one) and the second identifier those same bytes were accepted
# under later (the alias).
CANONICAL = "AAPL-2023Q3"
ALIAS = "doc_aapl-2023q3"
OTHER = "AAPL-2022Q4"

CANONICAL_TEXT = "Apple net sales for the fiscal 2023 fourth quarter were 89,493 million."
OTHER_TEXT = "Apple net sales for the fiscal 2022 fourth quarter were 82,959 million."
VERSION = "v" * 8


@pytest.fixture
def identity_store() -> InMemoryDocumentIdentityStore:
    """The process identity mapping, with ``ALIAS`` resolving to ``CANONICAL``.

    Written through the store's own accept door: the second identifier for
    the same version records the alias and never renames the canonical id.
    """
    store = InMemoryDocumentIdentityStore()
    store.record_document(document_id=CANONICAL, version=VERSION)
    mapping = store.record_document(document_id=ALIAS, version=VERSION)
    assert mapping.canonical_document_id == CANONICAL
    set_default_document_identity_store(store)
    try:
        yield store
    finally:
        reset_default_document_identity_store()


def _chunk(
    document_id: str, *, text: str = CANONICAL_TEXT, version: str = VERSION
) -> Chunk:
    return Chunk(
        text=text,
        source_ref=Locator(document_id=document_id, version=version, unit=3),
        score=1.5,
    )


def _snapshot(*sources: tuple[str, str, str]) -> dict[str, Any]:
    """The wire shape a Runtime-accepted reply commits to the checkpoint."""
    raw: dict[str, Any] = {
        "schema_version": 1,
        "mode": "restricted",
        "sources": [
            {"source_kind": kind, "source_id": sid, "version": ver}
            for kind, sid, ver in sources
        ],
        "accepted_input_version": 1,
    }
    SourceScopeSnapshot.from_state_dict(raw)
    return raw


# ---------------------------------------------------------------------------
# the shared lookup: an unresolved id stands for itself
# ---------------------------------------------------------------------------


def test_an_unresolved_id_is_its_own_canonical_id() -> None:
    resolved = {ALIAS: CANONICAL}
    assert _canonical(resolved, ALIAS) == CANONICAL
    assert _canonical(resolved, CANONICAL) == CANONICAL
    assert _canonical(resolved, OTHER) == OTHER


# ---------------------------------------------------------------------------
# the versioned document scope
# ---------------------------------------------------------------------------


def test_the_selection_naming_an_alias_admits_the_canonical_hit(
    identity_store: InMemoryDocumentIdentityStore,
) -> None:
    canonical_hit = _chunk(CANONICAL)
    other_hit = _chunk(OTHER, text=OTHER_TEXT)

    kept = _narrow_chunks_to_document_scope(
        (canonical_hit, other_hit), (DocumentScopeEntry(ALIAS, VERSION),)
    )

    assert kept == (canonical_hit,)


def test_the_selection_naming_the_canonical_id_admits_the_alias_hit(
    identity_store: InMemoryDocumentIdentityStore,
) -> None:
    alias_hit = _chunk(ALIAS)
    other_hit = _chunk(OTHER, text=OTHER_TEXT)

    kept = _narrow_chunks_to_document_scope(
        (alias_hit, other_hit), (DocumentScopeEntry(CANONICAL, VERSION),)
    )

    assert kept == (alias_hit,)


def test_the_selection_admits_only_its_own_version(
    identity_store: InMemoryDocumentIdentityStore,
) -> None:
    current = _chunk(CANONICAL)
    stale = _chunk(CANONICAL, version="w" * 8)

    kept = _narrow_chunks_to_document_scope(
        (current, stale), (DocumentScopeEntry(ALIAS, VERSION),)
    )

    assert kept == (current,)


# ---------------------------------------------------------------------------
# the document-id corpus scope
# ---------------------------------------------------------------------------


def test_the_corpus_selection_naming_an_alias_admits_the_canonical_hit(
    identity_store: InMemoryDocumentIdentityStore,
) -> None:
    canonical_hit = _chunk(CANONICAL)
    other_hit = _chunk(OTHER, text=OTHER_TEXT)

    kept = _narrow_chunks_to_corpus_ids(
        (canonical_hit, other_hit), frozenset({ALIAS})
    )

    assert kept == (canonical_hit,)


def test_the_corpus_selection_naming_the_canonical_id_admits_the_alias_hit(
    identity_store: InMemoryDocumentIdentityStore,
) -> None:
    alias_hit = _chunk(ALIAS)
    other_hit = _chunk(OTHER, text=OTHER_TEXT)

    kept = _narrow_chunks_to_corpus_ids(
        (alias_hit, other_hit), frozenset({CANONICAL})
    )

    assert kept == (alias_hit,)


# ---------------------------------------------------------------------------
# the scope projection the port is called with
# ---------------------------------------------------------------------------


def test_the_projection_carries_the_canonical_id_for_an_alias_selection(
    identity_store: InMemoryDocumentIdentityStore,
) -> None:
    scope = _snapshot_corpus_document_scope(_snapshot(("corpus", ALIAS, VERSION)))

    assert scope == (DocumentScopeEntry(CANONICAL, VERSION),)


def test_the_projection_keeps_a_canonical_selection_unchanged(
    identity_store: InMemoryDocumentIdentityStore,
) -> None:
    scope = _snapshot_corpus_document_scope(
        _snapshot(("corpus", CANONICAL, VERSION))
    )

    assert scope == (DocumentScopeEntry(CANONICAL, VERSION),)


# ---------------------------------------------------------------------------
# end to end: an accepted alias selection reaches the canonical document
# ---------------------------------------------------------------------------


def _company_port() -> FakeRetrievePort:
    return FakeRetrievePort(
        hits_by_query={
            GROUNDED: [
                _chunk(CANONICAL),
                _chunk(OTHER, text=OTHER_TEXT),
            ]
        },
        known_document_ids={CANONICAL, OTHER},
        manifest_docs={
            CANONICAL: {"version": VERSION, "text": CANONICAL_TEXT},
            OTHER: {"version": VERSION, "text": OTHER_TEXT},
        },
    )


def _retrieve_call() -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "retrieve",
                "args": {"query": GROUNDED},
                "id": "c-retrieve",
                "type": "tool_call",
            }
        ],
    )


def test_a_committed_alias_selection_admits_the_canonical_documents_hits(
    identity_store: InMemoryDocumentIdentityStore,
) -> None:
    """The accepted selection names the alias; only the canonical doc answers."""
    port = _company_port()
    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=ScriptedChatModel(
            script=[_retrieve_call(), AIMessage(content="Reviewed.")]
        ),
        adapter=port,
        known_document_ids={CANONICAL, OTHER},
    )
    thread_id = InMemoryThreadStore().create_thread(title="Thread").id
    graph._graph.update_state(
        {"configurable": {"thread_id": thread_id}},
        {"source_scope": _snapshot(("corpus", ALIAS, VERSION))},
    )

    out = graph.invoke(
        reset_input_state(GROUNDED),
        config={"configurable": {"thread_id": thread_id}},
    )

    payloads = [
        json.loads(str(message.content))
        for message in out["messages"]
        if isinstance(message, ToolMessage) and message.name == "retrieve"
    ]
    assert len(payloads) == 1
    assert [chunk["source_ref"]["document_id"] for chunk in payloads[0]["chunks"]] == [
        CANONICAL
    ]
    assert [item.locator.document_id for item in out["evidence"]] == [CANONICAL]
    # The port was asked for the document the corpus stores the text under,
    # not for the alias the operator happened to pick.
    assert port.calls[0]["document_scope"] == (
        DocumentScopeEntry(CANONICAL, VERSION),
    )
    serialized = json.dumps([str(message.content) for message in out["messages"]])
    assert OTHER_TEXT not in serialized
