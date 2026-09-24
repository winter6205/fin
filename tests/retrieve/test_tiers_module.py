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
        COMMUNITY_SUMMARY_INDEX_FAILURE,
        REAL_DB_NOT_WIRED,
        RETRIEVE_TIERS,
        TIER_NOT_OPEN,
        TIER_UNAVAILABLE_CODE,
    )

    assert RETRIEVE_TIERS == ("lexical", "graph", "global", "sql")
    assert TIER_NOT_OPEN == "tier_not_open"
    assert REAL_DB_NOT_WIRED == "real_db_not_wired"
    assert COMMUNITY_SUMMARY_INDEX_FAILURE == "community_summary_index_failure"
    # Opening the global 综述 档 (fin task 4) moved it from a routing-time
    # plan slot to a real report arm, so it drops out of the「未开」table:
    # only ``sql`` is refused at routing time now. A global send's one
    # remaining refusal is the RUNTIME summary-index fact below, never this
    # table — so the table keeps exactly one row.
    assert TIER_UNAVAILABLE_CODE == {
        "sql": "real_db_not_wired",
    }


# ----- 1b. the retired letters are in NO set anymore -----


def test_the_refusal_message_names_the_word_form_set() -> None:
    """Negative class, copy level: the refusal a caller sees IS the
    documentation of the closed set, so it must spell the four word forms
    and never a retired letter — a message still advertising ``A``/``D``
    would teach the vocabulary the rename just retired.
    """
    from fin.retrieve.port import InvalidSearchModeError, resolve_search_mode
    from fin.retrieve.tiers import RETRIEVE_TIERS

    expected_set = "|".join(RETRIEVE_TIERS)
    for letter in ("A", "B", "C", "D"):
        try:
            resolve_search_mode(letter)
        except InvalidSearchModeError as exc:
            assert str(exc) == (
                f"search_mode {letter!r} is not one of {expected_set}"
            ), letter
        else:
            raise AssertionError(f"{letter!r} crossed the port")


def test_graph_and_global_are_both_open_tiers() -> None:
    """``graph`` 知识问答 is today's default 档, and opening ``global`` 综述
    (fin task 4) made it a real report arm too: neither carries a
    「未开」code, so naming either resolves to the token rather than raising.
    ``sql`` alone is still a plan slot."""
    from fin.retrieve.port import TierUnavailableError, resolve_search_mode
    from fin.retrieve.tiers import TIER_UNAVAILABLE_CODE

    assert "graph" not in TIER_UNAVAILABLE_CODE
    assert "global" not in TIER_UNAVAILABLE_CODE
    assert resolve_search_mode("graph") == "graph"
    assert resolve_search_mode("global") == "global"

    try:
        resolve_search_mode("sql")
    except TierUnavailableError as exc:
        assert exc.code == "real_db_not_wired"
    else:
        raise AssertionError("sql accepted as an open 档")


def test_a_letter_at_the_port_is_invalid_not_routed() -> None:
    """Negative class, and the whole point of the closed set shrinking.

    ``A`` used to be an open 档 on the port and ``D`` an unopened one; all
    four letters now answer ``invalid_search_mode``. Normalizing them here
    would be the silent mapping the spec forbids — the host already resolves
    its own spellings before the seam, so a letter that got this far is a
    wiring bug whose name must survive.
    """
    from fin.retrieve.port import (
        InvalidSearchModeError,
        TierUnavailableError,
        resolve_search_mode,
    )

    assert resolve_search_mode("lexical") == "lexical"
    assert resolve_search_mode("graph") == "graph"
    assert resolve_search_mode("global") == "global"
    assert resolve_search_mode(None) is None

    for token in ("A", "B", "C", "D"):
        try:
            resolve_search_mode(token)
        except InvalidSearchModeError as exc:
            assert exc.code == "invalid_search_mode", token
        else:
            raise AssertionError(f"{token} still crosses the port")

    # Only ``sql`` is a routing-time plan slot now: global was opened, so it
    # resolves to its token above and cannot be refused here.
    try:
        resolve_search_mode("sql")
    except TierUnavailableError as exc:
        assert exc.code == "real_db_not_wired"
    else:
        raise AssertionError("sql accepted as an open 档")


