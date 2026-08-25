"""Tests for the chat-model router (ADR 0033).

The router (``fin.host.model_router.resolve_chat_model``) dispatches
between ``ChatOpenAI`` and ``ChatAnthropic`` based on
``FIN_BASE_URL`` shape (auto mode) or an explicit
``FIN_MODEL_PROVIDER`` override. These tests pin the dispatch
matrix and the typed-error contract; they do NOT touch the network
(constructor calls only — both LangChain classes defer I/O to
``invoke``).

The hermetic default ``uv run pytest -q`` suite stays green
without a live ``OPENAI_API_KEY`` / ``ANTHROPIC_API_KEY``.
"""

from __future__ import annotations

import pytest

from fin.host.compile import DEFAULT_BASE_URL, DEFAULT_MODEL
from fin.host.model_router import (
    ModelRouterError,
    PROVIDER_ANTHROPIC,
    PROVIDER_OPENAI,
    resolve_chat_model,
)


# ----- dispatch by URL shape (auto mode) -----


def test_default_base_url_returns_chat_openai() -> None:
    """The legacy proxy URL (``/v1``) routes to ``ChatOpenAI``."""
    m = resolve_chat_model(
        {"FIN_BASE_URL": DEFAULT_BASE_URL, "ANTHROPIC_AUTH_TOKEN": "sk-test"}
    )
    assert type(m).__name__ == "ChatOpenAI"


def test_openai_compat_url_returns_chat_openai() -> None:
    """``api.openai.com/v1`` and ``api.minimaxi.com/v1`` both
    route to ``ChatOpenAI`` — both are OpenAI-compatible."""
    for url in (
        "https://api.openai.com/v1",
        "https://api.minimaxi.com/v1",
        "http://localhost:20128/v1",
    ):
        m = resolve_chat_model(
            {"FIN_BASE_URL": url, "ANTHROPIC_AUTH_TOKEN": "sk-test"}
        )
        assert type(m).__name__ == "ChatOpenAI", url


def test_anthropic_compat_url_returns_chat_anthropic() -> None:
    """URL ending in ``/anthropic`` routes to ``ChatAnthropic``.

    The dispatch is what makes the MiniMax Token Plan 订阅 key
    work — that key only authorizes the Anthropic-compatible
    endpoint, not the OpenAI Chat Completions one.
    """
    m = resolve_chat_model(
        {
            "FIN_BASE_URL": "https://api.minimaxi.com/anthropic",
            "ANTHROPIC_API_KEY": "sk-test",
        }
    )
    assert type(m).__name__ == "ChatAnthropic"


def test_anthropic_compat_url_with_trailing_slash() -> None:
    """Trailing slash on the URL must not break dispatch."""
    m = resolve_chat_model(
        {
            "FIN_BASE_URL": "https://api.minimaxi.com/anthropic/",
            "ANTHROPIC_API_KEY": "sk-test",
        }
    )
    assert type(m).__name__ == "ChatAnthropic"


def test_anthropic_compat_url_case_insensitive() -> None:
    """Upper-case host or suffix is normalised to lower-case."""
    m = resolve_chat_model(
        {
            "FIN_BASE_URL": "HTTPS://API.MINIMAXI.COM/Anthropic",
            "ANTHROPIC_API_KEY": "sk-test",
        }
    )
    assert type(m).__name__ == "ChatAnthropic"


# ----- explicit FIN_MODEL_PROVIDER override -----


def test_explicit_provider_anthropic_overrides_url_shape() -> None:
    """``FIN_MODEL_PROVIDER=anthropic`` wins over a non-anthropic URL."""
    m = resolve_chat_model(
        {
            "FIN_BASE_URL": "https://api.openai.com/v1",
            "FIN_MODEL_PROVIDER": "anthropic",
            "ANTHROPIC_API_KEY": "sk-test",
        }
    )
    assert type(m).__name__ == "ChatAnthropic"


def test_explicit_provider_openai_overrides_anthropic_url() -> None:
    """``FIN_MODEL_PROVIDER=openai`` wins over an /anthropic URL."""
    m = resolve_chat_model(
        {
            "FIN_BASE_URL": "https://api.minimaxi.com/anthropic",
            "FIN_MODEL_PROVIDER": "openai",
            "ANTHROPIC_AUTH_TOKEN": "sk-test",
        }
    )
    assert type(m).__name__ == "ChatOpenAI"


