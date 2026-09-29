"""Boundary tests for the v1 citation gate (T5).

The citation gate is the only authority in the host graph on whether
the model's answer becomes ``done``, ``bounce``, or ``refuse``
(ADR 0008 / ADR 0021). It is a **pure function** — no model, no I/O,
no time — so every test here is deterministic and does not need a
live LLM.

The suite covers:

- the five boundary classes (empty / negative / overflow /
  concurrent / exception) per the defensive-contract axis;
- the five spec acceptance scenarios from
  ``specs/v1-host-agent.md`` success criteria 4-6 (the three
  scripted runs that define v1 ship / no-ship);
- a few targeted regression tests for citation dedup, evidence-
  list iteration order, and the hidden-template contract;
- the slice-continuity rule: two ADJACENT returned slices of one
  frozen unit join into matchable text, an unseen gap never does;
- the closed quotable-kind set and the attachment trust re-check
  (an attachment item nobody attests cannot carry a quote).

The gate accepts evidence as ``list[EvidenceItem]`` whose
``unit_text`` is whatever the upstream tool produced for that frozen
unit (or passage slice). The committed extracted tree at
``data/corpus/extracted/`` (T2) is **not** required for most tests;
synthetic evidence is the boundary-friendly fixture.
"""

from __future__ import annotations

import ast
import concurrent.futures
import time
from pathlib import Path

import pytest

from fin.corpus.extracted import Locator
from fin.host.attachment_observations import PLOTTED_VALUE_KIND
from fin.host.citation_gate import (
    BounceResult,
    COMPANY_KIND,
    Citation,
    CitationGateError,
    DoneResult,
    EvidenceItem,
    GateResult,
    MalformedEvidenceError,
    QUOTABLE_EVIDENCE_KINDS,
    RefuseResult,
    verify_citations,
)
from fin.retrieve.source_read import ATTACHMENT_KIND


# ----- fixtures -----


# The frozen corpus exposes 12 ids (3 tickers × 4 periods). Use a
# subset for most tests; the gate does not require the full set.
KNOWN_IDS_DEFAULT: set[str] = {
    "AAPL-2022Q3",
    "AAPL-2023Q1",
    "MSFT-2023Q2",
    "AMZN-2023Q3",
}

# A 64-char hex sha256 (matching the manifest shape).
_SHA = "a7a0d8261a0923404fc45446afd71b9965bd2b4064772df0287e761bb68ba9f2"


def _locator(
    document_id: str = "AAPL-2022Q3",
    version: str = _SHA,
    unit: int = 1,
) -> Locator:
    """Build a Locator with sensible defaults."""
    return Locator(document_id=document_id, version=version, unit=unit)


def _evidence(
    unit_text: str,
    document_id: str = "AAPL-2022Q3",
    version: str = _SHA,
    unit: int = 1,
) -> EvidenceItem:
    """Build a single EvidenceItem with sensible defaults."""
    return EvidenceItem(
        locator=_locator(document_id, version, unit),
        unit_text=unit_text,
    )


def _slice_evidence(
    unit_text: str,
    *,
    position: int,
    next_position: int | None,
    unit: int = 7,
) -> EvidenceItem:
    """One returned slice of one frozen unit, at its read offsets.

    ``position`` / ``next_position`` are the code-point offsets the
    read contract handed back, which is what lets the gate tell an
    ADJACENT slice from one with an unseen hole between them.
    """
    return EvidenceItem(
        locator=_locator(unit=unit),
        unit_text=unit_text,
        position=position,
        next_position=next_position,
    )


def _attachment_evidence(
    unit_text: str,
    *,
    attachment_id: str = "att-1",
    version: str = "3",
    unit: int = 2,
    filename: str = "memo.txt",
    display: str = "unit",
) -> EvidenceItem:
    """One returned attachment slice with its trusted identity."""
    return EvidenceItem(
        # An attachment's identity rides ``attachment_id``; the company
        # document slot stays empty.
        locator=Locator(document_id="", version=version, unit=unit),
        unit_text=unit_text,
        kind=ATTACHMENT_KIND,
        attachment_id=attachment_id,
        version_int=int(version),
        filename=filename,
        display=display,
    )


# A 50-char verbatim span that the typical "done" tests use.
# 10 letters × 5 reps = 50 alphanumeric chars exactly.
_FIFTY_CHAR_SPAN = "ABCDEFGHIJ" * 5
assert len(_FIFTY_CHAR_SPAN) == 50


# ===== boundary 1: empty =====


def test_empty_evidence_with_non_refuse_answer_bounces() -> None:
    """Empty evidence + non-refuse answer → bounce (repair_count 1).

    Per the spec: "non-refuse answer + no evidence → bounce".
    """
    result = verify_citations(
        answer="The answer is grounded in our analysis of the quarter.",
        evidence=[],
        repair_count=0,
        known_document_ids=KNOWN_IDS_DEFAULT,
    )
    assert isinstance(result, BounceResult)
    assert result.outcome == "bounce"
    assert result.repair_count == 1
    assert result.hidden_template


