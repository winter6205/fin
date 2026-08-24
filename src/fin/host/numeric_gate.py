"""Numeric gate for the v1 research track (T5 / ADR 0031).

When research ``done`` claims a numeric answer matching a known
XBRL ``concept`` for a ``period``, this gate verifies the claimed
value matches an evidenced fact row the model looked up via
``get_xbrl_fact`` this run. Per CONTEXT ``get_xbrl_fact``
("a numeric citation gate matches the claimed value to that row;
the model does not invent the arithmetic") and ADR 0031 ("Numeric
gate compares claimed value to fact row value with tolerance (e.g.
relative 0.5% OR exact match for ints), not fuzzy match"), the
implementation is a pure function over the model's answer text and
the ``Fact`` rows the model surfaced this run. No embeddings, no
authoring of the number — only numeric extraction and tolerance
matching.

Scope
-----
* The gate runs on the research track ``done`` path ONLY when the
  model called ``get_xbrl_fact`` this run. Non-research tracks
  skip this gate (the citation verifier's evidence match is the
  grounding check; the numeric gate is layered on top).
* T5 contract: a single unmatched claim is enough to refuse the
  run (``status="refuse"``, ``status_reason="numeric_gate_fail"``).
  The model's claimed number did NOT match an evidenced row, so the
  claim is a fabrication and the run must not pass.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from fin.corpus.facts import Fact, fact_matches_value


# Regex: matches a "value-like" number in the answer prose.
#
# Catches (per ADR 0031):
#   "$383,285"  -> currency prefix + thousands separator
#   "383285"    -> 5+ digit integer (a small int like "12" or "2023"
#                  is unlikely to be a numeric claim)
#   "383.3"     -> decimal value
#   "$60,584"   -> currency + thousands
#
# Does NOT catch:
#   "first"     -> no digits at all
#   "2023"      -> 4-digit year inside a date / period string
#   "version_hash" -> 64-char hex strings (digits are interspersed
#                     with letters and not bounded by value-shape
#                     tokens like $ or ,).
#
# Tradeoff: "12345" (5 digits) is matched. We accept that as a
# small false-positive risk because the alternative — only matching
# comma-separated numbers — would miss plain ints like "383285"
# that the spec explicitly lists as a catch case.
_NUMERIC_REGEX: re.Pattern[str] = re.compile(
    r"""
    (?<![A-Za-z0-9.])              # not preceded by alnum or dot
    (?:
        \$\d[\d,]*(?:\.\d+)?       # currency-prefixed: $1, $12.34, $1,234
      |
        \d{1,3}(?:,\d{3})+(?:\.\d+)?  # comma-separated: 1,234 or 1,234,567.89
      |
        \d{5,}(?:\.\d+)?            # 5+ digit int: 12345 or 12345.67
      |
        \d+\.\d+                    # decimal: 1.23 or 123.45
    )
    """,
    re.VERBOSE,
)


def _parse_value(raw: str) -> float:
    """Strip currency / thousands separators / trailing whitespace
    and parse the digit sequence as ``float``.

    The regex matches digits + commas + dot + optional ``$``; we
    strip ``$`` and commas, then ``float()``.
    """
    cleaned = raw.replace("$", "").replace(",", "").strip()
    return float(cleaned)


def parse_claimed_numbers(answer: str) -> tuple[ClaimedNumber, ...]:
    """Extract every value-like number from ``answer``.

    Returns a tuple of ``ClaimedNumber(raw, value)`` in source order.
    An answer with no matches returns ``()``.
    """
    out: list[ClaimedNumber] = []
    for match in _NUMERIC_REGEX.finditer(answer):
        raw = match.group(0)
        try:
            value = _parse_value(raw)
        except ValueError:
            continue
        out.append(ClaimedNumber(raw=raw, value=value))
    return tuple(out)


@dataclass(frozen=True)
class ClaimedNumber:
    """One numeric token extracted from the model's answer text.

    ``raw`` is the matched substring (including ``$`` /
    thousands separators); ``value`` is the parsed ``float``.
    """

    raw: str
    value: float


@dataclass(frozen=True)
class NumericGateResult:
    """Outcome of the numeric gate.

    ``outcome == "pass"``:
        * No ``claimed_concepts`` were supplied (model did not
          invoke ``get_xbrl_fact`` this run — non-research /
          no-tool path; the gate is skipped per T5 acceptance).
        * OR every claimed ``(concept, period)`` pair has a matching
          ``Fact`` row whose value matches at least one parsed
          number in the answer (within tolerance), OR the answer has
          no parsed numbers at all (vacuous pass for that claim).

    ``outcome == "fail"``:
        * At least one claimed ``(concept, period)`` pair has no
          matching ``Fact`` row in ``available_facts`` and the answer
          contains parsed numbers (the model fabricated both the
          call AND the value), OR every matching ``Fact`` row has a
          value that fails to match any parsed number within
          tolerance.
    """

    outcome: str  # "pass" | "fail"
    claims: tuple[ClaimedNumber, ...]
    unmatched: tuple[ClaimedNumber, ...]  # subset that didn't match a fact


def evaluate_numeric_gate(
    answer: str,
    *,
    claimed_concepts: Iterable[tuple[str, str]],
    available_facts: Iterable[Fact],
    tolerance: float = 0.005,
) -> NumericGateResult:
    """Run the numeric gate.

    For each ``(concept, period)`` pair in ``claimed_concepts``,
    find a ``Fact`` row in ``available_facts`` with matching
    ``concept`` + ``period``. If the answer contains a parsed
    numeric token, verify at least one parsed token matches the
    fact's value within ``tolerance``.

    Per T5 spec ("Embeddings may resolve concept aliases (out of
    scope for T5); the model does NOT author the number"), this
    function does NOT embed the answer or the facts. It only
    parses numbers and compares.
    """
    claims = parse_claimed_numbers(answer)
    facts = tuple(available_facts)

    claimed_pairs = list(claimed_concepts)
    if not claimed_pairs:
        # No tool calls -> gate is skipped (T5 acceptance).
        return NumericGateResult(outcome="pass", claims=claims, unmatched=())

    unmatched: list[ClaimedNumber] = []
    for concept, period in claimed_pairs:
        matching_facts = [
            f for f in facts if f.concept == concept and f.period == period
        ]
        if not matching_facts:
            # Model called the tool with a (concept, period) the
            # fact table cannot satisfy; every parsed number in
            # the answer is unmatched (the model fabricated the
            # call's argument too).
            unmatched.extend(claims)
            continue
        if not claims:
            # No parsed numbers in the answer — vacuous pass for
            # this claim (the model wrote qualitative prose; the
            # citation verifier handles evidence grounding).
            continue
        # At least one fact must match at least one parsed number.
        any_match = any(
            fact_matches_value(fact, claim.value, tolerance=tolerance)
            for fact in matching_facts
            for claim in claims
        )
        if not any_match:
            unmatched.extend(claims)

    if unmatched:
        return NumericGateResult(
            outcome="fail", claims=claims, unmatched=tuple(unmatched)
        )
    return NumericGateResult(
        outcome="pass", claims=claims, unmatched=()
    )


__all__ = [
    "ClaimedNumber",
    "NumericGateResult",
    "evaluate_numeric_gate",
    "parse_claimed_numbers",
]
