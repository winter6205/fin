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
from fin.host.attachment.attachment_observations import PLOTTED_VALUE_KIND
from fin.host.grounded.citation_gate import (
    BounceResult,
    COMPANY_KIND,
    Citation,
    CitationGateError,
    DATABASE_ROW_DISPLAY,
    DATABASE_ROW_KIND,
    DatabaseRow,
    DoneResult,
    EvidenceItem,
    GateResult,
    MalformedEvidenceError,
    QUOTABLE_EVIDENCE_KINDS,
    RefuseResult,
    UNSUPPORTED_DATABASE_CLAIM,
    format_database_row_source_id,
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
    """Only the two read legs and the ledger row feed the quote channel.
    A generated visual observation is derived prose, never a verbatim
    source quote, so its kind is outside the set by construction."""
    assert QUOTABLE_EVIDENCE_KINDS == frozenset(
        {COMPANY_KIND, ATTACHMENT_KIND, DATABASE_ROW_KIND}
    )
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
        / "grounded"
        / "citation_gate.py"
    )
    _assert_no_pdfplumber(gate_path)


# ===== the row on the shared wire shaper =====


def test_a_verified_row_survives_onto_the_wire_as_its_snapshot() -> None:
    """The gate's citation becomes the stored dict the ``/ask`` response,
    the workspace SSE frame and the turn table all read, so the row's
    evidence has to travel with it. Without the snapshot those three
    would offer a citation no client could open without re-querying the
    ledger — the one thing opening a row may never do (spec 8.1).
    """
    from fin.host.run_payload import _run_to_response_payload

    row = _contract_row()
    result = verify_citations(
        answer=f"{row.summary}. Notice period 30 days.",
        evidence=[_row_evidence(row)],
        repair_count=0,
        known_document_ids=set(),
    )
    assert isinstance(result, DoneResult), result

    payload = _run_to_response_payload(
        {
            "status": "done",
            "answer": result.answer,
            "citations": result.citations,
        }
    )

    assert payload["citations"] == [
        {
            "kind": DATABASE_ROW_KIND,
            "document_id": "",
            "attachment_id": "",
            "version": "2026-03-01T09:14:22Z",
            "unit": 1,
            "display": DATABASE_ROW_DISPLAY,
            "filename": "",
            "quote": row.summary,
            "database_row": {
                "source": "ledger_ro.contract_expiry",
                "record_key": {"contract_id": "C-2291"},
                "queried_at": "2026-03-01T09:14:22Z",
                "fields": {
                    "counterparty": "Northwind Logistics",
                    "expiry_date": "2026-03-14",
                    "notice_days": "30",
                    "notice_by": "2026-02-12",
                },
                "summary": row.summary,
            },
        }
    ]


def test_a_document_citation_gains_no_key_on_the_wire() -> None:
    """The shared shaper OMITS the snapshot key for the two document
    kinds, so every citation row already in ``fin_run.citations`` is
    byte-equal to what the surfaces publish today. The key is absent,
    not ``None``: a stored row gaining a null field is still a
    migration every historical thread would need."""
    from fin.host.run_payload import _run_to_response_payload

    result = verify_citations(
        answer=f"Per the filing: {_MEMO_SPAN} end.",
        evidence=[_evidence("PREAMBLE_" + _MEMO_SPAN + "_POSTAMBLE")],
        repair_count=0,
        known_document_ids=KNOWN_IDS_DEFAULT,
    )
    assert isinstance(result, DoneResult), result

    payload = _run_to_response_payload(
        {
            "status": "done",
            "answer": result.answer,
            "citations": result.citations,
        }
    )

    assert set(payload["citations"][0]) == {
        "kind",
        "document_id",
        "attachment_id",
        "version",
        "unit",
        "display",
        "filename",
        "quote",
    }


