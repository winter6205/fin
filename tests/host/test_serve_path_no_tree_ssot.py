"""Serve-path evidence comes from this-run payloads only (T1).

Spec ``specs/corpus-serve-ingest.md`` Focus 1 / SC2 / SC3 / SC9 with
ADR 0046 / 0047: the serve path must not treat the local
``data/corpus/extracted`` tree as the company-corpus SSOT.

* **SC2** — a scripted research turn ends ``done`` with citations whose
  spans occur only in this-run ``retrieve`` / ``read_source`` payloads.
* **SC3** — with no read leg wired for the source the model names,
  ``retrieve`` / ``read_source`` return typed misses or Fake bodies; the
  graph never consults the tree to produce a success-shaped answer.
* **SC9** — the citation verifier still refuses unknown locators; with
  no static allow-list configured (the product boot shape), the
  verifier's allow-list IS this run's evidence set, so a locator claim
  nothing retrieved this run refuses immediately.

All tests are hermetic: Fake retrieve, scripted model, no live MCP,
and the extracted-tree loader is monkeypatched to raise so any
accidental tree read fails loudly.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from fin.corpus.extracted import ExtractedPage, Locator
from fin.host import ScriptedChatModel, compile_graph
from fin.retrieve import Chunk, FakeRetrievePort


DOC_ID = "TEST-2023Q1"
VERSION = "a" * 64
UNIT_TEXT = (
    "Net sales for the quarter rose twelve percent year over year to "
    "a record figure, driven by services growth across every segment."
)
OTHER_DOC = "META-2099Q1"


def _pages() -> tuple[ExtractedPage, ...]:
    return tuple(
        ExtractedPage(
            locator=Locator(document_id=DOC_ID, version=VERSION, unit=n),
            text=f"{UNIT_TEXT} (unit {n})",
            passages=(),
        )
        for n in range(1, 4)
    )


def _chunk() -> Chunk:
    return Chunk(text=UNIT_TEXT, source_ref=Locator(DOC_ID, VERSION, 1), score=2.5)


class _RetrieveOnlyPort:
    """A bound company port that exposes no ``read_slice`` face.

    The graph has no company read leg for such a build, so every
    company ``read_source`` names the typed miss rather than reaching
    for another body source."""

    def retrieve(self, query: str, **filters: Any) -> dict[str, list[Chunk]]:
        return {"chunks": []}


def _forbid_tree_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any ``load_extracted_tree`` call anywhere on the serve path
    fails the test loudly — the tree must not be an input at all.

    Patched at the loader itself and at every module that holds a
    direct reference to it, so a tree read cannot slip in through a
    name the guard did not cover."""

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("serve path consulted the extracted tree")

    for module in (
        "fin.corpus.extracted",
        "fin.corpus",
        "fin.host.tools.redline",
        "fin.host.tools.table_extract",
    ):
        monkeypatch.setattr(f"{module}.load_extracted_tree", _boom)


def _read_call(call_id: str = "c-read") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "read_source",
                "args": {
                    "source_ref": {
                        "document_id": DOC_ID,
                        "version": VERSION,
                        "unit": 1,
                    },
                    "position": 0,
                },
                "id": call_id,
                "type": "tool_call",
            }
        ],
    )


def _retrieve_call(call_id: str = "c-retrieve") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "retrieve",
                "args": {"query": "net sales"},
                "id": call_id,
                "type": "tool_call",
            }
        ],
    )


def _invoke(model: ScriptedChatModel, **graph_kwargs: Any) -> dict:
    g = compile_graph(
        checkpointer=InMemorySaver(), model=model, **graph_kwargs
    )
    return g.invoke(
        {
            "messages": [
                HumanMessage("What was AAPL net sales in 2023 Q3?")
            ],
            "evidence": [],
            "repair_count": 0,
            "step_count": 0,
        },
        config={"configurable": {"thread_id": "t-serve"}},
    )


def _read_source_tool_messages(state: dict) -> list[dict]:
    out: list[dict] = []
    for message in state["messages"]:
        if getattr(message, "name", None) == "read_source":
            out.append(json.loads(str(message.content)))
    return out


def _manifest_locator() -> tuple[str, str]:
    """The ``(document_id, version)`` of one document the committed
    on-disk extracted tree really carries.

    Read from the manifest alone — never through ``load_extracted_tree``
    — so a test can name a locator the local tree WOULD answer without
    the test itself becoming a tree read."""
    payload = json.loads(Path("data/corpus/manifest.json").read_text())
    for document in payload["documents"]:
        document_id = str(document["document_id"])
        if (Path("data/corpus/extracted") / document_id / "page-001.json").is_file():
            return document_id, str(document["sha256"])
    raise AssertionError("committed extracted tree carries no page-001 artifact")


# ===== SC2: read_source serves this-run bodies from the fake manifest =====


