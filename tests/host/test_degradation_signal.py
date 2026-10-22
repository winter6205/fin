"""Consuming `degraded` on the host side (plan T20, SC39, §B3).

The retrieve half — reading the envelope and recording the reasons — is
``tests/retrieve/test_degradation.py``. This module covers what the user
and the citation gate see:

* every item of ``degraded_reasons`` is recorded verbatim in the ledger
  **and** the user-visible surface announces the degradation (spec
  qa-chain-corpus-retrieval: the safe copy, never the raw reasons), so
  SC39's「既未记录原因也未暴露可见信号的次数」is 0;
* a degraded response whose request failed arrives as a named failure,
  never as ``{"chunks": []}``;
* the flag does not relax the citation gate by one judgement.

The injection is a retrieve port that answers with the two verbatim
fields — the shape §B3 fixes for the retrieval service.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from fin.corpus.extracted import Locator
from fin.host import ScriptedChatModel, compile_graph
from fin.host.degradation_notice import (
    DEGRADATION_NOTICE_FIELD,
    consume_degradation,
    degradation_notice_line,
)
from fin.host.graph import _make_retrieve_tool
from fin.host.grounded.readable_projection import DEGRADATION_SAFE_COPY
from fin.host.run_payload import _run_to_response_payload
from fin.host.state import reset_input_state
from fin.host.stream_map import format_tool_summary
from fin.host.tools.issuer_fanout import ISSUER_FANOUT_TOOL, issuer_query
from fin.retrieve import Chunk
from fin.retrieve.degradation import (
    DEGRADATION_STAGE_RETRIEVE,
    DEGRADATION_STAGE_USER_NOTICE,
    DegradationLedger,
    DegradationSignal,
    unsignalled_degradation_count,
)


REASONS = ("vector:attempt_failed", "graph_ppr:not_attempted")
DOCUMENT_ID = "DOC-2023Q1"
VERSION = "v-" + "a" * 60
PASSAGE = (
    "Total net sales for the quarter increased by nine percent "
    "compared with the prior year period."
)
UNGROUNDED = (
    "Management expects the pace of hiring to slow through the "
    "remainder of the fiscal year."
)
QUESTION = "What was AAPL revenue in 2023 Q1?"


def _locator(unit: int = 1) -> Locator:
    return Locator(document_id=DOCUMENT_ID, version=VERSION, unit=unit)


def _chunk() -> Chunk:
    return Chunk(text=PASSAGE, source_ref=_locator(), score=1.0)


def _degraded_hit_result() -> dict[str, Any]:
    return {
        "chunks": [_chunk()],
        "degraded": True,
        "degraded_reasons": list(REASONS),
    }


def _degraded_failure_result() -> dict[str, Any]:
    return {
        "error": "retrieve_degraded",
        "degraded": True,
        "degraded_reasons": list(REASONS),
    }


class _ScriptedPort:
    """Retrieve port answering with a fixed product-shape dict."""

    def __init__(self, result: dict[str, Any]) -> None:
        self._result = result
        self.calls = 0

    def retrieve(self, query: str, **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        return dict(self._result)


def _invoke_retrieve_tool(
    result: dict[str, Any], ledger: DegradationLedger
):
    tool = _make_retrieve_tool(
        _ScriptedPort(result), degradation_ledger=ledger
    )
    return tool.invoke(
        {
            "name": "retrieve",
            "args": {"query": "what was revenue"},
            "id": "c-retrieve",
            "type": "tool_call",
        }
    )


# ----- the notice helper -----


def test_notice_line_carries_the_safe_copy_and_no_raw_reason() -> None:
    """SC39's visible half changed shape: the ledger keeps every reason
    verbatim, and the rendered line says only that the result is partial.
    A reason names a backend capability the reader cannot act on, so it
    rides the access-controlled ledger and not the default view."""
    line = degradation_notice_line(
        DegradationSignal(degraded=True, degraded_reasons=REASONS)
    )

    assert line == DEGRADATION_SAFE_COPY
    for reason in REASONS:
        assert reason not in line


def test_consume_records_both_stages_and_exposes_the_line() -> None:
    ledger = DegradationLedger()
    shown: list[str] = []

    signal = consume_degradation(
        _degraded_hit_result(),
        ledger=ledger,
        expose=lambda _signal, line: shown.append(line),
    )

    assert signal.degraded is True
    assert ledger.reasons_at(DEGRADATION_STAGE_RETRIEVE) == REASONS
    assert ledger.reasons_at(DEGRADATION_STAGE_USER_NOTICE) == REASONS
    assert unsignalled_degradation_count(ledger) == 0
    assert shown == [DEGRADATION_SAFE_COPY]


def test_consume_records_nothing_for_a_healthy_response() -> None:
    """§B3 rule 3: 「能力已尝试但未找到任何命中」 is not a degradation, so a
    plain empty result must not put anything on the ledger."""
    ledger = DegradationLedger()
    shown: list[str] = []

    signal = consume_degradation(
        {"chunks": []},
        ledger=ledger,
        expose=lambda _signal, line: shown.append(line),
    )

    assert signal.degraded is False
    assert ledger.count() == 0
    assert shown == []


# ----- the host retrieve tool -----


def test_retrieve_tool_keeps_the_two_fields_on_the_envelope() -> None:
    ledger = DegradationLedger()
    command = _invoke_retrieve_tool(_degraded_hit_result(), ledger)

    payload = json.loads(command.update["messages"][0].content)
    assert payload["degraded"] is True
    assert payload["degraded_reasons"] == list(REASONS)
    assert [chunk["text"] for chunk in payload["chunks"]] == [PASSAGE]


def test_retrieve_tool_records_every_reason_and_signals_it() -> None:
    ledger = DegradationLedger()
    _invoke_retrieve_tool(_degraded_hit_result(), ledger)

    assert ledger.reasons_at(DEGRADATION_STAGE_RETRIEVE) == REASONS
    assert unsignalled_degradation_count(ledger) == 0


def test_a_degraded_hit_still_contributes_its_evidence() -> None:
    """§B2 第 1 条: a degraded result is 可用但必须可见 — flagging it must not
    quietly throw the passages away either."""
    ledger = DegradationLedger()
    command = _invoke_retrieve_tool(_degraded_hit_result(), ledger)

    assert [item.locator for item in command.update["evidence"]] == [
        _locator()
    ]


def test_an_undegraded_run_records_nothing_and_keeps_the_old_shape() -> None:
    ledger = DegradationLedger()
    command = _invoke_retrieve_tool({"chunks": [_chunk()]}, ledger)

    assert json.loads(command.update["messages"][0].content).keys() == {
        "chunks"
    }
    assert ledger.count() == 0


# ----- a degraded failure is named, not an empty result -----


def test_a_degraded_failure_is_not_an_empty_chunk_list() -> None:
    """SC39: 「请求失败则转为具名失败」— the one shape that must never read as
    「命中为空」."""
    ledger = DegradationLedger()
    command = _invoke_retrieve_tool(_degraded_failure_result(), ledger)

    payload = json.loads(command.update["messages"][0].content)
    assert payload["error"] == "retrieve_degraded"
    assert payload["degraded_reasons"] == list(REASONS)
    assert "chunks" not in payload
    assert not command.update.get("evidence")


def test_a_degraded_failure_is_recorded_and_signalled_too() -> None:
    ledger = DegradationLedger()
    _invoke_retrieve_tool(_degraded_failure_result(), ledger)

    assert ledger.reasons_at(DEGRADATION_STAGE_RETRIEVE) == REASONS
    assert unsignalled_degradation_count(ledger) == 0


# ----- the user-visible surface -----


def test_the_tool_trace_line_carries_the_copy_and_no_raw_reason() -> None:
    """The tools region of the workspace renders this string, and T10
    says the DEFAULT view must not forward a raw reason.

    The envelope here reached the renderer WITHOUT the rendered notice —
    no ``degradation_notice`` key — so this is the fallback branch, and
    the fallback is exactly where a reason used to reach the reader as
    ``degraded: vector:attempt_failed``. It must still say the result is
    partial, because a degraded row that reads as an ordinary hit count
    is its own lie; what it must not do is name the backend.
    """
    summary = format_tool_summary(
        "retrieve",
        result={
            "chunks": [],
            "degraded": True,
            "degraded_reasons": list(REASONS),
        },
        args={"query": "what was revenue"},
    )

    assert DEGRADATION_SAFE_COPY in summary
    for reason in REASONS:
        assert reason not in summary


def test_the_failure_trace_line_carries_the_copy_and_no_raw_reason() -> None:
    """The typed EXIT still names itself — that is a product copy token
    the SPA renders, not a backend capability — but the degradation
    reasons beside it do not reach the reader."""
    summary = format_tool_summary(
        "retrieve",
        result=_degraded_failure_result(),
        args={"query": "what was revenue"},
    )

    assert "retrieve_degraded" in summary
    assert DEGRADATION_SAFE_COPY in summary
    for reason in REASONS:
        assert reason not in summary


def test_the_notice_branch_of_the_fold_still_renders_the_notice() -> None:
    """Enforcement must not cost the notice the host already rendered:
    a result that DOES carry ``degradation_notice`` keeps that exact
    line, so the fix cannot be a blanket delete of the clause."""
    summary = format_tool_summary(
        "retrieve",
        result={
            "chunks": [],
            "degraded": True,
            "degraded_reasons": list(REASONS),
            DEGRADATION_NOTICE_FIELD: DEGRADATION_SAFE_COPY,
        },
        args={"query": "what was revenue"},
    )

    assert DEGRADATION_SAFE_COPY in summary
    for reason in REASONS:
        assert reason not in summary


def test_an_undegraded_row_carries_no_degradation_clause() -> None:
    """The fallback is gated on degradation, so the safe copy is never
    printed for a healthy row."""
    summary = format_tool_summary(
        "retrieve",
        result={"chunks": [_chunk()]},
        args={"query": "what was revenue"},
    )

    assert DEGRADATION_SAFE_COPY not in summary


def _graph(port: Any, answer: str, ledger: DegradationLedger, **kwargs: Any):
    script = kwargs.pop(
        "script",
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "retrieve",
                        "args": {"query": "what was revenue"},
                        "id": "c-retrieve",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content=answer),
        ],
    )
    return compile_graph(
        checkpointer=InMemorySaver(),
        model=ScriptedChatModel(script=script),
        adapter=port,
        known_document_ids={DOCUMENT_ID},
        degradation_ledger=ledger,
        **kwargs,
    )


def test_end_to_end_the_reasons_are_persisted_and_only_the_copy_is_shown(
    tmp_path: Path,
) -> None:
    """Both halves of SC39 in one run: the reasons are on disk afterwards,
    and the run payload the user sees carries the safe sentence instead of
    them."""
    path = tmp_path / "degradation.jsonl"
    ledger = DegradationLedger(path=path)
    graph = _graph(
        _ScriptedPort(_degraded_hit_result()),
        f"Per the filing: {PASSAGE}",
        ledger,
    )

    out = graph.invoke(
        reset_input_state(QUESTION),
        config={"configurable": {"thread_id": "t-degraded-visible"}},
    )

    payload = _run_to_response_payload(out)
    rendered = json.dumps(payload["tool_summaries"], ensure_ascii=False)
    assert DEGRADATION_SAFE_COPY in rendered
    for reason in REASONS:
        assert reason not in rendered

    reloaded = DegradationLedger(path=path)
    assert reloaded.count() > 0
    assert reloaded.reasons_at(DEGRADATION_STAGE_RETRIEVE) == REASONS
    assert unsignalled_degradation_count(reloaded) == 0


def test_repeated_degraded_responses_leave_no_unsignalled_reason() -> None:
    """SC39's count over several injections, not one lucky call."""
    ledger = DegradationLedger()
    for index in range(3):
        _invoke_retrieve_tool(_degraded_hit_result(), ledger)
        assert unsignalled_degradation_count(ledger) == 0, index

    assert ledger.count() == 2 * len(REASONS) * 3