# ===== the ledger row: a second verification rule behind one closed set =====
#
# ADR 0091 / spec 8: a row returned by the ``sql`` 档 is not a document
# passage. It verifies by reference to its ``record_key`` and the
# ``fields`` THIS ROUND returned, so the 32-code-point span rule does
# not apply to it. The asymmetry is deliberate and is not an exception
# to be cleaned up: a scalar ledger value is a real fact and a window
# around it proves nothing.

# The canonical row from spec 8, so the fixture and the decision it
# records cannot drift apart.
_CONTRACT_ROW_WIRE = {
    "kind": "database-row",
    "source": "ledger_ro.contract_expiry",
    "record_key": {"contract_id": "C-2291"},
    "queried_at": "2026-03-01T09:14:22Z",
    "fields": {
        "counterparty": "Northwind Logistics",
        "expiry_date": "2026-03-14",
        "notice_days": 30,
        "notice_by": "2026-02-12",
    },
    "summary": (
        "C-2291 Northwind Logistics, expires 2026-03-14, "
        "notice by 2026-02-12"
    ),
}


def _contract_row(**overrides: object) -> DatabaseRow:
    """The spec's own contract row, one field overridable."""
    wire = {**_CONTRACT_ROW_WIRE, **overrides}
    return DatabaseRow.from_wire(wire)


def _row_evidence(*rows: DatabaseRow, **overrides: object) -> EvidenceItem:
    """Evidence carrying ledger rows and no document identity.

    ``unit_text`` is the display line the model was shown, so the row
    is quotable the way a retrieved passage is: the model received it
    this round and may repeat it. ``locator`` is a row's own — empty
    document id, no manifest version, and no ordinal to bridge on.
    """
    fields: dict[str, object] = {
        # A row is not a page of a frozen file: the document slots stay
        # empty and there is no manifest version to state.
        "locator": Locator(document_id="", version="", unit=1),
        "unit_text": rows[0].summary,
        "kind": DATABASE_ROW_KIND,
        "display": DATABASE_ROW_DISPLAY,
        "database_row": rows,
    }
    fields.update(overrides)
    return EvidenceItem(**fields)  # type: ignore[arg-type]


def test_a_supported_ledger_figure_cites_without_any_span() -> None:
    """A ``notice_days`` of ``30`` is a legitimate citation.

    The answer quotes the row's display line and states a scalar. There
    is no ≥32-character span anywhere in it, and it does not need one:
    the row verifies by its record key and this round's returned fields
    (ADR 0091), so the span rule is scoped to document kinds and a
    scalar ledger fact stays citable.
    """
    result = verify_citations(
        answer=(
            f"{_CONTRACT_ROW_WIRE['summary']}. The notice period is "
            f"{_CONTRACT_ROW_WIRE['fields']['notice_days']} days."
        ),
        evidence=[_row_evidence(_contract_row())],
        repair_count=0,
        known_document_ids=set(),
    )

    assert isinstance(result, DoneResult), result
    assert len(result.citations) == 1
    citation = result.citations[0]
    assert citation.kind == DATABASE_ROW_KIND
    # The identity is the row's own: the view that answered, the record
    # it answered about, and when it was true.
    assert citation.database_source == "ledger_ro.contract_expiry"
    assert dict(citation.database_record_key) == {"contract_id": "C-2291"}
    assert citation.database_source_id == format_database_row_source_id(
        "ledger_ro.contract_expiry", {"contract_id": "C-2291"}
    )
    # A row opens through neither document id slot, and its version is
    # its time: a ledger value is true as of the query, not of a frozen
    # file.
    assert citation.document_id == ""
    assert citation.attachment_id == ""
    assert citation.version == "2026-03-01T09:14:22Z"
    assert citation.display == DATABASE_ROW_DISPLAY
    # The fields are carried whole so the citation can show the snapshot
    # it was built from without issuing a query.
    assert dict(citation.database_fields)["notice_days"] == "30"
    # The quote is the row's own display line, not a window of the
    # model's prose: a document citation echoes the span it matched and
    # a ledger row has no such span.
    assert citation.quote == _CONTRACT_ROW_WIRE["summary"]


