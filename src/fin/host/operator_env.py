"""Operator process entry: load a local ``.env``, then preflight it.

``compile_for_runtime`` still reads ``os.environ`` only. Tests that
call it stay hermetic. ``fin-serve`` and ``python -m fin`` apply
this file first so a checked-in-ignored ``.env`` is part of boot,
not a hidden shell ritual.

Existing process env wins (``setdefault``): CI and explicit exports
are not overwritten. A missing file is a no-op — production can
inject vars without shipping a file.

The product boot path no longer warns from here. ``fin.host.http.serve()``
calls :func:`fin.host.knowledge_mcp.publish_knowledge_mcp`, which probes the
iknow-memory MCP with a real ``initialize`` + ``tools/list`` handshake and
publishes the three states 缺 / 坏 / 可达 into the in-process snapshot shared
by ``GET /api/status`` and the conversation-entry gate. ADR 0033 was amended
2026-09-13 (ADR 0051) accordingly.

Retained here for backward compatibility is a PATH-only diagnostic,
:func:`probe_retrieve_reachability` (with its thin logging wrapper
:func:`warn_if_mcp_retrieve_unreachable`). It is legacy, NOT product truth:
it reads only whether ``DEFAULT_MCP_COMMAND[0]`` sits on ``PATH`` and cannot
see whether the server completes the handshake. New code should read the
``fin.host.knowledge_mcp`` snapshot instead.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Mapping, MutableMapping

logger = logging.getLogger(__name__)


# The two typed reachability values (ADR 0033). "Unreachable" means
# every retrieve call this process makes will answer with the typed
# ``retrieve_unavailable`` EXIT — it is a configuration statement
# about the port, not a claim that a reachable server will succeed.
RETRIEVE_REACHABLE = "reachable"
RETRIEVE_UNREACHABLE = "unreachable"


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


def resolve_retrieve_adapter_kind(
    *, environ: Mapping[str, str] | None = None
) -> str:
    """Return the retrieve adapter kind this process is configured for.

    ``FIN_RETRIEVE_ADAPTER`` when set, otherwise
    ``DEFAULT_RETRIEVE_ADAPTER`` — the same resolution
    ``fin.host.compile`` performs before it hands the kind to
    ``compile_graph``.

    A value naming no known kind is clamped to the product default
    and logged. ``compile_graph`` raises
    ``RetrieveAdapterConfigError`` on such a value, so no serving
    process is actually on one; the clamp exists so the diagnostic
    surfaces (the boot warning and ADR 0033's status route) keep
    answering with one of the two typed kinds instead of echoing a
    typo or raising on a read-only route.
    """
    import os

    from fin.host.compile import DEFAULT_RETRIEVE_ADAPTER
    from fin.host.graph import RETRIEVE_ADAPTER_KINDS

    source: Mapping[str, str] = os.environ if environ is None else environ
    configured = (source.get("FIN_RETRIEVE_ADAPTER") or "").strip()
    kind = configured or DEFAULT_RETRIEVE_ADAPTER
    if kind not in RETRIEVE_ADAPTER_KINDS:
        logger.warning(
            "FIN_RETRIEVE_ADAPTER=%r names no known retrieve adapter kind "
            "(expected one of %s); reporting %r. compile_graph refuses this "
            "value, so this process has no compiled graph.",
            configured,
            list(RETRIEVE_ADAPTER_KINDS),
            DEFAULT_RETRIEVE_ADAPTER,
        )
        return DEFAULT_RETRIEVE_ADAPTER
    return kind


def probe_retrieve_reachability(
    *, environ: Mapping[str, str] | None = None
) -> str:
    """Return ``RETRIEVE_REACHABLE`` / ``RETRIEVE_UNREACHABLE``.

    LEGACY diagnostic, not the product boot path (ADR 0033 amended
    2026-09-13 / ADR 0051). ``fin.host.http.serve()`` now calls
    :func:`fin.host.knowledge_mcp.publish_knowledge_mcp`; this PATH-only
    helper is retained for compatibility and its own tests.

    Unreachable is exactly one situation: the product ``mcp``
    adapter is configured and the launch command ``compile_graph``
    would spawn (``DEFAULT_MCP_COMMAND``) is not on ``PATH``, so
    every retrieve call answers with the typed
    ``retrieve_unavailable`` EXIT.

    The ``fake`` port is reachable — it answers every call. What it
    lacks is a corpus, and ``resolve_retrieve_adapter_kind`` is the
    field that says so (ADR 0033).

    ``PATH`` lookup only. No process is started and no health-check
    RPC is issued: a probe spawn costs a subprocess per call and
    still cannot prove the server answers ``tools/call``. That blind
    spot is why it is no longer product truth.
    """
    import os
    import shutil

    from fin.host.compile import DEFAULT_MCP_COMMAND
    from fin.host.graph import RETRIEVE_ADAPTER_MCP

    source: Mapping[str, str] = os.environ if environ is None else environ
    if resolve_retrieve_adapter_kind(environ=source) != RETRIEVE_ADAPTER_MCP:
        return RETRIEVE_REACHABLE
    if shutil.which(DEFAULT_MCP_COMMAND[0], path=source.get("PATH")) is None:
        return RETRIEVE_UNREACHABLE
    return RETRIEVE_REACHABLE


def warn_if_mcp_retrieve_unreachable(
    *, environ: Mapping[str, str] | None = None
) -> bool:
    """Log a boot WARNING when retrieve is wired to an absent MCP server.

    LEGACY diagnostic, not the product boot path (ADR 0033 amended
    2026-09-13 / ADR 0051). The product boot warning lives in
    :func:`fin.host.knowledge_mcp.publish_knowledge_mcp`, which reflects
    the real handshake the status route and conversation gate share.

    Returns ``True`` when the warning fired. This is a diagnostic,
    NOT a gate: the operator keeps a serving process (the API, the
    SPA, and every non-research track still work), exactly like the
    missing-``web/dist`` warning in ``fin.host.http.mount_spa``.
    Failing the boot here would also make the ``fake`` adapter the
    only way to start on a laptop, which is the wrong default.

    The reachability decision itself is
    :func:`probe_retrieve_reachability` — a PATH-only prediction, with
    the blind spot documented there. It is no longer the boot path.
    """
    import os

    from fin.host.compile import DEFAULT_MCP_COMMAND

    source: Mapping[str, str] = os.environ if environ is None else environ
    adapter = resolve_retrieve_adapter_kind(environ=source)
    if probe_retrieve_reachability(environ=source) == RETRIEVE_REACHABLE:
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
    "RETRIEVE_REACHABLE",
    "RETRIEVE_UNREACHABLE",
    "apply_dotenv",
    "find_env_file",
    "parse_env_file",
    "probe_retrieve_reachability",
    "resolve_retrieve_adapter_kind",
    "warn_if_mcp_retrieve_unreachable",
]
