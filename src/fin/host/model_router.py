"""Route model construction by env-supplied base_url.

Pure dispatcher between two LangChain chat model classes:

* ``ChatOpenAI`` — OpenAI-compatible endpoints (default; covers 按量
  付费 API keys and the legacy local proxy at ``localhost:20128``).
* ``ChatAnthropic`` — Anthropic-compatible endpoints (Token Plan
  订阅 keys only authorize this route on MiniMax; see ADR 0033).

Dispatch rule (auto mode, the default):

* ``FIN_BASE_URL`` ending in ``/anthropic`` → ``ChatAnthropic``.
* Otherwise → ``ChatOpenAI``.

Explicit ``FIN_MODEL_PROVIDER=anthropic|openai|auto`` overrides the
URL-shape detection. ``auto`` (and unset / whitespace) defers to
URL-shape.

Why a dedicated module
----------------------
``compile_graph`` and every test fake into ``ScriptedChatModel``.
Centralising the production wiring keeps the chat-model contract
single-sourced; tests freeze ``resolve_chat_model`` instead of
patching two branches scattered across ``compile.py``.

Failure modes
-------------
:class:`ModelRouterError` covers configuration mistakes:

* ``FIN_MODEL_PROVIDER`` set to an unknown value (typo). Silent
  fallback to ``ChatOpenAI`` would surprise operators whose intent
  was Anthropic.
* ``ChatAnthropic`` path selected while ``ANTHROPIC_API_KEY`` is
  unset (the OpenAI path's ``sk-no-key-required`` placeholder is
  intentionally absent here; the proxy keys do not translate).

Unknown URL shapes are NOT errors: the dispatcher defaults to
``ChatOpenAI``, which is correct for legacy and OpenAI URLs
(``http://localhost:20128/v1``, ``https://api.openai.com/v1``,
``https://api.minimaxi.com/v1``).
"""

from __future__ import annotations

import logging
from typing import Mapping

from langchain_core.language_models.chat_models import BaseChatModel

logger = logging.getLogger(__name__)


# ----- public provider identifiers -----


PROVIDER_OPENAI = "openai"
PROVIDER_ANTHROPIC = "anthropic"
_KNOWN_PROVIDERS: tuple[str, ...] = (PROVIDER_OPENAI, PROVIDER_ANTHROPIC)


# ----- typed error -----


class ModelRouterError(Exception):
    """The model router cannot satisfy a dispatch request.

    Distinct from ``HostCompileError`` because the failure happens
    BEFORE the saver or the graph is built: bad dispatch is a
    configuration mistake, not a runtime invariant. Raising here
    keeps the boot sequence fail-fast.
    """


# ----- helpers -----


def _read_env(env: Mapping[str, str] | None, name: str) -> str | None:
    """Return ``env[name]`` (or ``os.environ[name]``) if non-empty.

    Whitespace-only strings count as absent so a malformed env does
    not slip past as a real value. Mirrors ``compile._read_env`` to
    keep both modules consistent on what "set" means; kept local
    rather than imported to avoid a top-level cycle between
    ``model_router`` and ``compile``.
    """
    import os

    src = env if env is not None else os.environ
    value = src.get(name)
    if value is None or value.strip() == "":
        return None
    return value


def _is_anthropic_compat(base_url: str | None) -> bool:
    """True when ``base_url`` targets an Anthropic-compatible endpoint.

    Matches the URL suffix ``/anthropic`` (with or without trailing
    slash, case-insensitive). The MiniMax Anthropic-compatible
    endpoints (``https://api.minimaxi.com/anthropic`` and the
    overseas variant ``https://api.minimax.io/anthropic``) both end
    this way.
    """
    if not base_url:
        return False
    return base_url.rstrip("/").lower().endswith("/anthropic")


def _resolve_explicit_provider(env: Mapping[str, str]) -> str | None:
    """Return the explicit provider override, or None for ``auto``.

    Raises :class:`ModelRouterError` on a typo. ``auto`` (and unset
    / whitespace) defers to URL-shape detection.
    """
    raw = _read_env(env, "FIN_MODEL_PROVIDER")
    if raw is None:
        return None
    normalized = raw.strip().lower()
    if normalized == "auto":
        return None
    if normalized not in _KNOWN_PROVIDERS:
        raise ModelRouterError(
            f"FIN_MODEL_PROVIDER={raw!r} is not a known provider; "
            f"expected one of {', '.join(_KNOWN_PROVIDERS)} or 'auto'"
        )
    return normalized


# ----- builders -----


