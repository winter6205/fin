"""Soft-Track: classify_model must be threaded into ``compile_graph`` at every
production compile entry point.

Spec rule (specs/soft-track.md): ``classify_model`` must reuse ``FIN_MODEL``,
i.e. the same model already wired through ``compile_for_runtime``. The default
of ``compile_graph(classify_model=None)`` permanently skips the LLM tier of
Soft-Track, so every ambiguous utterance falls through to the residual
``track="research"`` branch in :mod:`fin.host.track_decision`. That violates
the "no single global default track" rule and breaks the
``conditional_policy`` contract.

These tests assert that both runtime entry points — durable
(:func:`fin.host.compile.compile_for_runtime`) and in-memory
(:func:`fin.host.compile.compile_for_runtime_dev`) — pass the SAME model
object as both ``model=`` and ``classify_model=`` when handing control off
to ``compile_graph``. We mock ``compile_graph`` so the test stays hermetic
(no Postgres, no live LLM).
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from fin.host.compile import (
    compile_for_runtime,
    compile_for_runtime_dev,
)


# ----- fake runtime deps -----


def _install_fake_langchain_openai(
    monkeypatch: pytest.MonkeyPatch, captured: dict[str, object]
) -> None:
    """Inject a stub ``langchain_openai.ChatOpenAI``.

    The stub exposes ``bind_tools`` / ``bind`` so the boot path
    can still layer the Soft-Track tool bindings without an
    actual ``langchain_openai`` install in the test env.
    """

    fake_mod = types.ModuleType("langchain_openai")

    class _FakeChatOpenAI:
        def __init__(
            self,
            *,
            model: str,
            base_url: str | None = None,
            api_key: str | None = None,
            **_kwargs: object,
        ) -> None:
            captured["model"] = model
            captured["base_url"] = base_url
            captured["api_key"] = api_key
            captured["extra_body"] = _kwargs.get("extra_body")
            captured["model_kwargs"] = _kwargs.get("model_kwargs")
            captured["timeout"] = _kwargs.get("timeout")

        def bind_tools(self, _tools: object) -> "_FakeChatOpenAI":
            return self

        def bind(self, **_kwargs: object) -> "_FakeChatOpenAI":
            return self

    fake_mod.ChatOpenAI = _FakeChatOpenAI
    monkeypatch.setitem(sys.modules, "langchain_openai", fake_mod)


def _install_fake_postgres_saver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stub ``langgraph.checkpoint.postgres.PostgresSaver`` so the durable
    entry point reaches ``compile_graph`` without a live database."""

    class _FakePostgresSaver:
        def __init__(self) -> None:
            self.setup_called = False

        def setup(self) -> None:
            self.setup_called = True

        @classmethod
        def from_conn_string(
            cls, url: str, *, pipeline: bool = False
        ) -> "_FakePostgresCM":
            return _FakePostgresCM()

    class _FakePostgresCM:
        def __enter__(self) -> _FakePostgresSaver:
            return _FakePostgresSaver()

        def __exit__(self, *exc: object) -> None:
            return None

    pg_mod = types.ModuleType("langgraph.checkpoint.postgres")
    pg_mod.PostgresSaver = _FakePostgresSaver
    monkeypatch.setitem(sys.modules, "langgraph.checkpoint.postgres", pg_mod)


# ----- the contract tests -----


@pytest.mark.soft_track
def test_compile_for_runtime_injects_classify_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``compile_for_runtime`` MUST pass ``classify_model=model`` so the
    Soft-Track LLM tier is reachable in production.

    Identity (``is``), not equality — the spec rule is "reuse the
    same model". ``compile_graph`` keys its idempotence cache on
    ``id(classify_model)``; reusing the same object means two
    runtime calls on the same ``FIN_MODEL`` resolve to the same
    proxy.
    """
    monkeypatch.delenv("FIN_MODEL", raising=False)
    monkeypatch.delenv("FIN_BASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("FIN_CHECKPOINT_URL", "postgresql://placeholder")

    captured: dict[str, object] = {}
    _install_fake_langchain_openai(monkeypatch, captured)
    _install_fake_postgres_saver(monkeypatch)

    graph_calls: list[dict[str, Any]] = []

    def _fake_compile_graph(**kw: Any) -> str:
        graph_calls.append(kw)
        return "compiled-graph"

    monkeypatch.setattr(
        "fin.host.compile.compile_graph", _fake_compile_graph
    )

    result = compile_for_runtime()
    assert result == "compiled-graph"
    assert len(graph_calls) == 1, (
        "compile_for_runtime must call compile_graph exactly once"
    )
    call = graph_calls[0]
    model = call["model"]
    assert model is not None, "compile_graph must receive model="
    # The whole point of this fix: the LLM classify model is the
    # same object as the runtime model.
    assert call.get("classify_model") is model, (
        "compile_for_runtime must thread the SAME model object "
        "through to compile_graph as classify_model= (spec "
        "specs/soft-track.md — 'classify_model must reuse "
        "FIN_MODEL'). Without it the LLM tier is silently "
        "skipped in production."
    )


@pytest.mark.soft_track
def test_compile_for_runtime_dev_injects_classify_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``compile_for_runtime_dev`` MUST also pass ``classify_model=model``.

    Same contract as the durable path: the LLM tier must be
    reachable in the in-memory dev path so dev runs exercise
    production behavior.
    """
    monkeypatch.delenv("FIN_MODEL", raising=False)
    monkeypatch.delenv("FIN_BASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("FIN_CHECKPOINT_URL", raising=False)

    captured: dict[str, object] = {}
    _install_fake_langchain_openai(monkeypatch, captured)

    graph_calls: list[dict[str, Any]] = []

    def _fake_compile_graph(**kw: Any) -> str:
        graph_calls.append(kw)
        return "compiled-graph"

    monkeypatch.setattr(
        "fin.host.compile.compile_graph", _fake_compile_graph
    )

    # The dev helper emits a ``warnings.warn`` on every call; the
    # test suite is allowed to ignore that here.
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        result = compile_for_runtime_dev()
    assert result == "compiled-graph"
    assert len(graph_calls) == 1, (
        "compile_for_runtime_dev must call compile_graph exactly once"
    )
    call = graph_calls[0]
    model = call["model"]
    assert model is not None, "compile_graph must receive model="
    assert call.get("classify_model") is model, (
        "compile_for_runtime_dev must thread the SAME model "
        "object through to compile_graph as classify_model= "
        "(spec specs/soft-track.md)."
    )