# ----- the fan-out branch consumes the signal too -----


def test_the_fanout_branch_records_and_signals_degradation() -> None:
    """A degraded response reaching fin through the fan-out worker is the
    same contract: no consumption point may be the silent one."""
    ledger = DegradationLedger()
    issuer = "AAPL"
    port = _ScriptedPort(_degraded_hit_result())
    graph = _graph(
        port,
        f"Per the filing: {PASSAGE}",
        ledger,
        script=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": ISSUER_FANOUT_TOOL,
                        "args": {
                            "issuers": [issuer],
                            "query": "net sales",
                        },
                        "id": "c-fanout",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content=f"Per the filing: {PASSAGE}"),
        ],
    )

    out = graph.invoke(
        reset_input_state(
            "Compare net sales for AAPL in Q3 2023 across the roster."
        ),
        config={"configurable": {"thread_id": "t-degraded-fanout"}},
    )

    assert port.calls >= 1
    assert issuer_query(issuer, "net sales")
    assert ledger.reasons_at(DEGRADATION_STAGE_RETRIEVE) == REASONS
    assert unsignalled_degradation_count(ledger) == 0
    rendered = json.dumps(
        _run_to_response_payload(out)["tool_summaries"], ensure_ascii=False
    )
    assert DEGRADATION_SAFE_COPY in rendered
    for reason in REASONS:
        assert reason not in rendered


