"""空绑 / 映射丢 / 校验器拒绝 是三个不同的具名失败 (T3, spec SC4–SC5).

CONTEXT **具名失败**: "A typed product failure that names empty-bound
资料, mapping-drop, citation-verifier refuse, 档未开, or 真库未接 as
distinct exits — not one ``unknown_locator`` plus empty citations."

Before this ticket a research send whose retrieve returned nothing ended
the same way whether the 资料集合 was empty or every hit was dropped by
the mapper: the run refused with the verifier's own reason
(``unknown_locator`` / ``citation_repair_cap``) and empty citations. The
operator could not tell 「库是空的」 from 「命中被映射丢了」 from
「模型没引对」.

This module pins the three exits as DISTINCT on the operator-visible
payload (``status_reason`` + ``ordinary_person_copy``), plus the
boundary cases that keep the collapse from coming back:

* zero chunks with zero mapping failures -> 未就绪;
* zero chunks with mapping failures      -> 映射丢;
* a real hit (or no retrieve at all)     -> the verifier's own verdict
  (unchanged, still reachable).

The model-facing tool envelope is deliberately NOT part of the change:
``{"chunks": []}`` stays the miss shape the model reads (fin's internal
map is not the model's business) — the named failure is a RUN fact.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver

from fin.corpus.extracted import ExtractedPage, Locator
from fin.host import ScriptedChatModel, compile_graph
from fin.host.graph import _make_retrieve_tool
from fin.host.named_failures import (
    CORPUS_NOT_READY,
    MAPPING_DROP,
    named_refusal_reason,
    retrieve_blocking_failure,
    retrieve_outcomes_delta,
)
from fin.host.run_payload import ORDINARY_PERSON_COPY, _run_to_response_payload
from fin.host.state import reset_input_state
from fin.retrieve import Chunk, FakeRetrievePort
from fin.retrieve.hit_mapping import (
    MAPPING_FAILURE_UNMAPPED_SOURCE_REF,
    MAPPING_STAGE_RETRIEVE,
    MappingFailureLedger,
)
from fin.retrieve.mcp_adapter import MCPRetrieveAdapter


QUESTION = "What was AAPL revenue in 2023 Q1?"
EMPTY_COLLECTION_QUESTION = "empty-collection: what was AAPL revenue in 2023 Q1?"
DOCUMENT_ID = "DOC-2023Q1"
VERSION = "v-" + "a" * 60
PASSAGE = (
    "Total net sales for the quarter increased by nine percent "
    "compared with the prior year period."
)
# A prose answer with no >=32-char span from any evidence: the verifier's
# ordinary bounce path, which reaches ``citation_repair_cap``.
UNGROUNDED = (
    "Management expects the pace of hiring to slow through the "
    "remainder of the fiscal year."
)
RETRIEVAL_SIDE_ID = "iknow-row-000123"

_RETRIEVE_CALL: dict[str, Any] = {
    "name": "retrieve",
    "args": {"query": "what was revenue"},
    "id": "c-retrieve",
    "type": "tool_call",
}


def _locator(unit: int = 1) -> Locator:
    return Locator(document_id=DOCUMENT_ID, version=VERSION, unit=unit)


# ----- ports: the three retrieve shapes -----


class _EmptyPort:
    """Retrieve runs and legally matches nothing (0 chunks, 0 drops)."""

    def retrieve(self, query: str, **kwargs: Any) -> dict[str, Any]:
        return {"chunks": []}


class _DroppingPort:
    """Retrieve returns a hit; its ``source_ref`` never mapped."""

    def retrieve(self, query: str, **kwargs: Any) -> dict[str, Any]:
        return {
            "chunks": [
                Chunk(
                    text=PASSAGE,
                    source_ref=RETRIEVAL_SIDE_ID,  # type: ignore[arg-type]
                    score=9.0,
                )
            ]
        }


class _HitPort:
    """Retrieve returns one properly mapped chunk."""

    def retrieve(self, query: str, **kwargs: Any) -> dict[str, Any]:
        return {
            "chunks": [Chunk(text=PASSAGE, source_ref=_locator(), score=1.0)]
        }


# ----- graph harness -----


def _graph(
    port: Any,
    ledger: MappingFailureLedger,
    answer: str = UNGROUNDED,
    *,
    script: list[AIMessage] | None = None,
) -> Any:
    model = ScriptedChatModel(
        script=script
        if script is not None
        else [
            AIMessage(content="", tool_calls=[dict(_RETRIEVE_CALL)]),
            AIMessage(content=answer),
        ]
    )
    return compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        adapter=port,
        known_document_ids={DOCUMENT_ID},
        mapping_failure_ledger=ledger,
    )


def _run(graph: Any, thread_id: str) -> dict[str, Any]:
    return graph.invoke(
        reset_input_state(QUESTION),
        config={"configurable": {"thread_id": thread_id}},
    )


# ----- the pure decision -----


def test_no_retrieve_call_is_not_a_named_failure() -> None:
    """A run that never retrieved has nothing to name: the verifier's
    own verdict (a model citing a locator it never fetched) stands."""
    assert retrieve_blocking_failure(()) is None


def test_zero_chunks_and_zero_drops_name_the_empty_bound_collection() -> None:
    assert (
        retrieve_blocking_failure(
            [{"chunks": 0, "mapping_failures": 0}]
        )
        == CORPUS_NOT_READY
    )


def test_zero_chunks_with_drops_names_the_mapping_failure() -> None:
    assert (
        retrieve_blocking_failure(
            [{"chunks": 0, "mapping_failures": 3}]
        )
        == MAPPING_DROP
    )


def test_a_hit_anywhere_in_the_run_clears_the_named_failure() -> None:
    """Retrieve worked at least once: a refusal after that is the
    verifier's, not retrieve's."""
    assert (
        retrieve_blocking_failure(
            [
                {"chunks": 0, "mapping_failures": 0},
                {"chunks": 2, "mapping_failures": 0},
            ]
        )
        is None
    )


def test_unusable_records_are_not_guessed_at() -> None:
    """Defensive: a record outside the contract contributes nothing —
    the helper never invents a cause from a malformed entry."""
    assert retrieve_blocking_failure([{"query": "x"}]) is None
    assert (
        retrieve_blocking_failure([{"chunks": "many", "mapping_failures": 0}])
        is None
    )


def test_the_delta_skips_a_malformed_record_instead_of_raising() -> None:
    """Mirror of :func:`retrieve_blocking_failure`'s guard: a record
    outside the contract contributes nothing rather than making the
    ledger helper raise into a tool call that is otherwise fine."""
    assert retrieve_outcomes_delta(
        [{"chunks": "many", "mapping_failures": 0}], baseline=0
    ) == []
    assert retrieve_outcomes_delta([{"query": "x"}], baseline=0) == []


def test_the_delta_is_measured_against_the_callers_baseline() -> None:
    """The contract the tool relies on: the same absolute record yields
    nothing against its own baseline and a delta against a lower one."""
    record = {"chunks": 0, "mapping_failures": 3}

    assert retrieve_outcomes_delta([record], baseline=3) == [
        {"chunks": 0, "mapping_failures": 0}
    ]
    assert retrieve_outcomes_delta([record], baseline=0) == [
        {"chunks": 0, "mapping_failures": 3}
    ]


def test_evidence_from_any_route_keeps_the_verifier_reason() -> None:
    """The re-label applies to a run that had NOTHING to cite. Evidence
    brought by ``read_source`` means the collection was not what failed,
    so the verifier's verdict is kept even with a 0-chunk retrieve."""
    assert (
        named_refusal_reason(
            evidence=[object()],
            outcomes=[{"chunks": 0, "mapping_failures": 0}],
            verifier_reason="citation_repair_cap",
        )
        == "citation_repair_cap"
    )


