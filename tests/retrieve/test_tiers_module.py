"""档 SSOT 常量住在中立模块 ``fin.retrieve.tiers`` (review F3).

The cycle being broken (Medium 分层反转): ``fin.host.graph`` imports
``fin.retrieve.port``, so a top-level ``from fin.host.named_failures``
inside the port would be a hard ImportError at boot — until this change
the port hid that with a function-level lazy import. The 档 closed set
and its routing-time codes are retrieve-side facts, so they now live in
``fin.retrieve.tiers`` (leaf: stdlib imports only) and
``fin.host.named_failures`` RE-EXPORTS them: every existing
``from fin.host.named_failures import RETRIEVE_TIERS, ...`` consumer
keeps working unchanged.

The tests below pin the three properties the refactor must hold:

1. the neutral module exists and carries the values (fails before the
   move — no such module yet);
2. the re-export bindings are the SAME objects (no copy-paste drift);
3. importing / calling through the port never pulls ``fin.host``
   (fails before the fix: the lazy import boots the host package).
"""

from __future__ import annotations

import subprocess
import sys


def _run_python(statement: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", statement],
        capture_output=True,
        text=True,
        timeout=120,
    )


# ----- 1. the neutral module carries the SSOT -----


def test_tiers_module_exists_with_the_closed_set() -> None:
    from fin.retrieve.tiers import (
        REAL_DB_NOT_WIRED,
        RETRIEVE_TIERS,
        TIER_NOT_OPEN,
        TIER_UNAVAILABLE_CODE,
    )

    assert RETRIEVE_TIERS == ("A", "B", "C", "D")
    assert TIER_NOT_OPEN == "tier_not_open"
    assert REAL_DB_NOT_WIRED == "real_db_not_wired"
    assert TIER_UNAVAILABLE_CODE == {"C": "tier_not_open", "D": "real_db_not_wired"}


def test_tiers_module_is_public_via_dunder_all() -> None:
    import fin.retrieve.tiers as tiers

    assert set(tiers.__all__) == {
        "REAL_DB_NOT_WIRED",
        "RETRIEVE_TIERS",
        "RetrieveTier",
        "TIER_NOT_OPEN",
        "TIER_UNAVAILABLE_CODE",
    }


# ----- 2. named_failures re-exports the SAME objects -----


def test_named_failures_reexports_are_identical_objects() -> None:
    import fin.host.named_failures as named_failures
    import fin.retrieve.tiers as tiers

    for name in tiers.__all__:
        assert getattr(named_failures, name) is getattr(tiers, name), name
    # The re-exported names stay in named_failures' public surface, so
    # ``from fin.host.named_failures import RetrieveTier`` and the
    # ``__all__`` drift guards keep holding.
    assert set(tiers.__all__) <= set(named_failures.__all__)


def test_retrieve_tier_literal_is_derived_from_the_moved_tuple() -> None:
    from typing import get_args

    from fin.retrieve.tiers import RETRIEVE_TIERS, RetrieveTier

    assert get_args(RetrieveTier) == RETRIEVE_TIERS


# ----- 3. the port no longer reaches into fin.host -----


def test_port_module_imports_standalone_without_host() -> None:
    """Fresh-interpreter proof: importing the port alone must not boot
    the host package (it cannot, without a cycle — but the point is the
    port no longer depends on host at all)."""
    r = _run_python(
        "import sys; import fin.retrieve.port; "
        "assert not [m for m in sys.modules if m == 'fin.host' "
        "or m.startswith('fin.host.')], "
        "sorted(m for m in sys.modules if m.startswith('fin.'))"
    )
    assert r.returncode == 0, r.stderr


def test_resolve_search_mode_calls_without_host_import() -> None:
    """The lazy import this refactor removes made every first call boot
    ``fin.host`` (and the graph through it). The pure validation path
    must answer from the neutral module only."""
    r = _run_python(
        """
import sys
import fin.retrieve.port as p

assert p.resolve_search_mode("A") == "A"
assert p.resolve_search_mode(None) is None
try:
    p.resolve_search_mode("C")
except p.TierUnavailableError as e:
    assert e.code == "tier_not_open"
else:
    raise AssertionError("C not refused")
try:
    p.resolve_search_mode("Z")
except p.InvalidSearchModeError as e:
    assert e.code == "invalid_search_mode"
else:
    raise AssertionError("Z accepted")
host = [m for m in sys.modules if m == "fin.host" or m.startswith("fin.host.")]
assert not host, sorted(host)
"""
    )
    assert r.returncode == 0, r.stderr


def test_named_failures_imports_before_port_and_after() -> None:
    """Both import orders boot cleanly — the re-export direction is
    host → retrieve only."""
    forward = _run_python(
        "import fin.host.named_failures, fin.retrieve.port"
    )
    assert forward.returncode == 0, forward.stderr
    reverse = _run_python(
        "import fin.retrieve.port, fin.host.named_failures"
    )
    assert reverse.returncode == 0, reverse.stderr
