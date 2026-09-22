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

    assert RETRIEVE_TIERS == ("A", "B", "C", "D", "graph", "global")
    assert TIER_NOT_OPEN == "tier_not_open"
    assert REAL_DB_NOT_WIRED == "real_db_not_wired"
    assert TIER_UNAVAILABLE_CODE == {
        "C": "tier_not_open",
        "global": "tier_not_open",
        "D": "real_db_not_wired",
    }


# ----- 1b. the lasting tokens sit BESIDE the letters this increment -----


def test_the_former_letters_are_still_members() -> None:
    """The rename ships in two increments: normalize (the next bullet)
    has to retire ``B``/``C``, so removing them here would break the
    surfaces that still produce them."""
    from fin.retrieve.tiers import RETRIEVE_TIERS

    for letter in ("B", "C"):
        assert letter in RETRIEVE_TIERS


def test_graph_is_an_open_tier_and_global_is_not() -> None:
    """``graph`` 知识问答 is today's default 档, so it must carry no
    「未开」code; ``global`` 综述 is still a plan slot, so naming it is
    档未开 — never an empty success."""
    from fin.retrieve.port import TierUnavailableError, resolve_search_mode
    from fin.retrieve.tiers import TIER_UNAVAILABLE_CODE

    assert "graph" not in TIER_UNAVAILABLE_CODE
    assert resolve_search_mode("graph") == "graph"

    try:
        resolve_search_mode("global")
    except TierUnavailableError as exc:
        assert exc.code == "tier_not_open"
    else:
        raise AssertionError("global accepted as an open 档")


def test_the_letters_route_exactly_as_before() -> None:
    """A superset must not move an existing exit: ``B`` stays open and
    ``C``/``D`` keep their own「未开」codes."""
    from fin.retrieve.port import TierUnavailableError, resolve_search_mode

    assert resolve_search_mode("A") == "A"
    assert resolve_search_mode("B") == "B"
    assert resolve_search_mode(None) is None

    for token, code in (
        ("C", "tier_not_open"),
        ("D", "real_db_not_wired"),
    ):
        try:
            resolve_search_mode(token)
        except TierUnavailableError as exc:
            assert exc.code == code, token
        else:
            raise AssertionError(f"{token} accepted as an open 档")


def test_an_unknown_token_stays_unknown_after_the_rename() -> None:
    """Negative class: adding ``graph``/``global`` must not turn every
    non-token into a 档. An unknown token — including the upper-cased
    spelling of a lasting token — is ``invalid_search_mode``, not a
    silent map onto ``graph`` or ``global``."""
    from fin.retrieve.port import InvalidSearchModeError, resolve_search_mode

    for token in ("local", "", "hybrid", "GRAPH", "Global", "b"):
        try:
            resolve_search_mode(token)
        except InvalidSearchModeError as exc:
            assert exc.code == "invalid_search_mode"
        else:
            raise AssertionError(f"{token!r} accepted by the closed set")


def test_tiers_module_is_public_via_dunder_all() -> None:
    import fin.retrieve.tiers as tiers

    assert set(tiers.__all__) == {
        "LASTING_RETRIEVE_TIERS",
        "REAL_DB_NOT_WIRED",
        "RETIRED_TIER_TOKENS",
        "RETRIEVE_TIERS",
        "RetrieveTier",
        "TIER_LABELS",
        "TIER_NOT_OPEN",
        "TIER_UNAVAILABLE_CODE",
        "normalize_tier_token",
    }


def test_the_alias_map_is_the_one_authority_for_retired_spellings() -> None:
    """The rename needs exactly one map, or a surface that forgets to read
    it starts forwarding a token the wire no longer carries. Every lasting
    token it points at must itself be a member of the closed set, and no
    entry may point at another retired spelling (a chain would make the
    normalize order-dependent)."""
    from fin.retrieve.tiers import (
        LASTING_RETRIEVE_TIERS,
        RETIRED_TIER_TOKENS,
        RETRIEVE_TIERS,
        TIER_LABELS,
        normalize_tier_token,
    )

    assert RETIRED_TIER_TOKENS == {"B": "graph", "C": "global"}
    assert set(RETIRED_TIER_TOKENS.values()) <= set(RETRIEVE_TIERS)
    assert LASTING_RETRIEVE_TIERS == tuple(
        token for token in ("A", "graph", "global", "D")
    )
    assert set(TIER_LABELS) == set(LASTING_RETRIEVE_TIERS)
    # Nothing the map already resolves needs a second lookup.
    for retired, lasting in RETIRED_TIER_TOKENS.items():
        assert normalize_tier_token(retired) == lasting
        assert normalize_tier_token(retired.lower()) == lasting
        assert lasting not in RETIRED_TIER_TOKENS
    # The letters are still accepted as wire tokens by the closed set
    # until the port retires them; the map is what keeps them OUT of the
    # host's own output.
    assert set(RETRIEVE_TIERS) == set(LASTING_RETRIEVE_TIERS) | set(
        RETIRED_TIER_TOKENS
    )


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