def test_no_evidence_lets_the_retrieve_fact_relabel_the_refusal() -> None:
    assert (
        named_refusal_reason(
            evidence=[],
            outcomes=[{"chunks": 0, "mapping_failures": 0}],
            verifier_reason="unknown_locator",
        )
        == CORPUS_NOT_READY
    )
    assert (
        named_refusal_reason(
            evidence=[],
            outcomes=[{"chunks": 0, "mapping_failures": 4}],
            verifier_reason="unknown_locator",
        )
        == MAPPING_DROP
    )


def test_no_retrieve_outcome_keeps_the_verifier_reason() -> None:
    assert (
        named_refusal_reason(
            evidence=[],
            outcomes=(),
            verifier_reason="unknown_locator",
        )
        == "unknown_locator"
    )


# ----- the tool seam -----


def _invoke_tool(adapter: Any, ledger: MappingFailureLedger) -> Any:
    tool = _make_retrieve_tool(adapter, mapping_ledger=ledger)
    return tool.invoke(
        {
            "name": "retrieve",
            "args": {"query": "what was revenue"},
            "id": "c-retrieve",
            "type": "tool_call",
        }
    )


def test_the_model_facing_envelope_keeps_the_miss_shape() -> None:
    """0 chunks stays ``{"chunks": []}`` for the MODEL: the named
    failure is a run fact, and fin's internal map is not the model's
    business (the mapping-drop ticket pinned the same shape)."""
    ledger = MappingFailureLedger()
    for port in (_EmptyPort(), _DroppingPort()):
        command = _invoke_tool(port, ledger)
        messages = command.update["messages"]
        assert json.loads(messages[0].content) == {"chunks": []}


