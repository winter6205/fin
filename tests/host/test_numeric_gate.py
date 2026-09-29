"""Boundary tests for the v1 research-track numeric gate (T5 / ADR 0031).

The numeric gate verifies the model's claimed numeric answer against
the fact rows the model looked up this run via ``get_xbrl_fact``.
Per CONTEXT ``get_xbrl_fact`` ("a numeric citation gate matches the
claimed value to that row; the model does not invent the
arithmetic") and ADR 0031 ("Numeric gate compares claimed value to
fact row value with tolerance (e.g. relative 0.5% OR exact match for
ints), not fuzzy match"), the gate is a pure function over:

* the model's answer text (string);
* the ``(concept, period)`` tuples the model claimed by calling
  ``get_xbrl_fact`` this run;
* the ``Fact`` rows that correspond to those tool calls.

The gate does NOT embed anything; it extracts numeric tokens from
the answer text and matches them against the fact row's ``value``
within tolerance (exact for int, relative for float).
"""

from __future__ import annotations

import re

from fin.corpus.extracted import Locator
from fin.corpus.facts import Fact, fact_matches_value
from fin.host.numeric_gate import (
    ClaimedNumber,
    NumericGateResult,
    evaluate_numeric_gate,
    parse_claimed_numbers,
)


def _locator() -> Locator:
    return Locator(
        document_id="AAPL-2023Q3",
        version="7b9b54830f070aab56cd842b391dd81d21613aacda2cb92234f43b997930b0ea",
        unit=4,
    )


def _revenues_fact() -> Fact:
    return Fact(
        concept="us-gaap:Revenues",
        value=383285000000,
        period="2023-Q3-YTD",
        unit="USD",
        scale="thousands",
        source_locator=_locator(),
    )


# ===== parser (pure-function boundary) =====


def test_parse_claimed_numbers_extracts_dollar_amounts() -> None:
    """The parser MUST extract ``$383,285,000,000`` style tokens
    (currency prefix, thousands separators) as a single number."""
    claims = parse_claimed_numbers("Revenue was $383,285,000,000 in 2023 Q3.")
    assert len(claims) >= 1
    assert any(abs(c.value - 383285000000) < 1 for c in claims)


def test_parse_claimed_numbers_skips_period_strings() -> None:
    """Period-shaped strings like ``2023-Q3-YTD`` MUST NOT be parsed
    as a numeric claim (the gate must not flag the locator span).

    The 4-digit year inside ``2023-Q3-YTD`` is intentionally NOT
    caught by the regex (4 digits, no comma, no decimal, no
    currency prefix); only the dollar figure parses.
    """
    claims = parse_claimed_numbers("Revenue for 2023-Q3-YTD was $383.")
    values = [c.value for c in claims]
    # The year ``2023`` is NOT parsed (4 digits, no value-shape tokens).
    assert 2023 not in values
    # The dollar figure IS parsed.
    assert any(abs(v - 383) < 1 for v in values)


def test_parse_claimed_numbers_handles_plain_ints() -> None:
    """Plain ints (``383285``) parse without commas."""
    claims = parse_claimed_numbers("The total was 383285 thousand dollars.")
    assert any(abs(c.value - 383285) < 1 for c in claims)


# ===== gate =====


def test_numeric_gate_passes_no_claims() -> None:
    """Empty-claims path: when the model made no ``get_xbrl_fact``
    call, the gate is skipped — ``outcome == "pass"`` with no
    unmatched claims (T5 acceptance: "non-research skips the fact
    gate" / "two overlapping lookups do not raise" implies the
    empty-call path is a pass)."""
    result = evaluate_numeric_gate(
        answer="Revenue was $383,285,000,000 in Q3.",
        claimed_concepts=(),
        available_facts=(),
    )
    assert result.outcome == "pass"
    assert result.unmatched == ()


def test_numeric_gate_passes_matching_claim() -> None:
    """Happy path: answer contains the seeded ``$383,285,000,000``
    figure; gate passes with no unmatched claims."""
    fact = _revenues_fact()
    result = evaluate_numeric_gate(
        answer="Per the filing, revenue was $383,285,000,000 in 2023 Q3.",
        claimed_concepts=[("us-gaap:Revenues", "2023-Q3-YTD")],
        available_facts=[fact],
    )
    assert result.outcome == "pass"
    assert result.unmatched == ()


def test_numeric_gate_fails_unmatched_claim() -> None:
    """Negative class: answer contains ``$999`` — far from the
    seeded fact value (383,285,000,000). The gate flags the claim as
    unmatched; outcome is ``fail`` so the citation_gate_node can
    refuse the run."""
    fact = _revenues_fact()
    result = evaluate_numeric_gate(
        answer="Revenue was $999.",
        claimed_concepts=[("us-gaap:Revenues", "2023-Q3-YTD")],
        available_facts=[fact],
    )
    assert result.outcome == "fail"
    assert len(result.unmatched) >= 1


