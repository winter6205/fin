"""Invalid input reaches the model as a named EXIT, not as a miss (T21).

The port raises a typed ``InputValidationError``; the host tool's
contract is an envelope, so the code travels as ``{"error": <code>}``.
The shape matters: ``{"chunks": []}`` would tell the model the corpus has
nothing to say about a question that was never askable, which is exactly
the conflation §B2 第 5 条 forbids. A legal empty result keeps the
``{"chunks": []}`` shape it always had.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from langchain_core.messages import ToolMessage

from fin.corpus.extracted import Locator
from fin.host.graph import _make_retrieve_tool
from fin.retrieve.fake_port import FakeRetrievePort
from fin.retrieve.input_validation import (
    OUTCOME_INVALID_INPUT,
    OUTCOME_LEGAL_EMPTY,
    InputValidationLedger,
    observe_input_outcome,
)
from fin.retrieve.port import Chunk

AAPL_CIK = "0000320193"
MSFT_CIK = "0000789019"

MANIFEST_DOCS: dict[str, dict[str, object]] = {
    "DOC-2023Q1": {"cik": AAPL_CIK, "ticker": "AAPL", "form": "10-Q"},
}


def _chunk() -> Chunk:
    return Chunk(
        text="revenue rose",
        source_ref=Locator(
            document_id="DOC-2023Q1", version="v-" + "a" * 60, unit=1
        ),
        score=1.0,
    )


def _port() -> FakeRetrievePort:
    return FakeRetrievePort(
        hits_by_query={"revenue": [_chunk()]},
        known_document_ids={"DOC-2023Q1"},
        manifest_docs=MANIFEST_DOCS,
    )


def _invoke(adapter: Any, **args: Any) -> dict[str, Any]:
    """Run the host ``retrieve`` tool once; return the parsed envelope."""
    tool = _make_retrieve_tool(adapter)
    command = tool.invoke(
        {
            "name": "retrieve",
            "args": args,
            "id": "c-retrieve",
            "type": "tool_call",
        }
    )
    messages = command.update["messages"]
    assert len(messages) == 1
    assert isinstance(messages[0], ToolMessage)
    return command.update, json.loads(messages[0].content)


@pytest.mark.parametrize(
    ("args", "code"),
    [
        pytest.param({"query": "   "}, "empty_query", id="empty-query"),
        pytest.param(
            {"query": "revenue", "ticker": "AAPL", "cik": MSFT_CIK},
            "conflicting_filters",
            id="conflicting-filters",
        ),
    ],
)
def test_invalid_input_is_a_named_envelope_not_empty_chunks(
    args: dict[str, Any], code: str
) -> None:
    update, payload = _invoke(_port(), **args)

    assert payload == {"error": code}
    assert "chunks" not in payload
    assert not update.get("evidence")


def test_a_blank_query_does_not_take_the_run_down() -> None:
    """The model may pass ``""`` while reasoning aloud. That is invalid
    input, so it must be *named* — and it must not raise out of the tool
    and kill the graph run."""
    update, payload = _invoke(_port(), query="")

    assert payload["error"] == "empty_query"
    assert update["messages"][0].name == "retrieve"


def test_a_legal_empty_result_keeps_the_miss_shape() -> None:
    """The other side of the split, through the same tool: an unknown
    filter answers with the empty contract and no error key."""
    _, payload = _invoke(_port(), query="revenue", ticker="ZZZZ")

    assert payload == {"chunks": [], "distribution": "zero"}
    assert "error" not in payload


def test_the_two_sides_do_not_share_a_shape_at_the_tool_boundary() -> None:
    """The direct comparison, at the layer the model actually reads."""
    ledger = InputValidationLedger()

    invalid = observe_input_outcome(
        lambda: _invoke(_port(), query="")[1], ledger=ledger
    )
    legal = observe_input_outcome(
        lambda: _invoke(_port(), query="nothing matches this")[1],
        ledger=ledger,
    )

    assert invalid.kind == OUTCOME_INVALID_INPUT
    assert invalid.code == "empty_query"
    assert legal.kind == OUTCOME_LEGAL_EMPTY
    assert ledger.count() == 1


def test_the_ports_own_count_moved_even_though_the_tool_returned() -> None:
    """SC42's count is taken where the validation happened, so the tool
    turning the failure into an envelope does not lose the number."""
    port = _port()

    _invoke(port, query="   ")
    _invoke(port, query="revenue", ticker="AAPL", cik=MSFT_CIK)

    assert port.input_validation_failures.count_by_code() == {
        "empty_query": 1,
        "conflicting_filters": 1,
    }