def test_the_span_rule_does_not_apply_to_a_database_row() -> None:
    """The asymmetry is asserted as a PROPERTY of this kind, not as an
    accident: the same answer and the same evidence are bounced when the
    figure is a document passage and accepted when it is a row."""
    answer = "The notice period is 30 days."
    assert len(answer) < 32, "this answer must be shorter than one span"

    document = verify_citations(
        answer=answer,
        evidence=[_evidence("The notice period is 30 days per the filing.")],
        repair_count=0,
        known_document_ids=KNOWN_IDS_DEFAULT,
    )
    row = verify_citations(
        answer=answer,
        evidence=[_row_evidence(_contract_row())],
        repair_count=0,
        known_document_ids=set(),
    )

    assert isinstance(document, BounceResult), document
    assert isinstance(row, DoneResult), row


def test_figures_supported_by_different_rows_together_pass_the_subset_check() -> None:
    """The SUBSET rule is per-token, not per-row-identity: a figure one
    row returned and a figure another row returned are both returned
    values this round, and a fully supported multi-figure answer must
    still complete (and both rows cite what they each state)."""
    second = DatabaseRow.from_wire(
        {
            **_CONTRACT_ROW_WIRE,
            "record_key": {"contract_id": "C-3407"},
            "fields": {
                "counterparty": "Vertex Freight",
                "expiry_date": "2026-04-30",
                "notice_days": 45,
                "notice_by": "2026-03-16",
            },
            "summary": (
                "C-3407 Vertex Freight, expires 2026-04-30, "
                "notice by 2026-03-16"
            ),
        }
    )
    result = verify_citations(
        answer="One notice period is 30 days; the other is 45 days.",
        evidence=[
            _row_evidence(_contract_row()),
            _row_evidence(second),
        ],
        repair_count=0,
        known_document_ids=set(),
    )

    assert isinstance(result, DoneResult), result
    assert [c.database_source_id for c in result.citations] == [
        format_database_row_source_id(
            "ledger_ro.contract_expiry", {"contract_id": "C-2291"}
        ),
        format_database_row_source_id(
            "ledger_ro.contract_expiry", {"contract_id": "C-3407"}
        ),
    ]


def test_a_number_supported_by_no_row_refuses_beside_supported_ones() -> None:
    """Even with two rows in the round, a figure NEITHER returned
    refuses: the union of returned numbers is the whole allowlist for
    this answer's claims (spec §8)."""
    result = verify_citations(
        answer="One notice period is 30 days; the penalty is 777 days.",
        evidence=[_row_evidence(_contract_row())],
        repair_count=0,
        known_document_ids=set(),
    )

    assert isinstance(result, RefuseResult), result
    assert result.reason == UNSUPPORTED_DATABASE_CLAIM


def test_an_unsupported_figure_is_refused_on_its_own_reason() -> None:
    """A number no returned field supports is an unsupported claim, and
    it refuses on its own reason — never as a bounce and never as a
    missing locator.

    The distinction is the product fact: a bounce says "cite
    something", and there is nothing here to cite, so a repair loop
    would only produce another invented figure. ``unknown_locator``
    would say a filing identity is wrong, and none is. Both wrong copies
    would send the operator after the wrong fix.
    """
    result = verify_citations(
        answer=(
            f"{_CONTRACT_ROW_WIRE['summary']}. The notice period is "
            f"{99} days."
        ),
        evidence=[_row_evidence(_contract_row())],
        repair_count=0,
        known_document_ids=set(),
    )

    assert isinstance(result, RefuseResult), result
    assert result.reason == UNSUPPORTED_DATABASE_CLAIM
    assert result.reason != "unknown_locator"
    assert result.reason != "citation_repair_cap"