def test_empty_evidence_with_empty_answer_bounces() -> None:
    """Empty evidence + empty answer → bounce (cannot match ≥32 chars)."""
    result = verify_citations(
        answer="",
        evidence=[],
        repair_count=0,
        known_document_ids=KNOWN_IDS_DEFAULT,
    )
    assert isinstance(result, BounceResult)
    assert result.outcome == "bounce"
    assert result.repair_count == 1


def test_empty_answer_with_evidence_cannot_match_min_span_len() -> None:
    """A short answer cannot match the ≥32-char requirement; bounces."""
    result = verify_citations(
        answer="short",
        evidence=[_evidence("some evidence page text " * 100)],
        repair_count=0,
        known_document_ids=KNOWN_IDS_DEFAULT,
    )
    assert isinstance(result, BounceResult)
    assert result.outcome == "bounce"


# ===== boundary 2: negative =====


def test_repair_count_three_refuses_with_citation_repair_cap() -> None:
    """``repair_count == repair_cap`` → refuse ``citation_repair_cap``."""
    result = verify_citations(
        answer="No citations here, just an unsupported claim.",
        evidence=[],
        repair_count=3,
        known_document_ids=KNOWN_IDS_DEFAULT,
    )
    assert isinstance(result, RefuseResult)
    assert result.outcome == "refuse"
    assert result.reason == "citation_repair_cap"


def test_repair_count_above_cap_also_refuses() -> None:
    """``repair_count > repair_cap`` → still refuses (idempotent)."""
    result = verify_citations(
        answer="Still no citations here.",
        evidence=[],
        repair_count=10,
        known_document_ids=KNOWN_IDS_DEFAULT,
    )
    assert isinstance(result, RefuseResult)
    assert result.reason == "citation_repair_cap"


def test_negative_repair_count_is_clamped_to_zero() -> None:
    """``repair_count < 0`` is treated as 0 — bounce, not refuse.

    Defensive contract: a caller bug that leaks a negative counter
    must not accidentally cap the bounce path on the very first
    call.
    """
    result = verify_citations(
        answer="Some answer without citation.",
        evidence=[],
        repair_count=-1,
        known_document_ids=KNOWN_IDS_DEFAULT,
    )
    assert isinstance(result, BounceResult)
    assert result.repair_count == 1  # 0 + 1 after bounce


def test_known_locator_in_answer_does_not_trigger_unknown_locator() -> None:
    """A manifest ``document_id`` claim in the answer is allowed.

    The unknown-locator heuristic only flags ids that are not in
    ``known_document_ids``. The model may legitimately mention a
    known id without it being a refusal.
    """
    answer = (
        "Per " + "AAPL-2022Q3" + ", revenue grew year over year. "
        "No verbatim evidence span included in this answer."
    )
    result = verify_citations(
        answer=answer,
        evidence=[],
        repair_count=0,
        known_document_ids={"AAPL-2022Q3"},
    )
    # No evidence → no spans → bounce (NOT refuse), even though
    # AAPL-2022Q3 was claimed.
    assert isinstance(result, BounceResult)


def test_unknown_locator_in_answer_refuses_immediately() -> None:
    """An unknown manifest-shaped ``document_id`` claim refuses on
    the first call (no repair) regardless of ``repair_count``.

    Note: this is the **scoped** version of the unknown-locator
    spec test — it lives in this negative class.
    """
    result = verify_citations(
        answer="See FOO-2023Q1 page 1 for the relevant figure.",
        evidence=[],
        repair_count=2,  # mid-bounce; would normally bounce
        known_document_ids=KNOWN_IDS_DEFAULT,
    )
    assert isinstance(result, RefuseResult)
    assert result.outcome == "refuse"
    assert result.reason == "unknown_locator"


def test_q4_locator_pattern_matches_and_unknown_id_refuses() -> None:
    """``Q4`` is part of the locator regex; an unknown Q4 id still refuses."""
    result = verify_citations(
        answer="See BOGUS-2099Q4 page 5.",
        evidence=[],
        repair_count=0,
        known_document_ids=KNOWN_IDS_DEFAULT,
    )
    assert isinstance(result, RefuseResult)
    assert result.reason == "unknown_locator"


# ===== boundary 3: overflow =====