def test_the_tool_records_this_calls_outcome_for_the_run() -> None:
    """The run-scoped record the citation gate reads: chunk count plus
    the ledger DELTA this call produced (not the process total)."""
    ledger = MappingFailureLedger()
    empty = _invoke_tool(_EmptyPort(), ledger)
    dropped = _invoke_tool(_DroppingPort(), ledger)
    hit = _invoke_tool(_HitPort(), ledger)

    assert empty.update["retrieve_outcomes"] == [
        {"chunks": 0, "mapping_failures": 0}
    ]
    assert dropped.update["retrieve_outcomes"] == [
        {"chunks": 0, "mapping_failures": 1}
    ]
    assert hit.update["retrieve_outcomes"] == [
        {"chunks": 1, "mapping_failures": 0}
    ]


def test_a_replayed_ledger_is_not_charged_to_this_call() -> None:
    """Boundary: ``FIN_MAPPING_FAILURE_LOG`` replays a PREVIOUS
    process's failures, so a healthy call over a non-empty ledger must
    not be reported as a mapping drop (that would be a false 具名失败)."""
    ledger = MappingFailureLedger()
    ledger.record(
        code=MAPPING_FAILURE_UNMAPPED_SOURCE_REF,
        stage=MAPPING_STAGE_RETRIEVE,
        retrieval_ref="iknow-row-from-yesterday",
        message="recorded by an earlier process",
    )
    assert ledger.count() == 1

    command = _invoke_tool(_HitPort(), ledger)

    assert command.update["retrieve_outcomes"] == [
        {"chunks": 1, "mapping_failures": 0}
    ]


class _ScriptedWireAdapter(MCPRetrieveAdapter):
    """The REAL product adapter with its documented ``_call_mcp`` seam
    scripted to one wire response.

    Unlike :class:`_DroppingPort`, this adapter is the product class: it
    maps the wire envelope through ``map_hits_to_chunks`` and records
    every dropped hit on ``self.mapping_failures`` from INSIDE
    ``retrieve``. That is the shape that exposed the baseline bug —
    a fabricated port cannot reproduce it.

    ``mapping_ledger`` is the graph's own ledger, which
    ``compile_graph`` injects into the product adapter and the tool
    alike (one counter for both seams); a test that skipped it would
    measure two different numbers.
    """

    def __init__(
        self, *, response: dict[str, Any], mapping_ledger: MappingFailureLedger
    ) -> None:
        super().__init__(
            locator_map={},
            timeout_seconds=1.0,
            mapping_ledger=mapping_ledger,
        )
        self._response = response

    async def _call_mcp(self, payload: dict[str, Any]) -> dict[str, Any]:  # type: ignore[override]
        return self._response


def test_the_product_adapter_drop_is_a_mapping_drop_not_an_empty_collection() -> None:
    """The adapter records its drops into the SAME ledger the tool
    reads, and it does so from inside ``retrieve`` — before the tool's
    post-call code runs. The run-scoped delta must still see them.

    Two records, not one: the drop leaves the tool's result empty, so
    the T9 rewrite helper performs its one retry, and BOTH attempts
    belong to this call's window (the port is called twice, each call
    counts its own drop). The run-scoped record must therefore be
    ``mapping_failures=2`` — not the ``0`` a post-call baseline sees.
    """
    ledger = MappingFailureLedger()
    adapter = _ScriptedWireAdapter(
        response={
            "hits": [
                {"text": PASSAGE, "source_ref": RETRIEVAL_SIDE_ID, "score": 9.0}
            ]
        },
        mapping_ledger=ledger,
    )

    assert ledger.count() == 0
    command = _invoke_tool(adapter, ledger)

    assert ledger.count() == 2
    assert ledger.count_by_code() == {MAPPING_FAILURE_UNMAPPED_SOURCE_REF: 2}
    assert command.update["retrieve_outcomes"] == [
        {"chunks": 0, "mapping_failures": 2}
    ]


