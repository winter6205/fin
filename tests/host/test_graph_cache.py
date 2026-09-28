"""Regression coverage for ``compile_graph``'s process-local cache."""

from __future__ import annotations

import builtins
import gc

import fin.host.graph as graph_module
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
import pytest

from fin.host import ScriptedChatModel, compile_graph


class _ValueEqualDisabledMemoryPort:
    """An OFF memory port whose value equality must not define graph identity."""

    enabled = False
    __hash__ = None

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _ValueEqualDisabledMemoryPort)


@pytest.fixture
def isolated_graph_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give cache identity cases an isolated cache without mutating the global one."""
    monkeypatch.setattr(graph_module, "_IDEMPOTENCE_CACHE", {})


def _force_memory_port_id_collision(monkeypatch: pytest.MonkeyPatch) -> None:
    real_id = builtins.id

    def colliding_id(value: object) -> int:
        if isinstance(value, _ValueEqualDisabledMemoryPort):
            return 318
        return real_id(value)

    monkeypatch.setattr(graph_module, "id", colliding_id, raising=False)


def test_distinct_equal_ports_do_not_alias_when_numeric_ids_collide(
    monkeypatch: pytest.MonkeyPatch, isolated_graph_cache: None
) -> None:
    checkpointer = InMemorySaver()
    model = ScriptedChatModel(script=[AIMessage(content="")])
    first_port = _ValueEqualDisabledMemoryPort()
    second_port = _ValueEqualDisabledMemoryPort()
    assert first_port == second_port
    with pytest.raises(TypeError):
        hash(first_port)

    _force_memory_port_id_collision(monkeypatch)

    first = compile_graph(
        checkpointer=checkpointer,
        model=model,
        memory_port=first_port,
    )
    same = compile_graph(
        checkpointer=checkpointer,
        model=model,
        memory_port=first_port,
    )
    other = compile_graph(
        checkpointer=checkpointer,
        model=model,
        memory_port=second_port,
    )

    assert same is first
    assert other is not first


def test_collected_port_cannot_leave_a_stale_graph_for_a_reused_id(
    monkeypatch: pytest.MonkeyPatch, isolated_graph_cache: None
) -> None:
    checkpointer = InMemorySaver()
    model = ScriptedChatModel(script=[AIMessage(content="")])
    _force_memory_port_id_collision(monkeypatch)

    original_port = _ValueEqualDisabledMemoryPort()
    original_graph = compile_graph(
        checkpointer=checkpointer,
        model=model,
        memory_port=original_port,
    )
    del original_port
    gc.collect()

    replacement_port = _ValueEqualDisabledMemoryPort()
    replacement_graph = compile_graph(
        checkpointer=checkpointer,
        model=model,
        memory_port=replacement_port,
    )

    assert replacement_graph is not original_graph
