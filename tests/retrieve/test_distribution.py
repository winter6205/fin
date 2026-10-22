"""The mechanical ``distribution`` field on the retrieve envelope.

Spec ``specs/qa-chain-corpus-retrieval.md`` §"Result, error, readiness,
and degradation wire": a successful result is
``{"chunks": [...], "distribution": "zero" | "hit" | "dispersed"}``.
The value is computed mechanically from the distinct document IDs among
the USABLE returned chunks — chunks that survived scope, filter, and
locator validation:

* ``zero``: no usable returned chunks;
* ``hit``: usable chunks identify exactly one document;
* ``dispersed``: usable chunks identify more than one document.

It describes where returned evidence came from and makes no claim about
answerability or ambiguity; nothing downstream may read it as one.
"""

from __future__ import annotations

from typing import Any

import pytest

from fin.corpus.extracted import Locator
from fin.corpus.push import serialize_source_ref
from fin.corpus.parsers._common import UnitLocator
from fin.retrieve import Chunk
from fin.retrieve.fake_port import FakeRetrievePort
from fin.retrieve.mcp_adapter import MCPRetrieveAdapter


def _ref(document_id: str) -> str:
    """The B4 canonical fin ``source_ref`` serialization for one locator."""
    return serialize_source_ref(
        UnitLocator(document_id=document_id, version="v-" + "a" * 60, unit=1)
    )


def _wire_chunk(document_id: str) -> dict[str, Any]:
    return {
        "content": "t",
        "source_ref": _ref(document_id),
        "score": 1.0,
    }


class _ScriptedAdapter(MCPRetrieveAdapter):
    """Skip ``__init__`` and script one response (documented seam)."""

    def __init__(self, scripted: dict[str, Any]) -> None:
        self._command = None
        self._locator_map = {}
        self._known_doc_ids = None
        self._timeout = 5.0
        self._scripted_response = scripted
        self._scripted_exception = None

    async def _call_mcp(self, payload: dict[str, Any]) -> dict[str, Any]:  # type: ignore[override]
        assert self._scripted_response is not None
        return self._scripted_response


def _fake(hits: list[Chunk]) -> FakeRetrievePort:
    return FakeRetrievePort(hits_by_query={"q": hits})


def _chunk(document_id: str, unit: int = 1) -> Chunk:
    return Chunk(
        text="t",
        source_ref=Locator(
            document_id=document_id, version="v-" + "a" * 60, unit=unit
        ),
        score=1.0,
    )


# ----- the three mechanical values -----


def test_zero_when_no_chunks_are_returned() -> None:
    adapter = _ScriptedAdapter({"result": {"chunks": []}})
    result = adapter.retrieve("q")
    assert result["chunks"] == []
    assert result["distribution"] == "zero"


@pytest.mark.parametrize(
    "make_port",
    [
        pytest.param(
            lambda: _ScriptedAdapter(
                {"result": {"chunks": [_wire_chunk("DOC-1")]}}
            ),
            id="mcp-adapter",
        ),
        pytest.param(lambda: _fake([_chunk("DOC-1")]), id="fake-port"),
    ],
)
def test_hit_when_all_usable_chunks_come_from_one_document(
    make_port: Any,
) -> None:
    port = make_port()
    result = port.retrieve("q")
    assert len(result["chunks"]) == 1
    assert result["distribution"] == "hit"


@pytest.mark.parametrize(
    "make_port",
    [
        pytest.param(
            lambda: _ScriptedAdapter(
                {
                    "result": {
                        "chunks": [
                            _wire_chunk("DOC-1"),
                            _wire_chunk("DOC-2"),
                        ]
                    }
                }
            ),
            id="mcp-adapter",
        ),
        pytest.param(
            lambda: _fake([_chunk("DOC-1"), _chunk("DOC-2")]),
            id="fake-port",
        ),
    ],
)
def test_dispersed_when_usable_chunks_span_documents(
    make_port: Any,
) -> None:
    port = make_port()
    result = port.retrieve("q")
    assert len(result["chunks"]) == 2
    assert result["distribution"] == "dispersed"


def test_multiple_chunks_of_one_document_stay_a_hit() -> None:
    """Distinct document IDs, not chunk count, decides: several units
    of the same document are one ``hit``, not a ``dispersed``."""
    adapter = _ScriptedAdapter(
        {
            "result": {
                "chunks": [
                    _wire_chunk("DOC-1"),
                    _wire_chunk("DOC-1"),
                ]
            }
        }
    )
    result = adapter.retrieve("q")
    assert result["distribution"] == "hit"


def test_locator_invalid_chunks_do_not_count_toward_distribution() -> None:
    """Only USABLE chunks count: a chunk whose source_ref cannot be
    mapped to a fin locator is dropped (counted on the ledger) and
    must not turn a one-document hit into a dispersed."""
    adapter = _ScriptedAdapter(
        {
            "result": {
                "chunks": [
                    _wire_chunk("DOC-1"),
                    {"content": "bad", "source_ref": "not-a-ref", "score": 1.0},
                ]
            }
        }
    )
    result = adapter.retrieve("q")
    assert [chunk.text for chunk in result["chunks"]] == ["t"]
    assert result["distribution"] == "hit"


def test_every_chunk_unusable_is_zero_not_hit() -> None:
    """All chunks dropped by locator validation: ``zero`` — no usable
    returned chunks, whatever the wire carried."""
    adapter = _ScriptedAdapter(
        {
            "result": {
                "chunks": [
                    {"content": "bad", "source_ref": "not-a-ref", "score": 1.0},
                ]
            }
        }
    )
    result = adapter.retrieve("q")
    assert result["chunks"] == []
    assert result["distribution"] == "zero"


def test_named_failures_carry_no_distribution() -> None:
    """``distribution`` belongs to the success envelope only; a named
    failure keeps its no-``chunks`` shape and gains no distribution."""
    adapter = _ScriptedAdapter({"result": {"chunks": [_wire_chunk("DOC-1")]}})
    result = adapter.retrieve("q", filters={"cik": "0000320193"})
    assert result == {"error": "invalid_filter"}
    assert "distribution" not in result
