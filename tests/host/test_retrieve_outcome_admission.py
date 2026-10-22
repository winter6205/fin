"""A recorded retrieve attempt is what makes an evidence refusal admissible.

GE-2 (``specs/qa-chain-grounded-experience.md``) requires an
``evidence_refuse`` decision to follow **at least one actual retrieval**.
This module pins that contract at the two seams that used to disagree:

* the graph's tool step sets the run-scoped ``retrieve_attempted`` flag
  for every invocation, and records ONE outcome only for a retrieval
  that produced a chunk count — an empty successful retrieve is both an
  attempt and a zero-chunk outcome; ``reset_input_state`` clears the
  flag, so a follow-up turn cannot inherit the previous attempt;
* the grounded admission node must read the attempt flag (or a recorded
  outcome) as "retrieval attempted", not ``chunks > 0``. A turn whose
  corpus is empty is still a searched turn: refusing it as unanswered is
  the GE-2 violation this module forbids.

A typed error and a binding refusal are attempts recorded on the boolean
channel ONLY, never in ``retrieve_outcomes``: that channel feeds
``retrieve_blocking_failure``, which maps a zero-chunk record with no job
fact to ``corpus_not_ready`` and would relabel the named EXIT. The named
code stays on the model-facing envelope. An unexpected port exception is
the same case from the other end: it leaves no Command at all, yet the
pending ``retrieve`` call still sets the attempt flag before the
ToolNode's error text is returned.

The assertions are on side effects (the attempt flag, the recorded
outcome contents and counts, the ``grounded_decision`` audit fields, the
model-facing envelope), not only on the absence of an exception.

Input classes NOT covered here, with the reason:

* **invalid / negative input** — owned by the tool's input-validation
  seam (empty query, out-of-contract filters, conflicting search modes)
  and already covered by ``tests/host/test_retrieve_typed_exit.py`` and
  the search-mode named-exit suites; this change does not read the
  arguments, only the attempt / outcome channels.
* **overflow** — the ``limit`` cap and the mapping-failure arithmetic
  are owned elsewhere; ``retrieve_outcomes_delta`` clamping is pinned in
  ``tests/host/test_named_failure_retrieve.py``.
* **concurrent** — N/A: ``retrieve_attempted`` and ``retrieve_outcomes``
  are per-run state channels written by one ToolNode step at a time for
  that run, so there is no shared writer to race. The shared mapping
  ledger's cross-run behavior is covered in
  ``tests/host/test_named_failure_retrieve.py``.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from typing import Any, cast

from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from fin.corpus.extracted import Locator
from fin.host import ScriptedChatModel, compile_graph
from fin.host.evidence.evidence_contract import UNSUPPORTED_SOURCE_BINDING
from fin.host.evidence.evidence_session_registry import EvidenceBindingRefusal
from fin.host.graph import _make_retrieve_tool
from fin.host.named_failures import (
    COMMUNITY_SUMMARY_INDEX_FAILURE,
    RETRIEVE_OUTCOME_COMMUNITY_SUMMARY_UNREADY,
)
from fin.host.state import reset_input_state
from fin.retrieve import Chunk
from fin.retrieve.hit_mapping import MappingFailureLedger


QUESTION = "What was AAPL revenue in 2023 Q1?"
DOCUMENT_ID = "DOC-2023Q1"
VERSION = "v-" + "a" * 60
PASSAGE = (
    "Total net sales for the quarter increased by nine percent "
    "compared with the prior year period."
)
# A prose answer with no >=32-char span from any evidence: the verifier
# rejects it, which is the refusal the admission node must admit (or not)
# based on the run's recorded retrieval attempt.
UNGROUNDED = (
    "Management expects the pace of hiring to slow through the "
    "remainder of the fiscal year."
)


def _locator(unit: int = 1) -> Locator:
    return Locator(document_id=DOCUMENT_ID, version=VERSION, unit=unit)


class _HitPort:
    def retrieve(self, query: str, **kwargs: Any) -> dict[str, Any]:
        return {
            "chunks": [Chunk(text=PASSAGE, source_ref=_locator(), score=1.0)]
        }


class _EmptyPort:
    def retrieve(self, query: str, **kwargs: Any) -> dict[str, Any]:
        return {"chunks": []}


class _TypedErrorPort:
    """A port whose retrieve answered the typed EXIT dict verbatim."""

    def __init__(self, result: dict[str, Any]) -> None:
        self._result = result

    def retrieve(self, query: str, **kwargs: Any) -> dict[str, Any]:
        return dict(self._result)


class _RaisingPort:
    """A port whose retrieve raises: the ToolNode turns it into text."""

    def retrieve(self, query: str, **kwargs: Any) -> dict[str, Any]:
        raise TypeError("fixture port signature drift")


class _QueryScriptedPort:
    """Empty for every query, except one that explicitly says ``hit``."""

    def retrieve(self, query: str, **kwargs: Any) -> dict[str, Any]:
        if "hit" in query:
            return {
                "chunks": [
                    Chunk(text=PASSAGE, source_ref=_locator(), score=1.0)
                ]
            }
        return {"chunks": []}


def _retrieve_call(call_id: str, query: str) -> dict[str, Any]:
    return {
        "name": "retrieve",
        "args": {"query": query},
        "id": call_id,
        "type": "tool_call",
    }


def _refusal_script(*tool_calls: dict[str, Any]) -> list[AIMessage]:
    return [
        AIMessage(content="", tool_calls=list(tool_calls)),
        AIMessage(content=UNGROUNDED),
    ]


def _graph(
    port: Any, script: list[AIMessage], evidence: Any | None = None
) -> Any:
    return compile_graph(
        checkpointer=InMemorySaver(),
        model=ScriptedChatModel(script=script),
        adapter=port,
        known_document_ids={DOCUMENT_ID},
        mapping_failure_ledger=MappingFailureLedger(),
        evidence=evidence,
    )


def _run(graph: Any, thread_id: str) -> dict[str, Any]:
    return graph.invoke(
        reset_input_state(QUESTION),
        config={"configurable": {"thread_id": thread_id}},
    )


def _retrieve_payloads(out: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        json.loads(cast(str, message.content))
        for message in out["messages"]
        if isinstance(message, ToolMessage) and message.name == "retrieve"
    ]


def _decision(out: dict[str, Any]) -> dict[str, Any]:
    return out["grounded_decision"]


def _invoke_tool(adapter: Any, **kwargs: Any) -> Any:
    tool = _make_retrieve_tool(
        adapter,
        mapping_ledger=kwargs.pop("ledger", MappingFailureLedger()),
        **kwargs,
    )
    return tool.invoke(
        {
            "name": "retrieve",
            "args": {"query": "what was revenue"},
            "id": "c-retrieve",
            "type": "tool_call",
        }
    )


# ----- happy path -----


def test_a_hit_records_an_outcome_and_admits_the_refusal() -> None:
    """One retrieve with a hit -> chunks>0 -> the refusal is admitted."""
    out = _run(
        _graph(_HitPort(), _refusal_script(_retrieve_call("c-hit", "revenue"))),
        "t-outcome-hit",
    )

    assert out["retrieve_outcomes"] == [
        {"chunks": 1, "mapping_failures": 0}
    ]
    decision = _decision(out)
    assert decision["outcome"] == "evidence_refuse"
    assert decision["retrieval_attempted"] is True
    assert decision["candidate_count"] == 0
    # The admission publishes nothing; the citation gate stays terminal.
    assert out["status"] == "refuse"


# ----- empty retrieve: the behavior change GE-2 requires -----


def test_an_empty_retrieve_is_still_an_attempt_and_admits_the_refusal() -> None:
    """0 chunks records BOTH the attempt flag and an outcome.

    Before this contract the admission flag read ``chunks > 0``, so this
    run fell through to the citation gate with the
    "refusal without a recorded retrieval attempt" warning. An empty
    collection is a retrieval, not a turn that never searched.
    """
    out = _run(
        _graph(
            _EmptyPort(),
            _refusal_script(_retrieve_call("c-empty", "what was revenue")),
        ),
        "t-outcome-empty",
    )

    assert out["retrieve_outcomes"] == [
        {"chunks": 0, "mapping_failures": 0}
    ]
    assert out["retrieve_attempted"] is True
    decision = _decision(out)
    assert decision["outcome"] == "evidence_refuse"
    assert decision["retrieval_attempted"] is True
    assert out["status"] == "refuse"


# ----- typed error: attempt recorded on the flag, never as an outcome -----


def test_a_typed_error_records_an_attempt_but_no_outcome() -> None:
    """A typed retrieve error is an attempt; its code stays model-visible."""
    out = _run(
        _graph(
            _TypedErrorPort({"error": "retrieve_unavailable"}),
            _refusal_script(_retrieve_call("c-typed", "what was revenue")),
        ),
        "t-outcome-typed",
    )

    assert out["retrieve_outcomes"] == []
    assert out["retrieve_attempted"] is True
    assert _retrieve_payloads(out) == [{"error": "retrieve_unavailable"}]
    decision = _decision(out)
    assert decision["outcome"] == "evidence_refuse"
    assert decision["retrieval_attempted"] is True
    assert out["status"] == "refuse"
    # The named EXIT is not relabelled by the empty outcome channel.
    assert out["status_reason"] == "citation_repair_cap"


def test_a_typed_error_does_not_relabel_the_refusal() -> None:
    """A job fact stays on the typed EXIT; it never becomes an outcome.

    Recording a zero-chunk outcome here would let
    ``retrieve_blocking_failure`` rewrite the named EXIT as
    ``corpus_not_ready`` — a wrong operator fact. The job fact rides the
    model-facing envelope instead.
    """
    command = _invoke_tool(
        _TypedErrorPort(
            {
                "error": COMMUNITY_SUMMARY_INDEX_FAILURE,
                RETRIEVE_OUTCOME_COMMUNITY_SUMMARY_UNREADY: True,
            }
        )
    )

    assert "retrieve_outcomes" not in command.update
    assert json.loads(command.update["messages"][0].content) == {
        "error": COMMUNITY_SUMMARY_INDEX_FAILURE,
        RETRIEVE_OUTCOME_COMMUNITY_SUMMARY_UNREADY: True,
    }


def test_an_unexpected_port_exception_is_still_an_attempt() -> None:
    """The live incident: an unexpected exception leaves no Command, so
    no outcome exists. The pending ``retrieve`` call is still an attempt,
    which is what keeps the later refusal admissible (T6 unchanged: the
    exception itself still reaches the ToolNode as plain error text)."""
    out = _run(
        _graph(
            _RaisingPort(),
            _refusal_script(_retrieve_call("c-raise", "what was revenue")),
        ),
        "t-outcome-raise",
    )

    assert out["retrieve_outcomes"] == []
    assert out["retrieve_attempted"] is True
    error_texts = [
        cast(str, message.content)
        for message in out["messages"]
        if isinstance(message, ToolMessage) and message.name == "retrieve"
    ]
    assert len(error_texts) == 1
    assert error_texts[0].startswith("Error: TypeError")
    decision = _decision(out)
    assert decision["outcome"] == "evidence_refuse"
    assert decision["retrieval_attempted"] is True
    assert out["status"] == "refuse"


# ----- no retrieve at all: the contract the change must preserve -----


def test_a_refusal_with_no_retrieve_call_is_not_admitted(
    caplog: Any,
) -> None:
    """Zero attempts means the turn never searched; the warning is here.

    This is the negative half of GE-2 and the only path that may log
    "evidence refusal without a recorded retrieval attempt": the node
    returns ``{}`` so the citation gate remains the terminal authority.
    """
    graph = _graph(_HitPort(), [AIMessage(content=UNGROUNDED)])

    with caplog.at_level(logging.WARNING, logger="fin.host.graph"):
        out = _run(graph, "t-outcome-no-retrieve")

    assert out["retrieve_outcomes"] == []
    assert out["retrieve_attempted"] is False
    assert not _decision(out)
    assert out["status"] == "refuse"
    assert (
        "evidence refusal without a recorded retrieval attempt"
        in caplog.text
    )


def test_a_pre_flag_checkpoint_with_a_hit_still_admits() -> None:
    """A checkpoint written before the flag existed keeps the old derivation.

    Durable threads resume with ``retrieve_outcomes`` populated and no
    ``retrieve_attempted`` key. Admission must read the hit outcome, or a
    resumed refusal loses an attempt the run actually made.
    """
    graph = _graph(_HitPort(), [AIMessage(content=UNGROUNDED)])
    state = reset_input_state(QUESTION)
    state.pop("retrieve_attempted")
    state["retrieve_outcomes"] = [{"chunks": 1, "mapping_failures": 0}]

    out = graph.invoke(
        state, config={"configurable": {"thread_id": "t-outcome-preflag"}}
    )

    decision = _decision(out)
    assert decision["outcome"] == "evidence_refuse"
    assert decision["retrieval_attempted"] is True


def test_a_follow_up_turn_does_not_inherit_the_previous_attempt(
    caplog: Any,
) -> None:
    """The flag is run-scoped: turn 1's search cannot admit turn 2.

    Same thread, same checkpointer. Turn 1 retrieves and is admitted;
    turn 2 sends no retrieve at all. The reset must clear both the flag
    and the outcome list, or the follow-up refusal would be admitted on
    a search it never made.
    """
    graph = _graph(
        _HitPort(),
        [
            AIMessage(
                content="",
                tool_calls=[_retrieve_call("c-turn-one", "revenue")],
            ),
            AIMessage(content=UNGROUNDED),
            AIMessage(content=UNGROUNDED),
        ],
    )

    assert reset_input_state(QUESTION)["retrieve_attempted"] is False
    first = _run(graph, "t-outcome-scoped")
    assert first["retrieve_attempted"] is True

    with caplog.at_level(logging.WARNING, logger="fin.host.graph"):
        second = graph.invoke(
            reset_input_state("What about the next quarter?"),
            config={"configurable": {"thread_id": "t-outcome-scoped"}},
        )

    assert second["retrieve_attempted"] is False
    assert second["retrieve_outcomes"] == []
    assert not second["grounded_decision"]
    assert (
        "evidence refusal without a recorded retrieval attempt"
        in caplog.text
    )


# ----- several attempts accumulate -----


def test_one_empty_and_one_hit_record_two_outcomes() -> None:
    """Several retrieve calls are several records, in call order."""
    out = _run(
        _graph(
            _QueryScriptedPort(),
            _refusal_script(
                _retrieve_call("c-empty", "what was revenue"),
                _retrieve_call("c-hit", "hit revenue"),
            ),
        ),
        "t-outcome-multi",
    )

    assert out["retrieve_outcomes"] == [
        {"chunks": 0, "mapping_failures": 0},
        {"chunks": 1, "mapping_failures": 0},
    ]
    decision = _decision(out)
    assert decision["outcome"] == "evidence_refuse"
    assert decision["retrieval_attempted"] is True
    assert out["status"] == "refuse"


# ----- binding refusal: a typed EXIT before the port -----


class _RefusingSessions:
    def bind(self, binding: Any) -> Any:
        raise EvidenceBindingRefusal(
            "this run resolved no evidence identity binding"
        )


def test_the_binding_refusal_command_records_no_outcome() -> None:
    """A refused identity binding never reaches the port, so it has no
    outcome record; the attempt is the graph's flag, not a fake count."""
    evidence = SimpleNamespace(
        sessions=_RefusingSessions(), resolve=lambda _config: None
    )

    command = _invoke_tool(_HitPort(), evidence=evidence)

    assert "retrieve_outcomes" not in command.update
    assert json.loads(command.update["messages"][0].content) == {
        "error": UNSUPPORTED_SOURCE_BINDING
    }


def test_a_binding_refusal_is_an_attempt_but_records_no_outcome() -> None:
    """Graph-level mirror: the model invoked retrieve and got the binding
    EXIT, so the refusal is admissible without corrupting the outcome
    channel."""
    out = _run(
        _graph(
            _HitPort(),
            _refusal_script(_retrieve_call("c-binding", "what was revenue")),
            evidence=SimpleNamespace(
                sessions=_RefusingSessions(), resolve=lambda _config: None
            ),
        ),
        "t-outcome-binding",
    )

    assert out["retrieve_outcomes"] == []
    assert out["retrieve_attempted"] is True
    assert _retrieve_payloads(out) == [
        {"error": UNSUPPORTED_SOURCE_BINDING}
    ]
    decision = _decision(out)
    assert decision["outcome"] == "evidence_refuse"
    assert decision["retrieval_attempted"] is True
    assert out["status"] == "refuse"
    # A binding refusal is not rewritten into the corpus-not-ready miss.
    assert out["status_reason"] == "citation_repair_cap"