def test_an_unsupported_figure_refuses_at_repair_count_zero() -> None:
    """The refusal is immediate and does not consume the repair budget:
    a repair loop cannot produce a figure the ledger never returned, so
    spending three bounces on it would only delay the same answer."""
    result = verify_citations(
        answer="The notice period is 99 days.",
        evidence=[_row_evidence(_contract_row())],
        repair_count=0,
        known_document_ids=set(),
    )

    assert isinstance(result, RefuseResult), result
    assert result.reason == UNSUPPORTED_DATABASE_CLAIM


def test_a_refused_figure_and_a_missing_locator_are_distinguishable() -> None:
    """The two terminal failures answer different product facts, so each
    is reachable from an answer the other rule would have allowed."""
    unknown_filing = verify_citations(
        answer="Per AAPL-2022Q3, revenue rose.",
        evidence=[_row_evidence(_contract_row())],
        repair_count=0,
        known_document_ids=set(),
    )
    unsupported_figure = verify_citations(
        answer="The notice period is 99 days.",
        evidence=[_row_evidence(_contract_row())],
        repair_count=0,
        known_document_ids=set(),
    )

    assert isinstance(unknown_filing, RefuseResult), unknown_filing
    assert unknown_filing.reason == "unknown_locator"
    assert isinstance(unsupported_figure, RefuseResult), unsupported_figure
    assert unsupported_figure.reason == UNSUPPORTED_DATABASE_CLAIM
    assert unknown_filing.reason != unsupported_figure.reason


def test_an_answer_with_no_figure_and_no_quoted_row_still_bounces() -> None:
    """An answer that states no number and quotes no row's display line
    cited nothing this round. That is the ordinary bounce, and it keeps
    counting toward the ordinary repair cap — the unsupported-claim
    refusal needs a figure to be unsupported."""
    answer = "The contract is expiring soon, per the operations ledger."

    bouncing = verify_citations(
        answer=answer,
        evidence=[_row_evidence(_contract_row())],
        repair_count=0,
        known_document_ids=set(),
    )
    refused = verify_citations(
        answer=answer,
        evidence=[_row_evidence(_contract_row())],
        repair_count=3,
        known_document_ids=set(),
    )

    assert isinstance(bouncing, BounceResult), bouncing
    assert bouncing.repair_count == 1
    assert isinstance(refused, RefuseResult), refused
    assert refused.reason == "citation_repair_cap"


def test_a_field_value_never_supports_a_different_number() -> None:
    """A field of ``30`` supports a claim of 30 and nothing else: the
    check is on the number, not on a substring of the answer."""
    for claim in (30, 99, 3, 300, 0):
        result = verify_citations(
            answer=f"The notice period is {claim} days.",
            evidence=[_row_evidence(_contract_row())],
            repair_count=0,
            known_document_ids=set(),
        )
        if claim == 30:
            assert isinstance(result, DoneResult), (claim, result)
        else:
            assert isinstance(result, RefuseResult), (claim, result)
            assert result.reason == UNSUPPORTED_DATABASE_CLAIM


def test_a_record_key_never_supports_a_number_it_does_not_contain() -> None:
    """A counterparty reference shaped like a filing locator
    (``ACME-2024Q1``) is an ordinary ledger VALUE, and a row's own key
    supports the numbers it actually contains.

    This is the case that motivated the separate rule: a
    ``field=value`` text line fed to the document locator regex read a
    normal ledger value as a forged filing locator and refused the whole
    run. A row is verified against its own returned values, so no
    ledger value can trip the filing-identity heuristic.
    """
    row = _contract_row(
        source="ledger_ro.contract_expiry",
        record_key={"contract_id": "ACME-2024Q1"},
    )

    result = verify_citations(
        answer="ACME-2024Q1 expires 2026-03-14, notice by 2026-02-12.",
        evidence=[_row_evidence(row)],
        repair_count=0,
        known_document_ids=set(),
    )

    assert isinstance(result, DoneResult), result
    assert [c.kind for c in result.citations] == [DATABASE_ROW_KIND]


