"""`distribution` reaches the model-visible retrieve tool envelope.

Spec ``specs/qa-chain-corpus-retrieval.md`` §"Result, error, readiness, and
degradation wire": a successful result is
``{"chunks": [...], "distribution": "zero" | "hit" | "dispersed"}``, computed
mechanically from the distinct document IDs among the usable returned chunks.
The port computes it; this module proves the HOST envelope carries it, because
the host tool's ``ToolMessage`` is the shape the model actually reads.

The two boundaries this pins:

* the named-failure envelopes have no ``chunks`` key, so they carry no
  ``distribution`` either;
* the empty-result path keeps its own shape — the spec's success shape covers
  a result that carries chunks, and the miss shape stays what it is.
"""

from __future__ import annotations

import json
from typing import Any

from fin.corpus.extracted import Locator
from fin.host.graph import _make_retrieve_tool
from fin.retrieve import Chunk
from fin.retrieve.port import distribution_of


def _chunk(document_id: str, unit: int = 1) -> Chunk:
    return Chunk(
        text="net sales rose",
        source_ref=Locator(
            document_id=document_id,
            version="v-" + "a" * 60,
            unit=unit,
        ),
        score=1.0,
    )


class _Port:
    """Retrieve port answering one scripted result."""

    def __init__(self, result: dict[str, Any]) -> None:
        self._result = result
        self.calls: list[dict[str, Any]] = []

    def retrieve(self, query: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"query": query, **kwargs})
        return dict(self._result)


def _envelope(port: Any, **kwargs: Any) -> dict[str, Any]:
    tool = _make_retrieve_tool(port)
    command = tool.invoke(
        {
            "name": "retrieve",
            "args": {"query": "what was revenue", **kwargs},
            "id": "c-retrieve",
            "type": "tool_call",
        }
    )
    return json.loads(command.update["messages"][0].content)


def test_one_document_is_a_hit() -> None:
    chunks = [_chunk("DOC-2023Q1"), _chunk("DOC-2023Q1", unit=2)]
    port = _Port({"chunks": chunks, "distribution": distribution_of(chunks)})

    payload = _envelope(port)

    assert payload["distribution"] == "hit"
    assert len(payload["chunks"]) == 2


def test_two_documents_are_dispersed() -> None:
    chunks = [_chunk("DOC-2023Q1"), _chunk("DOC-2023Q2")]
    port = _Port({"chunks": chunks, "distribution": distribution_of(chunks)})

    payload = _envelope(port)

    assert payload["distribution"] == "dispersed"
    assert len(payload["chunks"]) == 2


def test_a_zero_result_is_zero() -> None:
    port = _Port({"chunks": [], "distribution": "zero"})

    assert _envelope(port)["distribution"] == "zero"


def test_a_named_failure_carries_no_distribution() -> None:
    """The named failure envelopes have no ``chunks`` key; a
    ``distribution`` beside them would describe evidence that is not there."""
    port = _Port({"error": "unsupported_source_scope"})

    payload = _envelope(port)

    assert payload == {"error": "unsupported_source_scope"}
    assert "distribution" not in payload
    assert "chunks" not in payload


def test_a_miss_keeps_its_own_shape() -> None:
    """The empty-result path is the miss shape, not the success envelope:
    adding ``distribution`` to it would change a pinned contract for no
    gain, since it carries no evidence to describe."""
    port = _Port({"chunks": []})

    assert _envelope(port) == {"chunks": []}


def test_a_dropped_chunk_changes_what_the_value_describes() -> None:
    """``distribution`` describes the USABLE returned chunks, so a chunk the
    admission boundary dropped must not be counted toward it."""
    port = _Port(
        {
            "chunks": [_chunk("DOC-2023Q1"), {"text": "no locator", "score": 1.0}],
            "distribution": "hit",
        }
    )

    payload = _envelope(port)

    assert payload["distribution"] == "hit"
    assert len(payload["chunks"]) == 1