def test_overflow_10k_answer_50_evidence_completes_under_one_second() -> None:
    """10K-char answer + 50 × 1K-char evidence items → ``Done`` in <1s.

    The gate must not regress on the realistic tool-call budget:
    one run's accumulated evidence list can have dozens of pages and
    passages. Wall-clock is asserted because a slower gate would be a
    visible regression for v1's compiled graph (host graph budget is
    fin state, not gate time).
    """
    # Five unique ≥50-char matching spans. Each span lives in exactly
    # one evidence item. The longest substring shared between any two
    # spans is strictly < 32 chars (the minimum span length) so the
    # gate can only match whole spans — no shared substrings get
    # inflated into extra citations.
    match_spans: list[str] = []
    unit_texts: list[str] = []
    unique_keys = ["QQQ", "RRR", "SSS", "TTT", "UUU"]
    suffix_keys = ["AAAAA", "BBBBB", "CCCCC", "DDDDD", "EEEEE"]
    for i in range(5):
        # ~55-char span: PROBE_NN_ + UNIQUE_KEY_TOKEN_AAAAA_PADDING_HERE_OK_END_NN
        # Wrapper context is shared but only ~23 chars (< 32). Both
        # body placeholders differ per span so no two spans share
        # any ≥ 32-char substring.
        body = (
            f"UNIQUE_{unique_keys[i]}_TOKEN_{suffix_keys[i]}_PADDING_HERE_OK"
        )
        span = f"PROBE_{i:02d}_{body}_END_{i:02d}"
        assert len(span) >= 50, f"span too short: {span!r} ({len(span)})"
        match_spans.append(span)
        # Each evidence item is exactly 1K chars: prefix + span + suffix.
        # The prefix/suffix use non-alnum boundary characters so the
        # greedy match does not bleed past the span into the answer's
        # surrounding context.
        prefix = "x" * 100
        suffix_padded = "y" * (1000 - len(prefix) - len(span))
        unit_texts.append(prefix + span + suffix_padded)
    assert all(len(pt) == 1000 for pt in unit_texts)

    # Fill the rest of the 50 evidence items with unrelated content.
    while len(unit_texts) < 50:
        unit_texts.append("z" * 1000)

    # Construct the answer: contains all 5 matching spans (so Done),
    # plus 10K chars of filler to hit the overflow target. The
    # surrounding context of each span in the answer is non-alnum so
    # the greedy match stays bounded at the seeded span length.
    span_block = "<" + "><".join(match_spans) + ">"
    filler = "q" * (10_000 - len(span_block))
    answer = span_block + filler
    assert len(answer) == 10_000

    evidence = [
        _evidence(pt, unit=(i % 28) + 1) for i, pt in enumerate(unit_texts)
    ]

    t0 = time.perf_counter()
    result = verify_citations(
        answer=answer,
        evidence=evidence,
        repair_count=0,
        known_document_ids=KNOWN_IDS_DEFAULT,
    )
    elapsed = time.perf_counter() - t0

    assert isinstance(result, DoneResult), (
        f"expected Done with 5 matching spans; got {result!r}"
    )
    # Wall-clock contract: < 1 second on a reasonable test machine.
    assert elapsed < 1.0, f"gate took {elapsed:.3f}s for 10K/50K inputs"
    # Exactly the 5 matching spans we seeded — no duplicates, no
    # substring inflation.
    assert len(result.citations) == 5
    found_quotes = {c.quote for c in result.citations}
    assert found_quotes == set(match_spans)


# ===== boundary 4: concurrent =====


def test_concurrent_verify_citations_is_deterministic_across_16_calls() -> None:
    """16 parallel ``verify_citations`` calls with identical inputs
    return deeply-equal results.

    The gate is a pure function; concurrent invocation must not
    perturb its return values (e.g., through shared module state).
    """
    answer = "Per AAPL-2022Q3 page 1, " + _FIFTY_CHAR_SPAN + " is filed."
    evidence = [_evidence(_FIFTY_CHAR_SPAN + " additional page text." * 5)]

    def _call() -> GateResult:
        return verify_citations(
            answer=answer,
            evidence=evidence,
            repair_count=0,
            known_document_ids={"AAPL-2022Q3"},
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda _: _call(), range(16)))

    # All 16 calls must produce deeply-equal results.
    first = results[0]
    assert isinstance(first, DoneResult)
    for other in results[1:]:
        assert isinstance(other, DoneResult)
        assert other == first


def test_concurrent_bounce_path_is_deterministic_across_threads() -> None:
    """Concurrent calls on the bounce path are also deterministic."""
    answer = "Unsupported claim."

    def _call() -> GateResult:
        return verify_citations(
            answer=answer,
            evidence=[],
            repair_count=1,
            known_document_ids=KNOWN_IDS_DEFAULT,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda _: _call(), range(16)))

    first = results[0]
    assert isinstance(first, BounceResult)
    for other in results[1:]:
        assert isinstance(other, BounceResult)
        assert other.repair_count == first.repair_count
        assert other.hidden_template == first.hidden_template


# ===== boundary 5: exception =====


def test_empty_unit_text_raises_malformed_evidence_error() -> None:
    """Evidence with ``unit_text == ""`` raises a typed error,
    not a silent skip (ADR 0021)."""
    with pytest.raises(MalformedEvidenceError) as excinfo:
        verify_citations(
            answer="Some answer.",
            evidence=[
                _evidence("legitimate unit text"),
                _evidence("", unit=2),  # malformed
            ],
            repair_count=0,
            known_document_ids=KNOWN_IDS_DEFAULT,
        )
    assert excinfo.value.index == 1


