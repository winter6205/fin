"""TypeSafe Jev (System One) credentials for host judges.

This is not the agent-loop LLM (``FIN_API_KEY`` / MiniMax). Soft-Track
and 档 assignment consume a closed-set Choice + confidence; retrieve
ranking stays in iknow-memory.

A missing key returns ``None`` and does not fail-start. Pytest strips
these vars so default ``uv run pytest -q`` stays Fake. Product
``fin-serve`` loads them via ``apply_operator_settings`` once the
operator sets ``jev.apiKey`` in ``~/.fin/settings.yaml``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

FIN_JEV_API_KEY_ENV = "FIN_JEV_API_KEY"
TYPESAFE_API_KEY_ENV = "TYPESAFE_API_KEY"
FIN_JEV_MODEL_ENV = "FIN_JEV_MODEL"
TYPESAFE_DEFAULT_MODEL_ENV = "TYPESAFE_DEFAULT_MODEL"
FIN_JEV_BASE_URL_ENV = "FIN_JEV_BASE_URL"
TYPESAFE_BASE_URL_ENV = "TYPESAFE_BASE_URL"

DEFAULT_JEV_MODEL = "jev-latest"
DEFAULT_JEV_BASE_URL = "https://api.typesafe.ai"


@dataclass(frozen=True, slots=True)
class JevConfig:
    api_key: str
    model: str
    base_url: str


def _trimmed(environ: Mapping[str, str], name: str) -> str | None:
    raw = environ.get(name)
    if raw is None:
        return None
    value = raw.strip()
    if not value:
        return None
    return value


def load_jev_config(*, environ: Mapping[str, str] | None = None) -> JevConfig | None:
    """Return Jev credentials, or ``None`` when no TypeSafe key is set."""
    import os

    source: Mapping[str, str] = os.environ if environ is None else environ
    api_key = _trimmed(source, FIN_JEV_API_KEY_ENV) or _trimmed(
        source, TYPESAFE_API_KEY_ENV
    )
    if api_key is None:
        return None
    model = (
        _trimmed(source, FIN_JEV_MODEL_ENV)
        or _trimmed(source, TYPESAFE_DEFAULT_MODEL_ENV)
        or DEFAULT_JEV_MODEL
    )
    base_url = (
        _trimmed(source, FIN_JEV_BASE_URL_ENV)
        or _trimmed(source, TYPESAFE_BASE_URL_ENV)
        or DEFAULT_JEV_BASE_URL
    ).rstrip("/")
    return JevConfig(api_key=api_key, model=model, base_url=base_url)


__all__ = [
    "DEFAULT_JEV_BASE_URL",
    "DEFAULT_JEV_MODEL",
    "FIN_JEV_API_KEY_ENV",
    "JevConfig",
    "load_jev_config",
]