def _build_openai(env: Mapping[str, str]) -> BaseChatModel:
    """Build the OpenAI-compatible chat model (default path).

    Reads ``FIN_MODEL``, ``FIN_BASE_URL``, ``ANTHROPIC_AUTH_TOKEN``
    (last one preserved as a backward-compat alias for pre-router
    configs; the proxy at ``localhost:20128`` accepted it as the
    OpenAI-shaped bearer).
    """
    from langchain_openai import ChatOpenAI

    model_name = _read_env(env, "FIN_MODEL") or "fin/MiniMax-M3"
    base_url = _read_env(env, "FIN_BASE_URL") or "http://localhost:20128/v1"
    api_key = _read_env(env, "ANTHROPIC_AUTH_TOKEN") or "sk-no-key-required"
    return ChatOpenAI(
        model=model_name,
        base_url=base_url,
        api_key=api_key,
        # ``extra_body`` carries the MiniMax-M3 thinking + split
        # channel (ADR 0022 + ADR 0026). OpenAI-shaped proxies pass
        # it through; the OpenAI-hosted API ignores unknown extras.
        extra_body={
            "thinking": {"type": "adaptive"},
            "reasoning_split": True,
        },
        timeout=120.0,
        max_retries=1,
    )


def _build_anthropic(env: Mapping[str, str]) -> BaseChatModel:
    """Build the Anthropic-compatible chat model (订阅 key path).

    Reads ``FIN_MODEL``, ``FIN_BASE_URL`` (required), and
    ``ANTHROPIC_API_KEY`` (required). The base URL must include the
    ``/anthropic`` suffix; the router asserts that upstream by
    dispatching only on URLs matching :func:`_is_anthropic_compat`.

    No ``extra_body`` is passed: ChatAnthropic routes thinking
    through ``betas`` rather than OpenAI-shaped extras, and the
    MiniMax Anthropic-compatible endpoint exposes the same
    reasoning payload under the same field names LangChain
    normalises (``reasoning_content`` etc.). A blank thinking
    region is acceptable degradation if a future proxy version
    drops the field.
    """
    from langchain_anthropic import ChatAnthropic

    model_name = _read_env(env, "FIN_MODEL") or "fin/MiniMax-M3"
    base_url = _read_env(env, "FIN_BASE_URL")
    if not base_url:
        raise ModelRouterError(
            "FIN_BASE_URL is required when the Anthropic-compatible "
            "path is selected (set it in the environment or in .env)."
        )
    api_key = _read_env(env, "ANTHROPIC_API_KEY")
    if not api_key:
        raise ModelRouterError(
            "ANTHROPIC_API_KEY is required when the Anthropic-compatible "
            "path is selected (Token Plan 订阅 keys do not auto-route "
            "through ANTHROPIC_AUTH_TOKEN)."
        )
    # ``ChatAnthropic`` exposes its base URL as ``anthropic_api_url``
    # with an ``alias="base_url"`` (verified in langchain-anthropic
    # 1.6.x). The keyword spelling is uniform across providers.
    return ChatAnthropic(
        model=model_name,
        api_key=api_key,
        base_url=base_url,
    )


# ----- public entry point -----


def resolve_chat_model(env: Mapping[str, str] | None = None) -> BaseChatModel:
    """Build the chat model the runtime will use.

    Dispatches on ``FIN_MODEL_PROVIDER`` (explicit) or ``FIN_BASE_URL``
    (auto) to :func:`_build_openai` or :func:`_build_anthropic`. The
    result is a LangChain ``BaseChatModel`` ready for ``bind_tools``.

    Reads the env keys ``FIN_MODEL``, ``FIN_BASE_URL``,
    ``FIN_MODEL_PROVIDER``, ``ANTHROPIC_API_KEY``, and
    ``ANTHROPIC_AUTH_TOKEN`` (the last only on the OpenAI path for
    backward compatibility with pre-router configs).
    """
    src: Mapping[str, str] = env if env is not None else __import__("os").environ
    base_url = _read_env(src, "FIN_BASE_URL")
    explicit = _resolve_explicit_provider(src)
    if explicit is None:
        provider = (
            PROVIDER_ANTHROPIC if _is_anthropic_compat(base_url) else PROVIDER_OPENAI
        )
    else:
        provider = explicit
    if provider == PROVIDER_ANTHROPIC:
        return _build_anthropic(src)
    return _build_openai(src)


__all__ = [
    "ModelRouterError",
    "PROVIDER_ANTHROPIC",
    "PROVIDER_OPENAI",
    "resolve_chat_model",
]
