"""QA-chain T8: route intent reaches the graph state and the SSE receipt.

The projection itself is unit-tested in ``test_route_intent.py``; these
cases pin the WIRING — that ``classify_node`` writes the projection onto
the run-scoped state and that the terminal SSE frame republishes it, so
a receipt can report the route source/intent (GE-1) without the SPA
re-deriving it.
"""

from __future__ import annotations

import json
from pathlib import Path

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

import pytest

from fin.corpus.catalog import record_filings
from fin.corpus.extracted import ExtractedPage, Locator, Passage
from fin.host import ScriptedChatModel, compile_graph
from fin.host.graph import _has_provider_policy_refusal
from fin.host.grounded.readable_projection import FORBIDDEN_LABELS, leaked_fragments
from fin.host.run_payload import RunResponsePayload, _run_to_response_payload
from fin.host.grounded.route_intent import normalize_route_intent, normalize_route_source
from fin.host.source_scope import SCOPE_COMPANY_NOT_PERMITTED, ScopeRestriction
from fin.host.state import reset_input_state
from fin.host.workspace_http import _terminal_sse_frames
from fin.retrieve import FakeRetrievePort

_ANNUAL_LEAVE_ANSWER = "Employees receive 10 days of annual leave per year."


def _annual_leave_adapter() -> FakeRetrievePort:
    """A read adapter serving one synthetic page, so the grounded leaf
    can actually read a source and produce a verifiable citation."""
    text = _ANNUAL_LEAVE_ANSWER
    page = ExtractedPage(
        locator=Locator(
            document_id="HR-2024", version="v-1", unit=1
        ),
        text=text,
        passages=(Passage(text=text, char_start=0, char_end=len(text)),),
    )
    return FakeRetrievePort(
        known_document_ids={"HR-2024"},
        pages_by_doc={"HR-2024": (page,)},
    )


def _graph(tmp_path: Path):  # type: ignore[no-untyped-def]
    model = ScriptedChatModel(script=[AIMessage(content="资料中未找到相关内容")])
    return compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=set(),
        catalog_path=tmp_path / "catalog.json",
    )


def _data_frames(chunks: list[str], event: str = "status") -> list[dict]:  # type: ignore[type-arg]
    out: list[dict] = []  # type: ignore[type-arg]
    for chunk in chunks:
        if f"event: {event}" not in chunk:
            continue
        for line in chunk.splitlines():
            if line.startswith("data:"):
                out.append(json.loads(line[5:].strip()))
    return out


def test_overview_phrases_carry_route_intent_on_state(tmp_path: Path) -> None:
    """GE-1: the three catalog asks project to overview/rule on state."""
    graph = _graph(tmp_path)
    for phrase in ("随便查一下", "看看", "有啥"):
        out = graph.invoke(
            {**reset_input_state(phrase), "evidence": []},
            config={"configurable": {"thread_id": f"route-{phrase}"}},
        )
        assert out["route_intent"] == "overview", phrase
        assert out["route_source"] == "rule", phrase
        assert out["track_decision"]["route_intent"] == "overview"