def test_a_document_span_never_stands_in_for_an_unsupported_figure() -> None:
    """The two rules never borrow each other's evidence. A run that saw
    both a ledger row and a filing passage refuses the unsupported
    figure even though a document span grounds some part of the answer
    — otherwise an ungrounded number could always buy itself a citation
    with any unrelated document text.
    """
    result = verify_citations(
        answer=(
            "Total net sales increased during the third quarter of 2022. "
            "The notice period is 99 days."
        ),
        evidence=[
            _evidence(
                "Total net sales increased during the third quarter of 2022."
            ),
            _row_evidence(_contract_row()),
        ],
        repair_count=0,
        known_document_ids=KNOWN_IDS_DEFAULT,
    )

    assert isinstance(result, RefuseResult), result
    assert result.reason == UNSUPPORTED_DATABASE_CLAIM


def test_a_ledger_figure_and_a_document_span_cite_both_kinds() -> None:
    """An answer that rests on a row AND on a document passage produces
    one citation of each kind, rather than one kind silently absorbing
    the other.

    The passage prose states NO figure on purpose: since the subset
    rule, a number the answer states must be a number a row returned
    (spec §8, and the sibling pin
    :func:`test_a_document_span_never_stands_in_for_an_unsupported_figure`
    refuses the alternative), so "a figure inside the document span"
    is not an input this acceptance may use."""
    result = verify_citations(
        answer=(
            "Total net sales increased during the third quarter. "
            f"{_CONTRACT_ROW_WIRE['summary']}. Notice period 30 days."
        ),
        evidence=[
            _evidence("Total net sales increased during the third quarter."),
            _row_evidence(_contract_row()),
        ],
        repair_count=0,
        known_document_ids=KNOWN_IDS_DEFAULT,
    )

    assert isinstance(result, DoneResult), result
    assert [c.kind for c in result.citations] == [
        DATABASE_ROW_KIND,
        COMPANY_KIND,
    ]


def test_the_document_and_attachment_legs_are_unchanged_by_the_row_rule() -> None:
    """Adding a third kind changed nothing for the two document kinds:
    the same evidence, the same answers and the same outcomes as before.

    The row branch is selected by WHAT the evidence is, so a run that
    produced no row cannot enter it — this asserts that directly rather
    than by reading the branch.
    """
    answer = f"Per the memo: {_MEMO_SPAN} end."

    company = verify_citations(
        answer=f"Per the filing: {_MEMO_SPAN} end.",
        evidence=[_evidence("PREAMBLE_" + _MEMO_SPAN + "_POSTAMBLE")],
        repair_count=0,
        known_document_ids=KNOWN_IDS_DEFAULT,
    )
    attachment = verify_citations(
        answer=answer,
        evidence=[_memo_evidence()],
        repair_count=0,
        known_document_ids=set(),
        attachment_identity_current=lambda item: True,
    )

    assert isinstance(company, DoneResult), company
    assert [c.kind for c in company.citations] == [COMPANY_KIND]
    assert isinstance(attachment, DoneResult), attachment
    assert [c.kind for c in attachment.citations] == [ATTACHMENT_KIND]


def test_an_unknown_kind_is_refused_rather_than_defaulted() -> None:
    """A kind outside the closed set raises, and a ``database-row`` item
    is NOT how an unknown kind reaches the document path. The two rules
    are selected by an exact kind compare, so nothing falls through to
    the other: an unknown kind must be refused, never defaulted into
    either rule (ADR 0091)."""
    with pytest.raises(MalformedEvidenceError) as exc:
        verify_citations(
            answer="A claim about an unknown kind of evidence.",
            evidence=[
                EvidenceItem(
                    locator=_locator(),
                    unit_text="legitimate text",
                    kind="some-future-kind",
                    database_row=(_contract_row(),),
                )
            ],
            repair_count=0,
            known_document_ids=KNOWN_IDS_DEFAULT,
        )

    assert exc.value.index == 0
    assert "some-future-kind" in str(exc.value)


