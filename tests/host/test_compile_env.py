"""Tests for the ``FIN_API_KEY`` env-var resolution in
``fin.host.compile._build_default_model``.

ADR 0022 update 2026-08-27 renamed the project-owned LLM API key
from the historical Anthropic-shaped ``ANTHROPIC_AUTH_TOKEN`` to
the provider-agnostic ``FIN_API_KEY``. This was a HARD switch:
the legacy name is no longer read at all. A missing
``FIN_API_KEY`` falls through to the placeholder
``sk-no-key-required`` and the provider returns 401 — a loud error
signal rather than a silent use of an unexpected credential left
over from before the MiniMax cutover.

These tests pin that contract end-to-end at the ``_build_default_model``
boundary (the function the prod and dev boot paths both call):

1. Only ``FIN_API_KEY`` is set → that value reaches the model.
2. Neither name is set → falls through to the placeholder; no
   ``DeprecationWarning`` (the contract is a hard switch — there
   is no soft-cutover warning to assert).
3. Only the legacy ``ANTHROPIC_AUTH_TOKEN`` is set → the legacy
   value is IGNORED. The api key is the placeholder, NOT the
   legacy value. This is the test that proves the hard switch:
   the very behaviour the user-facing motivation asks for.
4. ``FIN_API_KEY`` is empty / whitespace → treated as unset by
   ``_read_env``; falls through to the placeholder. The legacy
   value (if any) is still ignored — empty ``FIN_API_KEY`` does
   not unlock the legacy fallback path that does not exist.
"""

from __future__ import annotations

import warnings
from typing import Any

import pytest

from fin.host.compile import (
    FIN_API_KEY_ENV,
    _build_default_model,
)


# ----- helpers -----


def _install_fake_chat_openai(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    """Patch ``reasoning_preserving_chat_openai`` so
    ``_build_default_model`` resolves a stub callable that
    captures its kwargs. Avoids needing the real
    ``langchain_openai`` package in this test's import graph and
    keeps the assertion surface focused on the env-var contract.
    """
    import fin.host.compile as compile_module

    captured: dict[str, Any] = {}

    def _fake_chat_openai(**kwargs: Any) -> str:
        captured.update(kwargs)
        return "fake-model"

    monkeypatch.setattr(
        compile_module,
        "reasoning_preserving_chat_openai",
        lambda: _fake_chat_openai,
    )
    return captured


def _build(captured: dict[str, Any], env: dict[str, str]) -> None:
    """Call ``_build_default_model`` with explicit model + url so
    the env-var lookups for those are skipped; only the api-key
    resolution reads from ``env``."""
    _build_default_model(
        model_name="test-model",
        base_url="http://example.invalid/v1",
        env=env,
    )


# ----- the contract -----


def test_fin_api_key_constant_is_the_project_name() -> None:
    """Pin the public name. Tests, docs, and operator scripts all
    import or echo this constant; a silent rename would defeat the
    point of the rename."""
    assert FIN_API_KEY_ENV == "FIN_API_KEY"


def test_fin_api_key_is_read_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """Happy path: ``FIN_API_KEY`` is forwarded to ChatOpenAI as
    ``api_key=`` (official Chat Completions uses ``Authorization:
    Bearer``). The user's secret value must reach ChatOpenAI
    without ever being logged or echoed by this test."""
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    captured = _install_fake_chat_openai(monkeypatch)

    _build(captured, {"FIN_API_KEY": "sk-new-key"})

    assert captured["api_key"] == "sk-new-key"


def test_no_key_falls_back_to_placeholder_without_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When neither name is set, the placeholder is used and NO
    ``DeprecationWarning`` fires (there is no soft-cutover
    warning to emit on the hard switch). A dev boot or a
    missing-config boot must not raise noise that the operator
    has no action for."""
    monkeypatch.delenv("FIN_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    captured = _install_fake_chat_openai(monkeypatch)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _build(captured, {})

    assert captured["api_key"] == "sk-no-key-required"
    assert not [
        w for w in caught if issubclass(w.category, DeprecationWarning)
    ], "hard switch must not emit DeprecationWarning"


def test_legacy_anthropic_auth_token_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE contract that motivated the hard switch. When the
    legacy ``ANTHROPIC_AUTH_TOKEN`` is set but the new
    ``FIN_API_KEY`` is not, the legacy value MUST NOT reach the
    model. The placeholder is used instead and the provider
    returns 401 — a loud error signal rather than a silent use
    of a stale local-gateway key.

    This test is the regression gate: if a future change adds a
    fallback for any reason, this test fails and forces a
    conscious decision."""
    monkeypatch.delenv("FIN_API_KEY", raising=False)
    captured = _install_fake_chat_openai(monkeypatch)

    _build(captured, {"ANTHROPIC_AUTH_TOKEN": "sk-stale-local-gateway"})

    assert captured["api_key"] == "sk-no-key-required", (
        "hard switch must ignore the legacy name; a fallback "
        "would let a stale shell value masquerade as the active "
        "credential"
    )


def test_empty_fin_api_key_is_unset_and_falls_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_read_env`` treats whitespace / empty strings as unset.
    An explicitly empty ``FIN_API_KEY`` therefore falls through
    to the placeholder. The legacy value (if any) is still
    IGNORED — an empty ``FIN_API_KEY`` does not unlock a legacy
    fallback path that does not exist."""
    captured = _install_fake_chat_openai(monkeypatch)

    _build(
        captured,
        {
            "FIN_API_KEY": "   ",
            "ANTHROPIC_AUTH_TOKEN": "sk-stale-local-gateway",
        },
    )

    assert captured["api_key"] == "sk-no-key-required"