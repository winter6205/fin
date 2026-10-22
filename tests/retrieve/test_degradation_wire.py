"""The malformed degradation envelope's WIRE code is `retrieve_protocol_error`.

Spec ``specs/qa-chain-corpus-retrieval.md`` §"Result, error, readiness, and
degradation wire": 「a malformed or oversized degradation envelope is
``retrieve_protocol_error``, never an undegraded result」.

The exception family stays what it is — :class:`~fin.retrieve.degradation.
DegradedEnvelopeInvalidError` carries the diagnostic detail the ledger and
the logs want — but the token that reaches the product envelope is the spec's
named protocol failure. A bare ``degraded_envelope_invalid`` on the wire would
be a second, undocumented failure vocabulary.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from fin.corpus.extracted import Locator
from fin.retrieve.degradation import (
    DEGRADED_FIELD,
    DEGRADED_REASONS_FIELD,
    DegradedEnvelopeInvalidError,
    read_degradation_signal,
)
from fin.retrieve.mcp_adapter import MCPRetrieveAdapter


HIT = {"source_ref": "row-1", "text": "net sales rose", "score": 2.0}


class _ScriptedMCP(MCPRetrieveAdapter):
    """Adapter with the documented ``_call_mcp`` transport seam scripted."""

    def __init__(self, result: Any) -> None:
        super().__init__(
            locator_map={
                "row-1": Locator(
                    document_id="DOC-2023Q1",
                    version="v-" + "a" * 60,
                    unit=3,
                )
            }
        )
        self._result = result

    async def _call_mcp(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": 2, "result": self._result}


MALFORMED_REASONS: list[tuple[str, Any]] = [
    (
        "oversized-reason-count",
        [f"reason-{index}" for index in range(17)],
    ),
    ("oversized-reason", ["r" * 257]),
    ("control-character", ["vector\x07attempt_failed"]),
    ("not-a-list", "vector:attempt_failed"),
]


@pytest.mark.parametrize(
    ("label", "reasons"),
    MALFORMED_REASONS,
    ids=[label for label, _ in MALFORMED_REASONS],
)
def test_a_malformed_degradation_envelope_is_the_named_protocol_failure(
    label: str, reasons: Any
) -> None:
    adapter = _ScriptedMCP(
        {
            "hits": [HIT],
            DEGRADED_FIELD: True,
            DEGRADED_REASONS_FIELD: reasons,
        }
    )

    result = adapter.retrieve("net sales")

    assert result == {"error": "retrieve_protocol_error"}, label
    assert "chunks" not in result
    assert "degraded" not in result
    assert "degraded_reasons" not in result


@pytest.mark.parametrize(
    ("label", "reasons"),
    MALFORMED_REASONS,
    ids=[label for label, _ in MALFORMED_REASONS],
)
def test_a_malformed_envelope_is_never_an_undegraded_result(
    label: str, reasons: Any
) -> None:
    """The spec's teeth: neither the plain result nor the degraded flag's
    opposite may answer an envelope fin cannot read."""
    adapter = _ScriptedMCP(
        {
            "hits": [HIT],
            DEGRADED_FIELD: False,
            DEGRADED_REASONS_FIELD: reasons,
        }
    )

    result = adapter.retrieve("net sales")

    assert result == {"error": "retrieve_protocol_error"}, label


def test_a_renamed_signal_is_the_same_named_protocol_failure() -> None:
    """A renamed field is a renamed response contract, which the spec puts
    under the same 「malformed or incompatible response contract」 row."""
    adapter = _ScriptedMCP({"hits": [HIT], "is_degraded": True})

    assert adapter.retrieve("net sales") == {"error": "retrieve_protocol_error"}


def test_the_exception_class_still_carries_its_internal_token() -> None:
    """The pre-commit contract: the exception keeps its own code, because
    the ledger and the logs need to tell the two kinds of envelope problem
    apart even though the wire says one thing."""
    with pytest.raises(DegradedEnvelopeInvalidError) as excinfo:
        read_degradation_signal(
            {DEGRADED_FIELD: True, DEGRADED_REASONS_FIELD: "not a list"}
        )

    assert excinfo.value.code == "degraded_envelope_invalid"


def test_the_wire_code_is_the_spec_token_not_the_exception_code() -> None:
    """One map, so a second call site cannot recode it differently."""
    from fin.retrieve.mcp_adapter import (
        _degradation_wire_code,
        DegradedEnvelopeInvalidError as _Same,
        DegradedFieldAliasError,
    )

    assert _Same is DegradedEnvelopeInvalidError
    assert _degradation_wire_code(DegradedEnvelopeInvalidError) == (
        "retrieve_protocol_error"
    )
    assert _degradation_wire_code(DegradedFieldAliasError) == (
        "retrieve_protocol_error"
    )


def test_the_internal_token_never_reaches_the_wire() -> None:
    """A response the adapter cannot name at all is ``retrieve_unavailable``,
    so the malformed envelope's internal code must not leak through any of
    the sibling paths either."""
    adapter = _ScriptedMCP({"hits": [HIT], DEGRADED_FIELD: True})

    result = adapter.retrieve("net sales")

    assert json.loads(json.dumps(result)) == {"error": "retrieve_protocol_error"}