def test_a_row_claim_never_falls_through_to_the_document_span_rule() -> None:
    """A row item whose text happens to hold a long span is still not a
    document item: an answer that quotes only the row's display line and
    states a supported figure must not ALSO pick up a span citation
    from the same evidence, and one that states an unsupported figure
    must refuse rather than being rescued by a span.

    The row's ``unit_text`` is the display line, which is exactly the
    text a span match would find, so this is the overlap that would
    exist if the two rules shared one matching path.
    """
    row = _contract_row()
    result = verify_citations(
        answer=f"{row.summary}. The notice period is 30 days.",
        evidence=[_row_evidence(row)],
        repair_count=0,
        known_document_ids=set(),
    )

    assert isinstance(result, DoneResult), result
    assert [c.kind for c in result.citations] == [DATABASE_ROW_KIND]


def test_a_row_kept_its_snapshot_and_the_quote_is_the_snapshot() -> None:
    """Opening the citation shows the retained snapshot and issues no
    query: the citation carries the row's ``fields`` and the row's own
    ``queried_at`` as DATA, and nothing in it claims the data is still
    the same now (spec 8.1).

    The absence of any "unchanged"/"still"/"current" claim in the
    citation is the part that is assertable here; a drift promise is
    not something a citation is allowed to contain at all.
    """
    result = verify_citations(
        answer=f"{_CONTRACT_ROW_WIRE['summary']}. Notice period 30 days.",
        evidence=[_row_evidence(_contract_row())],
        repair_count=0,
        known_document_ids=set(),
    )

    assert isinstance(result, DoneResult), result
    citation = result.citations[0]
    assert citation.version == "2026-03-01T09:14:22Z"
    assert dict(citation.database_fields) == {
        key: str(value) for key, value in _CONTRACT_ROW_WIRE["fields"].items()
    }
    text = " ".join(
        [citation.quote, citation.version, citation.display]
    ).lower()
    for drift_promise in ("unchanged", "still", "as of today", "current"):
        assert drift_promise not in text, drift_promise


def test_two_rounds_produce_two_independent_citations() -> None:
    """A refresh is a NEW query: new rows, a new time, a new evidence
    set. It never rewrites a prior answer, so a citation from the first
    round keeps the first round's time and fields while the second
    round's citation carries the second round's.

    This is what "the citation shows what the answer was built from"
    means operationally: the two snapshots coexist, and nothing merges
    them or claims they agree.
    """
    first = _contract_row()
    second = DatabaseRow.from_wire(
        {
            **_CONTRACT_ROW_WIRE,
            "queried_at": "2026-04-02T11:00:00Z",
            "fields": {
                **_CONTRACT_ROW_WIRE["fields"],
                "notice_days": 45,
                "notice_by": "2026-02-27",
            },
            "summary": (
                "C-2291 Northwind Logistics, expires 2026-03-14, "
                "notice by 2026-02-27"
            ),
        }
    )

    first_result = verify_citations(
        answer=f"{first.summary}. Notice period 30 days.",
        evidence=[_row_evidence(first)],
        repair_count=0,
        known_document_ids=set(),
    )
    second_result = verify_citations(
        answer=f"{second.summary}. Notice period 45 days.",
        evidence=[_row_evidence(second)],
        repair_count=0,
        known_document_ids=set(),
    )

    assert isinstance(first_result, DoneResult), first_result
    assert isinstance(second_result, DoneResult), second_result
    first_citation = first_result.citations[0]
    second_citation = second_result.citations[0]
    # The first answer is intact: the refresh did not rewrite it.
    assert first_citation.version == "2026-03-01T09:14:22Z"
    assert dict(first_citation.database_fields)["notice_days"] == "30"
    assert second_citation.version == "2026-04-02T11:00:00Z"
    assert dict(second_citation.database_fields)["notice_days"] == "45"
    # And a figure from one round is unsupported in the other, which is
    # what makes them two answers rather than one drifting fact.
    stale = verify_citations(
        answer=f"{second.summary}. Notice period 30 days.",
        evidence=[_row_evidence(second)],
        repair_count=0,
        known_document_ids=set(),
    )
    assert isinstance(stale, RefuseResult), stale
    assert stale.reason == UNSUPPORTED_DATABASE_CLAIM