def test_an_unknown_token_stays_unknown_after_the_rename() -> None:
    """Negative class: adding ``graph``/``global`` must not turn every
    non-token into a 档. An unknown token — including the upper-cased
    spelling of a lasting token — is ``invalid_search_mode``, not a
    silent map onto ``graph`` or ``global``."""
    from fin.retrieve.port import InvalidSearchModeError, resolve_search_mode

    for token in ("local", "", "hybrid", "GRAPH", "Global", "b", "LEXICAL"):
        try:
            resolve_search_mode(token)
        except InvalidSearchModeError as exc:
            assert exc.code == "invalid_search_mode"
        else:
            raise AssertionError(f"{token!r} accepted by the closed set")


def test_tiers_module_is_public_via_dunder_all() -> None:
    import fin.retrieve.tiers as tiers

    assert set(tiers.__all__) == {
        "COMMUNITY_SUMMARY_INDEX_FAILURE",
        "LASTING_RETRIEVE_TIERS",
        "REAL_DB_NOT_WIRED",
        "RETRIEVE_TIERS",
        "TIER_LABELS",
        "TIER_NOT_OPEN",
        "TIER_UNAVAILABLE_CODE",
        "RetrieveTier",
        "normalize_tier_token",
    }


def test_the_closed_set_is_one_definition_not_a_copy() -> None:
    """The port's closed set and the operator-ordered lasting tuple are the
    SAME object: two literals would let the wire accept a token the host
    never produces (or refuse one it does)."""
    from fin.retrieve.tiers import LASTING_RETRIEVE_TIERS, RETRIEVE_TIERS

    assert RETRIEVE_TIERS is LASTING_RETRIEVE_TIERS


def test_the_normalizer_is_case_insensitive_and_names_no_letter() -> None:
    """The word-form set needs exactly ONE resolver, or a surface that
    forgets to read it starts forwarding a token the wire does not carry.

    Case and padding survive for the lasting tokens (a selector is a closed
    token, not prose); every retired letter resolves to nothing, in either
    case — the rename left no map behind, so there is nowhere a letter can
    still quietly become a 档.
    """
    from fin.retrieve.tiers import (
        LASTING_RETRIEVE_TIERS,
        RETRIEVE_TIERS,
        TIER_LABELS,
        normalize_tier_token,
    )

    assert LASTING_RETRIEVE_TIERS == ("lexical", "graph", "global", "sql")
    assert set(TIER_LABELS) == set(LASTING_RETRIEVE_TIERS)
    assert TIER_LABELS == {
        "lexical": "查找",
        "graph": "知识问答",
        "global": "综述",
        "sql": "真数据库",
    }
    for token in LASTING_RETRIEVE_TIERS:
        assert normalize_tier_token(token) == token
        assert normalize_tier_token(f"  {token.upper()} ") == token
    for letter in ("A", "a", "B", "b", "C", "c", "D", "d"):
        assert normalize_tier_token(letter) is None, letter
    assert set(RETRIEVE_TIERS) == set(LASTING_RETRIEVE_TIERS)


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

assert p.resolve_search_mode("lexical") == "lexical"
assert p.resolve_search_mode(None) is None
assert p.resolve_search_mode("global") == "global"
try:
    p.resolve_search_mode("sql")
except p.TierUnavailableError as e:
    assert e.code == "real_db_not_wired"
else:
    raise AssertionError("sql not refused")
try:
    p.resolve_search_mode("A")
except p.InvalidSearchModeError as e:
    assert e.code == "invalid_search_mode"
else:
    raise AssertionError("retired letter still crosses the port")
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