# ----- the citation gate is not relaxed -----


def test_degraded_does_not_let_an_ungrounded_answer_pass() -> None:
    """SC39: 「降级不得放宽引用门的任何判定」. The answer quotes nothing from the
    retrieved passage, so the run must not reach ``done`` — the flag is a
    visibility signal, never a waiver."""
    ledger = DegradationLedger()
    graph = _graph(
        _ScriptedPort(_degraded_hit_result()), UNGROUNDED, ledger
    )

    out = graph.invoke(
        reset_input_state(QUESTION),
        config={"configurable": {"thread_id": "t-degraded-ungrounded"}},
    )

    assert out["status"] != "done"
    payload = _run_to_response_payload(out)
    assert payload["citations"] == []
    # The degradation was still recorded and shown — refusing the answer
    # is not an excuse to drop the reasons.
    assert unsignalled_degradation_count(ledger) == 0


def test_an_ungrounded_answer_fails_the_same_way_without_the_flag() -> None:
    """Control: the verdict above belongs to the gate, not to this
    ticket's flag — the undegraded run of the same script fails alike."""
    ledger = DegradationLedger()
    graph = _graph(
        _ScriptedPort({"chunks": [_chunk()]}), UNGROUNDED, ledger
    )

    out = graph.invoke(
        reset_input_state(QUESTION),
        config={"configurable": {"thread_id": "t-plain-ungrounded"}},
    )

    assert out["status"] != "done"
    assert _run_to_response_payload(out)["citations"] == []