def test_explicit_provider_auto_defers_to_url_shape() -> None:
    """``auto`` is the documented value of the explicit knob."""
    m_anthropic = resolve_chat_model(
        {
            "FIN_BASE_URL": "https://api.minimaxi.com/anthropic",
            "FIN_MODEL_PROVIDER": "auto",
            "ANTHROPIC_API_KEY": "sk-test",
        }
    )
    assert type(m_anthropic).__name__ == "ChatAnthropic"
    m_openai = resolve_chat_model(
        {
            "FIN_BASE_URL": "https://api.minimaxi.com/v1",
            "FIN_MODEL_PROVIDER": "AUTO",  # case-insensitive
            "ANTHROPIC_AUTH_TOKEN": "sk-test",
        }
    )
    assert type(m_openai).__name__ == "ChatOpenAI"


def test_invalid_provider_raises_typed_error() -> None:
    """A typo in ``FIN_MODEL_PROVIDER`` must NOT silently fall back
    to ``ChatOpenAI`` — the operator's intent was Anthropic and a
    silent fallback would surprise them. Raise the typed error
    instead (CLAUDE.md: typed stop kinds)."""
    with pytest.raises(ModelRouterError) as excinfo:
        resolve_chat_model(
            {
                "FIN_BASE_URL": "https://api.openai.com/v1",
                "FIN_MODEL_PROVIDER": "anthropc",  # typo
                "ANTHROPIC_AUTH_TOKEN": "sk-test",
            }
        )
    assert "FIN_MODEL_PROVIDER" in str(excinfo.value)


# ----- Anthropic path: required env keys -----


def test_anthropic_path_requires_anthropic_api_key() -> None:
    """The Anthropic path has no placeholder for the api key
    (``sk-no-key-required`` would mask a real misconfiguration);
    the operator must set ``ANTHROPIC_API_KEY``."""
    with pytest.raises(ModelRouterError) as excinfo:
        resolve_chat_model(
            {
                "FIN_BASE_URL": "https://api.minimaxi.com/anthropic",
                # ANTHROPIC_API_KEY deliberately omitted
            }
        )
    assert "ANTHROPIC_API_KEY" in str(excinfo.value)


def test_anthropic_path_requires_fin_base_url() -> None:
    """Even with ``FIN_MODEL_PROVIDER=anthropic`` explicit, no URL
    is a real misconfiguration."""
    with pytest.raises(ModelRouterError) as excinfo:
        resolve_chat_model(
            {
                "FIN_MODEL_PROVIDER": "anthropic",
                "ANTHROPIC_API_KEY": "sk-test",
                # FIN_BASE_URL deliberately omitted
            }
        )
    assert "FIN_BASE_URL" in str(excinfo.value)


def test_anthropic_path_whitespace_only_api_key_treated_as_absent() -> None:
    """A whitespace-only ``ANTHROPIC_API_KEY`` is treated as absent
    so a malformed .env entry fails fast rather than passing an
    empty Bearer."""
    with pytest.raises(ModelRouterError):
        resolve_chat_model(
            {
                "FIN_BASE_URL": "https://api.minimaxi.com/anthropic",
                "ANTHROPIC_API_KEY": "   ",
            }
        )


# ----- OpenAI path: backward compat -----


def test_openai_path_uses_anthropic_auth_token_alias() -> None:
    """The OpenAI path keeps the legacy alias
    ``ANTHROPIC_AUTH_TOKEN`` so pre-router configs continue to
    work without operator action."""
    m = resolve_chat_model(
        {
            "FIN_BASE_URL": "https://api.minimaxi.com/v1",
            "ANTHROPIC_AUTH_TOKEN": "sk-test-legacy",
        }
    )
    assert type(m).__name__ == "ChatOpenAI"
    # ``openai_api_key`` is a ``SecretStr``; read via the accessor
    # so the assertion does not compare against the masked form.
    assert m.openai_api_key.get_secret_value() == "sk-test-legacy"


def test_openai_path_uses_no_key_placeholder_when_unset() -> None:
    """``ChatOpenAI`` accepts the ``sk-no-key-required`` placeholder
    so a hermetic boot (no key in env) does not crash. This is the
    historical behavior and the router preserves it."""
    m = resolve_chat_model(
        {"FIN_BASE_URL": "https://api.minimaxi.com/v1"}
    )
    assert type(m).__name__ == "ChatOpenAI"
    assert m.openai_api_key.get_secret_value() == "sk-no-key-required"