def test_the_product_adapter_drop_reaches_the_run_as_mapping_drop() -> None:
    """End to end, through the product adapter: an unmappable wire hit
    must refuse as ``mapping_drop`` — never as ``corpus_not_ready``,
    whose copy would send the operator to add documents that are
    already there."""
    ledger = MappingFailureLedger()
    graph = _graph(
        _ScriptedWireAdapter(
            response={
                "hits": [
                    {
                        "text": PASSAGE,
                        "source_ref": RETRIEVAL_SIDE_ID,
                        "score": 9.0,
                    }
                ]
            },
            mapping_ledger=ledger,
        ),
        ledger,
    )

    payload = _run_to_response_payload(_run(graph, "t-real-adapter-drop"))

    assert payload["status"] == "refuse"
    assert payload["reason"] == MAPPING_DROP
    assert payload["assistant_message"] == ORDINARY_PERSON_COPY[MAPPING_DROP]


def test_the_product_adapter_over_an_empty_wire_still_names_the_empty_bound_collection() -> None:
    """The counter-case to the mapping-drop tests, through the SAME
    product adapter: a wire answer with no hits adds no ledger records,
    so the run-scoped delta stays 0 and the run must keep naming the
    empty-bound 资料集合."""
    ledger = MappingFailureLedger()
    adapter = _ScriptedWireAdapter(
        response={"chunks": []}, mapping_ledger=ledger
    )
    graph = _graph(adapter, ledger)

    payload = _run_to_response_payload(_run(graph, "t-real-adapter-empty"))

    assert ledger.count() == 0
    assert payload["status"] == "refuse"
    assert payload["reason"] == CORPUS_NOT_READY
    assert payload["assistant_message"] == ORDINARY_PERSON_COPY[CORPUS_NOT_READY]


class _InterleavingWireAdapter(MCPRetrieveAdapter):
    """The product adapter whose two calls answer differently.

    Run A (the "empty collection" query) parks inside its wire call until
    run B has put its drop on the shared ledger; run B (the
    "every hit dropped" query) answers an unmappable hit. That ordering
    is what makes the cross-run window deterministic instead of racy.
    """

    a_is_in_flight: threading.Event
    b_has_dropped: threading.Event

    def __init__(self, *, ledger: MappingFailureLedger) -> None:
        super().__init__(
            locator_map={}, timeout_seconds=10.0, mapping_ledger=ledger
        )
        self.a_is_in_flight = threading.Event()
        self.b_has_dropped = threading.Event()

    async def _call_mcp(self, payload: dict[str, Any]) -> dict[str, Any]:  # type: ignore[override]
        query = payload.get("params", {}).get("arguments", {}).get("query", "")
        if "empty-collection" in query:
            self.a_is_in_flight.set()
            self.b_has_dropped.wait(timeout=10)
            return {"chunks": []}
        return {
            "hits": [
                {"text": PASSAGE, "source_ref": RETRIEVAL_SIDE_ID, "score": 9.0}
            ]
        }


class _TwoRunScriptedModel(BaseChatModel):
    """One tool-call turn per run, then an ungrounded answer.

    The shared ``ScriptedChatModel`` routes by call index, which two
    concurrent runs would interleave; this one routes by the question in
    the transcript so each run gets its own deterministic turn.
    """

    empty_query_marker: str = "empty-collection"

    @property
    def _llm_type(self) -> str:
        return "two-run-scripted"

    def bind_tools(self, tools: list[Any], **kwargs: Any) -> "_TwoRunScriptedModel":
        return self

    def _generate(
        self,
        messages: list[Any],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        already_called = any(
            getattr(message, "name", None) == "retrieve" for message in messages
        )
        if already_called:
            return self._chat_result(AIMessage(content=UNGROUNDED))
        # ``messages[0]`` is the host SYSTEM prompt; the run's question is
        # the human turn, so scan the transcript rather than index it.
        transcript = " ".join(str(message.content) for message in messages)
        marker = (
            self.empty_query_marker
            if self.empty_query_marker in transcript
            else "drop-every-hit"
        )
        return self._chat_result(
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "retrieve",
                        "args": {"query": marker},
                        "id": f"c-{marker}",
                        "type": "tool_call",
                    }
                ],
            )
        )

    @staticmethod
    def _chat_result(message: AIMessage) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=message)])


