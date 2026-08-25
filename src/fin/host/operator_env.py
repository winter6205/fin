"""Operator process entry: load a local ``.env``, then preflight it.

``compile_for_runtime`` still reads ``os.environ`` only. Tests that
call it stay hermetic. ``fin-serve`` and ``python -m fin`` apply
this file first so a checked-in-ignored ``.env`` is part of boot,
not a hidden shell ritual.

Existing process env wins (``setdefault``): CI and explicit exports
are not overwritten. A missing file is a no-op — production can
inject vars without shipping a file.

Once the env is assembled, :func:`warn_if_mcp_retrieve_unreachable`
checks the one setting whose default silently disables research:
``FIN_RETRIEVE_ADAPTER`` defaults to ``mcp``, so a machine without
the ``iknow-memory`` server serves a UI whose every retrieve call
answers with the typed ``retrieve_unavailable`` EXIT.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Mapping, MutableMapping

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


def warn_if_mcp_retrieve_unreachable(
    *, environ: Mapping[str, str] | None = None
) -> bool:
    """Log a boot WARNING when retrieve is wired to an absent MCP server.

    Returns ``True`` when the warning fired. This is a diagnostic,
    NOT a gate: the operator keeps a serving process (the API, the
    SPA, and every non-research track still work), exactly like the
    missing-``web/dist`` warning in ``fin.host.http.mount_spa``.
    Failing the boot here would also make the ``fake`` adapter the
    only way to start on a laptop, which is the wrong default.

    Reachability is decided by ``PATH`` lookup of the launch
    command ``compile_graph`` would spawn
    (``DEFAULT_MCP_COMMAND``). No process is started: a probe
    spawn on every boot costs a subprocess and still cannot prove
    the server answers ``tools/call``.
    """
    import os
    import shutil

    from fin.host.compile import DEFAULT_MCP_COMMAND, DEFAULT_RETRIEVE_ADAPTER

    source: Mapping[str, str] = os.environ if environ is None else environ
    configured = (source.get("FIN_RETRIEVE_ADAPTER") or "").strip()
    adapter = configured or DEFAULT_RETRIEVE_ADAPTER
    if adapter != "mcp":
        return False

    program = DEFAULT_MCP_COMMAND[0]
    if shutil.which(program, path=source.get("PATH")) is not None:
        return False

    logger.warning(
        "FIN_RETRIEVE_ADAPTER=%s but the MCP server command `%s` is not on "
        "PATH; retrieve calls will return the typed retrieve_unavailable "
        "EXIT and answers will carry no evidence. Install the iknow-memory "
        "server, or set FIN_RETRIEVE_ADAPTER=fake to boot without a corpus.",
        adapter,
        " ".join(DEFAULT_MCP_COMMAND),
    )
    return True


__all__ = [
    "apply_dotenv",
    "find_env_file",
    "parse_env_file",
    "warn_if_mcp_retrieve_unreachable",
]