@pytest.mark.parametrize("phrase", ("随便查一下", "看看", "有啥"))
def test_catalog_overview_phrases_render_the_injected_catalog_without_a_model(
    tmp_path: Path, phrase: str
) -> None:
    """GE-1: bare catalog asks return a readable inventory, without
    treating metadata enumeration as cited source evidence."""
    catalog_path = tmp_path / "catalog.json"
    # A NEW physical row carries the T6 display fields: the spec's row
    # contract (qa-chain-corpus-retrieval §"Catalog fields and
    # compatibility projection") requires a non-empty ``title`` and
    # ``doc_type`` on every write. Both rows share one ``doc_type``, so
    # the overview shows the company and period dimensions only.
    record_filings(
        [
            {
                "document_id": "APPLE-2023Q1",
                "title": "Apple 2023 Q1 10-Q",
                "doc_type": "10-Q",
                "company": "Apple Incorporated",
                "period": "2023 Q1",
                "version": "a" * 64,
                "page_count": 10,
            },
            {
                "document_id": "MICROSOFT-2023Q2",
                "title": "Microsoft 2023 Q2 10-Q",
                "doc_type": "10-Q",
                "company": "Microsoft Corporation",
                "period": "2023 Q2",
                "version": "b" * 64,
                "page_count": 8,
            },
        ],
        catalog_path=catalog_path,
    )
    model = ScriptedChatModel(script=[AIMessage(content="model output is not used")])
    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        catalog_path=catalog_path,
    )

    out = graph.invoke(
        {**reset_input_state(phrase), "evidence": []},
        config={"configurable": {"thread_id": f"overview-{phrase}"}},
    )

    answer = out["answer"]
    assert out["route_intent"] == "overview"
    assert out["status"] == "done"
    assert "2 份资料" in answer
    assert "Apple Incorporated" in answer
    assert "Microsoft Corporation" in answer
    assert "2023 Q1" in answer
    assert "2023 Q2" in answer
    assert out["citations"] == ()
    assert not leaked_fragments(answer)
    assert all(label.casefold() not in answer.casefold() for label in FORBIDDEN_LABELS)
    assert model._index == 0


def test_catalog_overview_with_no_filings_says_the_catalog_is_empty(
    tmp_path: Path,
) -> None:
    model = ScriptedChatModel(script=[AIMessage(content="model output is not used")])
    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        catalog_path=tmp_path / "missing" / "catalog.json",
    )

    out = graph.invoke(
        {**reset_input_state("看看"), "evidence": []},
        config={"configurable": {"thread_id": "overview-empty"}},
    )

    assert out["route_intent"] == "overview"
    assert out["status"] == "done"
    assert "没有可供浏览的资料" in out["answer"]
    assert out["citations"] == ()
    assert model._index == 0


def test_catalog_overview_keeps_the_company_scope_refusal(tmp_path: Path) -> None:
    catalog_path = tmp_path / "catalog.json"
    record_filings(
        [
            {
                "document_id": "APPLE-2023Q1",
                "title": "Apple 2023 Q1 10-Q",
                "doc_type": "10-Q",
                "company": "Apple Incorporated",
                "period": "2023 Q1",
                "version": "a" * 64,
                "page_count": 10,
            }
        ],
        catalog_path=catalog_path,
    )
    model = ScriptedChatModel(script=[AIMessage(content="model output is not used")])
    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        catalog_path=catalog_path,
        scope_restriction=ScopeRestriction(
            ("attachment-1",), "explicit", company_excluded=True
        ),
    )

    out = graph.invoke(
        {**reset_input_state("看看"), "evidence": []},
        config={"configurable": {"thread_id": "overview-denied"}},
    )

    assert out["route_intent"] == "overview"
    assert out["status"] == "refuse"
    assert out["status_reason"] == SCOPE_COMPANY_NOT_PERMITTED
    assert out["answer"] == ""
    assert out["citations"] == ()
    assert model._index == 0


def test_ordinary_question_without_jev_records_retrieve_fallback(
    tmp_path: Path,
) -> None:
    """GE-1: an unscoped question with Jev absent retrieves (fallback),
    never a pre-retrieval refusal."""
    graph = _graph(tmp_path)
    out = graph.invoke(
        {**reset_input_state("年假几天"), "evidence": []},
        config={"configurable": {"thread_id": "route-annual-leave"}},
    )
    assert out["route_intent"] == "retrieve"
    assert out["route_source"] == "fallback"


def test_greeting_records_chat_rule(tmp_path: Path) -> None:
    graph = _graph(tmp_path)
    out = graph.invoke(
        {**reset_input_state("Hello!"), "evidence": []},
        config={"configurable": {"thread_id": "route-greeting"}},
    )
    assert out["route_intent"] == "chat"
    assert out["route_source"] == "rule"