def test_zero_unit_in_locator_raises_malformed_evidence_error() -> None:
    """``locator.unit == 0`` raises a typed error."""
    with pytest.raises(MalformedEvidenceError) as excinfo:
        verify_citations(
            answer="Some answer.",
            evidence=[_evidence("text", unit=0)],
            repair_count=0,
            known_document_ids=KNOWN_IDS_DEFAULT,
        )
    assert excinfo.value.index == 0


def test_negative_unit_in_locator_raises_malformed_evidence_error() -> None:
    """``locator.unit < 0`` raises a typed error."""
    with pytest.raises(MalformedEvidenceError):
        verify_citations(
            answer="Some answer.",
            evidence=[_evidence("text", unit=-3)],
            repair_count=0,
            known_document_ids=KNOWN_IDS_DEFAULT,
        )


def test_empty_document_id_raises_malformed_evidence_error() -> None:
    """Empty ``locator.document_id`` raises a typed error."""
    with pytest.raises(MalformedEvidenceError):
        verify_citations(
            answer="Some answer.",
            evidence=[_evidence("text", document_id="")],
            repair_count=0,
            known_document_ids=KNOWN_IDS_DEFAULT,
        )


def test_empty_version_raises_malformed_evidence_error() -> None:
    """Empty ``locator.version`` raises a typed error."""
    with pytest.raises(MalformedEvidenceError):
        verify_citations(
            answer="Some answer.",
            evidence=[_evidence("text", version="")],
            repair_count=0,
            known_document_ids=KNOWN_IDS_DEFAULT,
        )


# A model-written description of a chart: derived prose, never text a
# read or retrieve call returned, so it must not ride the quote channel.
_VISUAL_OBSERVATION = (
    "The chart shows net sales climbing through the fiscal quarter."
)


@pytest.mark.parametrize(
    "bad_item",
    [
        # A generated visual observation is not a verbatim source quote.
        EvidenceItem(
            locator=_locator(),
            unit_text=_VISUAL_OBSERVATION,
            kind=PLOTTED_VALUE_KIND,
        ),
        EvidenceItem(locator=_locator(), unit_text=""),
        EvidenceItem(locator=_locator(unit=0), unit_text="legitimate text"),
        EvidenceItem(locator=_locator(unit=-3), unit_text="legitimate text"),
        # ``bool`` is an ``int`` subclass: True is a malformed ordinal,
        # never a spelling of unit 1.
        EvidenceItem(locator=_locator(unit=True), unit_text="legitimate text"),
        EvidenceItem(locator=_locator(version=""), unit_text="legitimate text"),
    ],
    ids=[
        "visual_observation_kind",
        "empty_unit_text",
        "zero_unit",
        "negative_unit",
        "bool_unit",
        "empty_version",
    ],
)
def test_malformed_evidence_names_the_offending_index(
    bad_item: EvidenceItem,
) -> None:
    """Every malformed shape is a typed error carrying ITS index, so a
    run log points at the broken evidence row instead of hiding it."""
    with pytest.raises(MalformedEvidenceError) as excinfo:
        verify_citations(
            answer="Some answer.",
            evidence=[_evidence("legitimate unit text"), bad_item],
            repair_count=0,
            known_document_ids=KNOWN_IDS_DEFAULT,
        )
    assert excinfo.value.index == 1


def test_malformed_evidence_error_is_a_citation_gate_error() -> None:
    """``MalformedEvidenceError`` subclasses ``CitationGateError`` so
    the host graph can catch the whole family with one ``except``
    clause (T6 contract)."""
    assert issubclass(MalformedEvidenceError, CitationGateError)


# ===== spec acceptance scenario 1: bounce then refuse on cap =====


def test_no_evidence_non_refuse_answer_bounces_then_refuses() -> None:
    """Spec scenario: a non-refuse answer with no evidence bounces
    three times, then refuses with reason ``citation_repair_cap``.

    Mirrors ``specs/v1-host-agent.md`` success criteria 4.
    """
    answer = (
        "Revenue grew year over year based on macro tailwinds. "
        "No verbatim evidence span included."
    )

    # repair_count 0/1/2 → bounce.
    r0 = verify_citations(
        answer=answer,
        evidence=[],
        repair_count=0,
        known_document_ids=KNOWN_IDS_DEFAULT,
    )
    assert isinstance(r0, BounceResult)
    assert r0.repair_count == 1

    r1 = verify_citations(
        answer=answer,
        evidence=[],
        repair_count=1,
        known_document_ids=KNOWN_IDS_DEFAULT,
    )
    assert isinstance(r1, BounceResult)
    assert r1.repair_count == 2

    r2 = verify_citations(
        answer=answer,
        evidence=[],
        repair_count=2,
        known_document_ids=KNOWN_IDS_DEFAULT,
    )
    assert isinstance(r2, BounceResult)
    assert r2.repair_count == 3

    # repair_count 3 → refuse.
    r3 = verify_citations(
        answer=answer,
        evidence=[],
        repair_count=3,
        known_document_ids=KNOWN_IDS_DEFAULT,
    )
    assert isinstance(r3, RefuseResult)
    assert r3.reason == "citation_repair_cap"


