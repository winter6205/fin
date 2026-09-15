"""Opt-in live retrieve probe (plan `first-closed-loop` T1).

Prints, for a query, the two numbers the closed loop turns on:

* ``n_chunks`` — how many chunks the **retrieve port** returned, and
* ``mapping_failures`` — how many of the received hits THIS CALL's
  mapper dropped: the ledger's delta around the retrieve, never the
  process-wide total and never inferred from an empty ``chunks``.

The shape this exists to make observable: on an empty **资料集合** the
envelope is ``chunks: []`` with ``mapping_failures=0`` (the 2026-09-16
probe), whereas a drop shows ``0`` chunks *and* a non-zero delta. Read
as one line each so a shell can grep the integers without parsing the
whole envelope. The count is a delta because the ledger is
process-wide: ``FIN_MAPPING_FAILURE_LOG` replays an earlier process's
drops, and printing those beside ``n_chunks=0`` would be the very
collapse the probe exists to prevent.

Same adapter the product uses (``MCPRetrieveAdapter`` over the real
stdio server), so the probe cannot drift from the product seam. This
command is **never** reached by ``uv run pytest``: the default suite
stays on the Fake port.

Usage::

    uv run python scripts/retrieve_probe.py "operating revenue"
    uv run python scripts/retrieve_probe.py "operating revenue" --limit 5
    uv run python scripts/retrieve_probe.py "revenue" --command node \\
        --arg ../iknow-memory/dist/index.js

Exit codes: ``0`` the probe ran (whatever the counts); ``2`` the probe
could not run (no launch command resolved — the binary is not on
``PATH``); ``1`` the retrieve port answered with a typed failure
envelope (the counts are still printed).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT / "src"))

from fin.retrieve.hit_mapping import ledger_from_env  # noqa: E402
from fin.retrieve.mcp_adapter import MCPRetrieveAdapter  # noqa: E402
from fin.retrieve.mcp_session import MCPStdioSession  # noqa: E402


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Probe the live retrieve port for chunk count and "
        "mapping-ledger count.",
    )
    parser.add_argument("query", help="the query text to send")
    parser.add_argument(
        "--limit", type=int, default=8, help="max chunks to request"
    )
    parser.add_argument(
        "--command",
        default=None,
        help="MCP server launch command (default: resolve like the "
        "product — $FIN_KNOWLEDGE_MCP_COMMAND, else iknow-memory)",
    )
    parser.add_argument(
        "--arg",
        action="append",
        default=[],
        dest="args",
        help="extra argv for the MCP server; repeatable",
    )
    parser.add_argument(
        "--timeout", type=float, default=20.0, help="per-call timeout (s)"
    )
    return parser.parse_args(argv)


def _resolve_command(ns: argparse.Namespace) -> list[str] | None:
    """The launch argv: explicit flag, then env, then the product default.

    Returns ``None`` — the EXIT 2 condition — when the resolved binary
    is not on ``PATH``. Resolving an unlaunchable argv would surface as
    the port's own ``retrieve_unavailable`` (exit 1), which reads as
    "the server answered badly" when the truth is "the probe never
    found a server to ask".
    """
    if ns.command:
        candidate = [ns.command, *ns.args]
    else:
        env_command = os.environ.get("FIN_KNOWLEDGE_MCP_COMMAND")
        candidate = [env_command or "iknow-memory", *ns.args]
    if shutil.which(candidate[0]) is None:
        return None
    return candidate


def run_probe(
    adapter: MCPRetrieveAdapter,
    query: str,
    *,
    limit: int,
    out: Any,
) -> int:
    """Run one retrieve and print the two greppable integers to ``out``.

    ``mapping_failures`` is the ledger DELTA this call produced, never
    the process-wide total: ``FIN_MAPPING_FAILURE_LOG`` may replay an
    earlier process's drops, and printing those beside ``n_chunks=0``
    for a healthy empty collection would be exactly the collapse this
    probe exists to prevent. The full envelope rides stderr.

    Returns the documented exit code: ``1`` when the port answered with
    a typed failure envelope (counts still printed), ``0`` otherwise.
    """
    before = adapter.mapping_failures.count()
    envelope = adapter.retrieve(query, limit=limit)
    chunks = envelope.get("chunks") or []
    failures = adapter.mapping_failures.count()
    print(f"n_chunks={len(chunks)}", file=out)
    print(f"mapping_failures={max(failures - before, 0)}", file=out)
    print(json.dumps(envelope, ensure_ascii=False, indent=2), file=sys.stderr)
    if envelope.get("error"):
        print(f"error={envelope['error']}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    ns = _parse_args(argv)
    command = _resolve_command(ns)
    # EXIT: 2 — the probe could not run (no resolvable launch command).
    if not command:
        print("probe could not resolve a launch command", file=sys.stderr)
        return 2

    session = MCPStdioSession(command, timeout_seconds=ns.timeout)
    adapter = MCPRetrieveAdapter(
        command=command,
        session=session,
        timeout_seconds=ns.timeout,
        mapping_ledger=ledger_from_env(),
    )
    try:
        return run_probe(adapter, ns.query, limit=ns.limit, out=sys.stdout)
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