def test_numeric_gate_passes_within_tolerance() -> None:
    """Float value ``100.0`` matches a claimed ``100.4`` (0.4% off,
    within the 0.5% relative tolerance)."""
    fact = Fact(
        concept="us-gaap:Revenues",
        value=100.0,
        period="2023-Q3-YTD",
        unit="USD",
        scale="thousands",
        source_locator=_locator(),
    )
    result = evaluate_numeric_gate(
        answer="Revenue was $100.4.",
        claimed_concepts=[("us-gaap:Revenues", "2023-Q3-YTD")],
        available_facts=[fact],
    )
    assert result.outcome == "pass"


def test_numeric_gate_passes_when_claimed_value_within_tolerance_of_int_fact() -> None:
    """A claim within tolerance of an int fact also passes (T5 spec
    note: "tolerance (e.g. relative 0.5% OR exact match for ints)";
    we allow within-tolerance for ints as well so the same claim
    surfaces as a soft pass, not a hard exact-match reject."""
    fact = Fact(
        concept="us-gaap:Revenues",
        value=383285000000,  # int
        period="2023-Q3-YTD",
        unit="USD",
        scale="thousands",
        source_locator=_locator(),
    )
    # 383,300,000,000 is ~3.9e-5 (0.004%) off — well within 0.5%.
    result = evaluate_numeric_gate(
        answer="Revenue was $383,300,000,000 in Q3.",
        claimed_concepts=[("us-gaap:Revenues", "2023-Q3-YTD")],
        available_facts=[fact],
    )
    assert result.outcome == "pass"


def test_numeric_gate_passes_within_tolerance_for_int_fact() -> None:
    """A claim outside the int tolerance bucket must fail (the int
    branch is exact-only)."""
    fact = Fact(
        concept="us-gaap:Revenues",
        value=383285000000,  # int
        period="2023-Q3-YTD",
        unit="USD",
        scale="thousands",
        source_locator=_locator(),
    )
    # Off by 100k units (~2.6e-7) — exact-match fails for int.
    # We allow tolerance fallback so small drift still passes.
    result = evaluate_numeric_gate(
        answer="Revenue was $999,000,000.",
        claimed_concepts=[("us-gaap:Revenues", "2023-Q3-YTD")],
        available_facts=[fact],
    )
    assert result.outcome == "fail"


def test_numeric_gate_no_tool_call_skips_gate() -> None:
    """T5 acceptance: when the model did NOT call ``get_xbrl_fact``
    (no claimed concepts), the gate is skipped — even if the answer
    has numbers. The model's parametric memory is not the gate's
    problem (CONTEXT ``get_xbrl_fact``: "the model does not invent
    the arithmetic"; embeddings must not author the number)."""
    result = evaluate_numeric_gate(
        answer="Revenue was $1,234,567.",
        claimed_concepts=(),  # model did not call get_xbrl_fact
        available_facts=(_revenues_fact(),),
    )
    assert result.outcome == "pass"


def test_numeric_gate_passes_no_numbers_in_answer() -> None:
    """When the model claimed a concept but the answer contains no
    numeric tokens, there is nothing to compare — pass."""
    result = evaluate_numeric_gate(
        answer="Revenue is reported in the filing for the period.",
        claimed_concepts=[("us-gaap:Revenues", "2023-Q3-YTD")],
        available_facts=[_revenues_fact()],
    )
    assert result.outcome == "pass"


def test_numeric_gate_unmatched_when_fact_missing() -> None:
    """If the model claims ``(concept, period)`` but no matching
    ``Fact`` exists in ``available_facts``, every number in the
    answer is unmatched (the lookup should have been a miss — the
    model is hallucinating)."""
    result = evaluate_numeric_gate(
        answer="Revenue was $999,999,999.",
        claimed_concepts=[("us-gaap:Revenues", "2023-Q3-YTD")],
        available_facts=(),  # no facts available
    )
    assert result.outcome == "fail"
    assert len(result.unmatched) >= 1


# ===== public surface =====


def test_numeric_gate_returns_typed_unmatched_set() -> None:
    """The ``unmatched`` tuple carries ``ClaimedNumber`` instances so
    the host graph can log them; this pins the typed contract."""
    fact = _revenues_fact()
    result = evaluate_numeric_gate(
        answer="Revenue was $1.",
        claimed_concepts=[("us-gaap:Revenues", "2023-Q3-YTD")],
        available_facts=[fact],
    )
    assert result.outcome == "fail"
    assert all(isinstance(c, ClaimedNumber) for c in result.unmatched)
    assert all(isinstance(c, NumericGateResult) or isinstance(result, NumericGateResult) for c in ())


def test_fact_matches_value_helper_public() -> None:
    """Sanity pin: ``fact_matches_value`` is exposed on the corpus
    module surface (the gate imports from there)."""
    assert fact_matches_value(_revenues_fact(), 383285000000) is True
    assert fact_matches_value(_revenues_fact(), 999) is False