# ===== spec acceptance scenario 2: evidenced span → Done =====


def test_evidenced_span_ends_done_with_matching_citations() -> None:
    """Spec scenario: an answer containing a verbatim ≥32-char span
    from an evidence unit ends ``done`` with citations equal to
    exactly that span + locator.

    Mirrors ``specs/v1-host-agent.md`` success criteria 5.

    The fixture uses non-space separators around the evidence span so
    the gate's greedy longest-match does not bleed into the answer's
    surrounding whitespace; the spec test asserts the citation quote
    is exactly the seeded 50-char span.
    """
    # 50-char verbatim span from evidence.
    span = _FIFTY_CHAR_SPAN
    answer = (
        "The SEC filing opens with: " + span + " as the cover header."
    )
    # Underscore separator in evidence vs space separator in answer:
    # the gate's match terminates at the span boundary, leaving the
    # quote at exactly 50 chars.
    evidence = [_evidence("PREAMBLE_" + span + "_POSTAMBLE_END")]

    result = verify_citations(
        answer=answer,
        evidence=evidence,
        repair_count=0,
        known_document_ids={"AAPL-2022Q3"},
    )

    assert isinstance(result, DoneResult)
    assert result.outcome == "done"
    assert result.answer == answer
    assert len(result.citations) == 1
    c = result.citations[0]
    assert c.kind == COMPANY_KIND
    assert c.document_id == "AAPL-2022Q3"
    assert c.version == _SHA
    assert c.unit == 1
    assert c.quote == span


# ===== spec acceptance scenario 3: unknown locator → refuse immediately =====


def test_unknown_locator_in_answer_refuses_immediately_no_repair() -> None:
    """Spec scenario: an answer claiming a ``document_id`` not in
    the frozen corpus refuses immediately (no repair, regardless of
    ``repair_count``).

    Mirrors ``specs/v1-host-agent.md`` success criteria 6.
    """
    result = verify_citations(
        answer="See FOO-2023Q1 page 1 for the figure.",
        evidence=[],
        repair_count=0,
        known_document_ids=KNOWN_IDS_DEFAULT,
    )
    assert isinstance(result, RefuseResult)
    assert result.outcome == "refuse"
    assert result.reason == "unknown_locator"


def test_unknown_locator_refuses_at_high_repair_count_too() -> None:
    """Spec scenario: even with ``repair_count > 0`` (mid-bounce),
    an unknown-locator claim still refuses with
    ``unknown_locator`` (not ``citation_repair_cap``)."""
    result = verify_citations(
        answer="See FOO-2023Q1 page 1.",
        evidence=[],
        repair_count=2,
        known_document_ids=KNOWN_IDS_DEFAULT,
    )
    assert isinstance(result, RefuseResult)
    assert result.reason == "unknown_locator"


# ===== spec acceptance scenario 4: done path does not increment =====


def test_done_path_does_not_increment_repair_count() -> None:
    """The Done path is non-mutating: passing ``repair_count = 2``
    then bouncing twice, then succeeding, must leave the Done call's
    ``repair_count`` input unchanged in the next call's logic
    (i.e., the gate does not increment on Done).

    Verified by exercising the Done path with ``repair_count = 0``,
    ``1``, ``2`` — every input must return Done (because the
    evidence supports it) without the result leaking any
    counter state.
    """
    span = _FIFTY_CHAR_SPAN
    answer = "Header: " + span + "."
    evidence = [_evidence(span + " more page text." * 5)]

    for rc in (0, 1, 2):
        result = verify_citations(
            answer=answer,
            evidence=evidence,
            repair_count=rc,
            known_document_ids={"AAPL-2022Q3"},
        )
        assert isinstance(result, DoneResult), (
            f"repair_count={rc} should still produce Done when evidence "
            f"supports the answer; got {result!r}"
        )
        assert len(result.citations) == 1


# ===== spec acceptance scenario 5: hidden template contract =====


def test_bounce_template_is_hidden_string() -> None:
    """The bounce template is a neutral instruction: it does NOT
    contain "error" or "failed" framing, and it does not leak the
    bounce-counter details the user should never see.

    The template is data — T6 injects it into the model's next-turn
    system message; T7 (CLI) and T9 (HTTP) render only
    ``DoneResult.answer`` and ``DoneResult.citations``.
    """
    result = verify_citations(
        answer="An answer without citation.",
        evidence=[],
        repair_count=0,
        known_document_ids=KNOWN_IDS_DEFAULT,
    )
    assert isinstance(result, BounceResult)
    template = result.hidden_template
    assert template, "hidden_template must be a non-empty string"
    # No alarming framing — the template is a neutral re-instruction.
    assert "error" not in template.lower()
    assert "failed" not in template.lower()
    # Mentions the 32-char minimum so the model knows what to fix.
    assert "32-character" in template or "32 character" in template.lower()


