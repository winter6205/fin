"""Jev / classify wiring must be threaded into ``compile_graph`` at every
production compile entry point.

ADR 0080 splits the two seams:

* ``jev_client`` — the Soft-Track gray zone. ``compile_for_runtime`` /
  ``compile_for_runtime_dev`` build it via ``build_jev_client`` from the
  same env; a missing key means ``None`` and the cascade fail-closes to
  clarify (never a MiniMax JSON fallback).
* ``classify_model`` — kept ONLY for the grounded turn's 检索档 cascade
  (search-mode host), still reusing ``FIN_MODEL`` identity.

We mock ``compile_graph`` so the tests stay hermetic (no Postgres, no
live LLM, no network).
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
from fin.host.jev_choice import JevChoiceClient


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
    monkeypatch.delenv("FIN_API_KEY", raising=False)
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
    monkeypatch.delenv("FIN_API_KEY", raising=False)
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
        "(the search-mode 检索档 cascade uses it)."
    )


# ----- ADR 0080: the gray-zone Jev client wiring -----


def _clear_jev_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "FIN_JEV_API_KEY",
        "TYPESAFE_API_KEY",
        "FIN_JEV_MODEL",
        "FIN_JEV_BASE_URL",
    ):
        monkeypatch.delenv(var, raising=False)


def _capture_compile_graph(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    graph_calls: list[dict[str, Any]] = []

    def _fake_compile_graph(**kw: Any) -> str:
        graph_calls.append(kw)
        return "compiled-graph"

    monkeypatch.setattr("fin.host.compile.compile_graph", _fake_compile_graph)
    return graph_calls


@pytest.mark.soft_track
def test_compile_for_runtime_threads_jev_client_when_key_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With ``FIN_JEV_API_KEY`` set, both runtime entry points must hand
    ``compile_graph`` a real ``JevChoiceClient`` for the gray zone."""
    _clear_jev_env(monkeypatch)
    monkeypatch.setenv("FIN_JEV_API_KEY", "test-only-not-a-real-key")
    monkeypatch.delenv("FIN_MODEL", raising=False)
    monkeypatch.delenv("FIN_BASE_URL", raising=False)
    monkeypatch.setenv("FIN_CHECKPOINT_URL", "postgresql://placeholder")

    _install_fake_langchain_openai(monkeypatch, {})
    _install_fake_postgres_saver(monkeypatch)
    graph_calls = _capture_compile_graph(monkeypatch)

    compile_for_runtime()
    assert len(graph_calls) == 1
    client = graph_calls[0].get("jev_client")
    assert isinstance(client, JevChoiceClient), (
        "compile_for_runtime must build the gray-zone Jev client via "
        "build_jev_client when a TypeSafe key exists (ADR 0080)."
    )


@pytest.mark.soft_track
def test_compile_for_runtime_dev_jev_client_none_when_key_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing key → ``jev_client=None``: the cascade fail-closes to
    clarify at the gray zone — no MiniMax fallback is wired (ADR 0080)."""
    _clear_jev_env(monkeypatch)
    monkeypatch.delenv("FIN_MODEL", raising=False)
    monkeypatch.delenv("FIN_BASE_URL", raising=False)
    monkeypatch.delenv("FIN_CHECKPOINT_URL", raising=False)

    _install_fake_langchain_openai(monkeypatch, {})
    graph_calls = _capture_compile_graph(monkeypatch)

    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        compile_for_runtime_dev()
    assert len(graph_calls) == 1
    assert graph_calls[0].get("jev_client") is None


@pytest.mark.soft_track
def test_compile_for_runtime_dev_threads_jev_client_when_key_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dev mirror of the runtime threading contract: key present → real
    client, so the dev boot exercises the same gray-zone tier."""
    _clear_jev_env(monkeypatch)
    monkeypatch.setenv("FIN_JEV_API_KEY", "test-only-not-a-real-key")
    monkeypatch.delenv("FIN_MODEL", raising=False)
    monkeypatch.delenv("FIN_BASE_URL", raising=False)

    _install_fake_langchain_openai(monkeypatch, {})
    graph_calls = _capture_compile_graph(monkeypatch)

    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        compile_for_runtime_dev()
    assert len(graph_calls) == 1
    assert isinstance(graph_calls[0].get("jev_client"), JevChoiceClient)


@pytest.mark.soft_track
def test_compile_for_runtime_jev_client_none_when_key_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runtime mirror of the dev contract: key missing → ``None`` client,
    the gray zone fail-closes to clarify with no fallback wired."""
    _clear_jev_env(monkeypatch)
    monkeypatch.delenv("FIN_MODEL", raising=False)
    monkeypatch.delenv("FIN_BASE_URL", raising=False)
    monkeypatch.setenv("FIN_CHECKPOINT_URL", "postgresql://placeholder")

    _install_fake_langchain_openai(monkeypatch, {})
    _install_fake_postgres_saver(monkeypatch)
    graph_calls = _capture_compile_graph(monkeypatch)

    compile_for_runtime()
    assert len(graph_calls) == 1
    assert graph_calls[0].get("jev_client") is None
