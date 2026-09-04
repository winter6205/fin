"""``fin.host.compile`` wires the PRODUCT retrieve adapter (T3).

Spec ``specs/s1-corpus-retrieve.md``:

* line 14 — "**product corpus**: ... ``fin-serve`` retrieve,
  ``read_file``, and the citation verifier read this tree under the
  live locator contract."
* line 27 — "Product retrieve = MCP client to **iknow-memory**
  behind the retrieve port (ADR 0030). ... Default pytest uses a
  fake RetrievePort".

Both production-shaped compilers — ``compile_for_runtime`` (Postgres)
and ``compile_for_runtime_dev`` (tests/eval InMemory) — must therefore
select the MCP adapter. The in-memory helper used to inherit
``FakeRetrievePort`` purely because it passes an ``InMemorySaver``.

``ChatOpenAI`` and ``PostgresSaver`` are stubbed; no network, no
database, and no MCP subprocess is started (the adapter spawns its
subprocess per ``retrieve`` call, not at construction).
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from fin.host.compile import (
    DEFAULT_RETRIEVE_ADAPTER,
    compile_for_runtime,
    compile_for_runtime_dev,
)
from fin.retrieve import MCPRetrieveAdapter


# ----- stubs -----


class _FakeChatOpenAI:
    def __init__(self, **_kwargs: object) -> None:
        pass

    def bind_tools(self, _tools: object) -> "_FakeChatOpenAI":
        return self

    def bind(self, **_kwargs: object) -> "_FakeChatOpenAI":
        return self


def _install_fake_chat_model(monkeypatch: pytest.MonkeyPatch) -> None:
    module = types.ModuleType("langchain_openai")
    module.ChatOpenAI = _FakeChatOpenAI
    monkeypatch.setitem(sys.modules, "langchain_openai", module)


class _FakePostgresSaver:
    def setup(self) -> None:
        return None

    @classmethod
    def from_conn_string(cls, url: str, *, pipeline: bool = False):
        return _FakePostgresCM()


class _FakePostgresCM:
    def __enter__(self) -> _FakePostgresSaver:
        return _FakePostgresSaver()

    def __exit__(self, *exc: object) -> None:
        return None


def _install_fake_postgres(monkeypatch: pytest.MonkeyPatch) -> None:
    module = types.ModuleType("langgraph.checkpoint.postgres")
    module.PostgresSaver = _FakePostgresSaver
    monkeypatch.setitem(
        sys.modules, "langgraph.checkpoint.postgres", module
    )


def _capture_compile_graph(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Replace ``compile_graph`` so the test reads the kwargs the
    compile helpers pass, without building a real graph."""
    calls: list[dict] = []

    def _fake(**kwargs: Any) -> str:
        calls.append(kwargs)
        return "compiled-graph"

    monkeypatch.setattr("fin.host.compile.compile_graph", _fake)
    return calls


def _record_mcp_construction(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    built: list[dict] = []

    class _RecordingMCP(MCPRetrieveAdapter):
        def __init__(self, **kwargs: Any) -> None:
            built.append(kwargs)
            super().__init__(**kwargs)

    monkeypatch.setattr("fin.host.graph.MCPRetrieveAdapter", _RecordingMCP)
    return built


@pytest.fixture(autouse=True)
def _clear_retrieve_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FIN_RETRIEVE_ADAPTER", raising=False)
    monkeypatch.delenv("FIN_MODEL", raising=False)
    monkeypatch.delenv("FIN_BASE_URL", raising=False)


# ----- the default kind -----


def test_product_default_retrieve_adapter_is_mcp() -> None:
    """ADR 0030: the product retrieve implementation is the MCP
    client. The fake port is the pytest surface, not a shipping
    default."""
    assert DEFAULT_RETRIEVE_ADAPTER == "mcp"


# ----- compile_for_runtime_dev still uses product retrieve -----


def test_compile_for_runtime_dev_selects_mcp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``compile_for_runtime_dev`` still serves the product corpus
    over MCP; checkpointer kind does not pick the retrieve adapter."""
    _install_fake_chat_model(monkeypatch)
    monkeypatch.delenv("FIN_CHECKPOINT_URL", raising=False)
    calls = _capture_compile_graph(monkeypatch)

    with pytest.warns(UserWarning, match="FIN_CHECKPOINT_URL"):
        compile_for_runtime_dev()

    assert len(calls) == 1
    assert calls[0]["retrieve_adapter"] == "mcp"


def test_compile_for_runtime_dev_builds_mcp_adapter_without_extracted_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end through the real factory: ``compile_for_runtime_dev``
    binds an ``MCPRetrieveAdapter`` WITHOUT reading the extracted tree
    (ADR 0046 / 0047 — the serve path has no on-disk SSOT). Canonical
    ``source_ref`` hits need no locator map; the client-side allow-list
    defers to the MCP server when none is configured."""

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("runtime compile consulted the extracted tree")

    monkeypatch.setattr("fin.retrieve.read_port.load_extracted_tree", _boom)
    monkeypatch.setattr("fin.corpus.extracted.load_extracted_tree", _boom)

    _install_fake_chat_model(monkeypatch)
    monkeypatch.delenv("FIN_CHECKPOINT_URL", raising=False)
    built = _record_mcp_construction(monkeypatch)

    with pytest.warns(UserWarning, match="FIN_CHECKPOINT_URL"):
        graph = compile_for_runtime_dev()

    assert graph is not None
    assert len(built) == 1
    assert built[0]["locator_map"] == {}
    assert built[0]["known_document_ids"] is None


def test_fin_retrieve_adapter_env_can_force_the_fake_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The operator escape hatch: an explicit ``FIN_RETRIEVE_ADAPTER
    =fake`` boots without a live iknow-memory server. It is opt-in
    and named, not an accident of the checkpointer."""
    _install_fake_chat_model(monkeypatch)
    monkeypatch.delenv("FIN_CHECKPOINT_URL", raising=False)
    monkeypatch.setenv("FIN_RETRIEVE_ADAPTER", "fake")
    calls = _capture_compile_graph(monkeypatch)

    with pytest.warns(UserWarning, match="FIN_CHECKPOINT_URL"):
        compile_for_runtime_dev()

    assert calls[0]["retrieve_adapter"] == "fake"


# ----- durable path -----


def test_compile_for_runtime_selects_mcp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Postgres path selects MCP from configuration too — the
    same explicit kind, not a side effect of the saver class."""
    _install_fake_chat_model(monkeypatch)
    _install_fake_postgres(monkeypatch)
    monkeypatch.setenv("FIN_CHECKPOINT_URL", "postgresql://placeholder")
    calls = _capture_compile_graph(monkeypatch)

    compile_for_runtime()

    assert len(calls) == 1
    assert calls[0]["retrieve_adapter"] == "mcp"


def test_compile_for_runtime_honours_retrieve_adapter_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_chat_model(monkeypatch)
    _install_fake_postgres(monkeypatch)
    monkeypatch.setenv("FIN_CHECKPOINT_URL", "postgresql://placeholder")
    monkeypatch.setenv("FIN_RETRIEVE_ADAPTER", "fake")
    calls = _capture_compile_graph(monkeypatch)

    compile_for_runtime()

    assert calls[0]["retrieve_adapter"] == "fake"