# ===== regression tests =====


def test_citation_dataclass_is_frozen_and_hashable() -> None:
    """``Citation`` is a frozen dataclass (host graph may use as dict key)."""
    c = Citation(
        kind=COMPANY_KIND,
        document_id="AAPL-2022Q3",
        attachment_id="",
        version=_SHA,
        unit=1,
        display="page",
        filename="",
        quote="verbatim span of at least thirty-two characters here",
    )
    with pytest.raises(Exception):
        # Frozen dataclass → mutation raises FrozenInstanceError.
        c.document_id = "OTHER"  # type: ignore[misc]
    # Hashable for use as a dict / set key.
    s = {c}
    assert c in s


def test_done_result_is_frozen() -> None:
    """Result types are frozen: callers cannot mutate a returned result."""
    result = verify_citations(
        answer="Header: " + _FIFTY_CHAR_SPAN + ".",
        evidence=[_evidence(_FIFTY_CHAR_SPAN + " rest of the page text.")],
        repair_count=0,
        known_document_ids={"AAPL-2022Q3"},
    )
    assert isinstance(result, DoneResult)
    with pytest.raises(Exception):
        result.outcome = "bounce"  # type: ignore[misc]


def test_duplicate_citations_across_evidence_items_are_deduplicated() -> None:
    """If the same span appears in two evidence items, dedup keeps one
    ``Citation`` per unique ``(locator, quote)`` pair.

    Note: two different evidence items have different locators, so
    dedup happens by ``quote`` only when the locator is also equal
    (e.g., the same unit retrieved twice)."""
    span = _FIFTY_CHAR_SPAN
    answer = "Header: " + span + "."
    # Two evidence items with the SAME locator (e.g., the same unit
    # retrieved via two tool calls).
    evidence = [
        _evidence(span + " rest A"),
        _evidence(span + " rest B"),
    ]
    result = verify_citations(
        answer=answer,
        evidence=evidence,
        repair_count=0,
        known_document_ids={"AAPL-2022Q3"},
    )
    assert isinstance(result, DoneResult)
    # Two distinct locators → two citations (locator differs).
    # The dedup rule is per ``(locator, quote)``, so identical
    # (locator, quote) pairs would collapse; here locators are equal
    # AND quotes are equal, so we get exactly 1.
    assert len(result.citations) == 1


def test_different_quotes_from_same_locator_are_separate_citations() -> None:
    """Two distinct matching spans from the same unit = two citations."""
    span_a = "ALPHANUMERIC_HEADER_OF_AT_LEAST_THIRTYTWO_CHARS_AAAAA"
    span_b = "ALPHANUMERIC_HEADER_OF_AT_LEAST_THIRTYTWO_CHARS_BBBBB"
    assert len(span_a) >= 50
    assert len(span_b) >= 50

    answer = "First: " + span_a + ". Second: " + span_b + "."
    evidence = [_evidence(span_a + " ... " + span_b)]

    result = verify_citations(
        answer=answer,
        evidence=evidence,
        repair_count=0,
        known_document_ids={"AAPL-2022Q3"},
    )
    assert isinstance(result, DoneResult)
    assert len(result.citations) == 2


def test_punctuation_only_match_is_ignored() -> None:
    """A span with no alphanumeric token is ignored (ADR 0021)."""
    punctuation = "." * 50
    answer = "Header: " + punctuation + "."
    result = verify_citations(
        answer=answer,
        evidence=[_evidence(punctuation + " trailing page text")],
        repair_count=0,
        known_document_ids={"AAPL-2022Q3"},
    )
    # The punctuation-only span is ignored → no spans pass → bounce.
    assert isinstance(result, BounceResult)


def test_whitespace_only_match_is_ignored() -> None:
    """A whitespace-only span is ignored."""
    whitespace = " " * 50
    answer = "Header: " + whitespace + "."
    result = verify_citations(
        answer=answer,
        evidence=[_evidence(whitespace + " trailing page text")],
        repair_count=0,
        known_document_ids={"AAPL-2022Q3"},
    )
    assert isinstance(result, BounceResult)


