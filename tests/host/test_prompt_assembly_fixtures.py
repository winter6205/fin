"""Focus 5 T1 fixtures: prompt-assembly invariants, written before copy.

``docs/guides/prompt-development.md`` 流程 1 (先写夹具，再动文案): a prompt
change starts with the fixture, not with the prose. Every test in this
module asserts the POST-split invariant from spec
``specs/workbench-operator-workspace.md`` SC10–SC12 — research prefix
free of ``12 SEC`` / ``page_limit is capped at 3`` /
``32 or more characters``, no hardcoded AAPL/MSFT/AMZN quarter list, and
four **track** prompts that share **agent identity** + **agent soul**
bytes.

The current prompts still violate all of it, so each test carries
``@pytest.mark.xfail(strict=True)``. That is the protocol, not a
concession:

* today the assertion really fails, which is the red proof T1 owes —
  a passing test here would mean the fixture does not bite;
* ``strict=True`` turns an unexpected pass into a FAILURE
  (``XPASS(strict)``), so once T2 rewrites the prompts this module goes
  red until the marks are deleted. T2 removes every ``xfail`` mark in
  this file; it must not weaken an assertion instead;
* default ``uv run pytest -q`` stays at exit 0 meanwhile (SC1).

Style follows ``tests/host/test_agent_instructions.py``: plain functions,
prompt constants imported directly.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from fin.host.agent_instructions import (
    CLARIFY_SYSTEM_PROMPT,
    DIALOGUE_SYSTEM_PROMPT,
    HOST_SYSTEM_PROMPT,
    OFF_TOPIC_SYSTEM_PROMPT,
    clarify_prompt,
)

_T1_XFAIL = pytest.mark.xfail(
    strict=True,
    reason="T1 fixture: pre-split prompt still violates SC10/SC11 — removed in T2",
)

# The four **track** prompts of the Soft-Track host. Identity and soul
# are shared by contract (SC11); only usage differs per track.
TRACK_PROMPTS: dict[str, str] = {
    "research": HOST_SYSTEM_PROMPT,
    "dialogue": DIALOGUE_SYSTEM_PROMPT,
    "off_topic": OFF_TOPIC_SYSTEM_PROMPT,
    "clarify": CLARIFY_SYSTEM_PROMPT,
}

# Identity + soul is a persona paragraph plus the "parametric memory is
# not evidence" truth, so the block the four tracks hold in common is
# hundreds of bytes once split. The floor is deliberately far above the
# incidental overlap of today's independently written prompts (26 chars:
# "_INSTRUCTIONS]\nYou are fin") so the test measures shared identity /
# soul, not a shared greeting.
MIN_SHARED_IDENTITY_SOUL_CHARS = 200


def _longest_common_block(texts: Sequence[str]) -> str:
    """Return the longest contiguous string present in every ``texts``.

    Substring rather than common *prefix*: each track prompt opens with
    its own instructions marker, so identity/soul bytes are shared but
    not necessarily at offset 0. Grown from the shortest input and only
    ever probed past the current best, which keeps a few-hundred-byte
    prompt set well inside test latency.
    """
    shortest = min(texts, key=len)
    best = ""
    for start in range(len(shortest)):
        end = start + len(best) + 1
        while end <= len(shortest):
            candidate = shortest[start:end]
            if not all(candidate in text for text in texts):
                break
            best = candidate
            end += 1
    return best


@_T1_XFAIL
def test_research_prefix_has_no_frozen_corpus_count() -> None:
    """SC10: the corpus size is catalog state, not persona prose.

    ``12 SEC`` in the research prefix (and ``12-filing`` in any track)
    hardcodes a 语料清单 count that a single ingest invalidates — the
    catalog is the single source of truth for what the 知识库 holds.
    """
    assert "12 SEC" not in HOST_SYSTEM_PROMPT
    for track, prompt in TRACK_PROMPTS.items():
        assert "12-filing" not in prompt, track


@_T1_XFAIL
def test_research_prefix_does_not_restate_page_limit_cap() -> None:
    """SC10: ``page_limit`` is a tool field cap, owned by the schema.

    Restating it in prose gives it two homes, and prose loses silently
    when the schema changes (CONTEXT **usage** _Avoid_).
    """
    assert "page_limit is capped at 3" not in HOST_SYSTEM_PROMPT


@_T1_XFAIL
def test_research_prefix_does_not_restate_citation_span_number() -> None:
    """SC10: the 32-character span threshold belongs to the gate.

    The **citation verifier** enforces span length in code (ADR 0046);
    usage describes citation *behavior* only (说明书不是闸).
    """
    assert "32 or more characters" not in HOST_SYSTEM_PROMPT


@_T1_XFAIL
def test_track_prompts_do_not_hardcode_ticker_list() -> None:
    """SC10/SC11: no track prompt carries the AAPL/MSFT/AMZN seed list.

    The tickers are 知识库 content, discoverable via ``list_files``; in
    the prefix they are a stale catalog the operator cannot refresh.
    """
    for track, prompt in TRACK_PROMPTS.items():
        for ticker in ("AAPL", "MSFT", "AMZN"):
            assert ticker not in prompt, f"{track} names {ticker}"


@_T1_XFAIL
def test_clarify_prompt_does_not_hardcode_quarter_list() -> None:
    """SC10: the rendered clarify ask grounds options without a snapshot.

    Checked on ``clarify_prompt`` output, not only on the constant: the
    per-slot asks are the bytes the model actually sees on a clarify
    turn, so a split that cleans the constant and leaves the slot asks
    hardcoded still ships the frozen quarter list.
    """
    rendered = clarify_prompt(["company", "period"])
    for period in ("2022 Q3", "2023 Q1", "2023 Q2", "2023 Q3"):
        assert period not in rendered, period
    for ticker in ("AAPL", "MSFT", "AMZN"):
        assert ticker not in rendered, ticker


@_T1_XFAIL
def test_track_prompts_share_identity_soul_bytes() -> None:
    """SC11: four tracks reuse identity + soul and differ in usage only.

    Measured as the longest block common to all four prompts. Today the
    prompts were authored independently and share ~nothing, so the block
    is a couple of dozen characters; after the split it is the identity
    and soul paragraphs verbatim.
    """
    shared = _longest_common_block(list(TRACK_PROMPTS.values()))
    assert len(shared) >= MIN_SHARED_IDENTITY_SOUL_CHARS, (
        f"longest block shared by all four tracks is {len(shared)} chars: "
        f"{shared!r}"
    )


@_T1_XFAIL
def test_identity_is_not_a_research_agent() -> None:
    """SC10 / CONTEXT **agent identity** _Avoid_: fin is not a research agent.

    fin is an enterprise knowledge-base Agent; ``research`` names a
    **track**, never the product kind.
    """
    assert "research agent" not in HOST_SYSTEM_PROMPT