def test_provider_typed_refusal_refines_route_and_stops_before_citation_gate(
    tmp_path: Path,
) -> None:
    provider_text = "PRIVATE PROVIDER REFUSAL DETAIL"
    model = ScriptedChatModel(
        stream_script=[
            AIMessageChunk(
                content=provider_text,
                additional_kwargs={"refusal": "provider internal refusal detail"},
            )
        ]
    )
    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        catalog_path=tmp_path / "catalog.json",
    )

    events = list(
        graph.stream(
            {**reset_input_state("年假几天"), "evidence": []},
            config={"configurable": {"thread_id": "typed-policy-refusal"}},
            stream_mode="updates",
        )
    )
    visited_nodes = [next(iter(update)) for update in events]
    out: dict[str, object] = {}
    for update in events:
        for node_update in update.values():
            if node_update:
                out.update(node_update)

    assert out["status"] == "refuse"
    assert out["status_reason"] == "policy_refuse"
    assert out["answer"] == "抱歉，我无法协助处理这个请求。"
    assert out["citations"] == ()
    assert out["route_intent"] == "policy_refuse"
    assert out["route_source"] == "fallback"
    track_decision = out["track_decision"]
    assert isinstance(track_decision, dict)
    assert track_decision["route_intent"] == "policy_refuse"
    assert track_decision["route_source"] == "fallback"
    assert "PRIVATE PROVIDER REFUSAL DETAIL" not in str(out["answer"])
    assert "provider internal refusal detail" not in str(out["answer"])
    messages = out["messages"]
    assert isinstance(messages, list)
    assert messages[-1].content == out["answer"]
    assert "citation_gate" not in visited_nodes
    assert "task_outcome" in visited_nodes


def test_provider_policy_refusal_requires_an_explicit_typed_signal() -> None:
    assert _has_provider_policy_refusal(
        AIMessage(content="", additional_kwargs={"refusal": "policy refusal"})
    )
    assert _has_provider_policy_refusal(
        AIMessage(content="", response_metadata={"finish_reason": "content_filter"})
    )
    assert not _has_provider_policy_refusal(
        AIMessage(content="我不能帮你处理这个请求。")
    )
    assert not _has_provider_policy_refusal(
        AIMessage(content="", additional_kwargs={"refusal": "  "})
    )
    assert not _has_provider_policy_refusal(
        AIMessage(content="", additional_kwargs={"refusal": 123})
    )
    assert not _has_provider_policy_refusal(
        AIMessage(content="", response_metadata={"finish_reason": "content_filtering"})
    )


@pytest.mark.parametrize(
    ("content", "refusal"),
    [
        ("我不能帮你处理这个请求。", None),
        ("普通回复。", ""),
        ("普通回复。", 123),
    ],
)
def test_untyped_or_empty_refusal_does_not_refine_route(
    tmp_path: Path, content: str, refusal: object
) -> None:
    additional_kwargs = {} if refusal is None else {"refusal": refusal}
    model = ScriptedChatModel(
        stream_script=[
            AIMessageChunk(content=content, additional_kwargs=additional_kwargs)
        ]
    )
    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        catalog_path=tmp_path / "catalog.json",
    )

    out = graph.invoke(
        {**reset_input_state("年假几天"), "evidence": []},
        config={"configurable": {"thread_id": f"untyped-policy-refusal-{content}"}},
    )

    assert out["route_intent"] == "retrieve"
    assert out["route_source"] == "fallback"
    assert out["track_decision"]["route_intent"] == "retrieve"
    assert out["track_decision"]["route_source"] == "fallback"
    assert out["status_reason"] != "policy_refuse"


