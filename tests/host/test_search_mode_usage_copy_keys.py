"""检索档 usage-copy keys and the retired-spelling invariant.

The ``A``→``lexical`` / ``D``→``sql`` rename left no alias behind, so on this
surface:

* :data:`SEARCH_MODE_USAGE_COPY` is keyed by exactly
  :data:`fin.retrieve.tiers.LASTING_RETRIEVE_TIERS` — a retired letter is
  not a key and, with the letters out of the closed set, never can be.
* :func:`fin.retrieve.tiers.normalize_tier_token` is the single
  spelling→token authority in front of the dict, so an upper-cased or
  padded lasting token reaches the SAME copy. Every assertion about a
  retired letter below is a literal: the SSOT keeps no map to read, so a
  letter must resolve to nothing here as everywhere else. A lasting tier
  that arrives without copy fails
  :func:`test_copy_keys_are_exactly_the_lasting_tiers`.
* the copy TEXT names no mode at all — neither the lasting MCP token nor
  the operator label — and never tells the model to send a mode filter,
  because the host assigns the mode and the model cannot send one.
* an unresolvable token injects nothing: the payload stays byte-identical
  to the no-档 call, so an out-of-set value never borrows another 档's
  instructions.
"""

from __future__ import annotations

import pytest
from langchain_core.messages import HumanMessage

from fin.host.agent_instructions import (
    SEARCH_MODE_INSTRUCTIONS_MARKER,
    SEARCH_MODE_USAGE_COPY,
    messages_for_model,
)
from fin.retrieve.tiers import LASTING_RETRIEVE_TIERS, TIER_LABELS
from search_mode_pins import RETIRED_LETTERS


def _payload(mode: object) -> list:
    return messages_for_model(
        [HumanMessage(content="q")], track="grounded", search_mode=mode
    )


def _mode_blocks(payload: list) -> list[str]:
    return [
        str(message.content)
        for message in payload
        if SEARCH_MODE_INSTRUCTIONS_MARKER in str(message.content)
    ]


def test_copy_keys_are_exactly_the_lasting_tiers() -> None:
    """No more, no less: the four lasting tokens, no retired spelling."""
    assert set(SEARCH_MODE_USAGE_COPY) == set(LASTING_RETRIEVE_TIERS)
    assert not set(SEARCH_MODE_USAGE_COPY) & set(RETIRED_LETTERS)


@pytest.mark.parametrize("mode", sorted(SEARCH_MODE_USAGE_COPY))
def test_every_copy_is_marked_once(mode: str) -> None:
    assert SEARCH_MODE_USAGE_COPY[mode].startswith(
        SEARCH_MODE_INSTRUCTIONS_MARKER
    )
    assert SEARCH_MODE_USAGE_COPY[mode].count(
        SEARCH_MODE_INSTRUCTIONS_MARKER
    ) == 1


@pytest.mark.parametrize("mode", LASTING_RETRIEVE_TIERS)
def test_a_copy_never_teaches_the_model_to_send_a_mode(mode: str) -> None:
    """The model cannot send a mode: the host assigns it and the retrieve
    call carries it (spec search-mode SC2). A copy that told the model to
    send a mode filter taught an argument the tool schema does not accept,
    and the model then spent turns trying to send it."""
    copy = SEARCH_MODE_USAGE_COPY[mode]
    assert "search_mode" not in copy
    for lasting in LASTING_RETRIEVE_TIERS:
        assert f"filters.search_mode={lasting}" not in copy


@pytest.mark.parametrize("mode", sorted(SEARCH_MODE_USAGE_COPY))
def test_no_copy_instructs_a_retired_letter(mode: str) -> None:
    """Nothing model-facing may teach a retired letter as the lasting MCP
    token."""
    copy = SEARCH_MODE_USAGE_COPY[mode]
    for letter in RETIRED_LETTERS:
        assert f"档 {letter}" not in copy, mode
        assert f"search_mode={letter}" not in copy, mode


