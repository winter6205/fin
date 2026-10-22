"""Retrieve-adapter diagnostics for the operator process.

``compile_for_runtime`` still reads ``os.environ`` only. Tests that
call it stay hermetic. ``fin-serve`` and ``python -m fin`` project
``~/.fin/settings.yaml`` first
(:func:`fin.host.operator_settings.apply_operator_settings`), so
operator settings are part of boot. There is no dotenv fill step.

Existing process env wins (``setdefault``): CI and explicit exports
are not overwritten. A missing settings file is a no-op — production
can inject vars without shipping a file.

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
import os
from typing import Mapping

logger = logging.getLogger(__name__)

#: The environment key naming the trusted corpus scope a public corpus upload
#: derives document identity from. It is STABLE configuration, not a
#: per-request value: same bytes in the same scope reuse an identity, so
#: a value that changed between processes would strand every previously
#: accepted document behind a new one.
CORPUS_SCOPE_ID_ENV = "FIN_CORPUS_SCOPE_ID"

#: The scope used when nothing is configured. A constant, not a random
#: or host-derived value, so an unconfigured deployment is still
#: deterministic across restarts and machines.
DEFAULT_CORPUS_SCOPE_ID = "fin-default"


# The two typed reachability values (ADR 0033). "Unreachable" means
# every retrieve call this process makes will answer with the typed
# ``retrieve_unavailable`` EXIT — it is a configuration statement
# about the port, not a claim that a reachable server will succeed.
RETRIEVE_REACHABLE = "reachable"
RETRIEVE_UNREACHABLE = "unreachable"


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


def resolve_corpus_scope_id(
    *, environ: Mapping[str, str] | None = None
) -> str:
    """The trusted corpus scope a public corpus upload derives identity from.

    ``FIN_CORPUS_SCOPE_ID`` when set, otherwise
    :data:`DEFAULT_CORPUS_SCOPE_ID`. Read from host configuration only —
    never from a filename, metadata field, filter or model argument —
    because the scope is the one input that makes the same bytes land on
    the same document id across retries and processes.

    A blank value is treated as unset so a lower layer or the default can
    win, matching how every other ``FIN_*`` reader treats whitespace. An
    injected scope is always a non-empty string; the endpoint refuses a
    blank one rather than minting an unscoped identity.
    """
    source: Mapping[str, str] = os.environ if environ is None else environ
    configured = (source.get(CORPUS_SCOPE_ID_ENV) or "").strip()
    return configured or DEFAULT_CORPUS_SCOPE_ID


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
    "CORPUS_SCOPE_ID_ENV",
    "DEFAULT_CORPUS_SCOPE_ID",
    "RETRIEVE_REACHABLE",
    "RETRIEVE_UNREACHABLE",
    "probe_retrieve_reachability",
    "resolve_corpus_scope_id",
    "resolve_retrieve_adapter_kind",
    "warn_if_mcp_retrieve_unreachable",
]