def test_a_concurrent_run_does_not_charge_its_drop_to_this_run() -> None:
    """The ledger and the adapter are process-wide; the 409 guard is
    per-thread. One graph, two thread_ids: run A is a genuinely empty
    collection and run B drops every hit. B's drop lands while A is
    inside its retrieve, so a process-wide before/after delta charges B's
    failure to A and refuses A as ``mapping_drop`` — telling the operator
    their documents are present but unmappable when the collection A
    searched is simply empty.

    The rendezvous makes that interleaving deterministic: A blocks inside
    its port call until B's drop is ON the shared ledger, so pre-fix A
    reads it and post-fix A's own per-call scope does not.
    """
    ledger = MappingFailureLedger()
    adapter = _InterleavingWireAdapter(ledger=ledger)
    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=_TwoRunScriptedModel(),
        adapter=adapter,
        known_document_ids={DOCUMENT_ID},
        mapping_failure_ledger=ledger,
    )
    outcomes: dict[str, Any] = {}
    failures: list[BaseException] = []

    def run(question: str, thread_id: str) -> None:
        try:
            outcomes[thread_id] = graph.invoke(
                reset_input_state(question),
                config={"configurable": {"thread_id": thread_id}},
            )
        except BaseException as exc:  # noqa: BLE001 - collect worker failures
            failures.append(exc)

    run_a = threading.Thread(
        target=run,
        args=(EMPTY_COLLECTION_QUESTION, "t-concurrent-empty"),
        name="run-a-empty",
    )
    run_b = threading.Thread(
        target=run, args=(QUESTION, "t-concurrent-drop"), name="run-b-drop"
    )
    run_a.start()
    assert adapter.a_is_in_flight.wait(timeout=10), "run A never reached the port"
    run_b.start()
    # Wait until run B's drop is on the shared ledger, then release A: the
    # record is now inside A's retrieve window, which is exactly the
    # cross-run contamination this test pins.
    deadline = time.monotonic() + 10
    while ledger.count() < 1 and time.monotonic() < deadline:
        time.sleep(0.005)
    adapter.b_has_dropped.set()
    run_a.join(timeout=30)
    run_b.join(timeout=30)

    assert not failures, failures
    assert ledger.count() >= 1, "run B never recorded its drop"
    a = outcomes["t-concurrent-empty"]
    b = outcomes["t-concurrent-drop"]
    assert a["retrieve_outcomes"] == [
        {"chunks": 0, "mapping_failures": 0}
    ], "run A was charged another run's mapping drop"
    assert _run_to_response_payload(a)["reason"] == CORPUS_NOT_READY
    assert _run_to_response_payload(b)["reason"] == MAPPING_DROP


def test_a_typed_error_envelope_is_not_recorded_as_a_retrieve_outcome() -> None:
    """``retrieve_unavailable`` is a different named EXIT with its own
    tool-trace surface; it must not be re-labelled as 未就绪."""

    class _UnavailablePort:
        def retrieve(self, query: str, **kwargs: Any) -> dict[str, Any]:
            return {"error": "retrieve_unavailable"}

    command = _invoke_tool(_UnavailablePort(), MappingFailureLedger())

    assert "retrieve_outcomes" not in command.update
    assert json.loads(command.update["messages"][0].content) == {
        "error": "retrieve_unavailable"
    }


# ----- end to end: three distinct exits on the run payload -----


def test_zero_chunk_retrieve_refuses_as_corpus_not_ready() -> None:
    """SC4: 0 successful chunks must NOT end as only a verifier reason
    with empty citations."""
    out = _run(_graph(_EmptyPort(), MappingFailureLedger()), "t-empty-bound")

    payload = _run_to_response_payload(out)
    assert payload["status"] == "refuse"
    assert payload["reason"] == CORPUS_NOT_READY
    assert payload["citations"] == []
    assert payload["assistant_message"] == ORDINARY_PERSON_COPY[CORPUS_NOT_READY]


