"""The attachment citation identity faces decided inside the wired graph.

Spec ``specs/thread-attachment-source-read.md`` (host-validated ownership
and parsed-content version before a citation stands) pinned where identity
is actually granted: ``compile_graph`` built with the REAL
:class:`fin.host.attachment.attachment_source_read.AttachmentSourceReader` over
:class:`fin.host.attachment.attachment_store.InMemoryAttachmentStore`, plus an
attachment search port. No hand-rolled reader stands in for the two faces
the graph asks — ``owned_record`` (which grants a citation its filename and
presentation) and ``identity_is_current`` (the gate's currency re-check) —
so the security question is answered against a real row: can a foreign,
re-prepared, or tombstoned attachment still get citation identity?

The race cases move the STORE between the read and the gate, which is the
only window where the gate's re-check is the last line of defence: every
hit and slice succeeded while the row was current.

Hermetic: in-memory stores, ``ScriptedChatModel``, ``InMemorySaver`` — no
network, no live backend, no Postgres.
"""

from __future__ import annotations

import uuid
from typing import Any, Callable

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from fin.host import ScriptedChatModel, compile_graph
from fin.host.attachment.attachment_index import build_postings
from fin.host.attachment.attachment_retrieval import AttachmentChunk, AttachmentLocator
from fin.host.attachment.attachment_source_read import AttachmentSourceReader
from fin.host.attachment.attachment_store import (
    InMemoryAttachmentStore,
    ParsedAttachmentUnit,
)
from fin.host.state import reset_input_state
from fin.host.thread_store import InMemoryThreadStore
from fin.retrieve.fake_port import FakeRetrievePort

KNOWN_IDS = {"AAPL-2023Q3"}

# Grounded by the track rules tier (mirrors the attachment gate/retrieve
# suites), so the run reaches the citation gate.
GROUNDED = "What was AAPL net sales in 2023 Q3?"

_HIT_TEXT = "The attached term sheet lists a purchase price of 12,000 thousand."
_OTHER_TEXT = "The appendix of that term sheet lists a termination fee of 900."

# The identity the answer claims for itself. A citation's identity comes
# from the stored record, so nothing here may reach the wire.
CLAIMED_FILENAME = "annual-report-2019.pdf"
CLAIMED_VERSION = 9

# The version and ordinal the store row is published at, and the ordinal
# the hit addresses: one literal pair so a refusal cannot be an artifact
# of two helpers disagreeing about which version was current.
_HIT_VERSION = 2
_HIT_UNIT = 2
_STORED_FILENAME = "term-sheet.pdf"


# ---------------------------------------------------------------------------
# real-store seeding (the same doors the preparation tests use)
# ---------------------------------------------------------------------------


def _store() -> tuple[InMemoryThreadStore, InMemoryAttachmentStore, str]:
    threads = InMemoryThreadStore()
    store = InMemoryAttachmentStore(threads)
    thread_id = threads.create_thread(title="Memo thread").id
    return threads, store, thread_id


def _ready_attachment(
    store: InMemoryAttachmentStore, thread_id: str, *, filename: str
) -> str:
    """Publish one ready version whose record the citation must match.

    ``create`` → ``record_units`` → ``put_index`` → ``mark_ready`` is the
    only way a row becomes citable text, so the record behind an identity
    answer is the record the preparation lane would have written.
    """
    attachment_id = f"att-{uuid.uuid4().hex[:8]}"
    store.create(
        thread_id=thread_id,
        attachment_id=attachment_id,
        filename=filename,
        size_bytes=2_048,
        sha256="cd" * 32,
        object_key=f"threads/{thread_id}/{attachment_id}",
    )
    texts = {1: _OTHER_TEXT, _HIT_UNIT: _HIT_TEXT}
    units = tuple(
        ParsedAttachmentUnit(unit=ordinal, text=text)
        for ordinal, text in sorted(texts.items())
    )
    store.record_units(attachment_id=attachment_id, version=_HIT_VERSION, units=units)
    store.put_index(
        attachment_id=attachment_id,
        version=_HIT_VERSION,
        postings=build_postings(units),
    )
    store.mark_ready(
        attachment_id=attachment_id, version=_HIT_VERSION, unit_count=len(units)
    )
    return attachment_id


