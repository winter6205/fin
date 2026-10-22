"""TypeSafe Jev operator config (host judge; not the agent-loop LLM).

Pytest stays Fake / unset. Product boot reads these vars after
``apply_operator_settings``; a missing key does not fail-start until the judge
adapter is the production default.
"""

from __future__ import annotations

from fin.host.question.jev_config import (
    DEFAULT_JEV_BASE_URL,
    DEFAULT_JEV_MODEL,
    FIN_JEV_API_KEY_ENV,
    load_jev_config,
)


def test_missing_key_returns_none() -> None:
    assert load_jev_config(environ={}) is None


def test_blank_key_returns_none() -> None:
    assert load_jev_config(environ={FIN_JEV_API_KEY_ENV: "  "}) is None


def test_namespaced_key_uses_official_defaults() -> None:
    cfg = load_jev_config(
        environ={FIN_JEV_API_KEY_ENV: "ts-test-key-not-a-real-secret"}
    )
    assert cfg is not None
    assert cfg.api_key == "ts-test-key-not-a-real-secret"
    assert cfg.model == DEFAULT_JEV_MODEL
    assert cfg.base_url == DEFAULT_JEV_BASE_URL


def test_falls_back_to_typesafe_api_key() -> None:
    cfg = load_jev_config(environ={"TYPESAFE_API_KEY": "vendor-key-not-a-real-secret"})
    assert cfg is not None
    assert cfg.api_key == "vendor-key-not-a-real-secret"
    assert cfg.model == DEFAULT_JEV_MODEL


def test_prefers_fin_jev_key_over_typesafe() -> None:
    cfg = load_jev_config(
        environ={
            FIN_JEV_API_KEY_ENV: "namespaced-jev-key",
            "TYPESAFE_API_KEY": "vendor-jev-key",
        }
    )
    assert cfg is not None
    assert cfg.api_key == "namespaced-jev-key"


def test_honors_model_and_strips_trailing_slash_on_base_url() -> None:
    cfg = load_jev_config(
        environ={
            FIN_JEV_API_KEY_ENV: "k",
            "FIN_JEV_MODEL": "jev-1.13.0",
            "FIN_JEV_BASE_URL": "https://api.typesafe.ai/",
        }
    )
    assert cfg is not None
    assert cfg.model == "jev-1.13.0"
    assert cfg.base_url == "https://api.typesafe.ai"