def test_all_dropped_hits_refuse_as_mapping_drop() -> None:
    """SC5: retrieve returned hits and every one failed to map — the
    run surfaces the mapping failure, never a silent miss."""
    ledger = MappingFailureLedger()
    out = _run(_graph(_DroppingPort(), ledger), "t-mapping-drop")

    payload = _run_to_response_payload(out)
    assert payload["status"] == "refuse"
    assert payload["reason"] == MAPPING_DROP
    assert ledger.count() > 0


def test_the_three_exits_are_distinguishable_on_the_wire() -> None:
    """The point of the ticket, as one comparison: 未就绪 / 映射丢 /
    校验器拒绝 produce three different operator-visible reasons AND
    three different copies."""
    empty = _run_to_response_payload(
        _run(_graph(_EmptyPort(), MappingFailureLedger()), "t-cmp-empty")
    )
    dropped = _run_to_response_payload(
        _run(_graph(_DroppingPort(), MappingFailureLedger()), "t-cmp-drop")
    )
    # A model that cites a locator the corpus does not know refuses
    # immediately (unknown_locator), with retrieve out of the picture.
    unknown = _run_to_response_payload(
        _run(
            _graph(
                _EmptyPort(),
                MappingFailureLedger(),
                script=[
                    AIMessage(
                        content=(
                            "Per the ZZZZ-9999Q1 filing, revenue was up "
                            "across all segments this quarter."
                        )
                    )
                ],
            ),
            "t-cmp-unknown",
        )
    )

    reasons = {
        empty["reason"],
        dropped["reason"],
        unknown["reason"],
    }
    assert reasons == {CORPUS_NOT_READY, MAPPING_DROP, "unknown_locator"}
    copies = {
        empty["assistant_message"],
        dropped["assistant_message"],
        unknown["assistant_message"],
    }
    assert len(copies) == 3
    assert all(copy.strip() for copy in copies)


def test_the_verifier_verdict_survives_when_retrieve_did_produce_hits() -> None:
    """The verifier is not bypassed: with a real hit in evidence, an
    uncited answer still refuses as ``citation_repair_cap``."""
    out = _run(_graph(_HitPort(), MappingFailureLedger()), "t-hit-uncited")

    payload = _run_to_response_payload(out)
    assert payload["status"] == "refuse"
    assert payload["reason"] == "citation_repair_cap"


def _readable_port(locator: Locator, text: str) -> FakeRetrievePort:
    """The fake serve port over one in-memory unit (evidence not from
    retrieve): its empty hit table answers ``{"chunks": []}`` and its
    ``read_slice`` face is the run's own ``read_source`` leg."""
    return FakeRetrievePort(
        pages_by_doc={
            locator.document_id: (
                ExtractedPage(locator=locator, text=text, passages=()),
            )
        }
    )


def test_read_source_evidence_keeps_a_passing_verification() -> None:
    """Boundary: a 0-chunk retrieve beside a ``read_source`` unit the
    answer DOES quote is a ``done`` run — the retrieve-side 具名失败
    must never pre-empt a passing verification, because the run's
    evidence came from somewhere else."""
    unit_text = (
        "Net sales for the quarter increased nine percent compared "
        "with the prior year period."
    )
    model = ScriptedChatModel(
        script=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "read_source",
                        "args": {
                            "source_ref": {
                                "document_id": DOCUMENT_ID,
                                "version": VERSION,
                                "unit": 1,
                            }
                        },
                        "id": "c-read",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content=f"Per the filing: {unit_text}"),
        ]
    )
    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        adapter=_readable_port(_locator(), unit_text),
        known_document_ids={DOCUMENT_ID},
        mapping_failure_ledger=MappingFailureLedger(),
    )

    payload = _run_to_response_payload(_run(graph, "t-read-source-evidence"))

    assert payload["status"] == "done"
    assert [c["document_id"] for c in payload["citations"]] == [DOCUMENT_ID]
    assert [c["unit"] for c in payload["citations"]] == [1]
    assert all("page" not in c for c in payload["citations"])