def test_min_span_len_kwarg_is_honored() -> None:
    """``min_span_len`` overrides the default 32 — verify the kwarg works.

    The fixture uses a non-space suffix in the evidence so the
    greedy match terminates at the 33-char span boundary; otherwise
    the algorithm could include the answer's trailing space and the
    match would reach the higher ``min_span_len=34`` threshold,
    masking the kwarg's effect.
    """
    # 33-char span.
    short_span = "ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567"  # 33 chars
    assert len(short_span) == 33
    answer = "Header: " + short_span + " end."
    evidence = [_evidence(short_span + "!_rest_of_page")]

    # Default min_span_len=32 → matches the 33-char span.
    r_default = verify_citations(
        answer=answer,
        evidence=evidence,
        repair_count=0,
        known_document_ids={"AAPL-2022Q3"},
    )
    assert isinstance(r_default, DoneResult)

    # min_span_len=34 → the 33-char span is too short → bounce.
    r_strict = verify_citations(
        answer=answer,
        evidence=evidence,
        repair_count=0,
        known_document_ids={"AAPL-2022Q3"},
        min_span_len=34,
    )
    assert isinstance(r_strict, BounceResult)


# ===== returned-slice continuity: adjacent joins, a gap never does =====


def test_quote_spanning_two_adjacent_returned_slices_passes() -> None:
    """One unit read in two calls whose offsets touch is ONE matchable
    text: a span copied verbatim across the boundary is grounded.

    Neither returned slice alone contains the span (20 characters of
    each), so this can only pass by joining on
    ``prev.next_position == next.position``.
    """
    first, second = "A" * 40, "B" * 40
    span = "A" * 20 + "B" * 20
    assert len(span) == 40
    answer = f"Across the boundary: {span} end."
    evidence = [
        _slice_evidence(first, position=0, next_position=40),
        _slice_evidence(second, position=40, next_position=None),
    ]

    result = verify_citations(
        answer=answer,
        evidence=evidence,
        repair_count=0,
        known_document_ids=KNOWN_IDS_DEFAULT,
    )

    assert isinstance(result, DoneResult), result
    assert len(result.citations) == 1
    citation = result.citations[0]
    assert citation.quote == span
    assert citation.unit == 7
    assert citation.version == _SHA


@pytest.mark.parametrize(
    "evidence",
    [
        # The run read 0..40 and 60..100: code points 40..59 are a hole
        # no returned slice covers, so the two texts must not be joined.
        [
            _slice_evidence("A" * 40, position=0, next_position=40),
            _slice_evidence("B" * 40, position=60, next_position=None),
        ],
        # The first slice declares no continuation point, so even a
        # touching offset proves nothing.
        [
            _slice_evidence("A" * 40, position=0, next_position=None),
            _slice_evidence("B" * 40, position=40, next_position=None),
        ],
    ],
    ids=["unseen_gap", "no_continuation_offset"],
)
def test_quote_spanning_an_unseen_gap_never_cites(
    evidence: list[EvidenceItem],
) -> None:
    """A quote that bridges text this run never received is not grounded:
    the answer bounces, and at the repair cap it refuses — the same
    repair rules every ungrounded answer gets."""
    span = "A" * 20 + "B" * 20
    answer = f"Across the boundary: {span} end."

    result = verify_citations(
        answer=answer,
        evidence=evidence,
        repair_count=0,
        known_document_ids=KNOWN_IDS_DEFAULT,
    )
    assert isinstance(result, BounceResult), result
    assert result.repair_count == 1

    capped = verify_citations(
        answer=answer,
        evidence=evidence,
        repair_count=3,
        known_document_ids=KNOWN_IDS_DEFAULT,
    )
    assert isinstance(capped, RefuseResult), capped
    assert capped.reason == "citation_repair_cap"


def test_retrieve_chunks_never_bridge_into_a_longer_span() -> None:
    """Two passage chunks of the SAME unit are not read offsets.

    A retrieve chunk carries no continuation point, so it can never join
    with its neighbour: a span stitched across two chunks the run only
    saw separately stays ungrounded, while a chunk that really contains
    the span still cites.
    """
    stitched = "A" * 20 + "B" * 20
    chunks = [_evidence("A" * 20), _evidence("B" * 20)]

    ungrounded = verify_citations(
        answer=f"Across the passages: {stitched} end.",
        evidence=chunks,
        repair_count=0,
        known_document_ids=KNOWN_IDS_DEFAULT,
    )
    assert isinstance(ungrounded, BounceResult), ungrounded

    grounded = verify_citations(
        answer=f"Across the passages: {stitched} end.",
        evidence=[_evidence(stitched)],
        repair_count=0,
        known_document_ids=KNOWN_IDS_DEFAULT,
    )
    assert isinstance(grounded, DoneResult), grounded


def test_quote_that_lives_only_in_never_returned_text_never_cites() -> None:
    """The gate matches what the tool RETURNED this run, not what the
    answer claims to have read: the unread half of the same unit cannot
    ground a span, while the returned half still can."""
    returned, unread = "A" * 40, "B" * 40
    evidence = [_slice_evidence(returned, position=0, next_position=40)]

    ungrounded = verify_citations(
        answer=f"Later in the unit: {unread} end.",
        evidence=evidence,
        repair_count=0,
        known_document_ids=KNOWN_IDS_DEFAULT,
    )
    assert isinstance(ungrounded, BounceResult), ungrounded

    grounded = verify_citations(
        answer=f"Earlier in the unit: {returned} end.",
        evidence=evidence,
        repair_count=0,
        known_document_ids=KNOWN_IDS_DEFAULT,
    )
    assert isinstance(grounded, DoneResult), grounded
    assert [c.quote for c in grounded.citations] == [returned]


