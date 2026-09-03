"""Internal append-only JSONL primitive for retrieve ledgers.

Domain ledgers keep their own record types and named error codes.
This module only writes one object per line, replays those lines, and
wraps I/O / decode failures in the error type the caller supplies.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TypeVar

T = TypeVar("T")


def append_jsonl_object(
    path: Path | None,
    payload: Mapping[str, object],
    *,
    unwritable: type[BaseException],
    message: str,
    ensure_ascii: bool = True,
) -> None:
    """Append one JSON object as a line. ``path is None`` is a no-op."""
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(payload, sort_keys=True, ensure_ascii=ensure_ascii)
                + "\n"
            )
    except OSError as exc:
        raise unwritable(f"{message}: {type(exc).__name__}: {exc}") from exc


def replay_jsonl_objects(
    path: Path,
    parse: Callable[[object, int], T],
    *,
    on_read_oserror: Callable[[OSError], BaseException] | None = None,
    on_unicode: Callable[[UnicodeDecodeError], BaseException] | None = None,
    on_json: Callable[[json.JSONDecodeError, int], BaseException] | None = None,
) -> list[T]:
    """Replay a JSONL file, skipping blank lines.

    Read / UTF-8 / JSON failures are wrapped only when the corresponding
    callback is provided, so callers that historically let ``OSError``
    escape (invalidation) keep that behavior.
    """
    try:
        contents = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        if on_unicode is None:
            raise
        raise on_unicode(exc) from exc
    except OSError as exc:
        if on_read_oserror is None:
            raise
        raise on_read_oserror(exc) from exc

    rows: list[T] = []
    for number, line in enumerate(contents.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            if on_json is None:
                raise
            raise on_json(exc, number) from exc
        rows.append(parse(event, number))
    return rows