@pytest.mark.parametrize(
    "broken, message",
    [
        ({"source": ""}, "source"),
        ({"record_key": {}}, "record_key"),
        ({"queried_at": ""}, "queried_at"),
    ],
    ids=["no_source", "no_record_key", "no_queried_at"],
)
def test_a_row_missing_its_own_identity_is_malformed(
    broken: dict[str, object], message: str
) -> None:
    """A row with no source, no key or no time states nothing the
    citation could point at, so it raises like any other broken evidence
    — it is not verified against an empty identity and published.

    ``queried_at`` is in the list because a citation about a value that
    moves, with no time on it, is a claim about a target that keeps
    moving (spec 8).
    """
    row = _contract_row(**broken)  # type: ignore[arg-type]

    with pytest.raises(MalformedEvidenceError) as exc:
        verify_citations(
            answer="A claim resting on a row with no identity.",
            evidence=[_row_evidence(row)],
            repair_count=0,
            known_document_ids=set(),
        )

    assert exc.value.index == 0
    assert message in str(exc.value)


def test_a_database_item_with_no_row_is_malformed() -> None:
    """The kind alone does not make text quotable: an item declared as a
    row that carries no row would otherwise be verified by the document
    span rule, which is the defaulting ADR 0091 forbids."""
    with pytest.raises(MalformedEvidenceError) as exc:
        verify_citations(
            answer="A claim resting on a row item that carries no row.",
            evidence=[
                EvidenceItem(
                    locator=Locator(document_id="", version="", unit=1),
                    unit_text="legitimate text",
                    kind=DATABASE_ROW_KIND,
                )
            ],
            repair_count=0,
            known_document_ids=set(),
        )

    assert exc.value.index == 0
    assert "database_row" in str(exc.value)


def test_a_row_never_borrows_the_document_id_slot() -> None:
    """A row that also names a document is refused: naming one would
    point the citation at a filing the ledger never read, which is the
    same confusion as an attachment item wearing a company id."""
    row = _contract_row()
    item = _row_evidence(row)
    with pytest.raises(MalformedEvidenceError) as exc:
        verify_citations(
            answer=f"{row.summary}. Notice period 30 days.",
            evidence=[
                EvidenceItem(
                    locator=Locator(
                        document_id="AAPL-2022Q3", version=item.locator.version, unit=1
                    ),
                    unit_text=item.unit_text,
                    kind=DATABASE_ROW_KIND,
                    database_row=item.database_row,
                )
            ],
            repair_count=0,
            known_document_ids=set(),
        )

    assert exc.value.index == 0
    assert "document_id" in str(exc.value)


def test_a_row_item_never_bridges_two_slices() -> None:
    """A record is not a slice, so a row item can never join a
    contiguous run: the offsets a ``read_source`` continuation uses are
    the document path's, and a row has no position in a frozen unit."""
    row = _contract_row()
    item = _row_evidence(row, position=10, next_position=10 + len(row.summary))

    result = verify_citations(
        answer=f"{row.summary} and then some unrelated trailing prose.",
        evidence=[item],
        repair_count=0,
        known_document_ids=set(),
    )

    assert isinstance(result, DoneResult), result
    assert [c.kind for c in result.citations] == [DATABASE_ROW_KIND]


def test_database_row_wire_shape_survives_number_and_date_fields() -> None:
    """The row is built from the wire a JSON adapter produces, so a
    numeric field arrives as ``int`` and a date as text. Both must read
    as the ledger stated them — and neither may be read as a filing
    locator or as a different number.
    """
    row = DatabaseRow.from_wire(_CONTRACT_ROW_WIRE)

    assert row.source == "ledger_ro.contract_expiry"
    assert dict(row.record_key) == {"contract_id": "C-2291"}
    assert row.queried_at == "2026-03-01T09:14:22Z"
    assert dict(row.fields) == {
        "counterparty": "Northwind Logistics",
        "expiry_date": "2026-03-14",
        "notice_days": "30",
        "notice_by": "2026-02-12",
    }
    # 30 is a returned value, and 31 is not one this row returned.
    assert row.tokens & {"30"}
    assert not row.tokens & {"99", "31", "2291"}