def test_openai_path_passes_extra_body() -> None:
    """``ChatOpenAI`` must receive the MiniMax-M3 thinking /
    reasoning_split extras (ADR 0022 + ADR 0026). The router
    preserves the original ChatOpenAI-side behavior verbatim."""
    m = resolve_chat_model(
        {
            "FIN_BASE_URL": "https://api.minimaxi.com/v1",
            "ANTHROPIC_AUTH_TOKEN": "sk-test",
        }
    )
    assert type(m).__name__ == "ChatOpenAI"
    # ``extra_body`` is a top-level Pydantic field on ChatOpenAI
    # (verified by ``dir()``); nested ``model_kwargs`` would be
    # rejected by the constructor with "specify extra_body
    # explicitly" (ADR 0022).
    extra_body = m.extra_body
    assert extra_body is not None
    assert extra_body.get("reasoning_split") is True
    thinking = extra_body.get("thinking")
    assert isinstance(thinking, dict)
    assert thinking.get("type") == "adaptive"


def test_anthropic_path_does_not_pass_extra_body() -> None:
    """``ChatAnthropic`` does NOT receive ``extra_body`` — its
    thinking channel is configured via ``betas`` (a different
    parameter surface) and the MiniMax Anthropic-compatible
    endpoint reuses the same field names LangChain normalises."""
    m = resolve_chat_model(
        {
            "FIN_BASE_URL": "https://api.minimaxi.com/anthropic",
            "ANTHROPIC_API_KEY": "sk-test",
        }
    )
    assert type(m).__name__ == "ChatAnthropic"
    # The Anthropic constructor does not accept ``extra_body``;
    # if it silently accepted one, downstream invocation would
    # 400 the first request. A non-None ``extra_body`` field would
    # be a regression (verified against ChatAnthropic's Pydantic
    # schema).
    assert getattr(m, "extra_body", None) is None


# ----- FIN_MODEL -----


def test_fin_model_passed_through_on_openai_path() -> None:
    """``FIN_MODEL`` is forwarded verbatim to ``ChatOpenAI``."""
    m = resolve_chat_model(
        {
            "FIN_BASE_URL": "https://api.minimaxi.com/v1",
            "FIN_MODEL": "gpt-4o-mini",
            "ANTHROPIC_AUTH_TOKEN": "sk-test",
        }
    )
    assert m.model_name == "gpt-4o-mini"


def test_fin_model_passed_through_on_anthropic_path() -> None:
    """``FIN_MODEL`` is forwarded verbatim to ``ChatAnthropic``
    too — MiniMax accepts ``MiniMax-M3`` on the Anthropic-compatible
    endpoint per its docs, so the same id works on both paths."""
    m = resolve_chat_model(
        {
            "FIN_BASE_URL": "https://api.minimaxi.com/anthropic",
            "FIN_MODEL": "MiniMax-M3",
            "ANTHROPIC_API_KEY": "sk-test",
        }
    )
    assert m.model == "MiniMax-M3"


def test_default_model_applied_when_fin_model_unset() -> None:
    """Both paths fall back to ``DEFAULT_MODEL`` (``fin/MiniMax-M3``)
    when ``FIN_MODEL`` is unset — same default as before the
    router existed."""
    m = resolve_chat_model(
        {
            "FIN_BASE_URL": "https://api.minimaxi.com/v1",
            "ANTHROPIC_AUTH_TOKEN": "sk-test",
        }
    )
    assert m.model_name == DEFAULT_MODEL


# ----- public surface -----


def test_provider_constants_exported() -> None:
    """The two provider identifiers are public so other modules
    (docs, eval, dashboards) can refer to them without string drift."""
    assert PROVIDER_OPENAI == "openai"
    assert PROVIDER_ANTHROPIC == "anthropic"


def test_model_router_error_is_exception() -> None:
    """``ModelRouterError`` is a regular Exception (not a
    ``BaseException``), so callers can catch it alongside other
    configuration errors."""
    assert issubclass(ModelRouterError, Exception)


def test_default_base_url_unchanged_for_backward_compat() -> None:
    """``DEFAULT_BASE_URL`` is the legacy local-proxy URL; the
    router must not silently change it or pre-router configs that
    relied on the default would now fail on the wrong URL."""
    assert DEFAULT_BASE_URL == "http://localhost:20128/v1"