def test_ordinary_question_without_jev_retrieves_and_cites(tmp_path: Path) -> None:
    """GE-1 (specs/qa-chain-grounded-experience.md:93): "an ordinary
    question with Jev absent/error retrieves and cites". With no Jev
    client the turn must reach the grounded leaf, run its tools and
    terminate with a citation — not stop at a pre-retrieval clarify."""
    model = ScriptedChatModel(
        script=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "read_source",
                        "args": {
                            "source_ref": {
                                "document_id": "HR-2024",
                                "version": "v-1",
                                "unit": 1,
                            },
                            "position": 0,
                        },
                        "id": "c-annual-leave",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content=_ANNUAL_LEAVE_ANSWER),
        ]
    )
    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids={"HR-2024"},
        adapter=_annual_leave_adapter(),
    )
    out = graph.invoke(
        {**reset_input_state("年假几天"), "evidence": []},
        config={"configurable": {"thread_id": "route-annual-leave-cites"}},
    )
    assert out["track"] == "grounded"
    assert out["track_decision"]["signals"].get("jev_exit") == "no_jev_client"
    assert out["status"] == "done"
    assert len(out["citations"]) >= 1


def test_terminal_status_frame_reports_route_intent() -> None:
    """Every receipt reports the observed route source/intent (GE-1)."""
    payload = RunResponsePayload(
        status="refuse",
        answer="",
        reason="unknown_locator",
        citations=[],
        assistant_message="",
        thinking_text="",
        tool_summaries=[],
        track="grounded",
        track_decision={
            "track": "grounded",
            "route_intent": "overview",
            "route_source": "rule",
        },
        search_mode_decision={},
        comparisons=[],
        analysis_results=[],
        evidence_locators=[],
        retrieval_window={},
    )
    frames = list(_terminal_sse_frames(run_id="r1", payload=payload))
    (status_body,) = _data_frames(frames)
    assert status_body["route_intent"] == "overview"
    assert status_body["route_source"] == "rule"


def test_collapsed_exchange_route_intent_survives_the_payload() -> None:
    """The status frame reads route intent off the mapped payload, so the
    graph dict -> RunResponsePayload mapping must carry it through."""
    payload = _run_to_response_payload(
        {
            "status": "refuse",
            "status_reason": "unknown_locator",
            "track": "grounded",
            "track_decision": {"route_intent": "retrieve", "route_source": "fallback"},
            "messages": [],
        }
    )
    frames = list(_terminal_sse_frames(run_id="r2", payload=payload))
    (status_body,) = _data_frames(frames)
    assert status_body["route_intent"] == "retrieve"
    assert status_body["route_source"] == "fallback"


# ----- GE-1: the four-value vocabulary is CLOSED on the wire -----

_ROUTE_VOCABULARY = ("retrieve", "overview", "chat", "policy_refuse")
_ROUTE_SOURCES = ("rule", "jev", "fallback")


def _status_body_for(track_decision: dict) -> dict:  # type: ignore[type-arg]
    payload = RunResponsePayload(
        status="done",
        answer="",
        reason="",
        citations=[],
        assistant_message="",
        thinking_text="",
        tool_summaries=[],
        track="grounded",
        track_decision=track_decision,
        search_mode_decision={},
        comparisons=[],
        analysis_results=[],
        evidence_locators=[],
        retrieval_window={},
    )
    frames = list(_terminal_sse_frames(run_id="r-vocab", payload=payload))
    (status_body,) = _data_frames(frames)
    return status_body


def _route_fields(status_body: dict) -> dict:  # type: ignore[type-arg]
    """Only the two route keys, so a scan cannot be satisfied by the
    ``track`` field — which legitimately carries the graph vocabulary and
    would make ``"grounded"`` look like a leak no matter what."""
    return {
        key: value
        for key, value in status_body.items()
        if key in ("route_intent", "route_source")
    }