def test_id_delimiters_in_record_values_never_collide_two_records() -> None:
    """The source id joins ``key=value`` pairs with ``,``, so a record
    key or value that itself carries those characters must be escaped
    at build time: without it, two different records spell ONE id and
    the exported trail or a clicked chip opens the wrong row
    (spec 8.0 — one id opens one record)."""
    # ``{"k": "a=b"}`` vs ``{"k=a": "b"}`` both spelled ``v#k=a=b``
    # before escaping existed.
    first = format_database_row_source_id("v", {"k": "a=b"})
    second = format_database_row_source_id("v", {"k=a": "b"})
    assert first != second, (first, second)

    # ``{"j": "1", "k": "b,c"}`` vs ``{"j": "1,k=b", "c": ...}`` —
    # an unescaped ``,`` inside a value is a second pair to the parser.
    third = format_database_row_source_id("v", {"j": "1", "k": "b,c"})
    fourth = format_database_row_source_id("v", {"j": "1,k=b"})
    assert third != fourth, (third, fourth)

    # And the escape itself is injective: a literal backslash cannot
    # imitate an escaped delimiter.
    assert format_database_row_source_id(
        "v", {"k": "a\\=b"}
    ) != format_database_row_source_id("v", {"k": "a=b"})


def test_two_records_with_delimiter_values_keep_distinct_source_ids() -> None:
    """The same rule through the row object: two ``DatabaseRow``s whose
    keys need escaping cite as two distinct records end to end."""
    row_a = _contract_row(record_key={"counterparty": "A=B, Ltd"})
    row_b = _contract_row(record_key={"counterparty": "A", "x": "B, Ltd"})
    assert row_a.source_id != row_b.source_id
    # Escaping lives in the ID, not in the evidence: the record key the
    # gate verifies claims against stays the ledger's own verbatim text.
    assert dict(row_a.record_key) == {"counterparty": "A=B, Ltd"}


def test_a_grouped_figure_is_the_same_figure() -> None:
    """A model may write a ledger's grouped figure either way; a
    supported value must not be refused over its punctuation."""
    row = _contract_row(
        fields={**_CONTRACT_ROW_WIRE["fields"], "penalty_usd": 125000}
    )

    grouped = verify_citations(
        answer=f"{row.summary}. The penalty is 125,000 USD.",
        evidence=[_row_evidence(row)],
        repair_count=0,
        known_document_ids=set(),
    )
    plain = verify_citations(
        answer=f"{row.summary}. The penalty is 125000 USD.",
        evidence=[_row_evidence(row)],
        repair_count=0,
        known_document_ids=set(),
    )

    assert isinstance(grouped, DoneResult), grouped
    assert isinstance(plain, DoneResult), plain


def test_a_date_field_is_one_value_and_not_three_numbers() -> None:
    """A ``2026-03-14`` expiry is one returned value. Read as three
    numbers it would make a claim of "2026 contracts" supported by a
    date, and would compare a claim about the month against hyphens."""
    row = _contract_row()

    same_date = verify_citations(
        answer="The expiry date is 2026-03-14 per the ledger.",
        evidence=[_row_evidence(row)],
        repair_count=0,
        known_document_ids=set(),
    )
    year_only = verify_citations(
        answer="The ledger shows 2026 contracts in the portfolio.",
        evidence=[_row_evidence(row)],
        repair_count=0,
        known_document_ids=set(),
    )

    assert isinstance(same_date, DoneResult), same_date
    assert isinstance(year_only, RefuseResult), year_only
    assert year_only.reason == UNSUPPORTED_DATABASE_CLAIM