def test_the_empty_bound_fact_outranks_a_hallucinated_locator() -> None:
    """SC4's core case: the collection had nothing, so the model
    invented a locator. The 未就绪 fact is the operator's real
    problem — reading ``unknown_locator`` would send them off to
    rephrase a question the empty collection can never answer."""
    out = _run(
        _graph(
            _EmptyPort(),
            MappingFailureLedger(),
            script=[
                AIMessage(content="", tool_calls=[dict(_RETRIEVE_CALL)]),
                AIMessage(
                    content=(
                        "Per the ZZZZ-9999Q1 filing, revenue was up across "
                        "all segments this quarter."
                    )
                ),
            ],
        ),
        "t-empty-with-unknown-claim",
    )

    payload = _run_to_response_payload(out)
    assert payload["reason"] == CORPUS_NOT_READY
    assert payload["assistant_message"] == ORDINARY_PERSON_COPY[CORPUS_NOT_READY]


def test_unknown_locator_survives_when_retrieve_did_produce_hits() -> None:
    """A model claiming a locator outside this run's evidence keeps
    the gate's own immediate refusal — the named失败 does not swallow
    it."""
    out = _run(
        _graph(
            _HitPort(),
            MappingFailureLedger(),
            script=[
                AIMessage(content="", tool_calls=[dict(_RETRIEVE_CALL)]),
                AIMessage(
                    content=(
                        "Per the ZZZZ-9999Q1 filing, revenue was up across "
                        "all segments this quarter."
                    )
                ),
            ],
        ),
        "t-hit-unknown-locator",
    )

    payload = _run_to_response_payload(out)
    assert payload["status"] == "refuse"
    assert payload["reason"] == "unknown_locator"


def test_a_run_that_never_retrieved_keeps_the_verifier_verdict() -> None:
    """No retrieve outcome at all is NOT 未就绪 — nothing was asked of
    the 资料集合, so the verifier's verdict is the whole truth."""
    out = _run(
        _graph(
            _EmptyPort(),
            MappingFailureLedger(),
            script=[
                AIMessage(
                    content=(
                        "Per the ZZZZ-9999Q1 filing, revenue was up across "
                        "all segments this quarter."
                    )
                )
            ],
        ),
        "t-no-retrieve",
    )

    payload = _run_to_response_payload(out)
    assert payload["reason"] == "unknown_locator"


def test_the_named_failure_is_run_scoped_across_repeat_submissions() -> None:
    """A second send on the same thread must not inherit the first
    run's verdict: the outcome channel is reset per run, so run 2's
    verdict comes from run 2's own retrieve."""
    ledger = MappingFailureLedger()
    model = ScriptedChatModel(
        script=[
            AIMessage(content="", tool_calls=[dict(_RETRIEVE_CALL)]),
            AIMessage(content=UNGROUNDED),
        ]
    )
    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        adapter=_EmptyPort(),
        known_document_ids={DOCUMENT_ID},
        mapping_failure_ledger=ledger,
    )
    thread = {"configurable": {"thread_id": "t-repeat"}}

    first = _run_to_response_payload(
        graph.invoke(reset_input_state(QUESTION), config=thread)
    )
    # Replay the same script: the scripted model's index would
    # otherwise leave run 2 without a retrieve call, which is a
    # different scenario than the one under test.
    model._index = 0
    second = _run_to_response_payload(
        graph.invoke(reset_input_state(QUESTION), config=thread)
    )

    assert first["reason"] == CORPUS_NOT_READY
    assert second["reason"] == CORPUS_NOT_READY


def test_the_outcome_channel_is_cleared_by_the_run_reset() -> None:
    """ADR 0016 run scoping: a follow-up send starts with an empty
    outcome channel, so a stale 未就绪 cannot outlive its run."""
    import typing

    from fin.host.state import AgentState

    assert reset_input_state(QUESTION)["retrieve_outcomes"] == []
    assert "retrieve_outcomes" in typing.get_type_hints(AgentState)


@pytest.mark.parametrize(
    "reason",
    [CORPUS_NOT_READY, MAPPING_DROP],
)
def test_each_named_failure_has_its_own_copy(reason: str) -> None:
    copy = ORDINARY_PERSON_COPY[reason]
    assert copy.strip()
    assert reason not in copy
