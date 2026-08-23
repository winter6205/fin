"""Load a local ``.env`` at operator process entry.

``compile_for_runtime`` still reads ``os.environ`` only. Tests that
call it stay hermetic. ``fin-serve`` and ``python -m fin`` apply
this file first so a checked-in-ignored ``.env`` is part of boot,
not a hidden shell ritual.

Existing process env wins (``setdefault``): CI and explicit exports
are not overwritten. A missing file is a no-op — production can
inject vars without shipping a file.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import MutableMapping

logger = logging.getLogger(__name__)


def parse_env_file(text: str) -> dict[str, str]:
    """Parse KEY=VALUE lines. Comments, blanks, and malformed lines drop."""
    pairs: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        pairs[key] = value
    return pairs


def find_env_file(*, start: Path | None = None) -> Path | None:
    """Walk from ``start`` (cwd by default) up to the project root.

    The closest ``.env`` wins. Walking stops at the first directory
    that contains ``pyproject.toml`` so a stray ``~/.env`` is never
    loaded because the operator launched from a nested cwd.
    """
    current = (start or Path.cwd()).resolve()
    while True:
        candidate = current / ".env"
        if candidate.is_file():
            return candidate
        if (current / "pyproject.toml").is_file():
            return None
        parent = current.parent
        if parent == current:
            return None
        current = parent


def apply_dotenv(
    *,
    environ: MutableMapping[str, str] | None = None,
    start: Path | None = None,
) -> Path | None:
    """Fill unset keys from the discovered ``.env``. Returns the path loaded."""
    import os

    target: MutableMapping[str, str]
    if environ is None:
        target = os.environ
    else:
        target = environ
    path = find_env_file(start=start)
    if path is None:
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        logger.warning("operator .env exists but could not be read: %s", path)
        return None
    for key, value in parse_env_file(text).items():
        target.setdefault(key, value)
    return path


__all__ = ["apply_dotenv", "find_env_file", "parse_env_file"]