def test_degraded_does_not_block_a_grounded_answer_either() -> None:
    """The other direction of the same rule: a degraded response whose
    passage the answer does quote still passes the gate unchanged."""
    ledger = DegradationLedger()
    graph = _graph(
        _ScriptedPort(_degraded_hit_result()),
        f"Per the filing: {PASSAGE}",
        ledger,
    )

    out = graph.invoke(
        reset_input_state(QUESTION),
        config={"configurable": {"thread_id": "t-degraded-grounded"}},
    )

    assert out["status"] == "done"
    assert [c.document_id for c in out["citations"]] == [DOCUMENT_ID]


def test_degraded_does_not_soften_an_unknown_locator_refusal() -> None:
    """SC43's refusal keeps its verdict under a degraded response."""
    ledger = DegradationLedger()
    graph = _graph(
        _ScriptedPort(_degraded_hit_result()),
        f"Per FAKE-9999Q1: {PASSAGE}",
        ledger,
    )

    out = graph.invoke(
        reset_input_state(QUESTION),
        config={"configurable": {"thread_id": "t-degraded-unknown-locator"}},
    )

    assert out["status"] == "refuse"
    assert out["status_reason"] == "unknown_locator"


def test_the_citation_gate_module_never_reads_the_flag() -> None:
    """The rule 「引用门规则不改」 as a file-level lock: the gate cannot
    branch on a signal it does not mention."""
    gate = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "fin"
        / "host"
        / "grounded"
        / "citation_gate.py"
    )

    assert "degraded" not in gate.read_text(encoding="utf-8")


def test_a_degraded_run_leaves_the_tool_message_history_readable() -> None:
    """The typed EXIT reaches the model as data, so the run can say why
    the answer is thin instead of hallucinating around it."""
    ledger = DegradationLedger()
    graph = _graph(
        _ScriptedPort(_degraded_failure_result()), UNGROUNDED, ledger
    )

    out = graph.invoke(
        reset_input_state(QUESTION),
        config={"configurable": {"thread_id": "t-degraded-exit"}},
    )

    tool_messages = [
        message
        for message in out["messages"]
        if isinstance(message, ToolMessage)
        and getattr(message, "name", "") == "retrieve"
    ]
    assert tool_messages
    payload = json.loads(tool_messages[0].content)
    assert payload["error"] == "retrieve_degraded"
    assert payload["degraded_reasons"] == list(REASONS)
