"""Every retrieve adapter answers the ``RetrievePort`` signature.

``RetrievePort.retrieve`` (``src/fin/retrieve/port.py``) declares
``query`` positional and everything else — ``document_id``
included — keyword-only. An adapter that accepts ``document_id``
positionally still satisfies the runtime ``Protocol`` check
(``isinstance`` only inspects attribute names), so signature drift
between the port and an adapter is invisible until a caller
switches adapters and a positional argument lands in the wrong
parameter.

Spec ``specs/vertical-slice-hardening.md`` Focus 2 (f) / SC8:
``MCPRetrieveAdapter.retrieve`` matches the port's keyword-only
``document_id`` shape, and ``src/fin/retrieve/tool.py`` types its
retrieve dependency against the port instead of the archived
``BM25Adapter`` (ADR 0032 keeps BM25 archived).
"""

from __future__ import annotations

import inspect
from typing import get_type_hints

import pytest

from fin.retrieve import tool as retrieve_tool_module
from fin.retrieve.bm25 import BM25Adapter
from fin.retrieve.fake_port import FakeRetrievePort
from fin.retrieve.mcp_adapter import MCPRetrieveAdapter
from fin.retrieve.port import RetrievePort


def _parameters(func: object) -> dict[str, inspect.Parameter]:
    """Signature parameters without the bound ``self``."""
    params = dict(inspect.signature(func).parameters)  # type: ignore[arg-type]
    params.pop("self", None)
    return params


_PORT_PARAMETERS = _parameters(RetrievePort.retrieve)


@pytest.mark.parametrize(
    "method",
    [
        pytest.param(MCPRetrieveAdapter.retrieve, id="mcp-retrieve"),
        pytest.param(MCPRetrieveAdapter.aretrieve, id="mcp-aretrieve"),
        pytest.param(FakeRetrievePort.retrieve, id="fake-retrieve"),
        pytest.param(BM25Adapter.retrieve, id="bm25-retrieve"),
    ],
)
def test_adapter_signature_matches_the_port(method: object) -> None:
    """Parameter names, kinds, and defaults match the port's.

    ``aretrieve`` is in the matrix because ``retrieve`` forwards to
    it: a keyword-only ``document_id`` on the sync seam with a
    positional one underneath is the same drift one layer down.
    """
    assert _parameters(method) == _PORT_PARAMETERS


@pytest.mark.parametrize(
    "method",
    [
        pytest.param(MCPRetrieveAdapter.retrieve, id="mcp-retrieve"),
        pytest.param(MCPRetrieveAdapter.aretrieve, id="mcp-aretrieve"),
    ],
)
def test_document_id_is_keyword_only(method: object) -> None:
    """The named case behind SC8, asserted directly so a failure
    reads as "document_id drifted" rather than "signatures
    differ"."""
    parameter = _parameters(method)["document_id"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY


def test_mcp_adapter_rejects_positional_document_id() -> None:
    """The observable consequence: a caller cannot pass
    ``document_id`` positionally on one adapter and by keyword on
    another."""
    adapter = MCPRetrieveAdapter(command=None)
    with pytest.raises(TypeError):
        adapter.retrieve("revenue", "DOC-2023Q1")  # type: ignore[misc]


@pytest.mark.parametrize(
    "adapter",
    [
        pytest.param(BM25Adapter({}), id="bm25"),
        pytest.param(FakeRetrievePort(), id="fake"),
    ],
)
def test_every_adapter_accepts_the_wrapper_s_whole_call(adapter: object) -> None:
    """``fin.retrieve.tool.retrieve`` forwards EVERY port parameter.

    The signature equality above compares names, kinds and defaults; it
    cannot catch an adapter whose body rejects a forwarded keyword. The
    observable half is that the wrapper's own default call works against
    each implementation — an adapter missing one parameter raises
    ``TypeError`` at the seam a caller would otherwise hit only after
    swapping adapters in a run.
    """
    result = retrieve_tool_module.retrieve("revenue", limit=3, adapter=adapter)
    assert isinstance(result, dict)


def test_tool_types_its_dependency_against_the_port() -> None:
    """``fin.retrieve.tool.retrieve`` takes ANY ``RetrievePort``.

    Typing the parameter as the archived ``BM25Adapter`` named a
    concrete implementation of a sibling module as the host's
    retrieve dependency — the leak SC8 closes.
    """
    hints = get_type_hints(retrieve_tool_module.retrieve)
    assert RetrievePort in getattr(hints["adapter"], "__args__", ())


def test_tool_module_does_not_name_the_archived_bm25_adapter() -> None:
    """SC8's ``rg -c "BM25Adapter" src/fin/retrieve/tool.py`` check,
    as a test: the product seam does not mention the archived
    adapter at all (ADR 0032)."""
    source = inspect.getsource(retrieve_tool_module)
    assert "BM25Adapter" not in source