def _republish_newer_version(store: InMemoryAttachmentStore, attachment_id: str) -> None:
    """Re-prepare the row one version forward, through the same doors."""
    units = (ParsedAttachmentUnit(unit=_HIT_UNIT, text=_HIT_TEXT),)
    newer = _HIT_VERSION + 1
    store.record_units(attachment_id=attachment_id, version=newer, units=units)
    store.put_index(
        attachment_id=attachment_id, version=newer, postings=build_postings(units)
    )
    store.mark_ready(attachment_id=attachment_id, version=newer, unit_count=1)


# ---------------------------------------------------------------------------
# the search seam (NOT the seam under test) and the real read seam
# ---------------------------------------------------------------------------


class _ScriptedSearchPort:
    """A thread-keyed attachment index standing in for the search port.

    Only the SEARCH leg is scripted here. Every identity answer this run
    needs comes from the real reader over the real store, so a grant or a
    refusal cannot be talked into passing by a fake that agrees with the
    implementation.
    """

    def __init__(self, by_thread: dict[str, list[AttachmentChunk]]) -> None:
        self.by_thread = by_thread

    def search(
        self,
        *,
        thread_id: str,
        query: str,
        attachment_ids: list[str] | None = None,
        limit: int | None = None,
    ) -> list[AttachmentChunk]:
        chunks = list(self.by_thread.get(thread_id, []))
        if attachment_ids is not None:
            wanted = set(attachment_ids)
            chunks = [
                chunk for chunk in chunks if chunk.source_ref.attachment_id in wanted
            ]
        return chunks[:8 if limit is None else limit]


def _real_reader_factory(store: InMemoryAttachmentStore) -> Callable[[str], Any]:
    """The production wiring: one thread-bound reader over the one store."""

    def build(thread_id: str) -> AttachmentSourceReader:
        return AttachmentSourceReader(store, thread_id)

    return build


class _StoreRaceModel(ScriptedChatModel):
    """Scripted model that moves the store on its own answer turn.

    ``race`` fires exactly once, right after the answer message is produced
    and before anything downstream reads it — the window the citation
    gate's re-check exists to cover. ``fired`` is asserted so a race that
    silently never ran cannot fake a refusal.
    """

    race: Any = None
    fired: int = 0

    def _generate(
        self,
        messages: list[Any],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> Any:
        result = super()._generate(
            messages, stop=stop, run_manager=run_manager, **kwargs
        )
        message = result.generations[0].message
        if self.race is not None and not message.tool_calls:
            pending, self.race = self.race, None
            pending()
            self.fired += 1
        return result


def _tool_call(name: str, args: dict[str, Any], call_id: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {"name": name, "args": args, "id": call_id, "type": "tool_call"}
        ],
    )


def _run_graph(
    store: InMemoryAttachmentStore,
    thread_id: str,
    attachment_id: str,
    *,
    race: Callable[[], None] | None = None,
) -> tuple[dict[str, Any], _StoreRaceModel]:
    """One run: retrieve the hit, read the published locator, answer verbatim.

    The answer quotes the attachment word for word, so whether the run can
    cite it depends only on the identity the host can attest from the
    store — never on what the answer claims.
    """
    source_ref = AttachmentLocator(
        attachment_id=attachment_id, version=_HIT_VERSION, unit=_HIT_UNIT
    ).as_source_ref()
    model = _StoreRaceModel(
        script=[
            _tool_call("retrieve", {"query": GROUNDED}, "c-retrieve"),
            _tool_call(
                "read_source", {"source_ref": source_ref, "position": 0}, "c-read"
            ),
            AIMessage(
                content=f"Per {CLAIMED_FILENAME} version {CLAIMED_VERSION}: {_HIT_TEXT}"
            ),
        ],
        race=race,
    )
    chunk = AttachmentChunk(
        text=_HIT_TEXT,
        source_ref=AttachmentLocator(
            attachment_id=attachment_id, version=_HIT_VERSION, unit=_HIT_UNIT
        ),
        score=0.75,
    )
    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        adapter=FakeRetrievePort(
            hits_by_query={}, known_document_ids=set(KNOWN_IDS)
        ),
        known_document_ids=set(KNOWN_IDS),
        attachment_retriever=_ScriptedSearchPort({thread_id: [chunk]}),
        attachment_source_reader_factory=_real_reader_factory(store),
    )
    out = graph.invoke(
        reset_input_state(GROUNDED),
        config={"configurable": {"thread_id": thread_id}},
    )
    return out, model


