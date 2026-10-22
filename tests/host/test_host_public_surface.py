"""The host package's public surface is frozen (``fin.host.__all__``).

The P0 capability split rewrote ``src/fin/host/__init__.py``'s import
block, so a name could be dropped or renamed with nothing noticing. The
frozen record commits to 27 names staying importable from ``fin.host``:
these tests pin the exact set and each name's importability.
"""

from __future__ import annotations

import fin.host


def test_host_public_surface_is_the_frozen_name_set() -> None:
    # The frozen 27 names: a drop or an addition must fail here.
    assert set(fin.host.__all__) == {
        "AgentState",
        "BounceResult",
        "Citation",
        "CitationGateError",
        "ConcurrentRunError",
        "DoneResult",
        "EVIDENCE_RESET",
        "EvidenceItem",
        "EvidenceReset",
        "GateResult",
        "MalformedEvidenceError",
        "OccupancyCompactor",
        "RECURSION_LIMIT",
        "REPAIR_CAP",
        "RefuseResult",
        "SESSION_TOKEN_BUDGET",
        "STEP_CAP",
        "ScriptedChatModel",
        "SessionSummaryError",
        "Status",
        "TERMINAL_STATUSES",
        "WorkspaceRootMissingError",
        "compile_graph",
        "count_tokens_approximately",
        "list_files",
        "session_budget_tokens",
        "verify_citations",
    }


def test_every_public_name_is_importable_from_fin_host() -> None:
    # ``__all__`` promises importability: a name listed but not bound is
    # a broken export even when the set matches.
    for name in fin.host.__all__:
        assert getattr(fin.host, name, None) is not None, name
