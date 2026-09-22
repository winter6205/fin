"""检索档 usage-copy keys during the ``B``→``graph`` / ``C``→``global`` rename.

The rename lands in two increments, so both halves are pinned here:

* :data:`SEARCH_MODE_USAGE_COPY` carries the lasting tokens as keys and
  still carries the former letters — the surfaces that produce ``B``/``C``
  today (pin, choice window) keep resolving their copy until normalize
  retires the letters.
* the copy TEXT is already the new one: it names the lasting MCP token and
  never tells the model its token is ``B`` or ``C`` (spec
  ``search-mode-graph-global`` SC4). A letter key and its lasting key must
  therefore read the same body — two divergent bodies would mean the
  rename drifted.
* resolving a copy is exact-match on the token's own spelling: an
  upper-cased ``GRAPH`` is not ``graph`` and injects nothing (the same
  no-silent-map rule the port enforces).
"""

from __future__ import annotations

import pytest
from langchain_core.messages import HumanMessage

from fin.host.agent_instructions import (
    SEARCH_MODE_INSTRUCTIONS_MARKER,
    SEARCH_MODE_USAGE_COPY,
    messages_for_model,
)

LASTING_TOKENS = ("graph", "global")
FORMER_LETTERS = ("B", "C")


def _payload(mode: str) -> list:
    return messages_for_model(
        [HumanMessage(content="q")], track="grounded", search_mode=mode
    )


def _mode_blocks(payload: list) -> list[str]:
    return [
        str(message.content)
        for message in payload
        if SEARCH_MODE_INSTRUCTIONS_MARKER in str(message.content)
    ]


def test_copy_keys_are_the_migration_superset() -> None:
    """New keys beside the old letters — no more, no less."""
    assert set(SEARCH_MODE_USAGE_COPY) == {"A", "B", "C", "D", *LASTING_TOKENS}


@pytest.mark.parametrize("mode", LASTING_TOKENS)
def test_lasting_copy_names_its_own_token(mode: str) -> None:
    copy = SEARCH_MODE_USAGE_COPY[mode]
    assert f"filters.search_mode={mode}" in copy
    assert copy.startswith(SEARCH_MODE_INSTRUCTIONS_MARKER)


@pytest.mark.parametrize("mode", sorted(SEARCH_MODE_USAGE_COPY))
def test_no_copy_instructs_a_former_letter(mode: str) -> None:
    """SC4: nothing in the model-facing copy may teach ``B``/``C`` as the
    lasting MCP token — including the 真数据库 copy, whose document tables
    stay on ``graph``."""
    copy = SEARCH_MODE_USAGE_COPY[mode]
    for letter in FORMER_LETTERS:
        assert f"档 {letter}" not in copy, mode
        assert f"search_mode={letter}" not in copy, mode


@pytest.mark.parametrize("mode", LASTING_TOKENS)
def test_a_letter_key_and_its_lasting_key_share_one_body(mode: str) -> None:
    """``B``/``graph`` and ``C``/``global`` resolve the same text, so the
    具名失败 meaning and the operator label cannot drift between them."""
    letter = {"graph": "B", "global": "C"}[mode]
    assert SEARCH_MODE_USAGE_COPY[letter] == SEARCH_MODE_USAGE_COPY[mode]


def test_operator_labels_stay_under_the_new_keys() -> None:
    """The 档 labels are unchanged (CONTEXT 检索档): only the token moved."""
    assert "知识问答" in SEARCH_MODE_USAGE_COPY["graph"]
    assert "综述" in SEARCH_MODE_USAGE_COPY["global"]
    assert "真数据库" in SEARCH_MODE_USAGE_COPY["D"]
    assert "查找" in SEARCH_MODE_USAGE_COPY["A"]
    assert "graph" in SEARCH_MODE_USAGE_COPY["D"]


@pytest.mark.parametrize("mode", (*LASTING_TOKENS, *FORMER_LETTERS))
def test_copy_resolves_for_every_key_it_has(mode: str) -> None:
    """Present as a key means reachable at model-call time — a key nobody
    can resolve is dead copy."""
    blocks = _mode_blocks(_payload(mode))
    assert len(blocks) == 1
    assert SEARCH_MODE_USAGE_COPY[mode] in blocks[0]


def _payload(mode: str) -> list:
    return messages_for_model(
        [HumanMessage(content="q")], track="grounded", search_mode=mode
    )


@pytest.mark.parametrize("mode", ["GRAPH", "Global", "local", "", None])
def test_an_unresolvable_token_injects_nothing(mode: str | None) -> None:
    """Case matters: an upper-cased lasting token is not that token, and
    an out-of-set string never resolves to some other 档's copy."""
    baseline = messages_for_model(
        [HumanMessage(content="q")], track="grounded"
    )
    out = messages_for_model(
        [HumanMessage(content="q")], track="grounded", search_mode=mode
    )
    assert [str(m.content) for m in out] == [str(m.content) for m in baseline]
    assert not _mode_blocks(out)
