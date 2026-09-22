"""检索档 usage-copy keys and the retired-spelling invariant.

The ``B``→``graph`` / ``C``→``global`` rename is complete on this surface:

* :data:`SEARCH_MODE_USAGE_COPY` is keyed by exactly
  :data:`fin.retrieve.tiers.LASTING_RETRIEVE_TIERS` — a retired spelling
  is not a key and never needs to be added here.
* a turn whose audit predates the rename still gets its copy, because
  :func:`fin.retrieve.tiers.normalize_tier_token` is the single
  spelling→token authority in front of the dict. Every assertion about a
  retired letter below is DERIVED from
  :data:`fin.retrieve.tiers.RETIRED_TIER_TOKENS`: adding a retired
  spelling to that map is covered without touching this file, and a
  lasting tier that arrives without copy fails
  :func:`test_copy_keys_are_exactly_the_lasting_tiers`.
* the copy TEXT names the lasting MCP token and never tells the model its
  token is a retired letter — including the 真数据库 copy, whose document
  tables stay on ``graph``.
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
from fin.retrieve.tiers import (
    LASTING_RETRIEVE_TIERS,
    RETIRED_TIER_TOKENS,
    TIER_LABELS,
)

# The lasting tokens a retired letter once named — read off the SSOT, so a
# new retirement lands here automatically.
LETTER_NAMED_TOKENS: tuple[str, ...] = tuple(
    sorted(set(RETIRED_TIER_TOKENS.values()))
)


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
    assert not set(SEARCH_MODE_USAGE_COPY) & set(RETIRED_TIER_TOKENS)


@pytest.mark.parametrize("mode", sorted(SEARCH_MODE_USAGE_COPY))
def test_every_copy_is_marked_once(mode: str) -> None:
    assert SEARCH_MODE_USAGE_COPY[mode].startswith(
        SEARCH_MODE_INSTRUCTIONS_MARKER
    )
    assert SEARCH_MODE_USAGE_COPY[mode].count(
        SEARCH_MODE_INSTRUCTIONS_MARKER
    ) == 1


@pytest.mark.parametrize("mode", LETTER_NAMED_TOKENS)
def test_a_word_token_copy_names_its_own_token(mode: str) -> None:
    """知识问答 / 综述 are the two 档 whose copy teaches the wire token."""
    copy = SEARCH_MODE_USAGE_COPY[mode]
    assert f"filters.search_mode={mode}" in copy


@pytest.mark.parametrize("mode", sorted(SEARCH_MODE_USAGE_COPY))
def test_no_copy_instructs_a_retired_letter(mode: str) -> None:
    """Nothing model-facing may teach a retired letter as the lasting MCP
    token."""
    copy = SEARCH_MODE_USAGE_COPY[mode]
    for letter in RETIRED_TIER_TOKENS:
        assert f"档 {letter}" not in copy, mode
        assert f"search_mode={letter}" not in copy, mode


@pytest.mark.parametrize("mode", sorted(SEARCH_MODE_USAGE_COPY))
def test_operator_label_sits_under_its_own_key(mode: str) -> None:
    """The 档 labels are unchanged (CONTEXT 检索档): only the token moved."""
    assert TIER_LABELS[mode] in SEARCH_MODE_USAGE_COPY[mode]


def test_real_db_copy_keeps_document_tables_on_graph() -> None:
    assert "graph" in SEARCH_MODE_USAGE_COPY["D"]
    assert "filters.search_mode=graph" in SEARCH_MODE_USAGE_COPY["D"]


@pytest.mark.parametrize("mode", LASTING_RETRIEVE_TIERS)
def test_copy_resolves_for_every_key_it_has(mode: str) -> None:
    """Present as a key means reachable at model-call time — a key nobody
    can resolve is dead copy."""
    blocks = _mode_blocks(_payload(mode))
    assert len(blocks) == 1
    assert SEARCH_MODE_USAGE_COPY[mode] in blocks[0]


@pytest.mark.parametrize(
    "retired, lasting", sorted(RETIRED_TIER_TOKENS.items())
)
def test_a_retired_spelling_reuses_its_lasting_copy_without_a_key(
    retired: str, lasting: str
) -> None:
    """The SSOT map, not this file, owns the retired spelling: a pre-rename
    audit resolves to the SAME text its lasting token gets."""
    assert retired not in SEARCH_MODE_USAGE_COPY
    assert _mode_blocks(_payload(retired)) == _mode_blocks(_payload(lasting))


@pytest.mark.parametrize(
    "mode, lasting",
    [("GRAPH", "graph"), ("Global", "global"), ("  d  ", "D"), ("a", "A")],
)
def test_case_and_padding_follow_the_ssot_normalizer(
    mode: str, lasting: str
) -> None:
    """Case-folding is not re-implemented here: whatever
    ``normalize_tier_token`` accepts, the copy resolves identically."""
    assert _mode_blocks(_payload(mode)) == _mode_blocks(_payload(lasting))


@pytest.mark.parametrize(
    "mode", ["E", "local", "fast", "B档", "", "   ", None, 3, True]
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
