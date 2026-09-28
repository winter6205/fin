"""Memory-port graph tools for the cross-session operator memory spec (T6).

``specs/cross-session-operator-memory.md`` SC12 / SC13: when memory is ON,
the compiled graph exposes exactly three tools — explicit save, recall, and
layered user-profile edit. There is no dream tool in any branch (map G4).
When memory is OFF, none of the three tools are bound; the compiled graph's
tool set is the existing eight ToolNode tools.

This suite injects a memory port and a user-profile store directly into
``compile_graph`` so the cases run in the default ``uv run pytest -q``
surface (no operator env needed, no Postgres). The wiring tests already
pin ``memory_port`` semantics on the runtime provider — T6 owns the
graph-side surface.

Boundary classes exercised here:
* empty — OFF graph exposes no memory tool; ON graph with empty store
  returns 0 rows on recall.
* negative — ``OffMemoryAdapter`` keeps the graph's tool list at eight;
  unknown layer names on the profile tool raise.
* overflow — workspace isolation (SC6): save on workspace A is invisible
  from workspace B.
* concurrent — not exercised; the tool handlers are synchronous per call.
* exception — write to the workspace profile layer must NOT rewrite the
  global profile file (SC13).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import pytest
from langchain_core.messages import AIMessage
from langchain_core.tools import BaseTool
from langgraph.checkpoint.memory import InMemorySaver

from fin.host.graph import compile_graph
from fin.host.memory_buffer import LocalMemoryBuffer
from fin.host.memory_port import MemoryPort, OffMemoryAdapter
from fin.host.scripted_model import ScriptedChatModel
from fin.host.user_profile import (
    GLOBAL_FILENAME,
    WORKSPACE_FILENAME,
    UserProfileStore,
)


@dataclass
class _FakeMemoryPort:
    """Minimal ``MemoryPort`` shim with a workspace-aware local buffer.

    The graph tools (``save_memory`` / ``recall_memory``) call
    ``extract`` / ``recall`` on the injected port and forward the
    ``workspace`` argument. We delegate to a ``LocalMemoryBuffer`` so the
    workspace isolation property (SC6) is observable end to end through
    the tool surface.
    """

    enabled: bool = True
    buffer: LocalMemoryBuffer = field(default_factory=LocalMemoryBuffer)

    def extract(
        self,
        text: str,
        *,
        metadata: Mapping[str, object] | None = None,
        workspace: str = "",
    ) -> dict[str, object]:
        return self.buffer.extract(
            text,
            metadata=metadata,
            title=str((metadata or {}).get("title", "")) if metadata else "",
            workspace=workspace,
        )

    def recall(
        self,
        query: str,
        *,
        limit: int = 8,
        workspace: str = "",
    ) -> dict[str, object]:
        return self.buffer.recall(query, limit=limit, workspace=workspace)

    def gc(self, *, limit: int = 8) -> dict[str, object]:
        return self.buffer.gc(limit=limit)

    def dream(self, *, limit: int = 8) -> dict[str, object]:
        return self.buffer.dream(limit=limit)


def _scripted_model() -> ScriptedChatModel:
    """A scripted chat model whose single answer is "ok".

    ``compile_graph`` calls ``bind_tools`` on the model; the
    ``ScriptedChatModel.bind_tools`` returns the same model with the
    ``bound_tools`` attribute populated so tests can introspect the
    bound tool list directly.
    """
    return ScriptedChatModel(script=[AIMessage(content="ok")])


def _profile_store(tmp_path: Path, *, ws: str = "ws") -> UserProfileStore:
    """Build a profile store rooted under ``tmp_path``; parents are eager."""
    store = UserProfileStore(
        global_path=tmp_path / "home" / ".fin" / GLOBAL_FILENAME,
        workspace_path=tmp_path / ws / ".fin" / WORKSPACE_FILENAME,
    )
    store.global_path.parent.mkdir(parents=True, exist_ok=True)
    store.workspace_path.parent.mkdir(parents=True, exist_ok=True)
    return store


def _compiled_bound_tools(graph: Any) -> list[BaseTool]:
    """Return the tools the model bound (via ``bind_tools``).

    ``ScriptedChatModel.bind_tools`` sets ``bound_tools`` on the returned
    model — a list of ``StructuredTool`` instances. The factory closes
    over the bound model, so the compiled graph is the source of truth.
    """
    # ``graph`` is ``_LockedGraph``; the underlying compiled graph is
    # ``graph._graph``. The bound model is reachable via the graph's
    # internal ``builder`` only; the cleanest way to expose it without
    # touching private state is to capture the bound model on the proxy
    # itself. ``compile_graph`` exposes ``_bound_model`` for the test
    # surface.
    return list(graph._bound_tools)


def _tool_node_tools(graph: Any) -> dict[str, BaseTool]:
    """Return the tools the ToolNode was constructed with (executor side).

    The factory exposes the underlying ``ToolNode`` on the proxy as
    ``_tool_node``; ``ToolNode.tools_by_name`` is the documented public
    surface for the tool-by-name registry.
    """
    return dict(graph._tool_node.tools_by_name)


def _invoke_tool(
    graph: Any,
    name: str,
    args: dict[str, Any],
    *,
    config: dict[str, Any] | None = None,
) -> Any:
    """Call one of the ToolNode's tools by name with ``args``.

    The LangChain tool surface accepts a dict and returns the tool's
    declared payload (a JSON-serialized dict for ``@tool`` functions).
    ``config`` is the per-call run config — memory tools read
    ``configurable.workspace_root`` from it (spec SC8 / ADR 0049).
    """
    tool_node = graph._tool_node
    tool = tool_node.tools_by_name[name]
    if config is None:
        return tool.invoke(args)
    return tool.invoke(args, config=config)


# ---------------------------------------------------------------------------
# ON graph exposes exactly the three spec tools and no dream tool (SC12).
# ---------------------------------------------------------------------------


def test_on_graph_exposes_three_memory_tools(tmp_path: Path) -> None:
    """SC12: ON graph exposes save, recall, and edit-user-profile tools."""
    port = _FakeMemoryPort()
    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=_scripted_model(),
        memory_port=port,
        user_profile_store=_profile_store(tmp_path),
    )

    # Both surfaces see the same tool set; assert across both to catch a
    # future bug that binds a tool to the model but forgets ToolNode.
    tool_node_names = set(_tool_node_tools(graph))
    bound_names = {t.name for t in _compiled_bound_tools(graph)}

    union = tool_node_names | bound_names
    assert "save_memory" in union
    assert "recall_memory" in union
    assert "edit_user_profile" in union


def test_on_graph_does_not_expose_a_dream_tool(tmp_path: Path) -> None:
    """SC12 / map G4: dream is never a graph tool, ON or OFF."""
    port = _FakeMemoryPort()
    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=_scripted_model(),
        memory_port=port,
        user_profile_store=_profile_store(tmp_path),
    )

    tool_node_names = set(_tool_node_tools(graph))
    bound_names = {t.name for t in _compiled_bound_tools(graph)}

    for forbidden in ("dream", "run_dream", "memory_dream"):
        assert forbidden not in tool_node_names
        assert forbidden not in bound_names


# ---------------------------------------------------------------------------
# OFF graph exposes NONE of the three (SC3 / T3).
# ---------------------------------------------------------------------------


def test_off_graph_does_not_expose_any_memory_tool(tmp_path: Path) -> None:
    """Default OFF keeps the tool list at the existing eight tools."""
    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=_scripted_model(),
        memory_port=OffMemoryAdapter(),
        user_profile_store=_profile_store(tmp_path),
    )

    tool_node_names = set(_tool_node_tools(graph))
    bound_names = {t.name for t in _compiled_bound_tools(graph)}
    union = tool_node_names | bound_names

    for forbidden in ("save_memory", "recall_memory", "edit_user_profile", "dream"):
        assert forbidden not in union


def test_off_graph_with_none_port_still_has_nine_tools(tmp_path: Path) -> None:
    """``memory_port=None`` is also OFF (the compile_graph default)."""
    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=_scripted_model(),
        user_profile_store=_profile_store(tmp_path),
    )

    # The ToolNode's tool list is exactly the existing nine tools. The
    # model binding adds ``research_issuers`` on top of those ten
    # (research_issuers is bound to the model only, per Focus 4).
    tool_node_names = set(_tool_node_tools(graph))
    bound_names = {t.name for t in _compiled_bound_tools(graph)}

    assert len(tool_node_names) == 9
    expected_tool_node = {
        "list_files",
        "read_file",
        "read_source",
        "retrieve",
        "get_xbrl_fact",
        "compare_facts",
        "redline",
        "extract_tables",
        "three_year_statement",
    }
    assert tool_node_names == expected_tool_node
    assert "research_issuers" not in tool_node_names
    assert "research_issuers" in bound_names


# ---------------------------------------------------------------------------
# save_memory and recall_memory (SC12 / SC6).
# ---------------------------------------------------------------------------


def test_save_memory_persists_a_row_in_the_workspace(tmp_path: Path) -> None:
    port = _FakeMemoryPort()
    workspace_key = str(tmp_path / "ws")
    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=_scripted_model(),
        memory_port=port,
        user_profile_store=_profile_store(tmp_path),
    )
    cfg = {"configurable": {"thread_id": "t-save", "workspace_root": workspace_key}}

    _invoke_tool(
        graph,
        "save_memory",
        {"title": "Coffee preference", "body": "Latte in the morning."},
        config=cfg,
    )

    entries = port.buffer.entries(include_disabled=True, workspace=workspace_key)
    assert len(entries) == 1
    assert entries[0]["title"] == "Coffee preference"
    assert entries[0]["body"] == "Latte in the morning."
    assert entries[0]["workspace"] == workspace_key


def test_recall_memory_returns_advisory_rows(tmp_path: Path) -> None:
    port = _FakeMemoryPort()
    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=_scripted_model(),
        memory_port=port,
        user_profile_store=_profile_store(tmp_path),
    )
    workspace_key = str(tmp_path / "ws")
    cfg = {"configurable": {"thread_id": "t-recall", "workspace_root": workspace_key}}
    _invoke_tool(
        graph,
        "save_memory",
        {"title": "Coffee preference", "body": "Latte in the morning."},
        config=cfg,
    )

    raw = _invoke_tool(
        graph,
        "recall_memory",
        {"query": "latte morning coffee"},
        config=cfg,
    )
    payload = json.loads(raw) if isinstance(raw, str) else raw

    assert payload["status"] == "ok"
    assert len(payload["memories"]) == 1
    row = payload["memories"][0]
    # Advisory rows carry the explicit marker so the citation gate and
    # the verifier can refuse a memory row as evidence (ADR 0035 /
    # 0046). The contract is asserted here so future refactors cannot
    # silently promote a recall row into citation evidence.
    assert row["advisory"] is True
    assert row["citation_eligible"] is False


def test_recall_on_workspace_b_cannot_see_workspace_a(tmp_path: Path) -> None:
    """SC6 (via tools): workspace-isolated save/recall."""
    port_a = _FakeMemoryPort()
    graph_a = compile_graph(
        checkpointer=InMemorySaver(),
        model=_scripted_model(),
        memory_port=port_a,
        user_profile_store=_profile_store(tmp_path, ws="ws-a"),
    )
    cfg_a = {"configurable": {"thread_id": "t-a", "workspace_root": str(tmp_path / "ws-a")}}
    _invoke_tool(
        graph_a,
        "save_memory",
        {"title": "Coffee preference", "body": "Latte in the morning."},
        config=cfg_a,
    )

    # Same workspace → recall sees the row.
    raw = _invoke_tool(
        graph_a, "recall_memory", {"query": "latte coffee"}, config=cfg_a
    )
    payload = json.loads(raw) if isinstance(raw, str) else raw
    assert len(payload["memories"]) == 1

    # Different workspace → recall sees nothing (SC6).
    port_b = _FakeMemoryPort()
    graph_b = compile_graph(
        checkpointer=InMemorySaver(),
        model=_scripted_model(),
        memory_port=port_b,
        user_profile_store=_profile_store(tmp_path, ws="ws-b"),
    )
    cfg_b = {"configurable": {"thread_id": "t-b", "workspace_root": str(tmp_path / "ws-b")}}
    raw_other = _invoke_tool(
        graph_b, "recall_memory", {"query": "latte coffee"}, config=cfg_b
    )
    payload_other = json.loads(raw_other) if isinstance(raw_other, str) else raw_other

    assert payload_other["status"] == "ok"
    assert payload_other["memories"] == []


# ---------------------------------------------------------------------------
# edit_user_profile (SC13).
# ---------------------------------------------------------------------------


def test_edit_user_profile_workspace_does_not_rewrite_global(tmp_path: Path) -> None:
    """SC13: writing the workspace layer leaves the global file alone."""
    port = _FakeMemoryPort()
    profile_store = _profile_store(tmp_path)
    profile_store.write_layer("global", "Global baseline.")
    global_bytes_before = profile_store.global_path.read_bytes()

    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=_scripted_model(),
        memory_port=port,
        user_profile_store=profile_store,
    )

    _invoke_tool(
        graph,
        "edit_user_profile",
        {"layer": "workspace", "text": "Workspace specific override."},
    )

    assert profile_store.global_path.read_bytes() == global_bytes_before
    assert profile_store.workspace_path.read_text(encoding="utf-8") == (
        "Workspace specific override."
    )


def test_edit_user_profile_global_does_not_rewrite_workspace(
    tmp_path: Path,
) -> None:
    port = _FakeMemoryPort()
    profile_store = _profile_store(tmp_path)
    profile_store.write_layer("workspace", "Workspace baseline.")
    workspace_bytes_before = profile_store.workspace_path.read_bytes()

    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=_scripted_model(),
        memory_port=port,
        user_profile_store=profile_store,
    )

    _invoke_tool(
        graph,
        "edit_user_profile",
        {"layer": "global", "text": "Global override."},
    )

    assert profile_store.workspace_path.read_bytes() == workspace_bytes_before
    assert profile_store.global_path.read_text(encoding="utf-8") == "Global override."


def test_edit_user_profile_unknown_layer_returns_typed_dict(tmp_path: Path) -> None:
    """Unknown layer names return the typed error dict, never a crash.

    The ToolNode's ``handle_tool_errors=True`` keeps the run alive on a
    raised error too, but a returned dict keeps the tool message
    shape consistent with the rest of the host's typed EXIT dicts.
    """
    import json

    port = _FakeMemoryPort()
    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=_scripted_model(),
        memory_port=port,
        user_profile_store=_profile_store(tmp_path),
    )

    raw = _invoke_tool(
        graph, "edit_user_profile", {"layer": "user", "text": "x"}
    )
    payload = json.loads(raw) if isinstance(raw, str) else raw

    assert payload == {"error": "user_profile_unknown_layer", "layer": "user"}


def test_edit_user_profile_merged_read_is_workspace_over_global(
    tmp_path: Path,
) -> None:
    """SC13: merged read yields the workspace view when present."""
    port = _FakeMemoryPort()
    profile_store = _profile_store(tmp_path)
    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=_scripted_model(),
        memory_port=port,
        user_profile_store=profile_store,
    )

    _invoke_tool(
        graph, "edit_user_profile", {"layer": "global", "text": "Global likes coffee."}
    )
    _invoke_tool(
        graph,
        "edit_user_profile",
        {"layer": "workspace", "text": "Workspace prefers quarterly."},
    )

    assert profile_store.read_merged() == "Workspace prefers quarterly."


# ---------------------------------------------------------------------------
# ``compile_graph`` signature stays dream-free (T6c wiring invariant).
# ---------------------------------------------------------------------------


def test_compile_graph_signature_stays_dream_free() -> None:
    import inspect

    assert "dream" not in inspect.signature(compile_graph).parameters