# ===== the closed quotable kinds and the attachment trust re-check =====


def test_quotable_evidence_kinds_are_closed() -> None:
    """Only the two read legs feed the quote channel. A generated visual
    observation is derived prose, never a verbatim source quote, so its
    kind is outside the set by construction."""
    assert QUOTABLE_EVIDENCE_KINDS == frozenset({COMPANY_KIND, ATTACHMENT_KIND})
    assert PLOTTED_VALUE_KIND not in QUOTABLE_EVIDENCE_KINDS


_MEMO_SPAN = "the purchase price in the attached memo is twelve million"
assert len(_MEMO_SPAN) >= 32


def _memo_evidence(**kwargs: object) -> EvidenceItem:
    """Attachment evidence whose text contains ``_MEMO_SPAN`` verbatim."""
    return _attachment_evidence(
        "PREAMBLE_" + _MEMO_SPAN + "_POSTAMBLE", **kwargs  # type: ignore[arg-type]
    )


def test_attachment_quote_cites_when_the_identity_is_attested() -> None:
    """With the caller's trust re-check satisfied, the attachment slice
    grounds the span exactly like a company slice does."""
    result = verify_citations(
        answer=f"Per the memo: {_MEMO_SPAN} end.",
        evidence=[_memo_evidence()],
        repair_count=0,
        known_document_ids=set(),
        attachment_identity_current=lambda item: True,
    )

    assert isinstance(result, DoneResult), result
    assert [c.attachment_id for c in result.citations] == ["att-1"]


@pytest.mark.parametrize(
    "attest",
    [lambda item: False, None],
    ids=["identity_rejected", "no_callback_fail_closed"],
)
def test_unattested_attachment_quote_never_cites(
    attest: object,
) -> None:
    """An attachment item nobody attests cannot carry a quote even when
    its text matches verbatim: a stale / foreign identity, and a run with
    no attester at all, both stay uncitable (fail-closed)."""
    result = verify_citations(
        answer=f"Per the memo: {_MEMO_SPAN} end.",
        evidence=[_memo_evidence()],
        repair_count=0,
        known_document_ids=set(),
        attachment_identity_current=attest,  # type: ignore[arg-type]
    )

    assert isinstance(result, BounceResult), result


@pytest.mark.parametrize(
    "filename, display",
    [("brief.pdf", "page"), ("notes.txt", "unit")],
    ids=["pdf_presents_as_page", "text_presents_as_unit"],
)
def test_attachment_citation_carries_identity_filename_and_display(
    filename: str, display: str
) -> None:
    """The citation the user sees is the matched item's identity —
    attachment id, version, unit, the safe display name, and how that
    unit is presented — never the model's claim and never a company
    ``document_id``."""
    item = _memo_evidence(
        attachment_id="att-9", version="3", unit=2, filename=filename,
        display=display,
    )

    result = verify_citations(
        answer=f"Per the memo: {_MEMO_SPAN} end.",
        evidence=[item],
        repair_count=0,
        known_document_ids=set(),
        attachment_identity_current=lambda every: True,
    )

    assert isinstance(result, DoneResult), result
    assert len(result.citations) == 1
    citation = result.citations[0]
    assert citation.kind == ATTACHMENT_KIND
    assert citation.document_id == ""
    assert citation.attachment_id == "att-9"
    assert citation.version == "3"
    assert citation.unit == 2
    assert citation.filename == filename
    assert citation.display == display
    assert citation.quote == _MEMO_SPAN


# ===== citation gate module: purity / no-pdfplumber guards =====


def _assert_no_pdfplumber(source_path: Path) -> None:
    """AST scan to ensure ``source_path`` does not import ``pdfplumber``
    (ADR 0020: the ask path must not open a PDF)."""
    tree = ast.parse(source_path.read_text())
    offending: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == "pdfplumber":
                    offending.append(f"line {node.lineno}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module.split(".")[0] == "pdfplumber":
                offending.append(
                    f"line {node.lineno}: from {node.module} import ..."
                )
    assert offending == [], (
        f"{source_path.name} must not import pdfplumber; offending: {offending}"
    )


def test_citation_gate_module_does_not_import_pdfplumber() -> None:
    """The citation gate is pure logic — it must never touch
    pdfplumber (ADR 0020: no PDF on the ask path)."""
    gate_path = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "fin"
        / "host"
        / "citation_gate.py"
    )
    _assert_no_pdfplumber(gate_path)