@pytest.mark.parametrize("mode", sorted(SEARCH_MODE_USAGE_COPY))
def test_no_copy_names_any_mode(mode: str) -> None:
    """The copy describes the path the host chose and names none of them.

    A named mode is a vocabulary the model then repeats in its own
    reasoning — and that reasoning is rendered — so the operator saw the
    internal mode names on the page. The operator's labels and the lasting
    tokens are both forbidden here."""
    copy = SEARCH_MODE_USAGE_COPY[mode]
    for lasting in LASTING_RETRIEVE_TIERS:
        assert f" {lasting} " not in f" {copy} ", mode
        assert f"{lasting} " not in copy, mode
        assert TIER_LABELS[lasting] not in copy, mode
    assert "档" not in copy, mode


def test_the_copies_still_describe_their_path() -> None:
    """Naming no mode must not empty the copy: each one still tells the
    model how to use the path the host picked."""
    assert "keyword-first" in SEARCH_MODE_USAGE_COPY["lexical"]
    assert "neighbourhoods" in SEARCH_MODE_USAGE_COPY["graph"]
    assert "community summaries" in SEARCH_MODE_USAGE_COPY["global"]
    assert "live" in SEARCH_MODE_USAGE_COPY["sql"]
    assert "expiry-window" in SEARCH_MODE_USAGE_COPY["sql"]


def test_the_lookup_copy_names_no_token_at_all() -> None:
    """The 查找 copy used to say the word three times (``检索档 lexical``,
    ``lexical-first``, the wire sentence), and a token repeated in prose is
    a second spelling waiting to drift. The copy now names it zero times."""
    copy = SEARCH_MODE_USAGE_COPY["lexical"]
    assert "lexical" not in copy
    assert "keyword-first" in copy


@pytest.mark.parametrize("mode", LASTING_RETRIEVE_TIERS)
def test_copy_resolves_for_every_key_it_has(mode: str) -> None:
    """Present as a key means reachable at model-call time — a key nobody
    can resolve is dead copy."""
    blocks = _mode_blocks(_payload(mode))
    assert len(blocks) == 1
    assert SEARCH_MODE_USAGE_COPY[mode] in blocks[0]


@pytest.mark.parametrize("retired", RETIRED_LETTERS)
def test_a_retired_letter_keys_no_copy_and_resolves_to_nothing(
    retired: str,
) -> None:
    """Zero coexistence: with no alias map left in the SSOT, a letter is not
    a spelling of anything — it must neither be a key nor borrow the copy of
    the 档 it once named, or a stale writer would keep reaching a model."""
    assert retired not in SEARCH_MODE_USAGE_COPY
    assert not _mode_blocks(_payload(retired))
    assert not _mode_blocks(_payload(retired.lower()))


@pytest.mark.parametrize(
    "mode, lasting",
    [
        ("GRAPH", "graph"),
        ("Global", "global"),
        ("  sql  ", "sql"),
        ("LEXICAL", "lexical"),
    ],
)
def test_case_and_padding_follow_the_ssot_normalizer(
    mode: str, lasting: str
) -> None:
    """Case-folding is not re-implemented here: whatever
    ``normalize_tier_token`` accepts, the copy resolves identically."""
    assert _mode_blocks(_payload(mode)) == _mode_blocks(_payload(lasting))


@pytest.mark.parametrize(
    "mode", ["E", "local", "fast", "B档", "lexical档", "", "   ", None, 3, True]
)
def test_an_unresolvable_token_injects_nothing(mode: object) -> None:
    """Absent / empty / out-of-set (including a retired letter written as
    prose) never resolves to some other 档's copy."""
    baseline = messages_for_model(
        [HumanMessage(content="q")], track="grounded"
    )
    out = _payload(mode)
    assert [str(m.content) for m in out] == [str(m.content) for m in baseline]
    assert not _mode_blocks(out)
