"""Read maintainer-held JSON fixtures from an external content-addressed vault."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any

MAX_FIXTURE_BYTES = 10 * 1024 * 1024
_FIXTURE_ROOT_ENV = "FIN_PRIVATE_FIXTURES"
_FIXTURE_KEY_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


class PrivateFixtureMissingError(RuntimeError):
    """The external vault is not configured or the fixture cannot be read."""


class PrivateFixtureInvalidError(RuntimeError):
    """The fixture key or stored fixture contents are invalid."""


def _read_fixture_bytes(path: Path) -> bytes:
    try:
        with path.open("rb") as fixture_file:
            return fixture_file.read(MAX_FIXTURE_BYTES + 1)
    except OSError:
        raise PrivateFixtureMissingError("private fixture is unavailable") from None


def load_private_fixture(key: str) -> Any:
    """Load JSON bytes whose SHA-256 digest is the supplied lowercase key."""
    if not isinstance(key, str) or _FIXTURE_KEY_PATTERN.fullmatch(key) is None:
        raise PrivateFixtureInvalidError("private fixture key is invalid")

    root = os.environ.get(_FIXTURE_ROOT_ENV)
    if not root:
        raise PrivateFixtureMissingError("private fixture root is not configured")

    path = Path(root) / "values" / f"{key}.json"
    payload = _read_fixture_bytes(path)
    if len(payload) > MAX_FIXTURE_BYTES:
        raise PrivateFixtureInvalidError("private fixture exceeds the size limit")
    if hashlib.sha256(payload).hexdigest() != key:
        raise PrivateFixtureInvalidError(
            "private fixture digest does not match its key"
        )

    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise PrivateFixtureInvalidError(
            "private fixture is not valid UTF-8 JSON"
        ) from None


__all__ = [
    "MAX_FIXTURE_BYTES",
    "PrivateFixtureInvalidError",
    "PrivateFixtureMissingError",
    "load_private_fixture",
]