def test_read_source_tool_serves_body_from_fake_pages_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fake retrieve port carries a ``pages_by_doc`` manifest, the
    graph binds its ``read_slice`` as the ``read_source`` company leg,
    and the run ends ``done`` citing the unit it read — no extracted
    tree involved anywhere."""
    _forbid_tree_reads(monkeypatch)
    answer = f"Per the filing: {UNIT_TEXT[:80]}"
    model = ScriptedChatModel(script=[_read_call(), AIMessage(content=answer)])
    out = _invoke(
        model,
        adapter=FakeRetrievePort(pages_by_doc={DOC_ID: _pages()}),
    )
    assert out["status"] == "done"
    citation = out["citations"][0]
    assert citation.document_id == DOC_ID
    assert citation.version == VERSION
    assert citation.unit == 1
    assert citation.quote == UNIT_TEXT[:80]
    # The one slice the model received is the whole frozen unit: the
    # success shape, a body, and no continuation cursor.
    (slice_result,) = _read_source_tool_messages(out)
    assert set(slice_result) == {"source_ref", "text", "next_position"}
    assert slice_result["text"] == f"{UNIT_TEXT} (unit 1)"
    assert slice_result["next_position"] is None


# ===== SC3: no leg wired for that kind → typed miss, never silent success =====


@pytest.mark.parametrize(
    "unread_adapter",
    [
        pytest.param(
            lambda: FakeRetrievePort(pages_by_doc={}),
            id="empty-read-manifest",
        ),
        pytest.param(
            lambda: _RetrieveOnlyPort(),
            id="no-read-face",
        ),
    ],
)
def test_read_source_without_a_leg_is_typed_miss_and_never_reads_tree(
    monkeypatch: pytest.MonkeyPatch,
    unread_adapter: Any,
) -> None:
    """With no leg that can answer the company kind — either an empty
    read manifest or a bound adapter with no ``read_slice`` face at
    all — ``read_source`` answers the typed miss dict and must NOT
    consult the extracted tree to do it. The model cannot turn a miss
    into a citation, so the run cannot ``done`` on tree-shaped
    evidence."""
    _forbid_tree_reads(monkeypatch)
    # The scripted answer quotes what the tree WOULD have said — the
    # run must still not produce a citation, because the payload never
    # arrived this run.
    answer = f"Per the filing: {UNIT_TEXT[:80]}"
    model = ScriptedChatModel(script=[_read_call(), AIMessage(content=answer)])
    out = _invoke(model, adapter=unread_adapter())
    misses = _read_source_tool_messages(out)
    assert misses == [{"error": "unknown_locator"}]
    assert out["status"] == "refuse"
    assert out["status_reason"] == "citation_repair_cap"
    assert not out.get("citations")
    assert list(out.get("evidence") or []) == []


def test_default_read_source_misses_even_when_the_tree_exists_locally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not merely 'tree absent': the default graph does not reach for
    the committed repo tree even when it is present. The scripted read
    names a locator the on-disk tree really carries, so a graph that
    still treated the tree as the company SSOT would answer a body
    here. The forbid patch stands in for every tree location — had the
    graph read the real ``data/corpus`` tree, the loader patch would
    have tripped the assertion."""
    _forbid_tree_reads(monkeypatch)
    document_id, version = _manifest_locator()
    model = ScriptedChatModel(
        script=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "read_source",
                        "args": {
                            "source_ref": {
                                "document_id": document_id,
                                "version": version,
                                "unit": 1,
                            },
                            "position": 0,
                        },
                        "id": "c-read-tree",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="ok"),
        ]
    )
    out = _invoke(model)
    assert _read_source_tool_messages(out) == [{"error": "unknown_locator"}]
    assert list(out.get("evidence") or []) == []


# ===== SC2: retrieve payload citations =====


def test_research_done_cites_only_this_run_retrieve_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A scripted research turn with the Fake port ends ``done`` with a
    citation whose span and locator both come from the chunk the port
    returned this run. ``known_document_ids`` is deliberately unset —
    the product boot shape — and must not be required."""
    _forbid_tree_reads(monkeypatch)
    answer = (
        f"{UNIT_TEXT} and geopolitical tensions across regions persisted."
    )
    model = ScriptedChatModel(
        script=[_retrieve_call(), AIMessage(content=answer)]
    )
    out = _invoke(model, adapter=FakeRetrievePort(hits_by_query={"net sales": [_chunk()]}))
    assert out["status"] == "done"
    citation = out["citations"][0]
    assert citation.document_id == DOC_ID
    assert citation.version == VERSION
    assert citation.unit == 1
    assert UNIT_TEXT[:80] == answer[:80]
    assert citation.quote.startswith("Net sales")


# ===== SC9: unknown locator still refused, now against this-run evidence =====


def test_locator_claim_outside_this_run_evidence_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no static allow-list (product boot shape), a regex-shaped
    locator claim the run never retrieved or read refuses immediately —
    even though the answer also carries a perfectly citable span."""
    _forbid_tree_reads(monkeypatch)
    answer = (
        f"{UNIT_TEXT} Compare with {OTHER_DOC} for the same quarter."
    )
    model = ScriptedChatModel(
        script=[_retrieve_call(), AIMessage(content=answer)]
    )
    out = _invoke(model, adapter=FakeRetrievePort(hits_by_query={"net sales": [_chunk()]}))
    assert out["status"] == "refuse"
    assert out["status_reason"] == "unknown_locator"


def test_static_allow_list_still_refuses_unknown_locators(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit ``known_document_ids`` set keeps working: it is
    unioned with this-run evidence, never replaced by the tree."""
    _forbid_tree_reads(monkeypatch)
    answer = f"{UNIT_TEXT} See {OTHER_DOC} as well."
    model = ScriptedChatModel(
        script=[_retrieve_call(), AIMessage(content=answer)]
    )
    out = _invoke(
        model,
        adapter=FakeRetrievePort(hits_by_query={"net sales": [_chunk()]}),
        known_document_ids={DOC_ID},
    )
    assert out["status"] == "refuse"
    assert out["status_reason"] == "unknown_locator"


def test_locator_claim_inside_this_run_evidence_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The positive half of SC9: naming a document the run actually
    retrieved, with a matching span, still reaches ``done``."""
    _forbid_tree_reads(monkeypatch)
    answer = f"Per {DOC_ID}: {UNIT_TEXT[:80]}"
    model = ScriptedChatModel(
        script=[_retrieve_call(), AIMessage(content=answer)]
    )
    out = _invoke(model, adapter=FakeRetrievePort(hits_by_query={"net sales": [_chunk()]}))
    assert out["status"] == "done"