# ---------------------------------------------------------------------------
# the grant: identity comes out of the stored record
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "filename,display",
    [("term-sheet.pdf", "page"), ("term-sheet.md", "unit")],
    ids=["pdf_is_paged", "non_pdf_is_unit"],
)
def test_a_verbatim_quote_is_cited_with_the_stored_identity(
    filename: str, display: str
) -> None:
    _threads, store, thread_id = _store()
    attachment_id = _ready_attachment(store, thread_id, filename=filename)

    out, _model = _run_graph(store, thread_id, attachment_id)

    assert out["status"] == "done", out
    assert len(out["citations"]) == 1
    citation = out["citations"][0]
    # The attachment identity is the store's row, and the presentation is
    # read off its filename: a PDF cites as a physical page, anything else
    # as a unit. The answer's own filename and version claims stay inert.
    assert citation.kind == "attachment"
    assert citation.attachment_id == attachment_id
    assert citation.document_id == ""
    assert citation.filename == filename
    assert citation.display == display
    assert citation.version == str(_HIT_VERSION)
    assert citation.unit == _HIT_UNIT
    assert _HIT_TEXT in citation.quote
    assert CLAIMED_FILENAME not in str(out["citations"])
    assert CLAIMED_FILENAME not in citation.filename


# ---------------------------------------------------------------------------
# the refusals: identity moved after the read
# ---------------------------------------------------------------------------


def test_a_row_reprepared_after_the_read_cannot_ground_the_quote() -> None:
    _threads, store, thread_id = _store()
    attachment_id = _ready_attachment(
        store, thread_id, filename=_STORED_FILENAME
    )

    out, model = _run_graph(
        store,
        thread_id,
        attachment_id,
        race=lambda: _republish_newer_version(store, attachment_id),
    )

    assert model.fired == 1
    assert out["status"] == "refuse", out
    assert out["citations"] == []
    # The face that refused, pinned at the same store state: the row is
    # still this thread's and still readable, only its version moved on —
    # which is exactly what the grant and the re-check must disagree about.
    reader = AttachmentSourceReader(store, thread_id)
    record = reader.owned_record(attachment_id)
    assert record is not None and record.version == _HIT_VERSION + 1
    assert reader.identity_is_current(attachment_id, _HIT_VERSION) is False


def test_a_row_deleted_after_the_read_cannot_ground_the_quote() -> None:
    _threads, store, thread_id = _store()
    attachment_id = _ready_attachment(
        store, thread_id, filename=_STORED_FILENAME
    )

    out, model = _run_graph(
        store,
        thread_id,
        attachment_id,
        race=lambda: store.tombstone(attachment_id=attachment_id, thread_id=thread_id),
    )

    assert model.fired == 1
    assert out["status"] == "refuse", out
    assert out["citations"] == []
    reader = AttachmentSourceReader(store, thread_id)
    assert reader.owned_record(attachment_id) is None
    assert reader.identity_is_current(attachment_id, _HIT_VERSION) is False


def test_a_moved_row_and_a_gone_row_refuse_identically() -> None:
    """No differentiating leak at the gate.

    A re-prepared row and a deleted row are different facts about the
    thread's data; the refusing run must not become a channel that tells
    them apart from the outside.
    """
    _threads_a, store_a, thread_a = _store()
    moved = _ready_attachment(store_a, thread_a, filename=_STORED_FILENAME)
    moved_out, moved_model = _run_graph(
        store_a,
        thread_a,
        moved,
        race=lambda: _republish_newer_version(store_a, moved),
    )

    _threads_b, store_b, thread_b = _store()
    deleted = _ready_attachment(store_b, thread_b, filename=_STORED_FILENAME)
    deleted_out, deleted_model = _run_graph(
        store_b,
        thread_b,
        deleted,
        race=lambda: store_b.tombstone(attachment_id=deleted, thread_id=thread_b),
    )

    def observable(out: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": out["status"],
            "status_reason": out.get("status_reason"),
            "answer": out.get("answer"),
            # The minted id differs per run by construction; what must not
            # differ is whether ANY identity field rode the refusal.
            "citation_identity": [
                (
                    citation.kind,
                    "<attachment_id>" if citation.attachment_id else "",
                    citation.version,
                    citation.unit,
                    citation.display,
                    citation.filename,
                )
                for citation in out["citations"]
            ],
        }

    assert moved_model.fired == 1 and deleted_model.fired == 1
    assert observable(moved_out) == observable(deleted_out)
    assert observable(moved_out)["status"] == "refuse"
    assert observable(moved_out)["citation_identity"] == []