@pytest.mark.parametrize(
    "raw_intent",
    [
        "clarify",  # the graph TRACK, which is not a product intent
        "grounded",  # the other graph track spelling
        "escalate",  # an invented fifth intent
        "RETRIEVE_ISH",
        "policy_refuse_v2",
        "retrieve;chat",
    ],
)
def test_an_out_of_vocabulary_route_intent_cannot_reach_the_wire(
    raw_intent: str,
) -> None:
    """T8: 「the wire exposes only retrieve, overview, chat, or
    policy_refuse」.

    The emitter used to forward whatever string ``track_decision`` held
    whenever it was merely truthy, so the closed vocabulary was enforced
    nowhere on the wire — the two ``normalize_*`` readers that own it had
    no production caller at all. ``clarify`` and ``grounded`` are the
    sharpest cases: they are REAL values in this codebase, so a reader
    could not tell by inspection that a graph track had leaked into a
    product-intent field.
    """
    assert normalize_route_intent(raw_intent) is None
    assert raw_intent not in _ROUTE_VOCABULARY

    status_body = _status_body_for(
        {"track": "grounded", "route_intent": raw_intent, "route_source": "rule"}
    )

    assert _route_fields(status_body) == {}, status_body


@pytest.mark.parametrize(
    "raw_source",
    ["rules", "model", "jev_v2", "heuristic", "fallback;rule", "", None],
)
def test_an_out_of_vocabulary_route_source_cannot_reach_the_wire(
    raw_source: object,
) -> None:
    """T8: the source is ``rule``, ``jev`` or ``fallback`` — no more.

    The source answers 「how was this turn routed」, so an invented
    spelling is a claim about the run nobody made. It is also the field a
    client would branch on, which is why a near-miss like ``rules`` (the
    internal decision-TIER name) must not ride beside it and be read as
    a synonym.
    """
    assert normalize_route_source(raw_source) is None

    status_body = _status_body_for(
        {"track": "grounded", "route_intent": "retrieve", "route_source": raw_source}
    )

    assert status_body["route_intent"] == "retrieve"
    assert "route_source" not in status_body, status_body


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        ("  Overview ", "overview"),
        ("POLICY_REFUSE", "policy_refuse"),
        ("CHAT", "chat"),
        ("Retrieve", "retrieve"),
    ],
)
def test_a_tolerated_intent_spelling_rides_in_its_canonical_form(
    raw: str, canonical: str
) -> None:
    """Normalization is not a reject-only filter.

    The reader tolerates case and surrounding whitespace because the
    value crosses a wire boundary, and the audit a run hands the emitter
    can carry such a spelling (a restored checkpoint, a hand-built
    payload). Publishing the raw text would put ``"  Overview "`` on the
    wire, where a client doing an exact switch misses it and reads the
    receipt as having no route at all — the same defect the acceptance is
    about, reached by a different door.
    """
    status_body = _status_body_for(
        {"track": "grounded", "route_intent": raw, "route_source": "rule"}
    )

    assert _route_fields(status_body) == {
        "route_intent": canonical,
        "route_source": "rule",
    }, status_body


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [(" RULE ", "rule"), ("JEV", "jev"), ("Fallback", "fallback")],
)
def test_a_tolerated_source_spelling_rides_in_its_canonical_form(
    raw: str, canonical: str
) -> None:
    status_body = _status_body_for(
        {"track": "grounded", "route_intent": "retrieve", "route_source": raw}
    )

    assert _route_fields(status_body) == {
        "route_intent": "retrieve",
        "route_source": canonical,
    }, status_body


def test_every_in_vocabulary_intent_and_source_still_rides() -> None:
    """The closed-set check must not cost the receipt its values.

    A filter that dropped the whole block, or one whose vocabulary had
    drifted from :mod:`fin.host.grounded.route_intent`, would pass every rejection
    case above while reporting nothing at all.
    """
    for intent in _ROUTE_VOCABULARY:
        for source in _ROUTE_SOURCES:
            status_body = _status_body_for(
                {"track": "grounded", "route_intent": intent, "route_source": source}
            )
            assert status_body["route_intent"] == intent, (intent, source)
            assert status_body["route_source"] == source, (intent, source)
