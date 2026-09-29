"""Parent/child chunking over frozen unit text (plan T11, SC11 / SC16).

Default-suite tests: small scripted fixtures only — the full-dataset
two-level substring assertion is the opt-in eval contract
(``tests/eval/test_chunking_contract.py``, run via
``uv run pytest -q -m eval``). What is covered here, on small units:

* two-level contiguity — every child block text is a contiguous
  substring of its parent block text, every parent block text is a
  contiguous substring of its frozen unit text (SC11, fixture level);
* block text is a verbatim slice of the frozen unit text — no second
  character-replacement rule runs after freezing (SC17 extension);
* split parameters come from exactly one T9 triad lookup
  (``lookup_chunk_params``) with integer density classification — no
  float threshold, no model judgment (SC13);
* the hit set contains only child blocks; parent blocks are not
  independently retrievable — they ride along as context (SC16);
* every child's push payload carries its parent's text;
* deterministic block identity (sha256) — same input, same ids
  (consumed by T12's byte comparison and T14's idempotency key);
* the citation gate matches ≥32-char spans of child payload text.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from fin.corpus.chunking import (
    KIND_CHILD,
    KIND_PARENT,
    split_document,
    split_unit,
)
from fin.corpus.push import envelopes_of_document
from fin.corpus.window import (
    EXTRACT_WINDOW_CHARS,
    EXTRACT_WINDOW_TOKENS,
    WINDOW_OVERLAP_TOLERANCE,
    count_window_tokens,
    window_spans,
)
from fin.corpus.normalization import (
    NORMALIZATION_RULE_VERSION,
    normalize_frozen_text,
)
from fin.corpus.params import (
    DENSITY_DENSE,
    SECTIONS,
    classify_text_density,
    lookup_chunk_params,
)
from fin.corpus.parsers import PARAMS_TABLE_VERSION, PARSE_RULE_VERSION, parse_document
from fin.corpus.parsers._common import build_unit
from fin.host.citation_gate import EvidenceItem, verify_citations
from fin.corpus.extracted import Locator

DOCUMENT_ID = "TEST-2023Q1"
VERSION = "a" * 64


def _sentences(n: int, start: int = 0) -> str:
    """n deterministic ~100-char sentences, each ending in a period."""
    return "".join(
        f"Sentence {i:03d} reports that operating revenue grew in the "
        "quarter and the gross margin improved again. "
        for i in range(start, start + n)
    )


def _unit(text: str, *, fmt: str = "txt", unit: int = 1):
    """One frozen unit built through the single construction point."""
    return build_unit(
        fmt=fmt,
        source_sha256=VERSION,
        document_id=DOCUMENT_ID,
        version=VERSION,
        unit=unit,
        text=text,
    )


def _parsed(text: str, *, fmt: str = "txt"):
    """A ParseResult produced by the real T6 entry point."""
    return parse_document(
        text.encode("utf-8"),
        fmt=fmt,
        document_id=DOCUMENT_ID,
        version=VERSION,
    )


# ---------------------------------------------------------------------------
# SC11: two-level contiguity on the fixture level
# ---------------------------------------------------------------------------


def test_parent_is_contiguous_substring_of_frozen_unit() -> None:
    result = _parsed(_sentences(40))
    chunking = split_document(result, doc_type="narrative")
    assert len(chunking.units) == 1
    uc = chunking.units[0]
    assert uc.parents, "a non-empty unit must produce at least one parent"
    for parent in uc.parents:
        assert parent.text in uc.unit_text, "parent must be a slice of the unit text"


def test_child_is_contiguous_substring_of_parent() -> None:
    result = _parsed(_sentences(40))
    chunking = split_document(result, doc_type="narrative")
    uc = chunking.units[0]
    assert uc.children
    by_id = {p.block_id: p for p in uc.parents}
    for child in uc.children:
        parent = by_id[child.parent_block_id]
        assert child.text in parent.text, "child must be a slice of its parent"
        assert parent.text in uc.unit_text


def test_block_text_is_verbatim_frozen_slice_no_renormalization() -> None:
    # SC17 extension: the chunk path performs NO character replacement;
    # block text is exactly a slice of the frozen unit text, hence a
    # fixed point of the single normalization outlet.
    text = _sentences(30)
    result = _parsed(text)
    chunking = split_document(result, doc_type="narrative")
    uc = chunking.units[0]
    frozen = uc.unit_text
    for block in [*uc.parents, *uc.children]:
        assert block.text in frozen
        assert normalize_frozen_text(block.text) == block.text


# ---------------------------------------------------------------------------
# SC13: parameters come from exactly one triad lookup
# ---------------------------------------------------------------------------


def test_params_come_from_single_triad_lookup() -> None:
    result = _parsed(_sentences(40))
    uc = split_document(result, doc_type="narrative").units[0]
    assert uc.section in SECTIONS
    assert uc.density == classify_text_density(uc.unit_text)
    expected = lookup_chunk_params("narrative", uc.section, uc.density)
    assert uc.params == expected
    # The params row bounds CHILDREN only. Parent (抽图窗) spans are
    # capped by fin.corpus.window.EXTRACT_WINDOW_CHARS instead — the
    # row must not shrink a window to child size (ADR 0060 / SC8).
    windows = window_spans(uc.unit_text)
    assert [p.text for p in uc.parents] == [
        w.text_of(uc.unit_text) for w in windows
    ]
    for w in windows:
        assert len(w.body_of(uc.unit_text)) <= EXTRACT_WINDOW_CHARS
    for child in uc.children:
        assert len(child.text) <= expected.max_chars


def test_table_dense_text_hits_table_section_and_chain() -> None:
    rows = "\n".join(
        f"| Item {i:02d} | revenue {i} | margin {i} |" for i in range(30)
    )
    result = _parsed(rows)
    uc = split_document(result, doc_type="table_dense").units[0]
    assert uc.section == "table"
    assert uc.density == DENSITY_DENSE
    assert uc.params == lookup_chunk_params("table_dense", "table", DENSITY_DENSE)
    # table_row boundaries are in the chain: every child is a
    # row-aligned slice — a child that starts on a data row keeps
    # starting on it, and no child's first line is a row continuation.
    for child in uc.children:
        assert child.text in uc.unit_text
        assert not child.text.startswith(" revenue")


def test_unknown_doc_type_lands_on_global_default_row() -> None:
    # T10 primary samples carry doc_type "mixed"; the lookup must be
    # total and land on the deterministic global default row.
    result = _parsed(_sentences(10))
    uc = split_document(result, doc_type="mixed").units[0]
    fallback = lookup_chunk_params("no-such-doc-type", "no-such-section", 999)
    assert uc.params == fallback


# ---------------------------------------------------------------------------
# SC16: hit set = children only; parents ride along
# ---------------------------------------------------------------------------


def test_hit_set_contains_only_children() -> None:
    result = _parsed(_sentences(40))
    chunking = split_document(result, doc_type="narrative")
    uc = chunking.units[0]
    assert uc.hittable_blocks() == uc.children
    for parent in uc.parents:
        assert parent.kind == KIND_PARENT
        assert parent.is_retrievable is False
    for child in uc.children:
        assert child.kind == KIND_CHILD
        assert child.is_retrievable is True
    # document level: the hit set is the union of the children
    assert chunking.hittable_blocks() == tuple(
        c for u in chunking.units for c in u.children
    )
    # parents are never part of the hit set even when their text is
    # identical to a child's (single-child parent)
    assert all(p.block_id not in {c.block_id for c in uc.children} for p in uc.parents)


def test_payload_carries_parent_text_and_metadata() -> None:
    result = _parsed(_sentences(40))
    uc = split_document(result, doc_type="narrative").units[0]
    payloads = uc.payloads()
    assert len(payloads) == len(uc.children)
    parents_by_id = {p.block_id: p for p in uc.parents}
    for payload, child in zip(payloads, uc.children):
        parent = parents_by_id[child.parent_block_id]
        assert payload.block_id == child.block_id
        assert payload.text == child.text
        assert payload.parent_block_id == parent.block_id
        assert payload.parent_text == parent.text
        assert payload.text in payload.parent_text
        # SC20 metadata fields ride along for the retrieval side
        assert payload.locator == uc.locator
        assert payload.fmt == "txt"
        assert payload.doc_type == "narrative"
        assert payload.section == uc.section
        assert payload.density == uc.density
        assert payload.source_sha256 == result.source_sha256
        assert payload.parse_rule_version == PARSE_RULE_VERSION
        assert payload.params_table_version == PARAMS_TABLE_VERSION
        assert payload.normalization_rule_version == NORMALIZATION_RULE_VERSION


def test_payload_to_dict_is_deterministic() -> None:
    result = _parsed(_sentences(20))
    uc = split_document(result, doc_type="narrative").units[0]
    dicts = [p.to_dict() for p in uc.payloads()]
    assert dicts == [p.to_dict() for p in uc.payloads()]
    assert all(set(d) for d in dicts)


# ---------------------------------------------------------------------------
# Citation gate compatibility over child payload text
# ---------------------------------------------------------------------------


def test_citation_gate_matches_span_of_child_payload_text() -> None:
    result = _parsed(_sentences(40))
    uc = split_document(result, doc_type="narrative").units[0]
    big_child = next(c for c in uc.children if len(c.text) >= 64)
    answer = f"See {DOCUMENT_ID}: {big_child.text[:48]}"
    evidence = [
        EvidenceItem(
            locator=Locator(
                document_id=uc.locator.document_id,
                version=uc.locator.version,
                unit=uc.locator.unit,
            ),
            unit_text=block.text,
        )
        for block in uc.hittable_blocks()
    ]
    outcome = verify_citations(
        answer,
        evidence,
        repair_count=0,
        known_document_ids={DOCUMENT_ID},
    )
    assert outcome.outcome == "done"


def test_span_across_child_cut_is_not_matched() -> None:
    # A span that exists only in the PARENT text (it straddles the cut
    # between two children) must not be matched when the evidence list
    # is the hit set — parents are not independently retrievable.
    # Sentences carry unique numbers so no interior coincidence can
    # reproduce the cross-cut span inside a single child.
    #
    # The parent level is now the extract window (ADR 0060), so the
    # fixture carries a heading and enough body for one window to need
    # several children: the precondition is a parent with >= 2
    # children, which the multi-heading shape below no longer supplies.
    sentences = "# Filing Part\n" + "".join(
        f"Part {i:03d} of the filing describes item {i:03d} with metric "
        f"{i * 7:04d} and value {i * 13:05d}. "
        for i in range(60)
    )
    uc = split_unit(_unit(sentences), doc_type="narrative")
    parent = uc.parents[0]
    children = [c for c in uc.children if c.parent_block_id == parent.block_id]
    assert len(children) >= 2, "precondition: two children to straddle"
    cut = uc.unit_text.find(children[1].text)
    span = uc.unit_text[cut - 24 : cut + 24]
    assert all(span not in c.text for c in uc.children), (
        "the crafted span must straddle the cut between two children"
    )
    evidence = [
        EvidenceItem(
            locator=Locator(
                document_id=uc.locator.document_id,
                version=uc.locator.version,
                unit=uc.locator.unit,
            ),
            unit_text=block.text,
        )
        for block in uc.hittable_blocks()
    ]
    outcome = verify_citations(
        f"Quote: {span}",
        evidence,
        repair_count=0,
        known_document_ids={DOCUMENT_ID},
    )
    assert outcome.outcome == "bounce"


# ---------------------------------------------------------------------------
# Boundary behavior (integer chain: heading → … → char_cap)
# ---------------------------------------------------------------------------


def test_heading_boundary_starts_new_parent() -> None:
    text = (
        "# Alpha\n" + _sentences(15) + "\n# Beta\n" + _sentences(15) + "\n# Gamma\n"
        + _sentences(15)
    )
    uc = split_unit(_unit(text), doc_type="narrative")
    assert len(uc.parents) == 3
    assert uc.parents[1].text.startswith("# Beta")
    assert uc.parents[2].text.startswith("# Gamma")
    assert [p.title for p in uc.parents] == ["Alpha", "Beta", "Gamma"]
    parents_by_id = {p.block_id: p for p in uc.parents}
    for child in uc.children:
        assert child.text in parents_by_id[child.parent_block_id].text


def test_char_cap_hard_split_without_natural_boundaries() -> None:
    # One ~10k-char line: no headings, no sentence punctuation. The
    # parent level is now the extract window's char cap (ADR 0060) and
    # the child level the row's char_cap rank — both still bound their
    # pieces, with no natural boundary available at either level.
    text = ("revenue " * 1250).strip()
    uc = split_unit(_unit(text), doc_type="narrative")
    assert max(len(p.text) for p in uc.parents) <= EXTRACT_WINDOW_CHARS
    assert max(len(c.text) for c in uc.children) <= uc.params.max_chars
    assert len(uc.parents) >= 2, "precondition: the window cap had to cut"
    for block in [*uc.parents, *uc.children]:
        assert block.text in uc.unit_text


def test_small_trailing_child_is_merged_into_neighbor() -> None:
    # The "# Alpha" parent is larger than target_chars, so packing had
    # to cut it; the trailing "End." tail is far below min_chars and
    # must be merged into the previous child instead of being pushed
    # as a tiny block.
    text = "# Alpha\n" + _sentences(15) + "End. \n# Beta\n" + _sentences(15)
    uc = split_unit(_unit(text), doc_type="narrative")
    params = uc.params
    alpha = next(p for p in uc.parents if p.text.startswith("# Alpha"))
    assert len(alpha.text) > params.target_chars, "precondition: parent needs packing"
    tail = alpha.text[alpha.text.find("End.") :]
    assert 0 < len(tail) <= params.min_chars, "precondition: tail below min_chars"
    alpha_children = [c for c in uc.children if c.parent_block_id == alpha.block_id]
    assert len(alpha_children) == 1, "small tail must be merged into its neighbour"
    assert alpha_children[0].text.rstrip().endswith("End.")


# ---------------------------------------------------------------------------
# SC2 / SC3 / SC5 / SC8: the parent level is the title-scoped 抽图窗
# ---------------------------------------------------------------------------


def _children_of(uc, parent) -> list:
    return [c for c in uc.children if c.parent_block_id == parent.block_id]


def _parent_offsets(uc) -> dict[str, tuple[int, int]]:
    """Unit-text offsets of every parent, walked in order (parents are
    ordered and non-identical, so a monotone search is exact)."""
    offsets: dict[str, tuple[int, int]] = {}
    cursor = 0
    for parent in uc.parents:
        start = uc.unit_text.find(parent.text, cursor)
        assert start != -1, "parent text must be a slice of the unit text"
        offsets[parent.block_id] = (start, start + len(parent.text))
        cursor = start
    return offsets


def _monotone_child_offsets(uc) -> list[tuple[int, int]]:
    """Walk the children in ordinal order with a monotone cursor over the
    frozen unit text — the wire invariant (iknow
    ``findSliceInvariantViolations``) restated for the fin side.

    ``body`` on the wire is the unit text, ``chunks[]`` are the children
    in ordinal order, so each child must be found at or after the cursor
    left by its predecessor. Fails loudly on the first break: once the
    cursor is wrong every later position is meaningless.
    """
    offsets: list[tuple[int, int]] = []
    cursor = 0
    for child in uc.children:
        start = uc.unit_text.find(child.text, cursor)
        assert start != -1, (
            f"child ordinal {child.ordinal} (parent "
            f"{child.parent_ordinal}) is not reachable at/after cursor "
            f"{cursor} — window overlap leaked onto the child sequence"
        )
        offsets.append((start, start + len(child.text)))
        cursor = start + len(child.text)
    return offsets


def _cross_window_child_overlaps(uc) -> list[tuple[int, int]]:
    """``(overlap_len, prev_len)`` for the adjacent child pairs whose
    parents DIFFER — the seam the window 8% used to leak through.

    The existing ``_sibling_overlaps`` walks within one parent only, so
    a pair straddling two windows was never measured: that is the gap
    that let the cross-window overlap through.
    """
    walked = list(zip(uc.children, _monotone_child_offsets(uc)))
    pairs: list[tuple[int, int]] = []
    for (prev, (prev_start, prev_end)), (nxt, (next_start, _)) in zip(
        walked, walked[1:]
    ):
        if prev.parent_block_id == nxt.parent_block_id:
            continue
        pairs.append((max(0, prev_end - next_start), prev_end - prev_start))
    return pairs


def _sibling_overlaps(uc) -> list[tuple[int, int]]:
    """``(overlap_len, prev_child_len)`` for adjacent children of the
    SAME parent (the pairs the 0-overlap / <=1-sentence rule governs).
    The denominator is the PREVIOUS CHILD's span — the quantity the
    spec's "at most one sentence" bounds — not the parent's
    unit-text offset.
    """
    pairs: list[tuple[int, int]] = []
    for parent in uc.parents:
        kids = _children_of(uc, parent)
        base = _parent_offsets(uc)[parent.block_id][0]
        spans = []
        for child in kids:
            rel = parent.text.find(child.text)
            assert rel != -1
            spans.append((base + rel, base + rel + len(child.text)))
        for (prev_start, prev_end), (next_start, _) in zip(spans, spans[1:]):
            pairs.append((max(0, prev_end - next_start), prev_end - prev_start))
    return pairs


def test_sc2_small_heading_body_is_one_window_with_zero_window_overlap() -> None:
    # SC2: a title-scoped body at or below 1200 extract-model tokens
    # yields exactly one 抽图窗 for that heading and window overlap 0.
    text = "# Alpha\n" + _sentences(30)
    uc = split_unit(_unit(text), doc_type="narrative")
    windows = window_spans(uc.unit_text)
    assert len(uc.parents) == 1
    assert [p.text for p in uc.parents] == [w.text_of(uc.unit_text) for w in windows]
    assert uc.parents[0].title == "Alpha"
    assert uc.parents[0].text.startswith("# Alpha")
    assert len(windows) == 1
    # no two windows of the same title overlap: one window, so vacuously
    # true and the rule is pinned by the count above.
    assert len({w.title for w in windows}) == 1
    # The cap is stated in extract-model TOKENS (ADR 0053), not chars:
    # the fixture's body must be at or below the token cap by the
    # module's own estimator, or "one window" would prove nothing.
    body = windows[0].body_of(uc.unit_text)
    assert count_window_tokens(body) <= EXTRACT_WINDOW_TOKENS


def test_sc3_overflow_windows_share_title_and_overlap_within_tolerance() -> None:
    # SC3: a heading body over 1200 tokens yields >= 2 windows sharing
    # the heading, adjacent overlap in the 6..10% band, heading text in
    # the first window only (not a second body cutter).
    text = "# Alpha\n" + _sentences(60)
    uc = split_unit(_unit(text), doc_type="narrative")
    alpha = [p for p in uc.parents if p.title == "Alpha"]
    assert len(alpha) >= 2
    offsets = _parent_offsets(uc)
    low, high = WINDOW_OVERLAP_TOLERANCE
    for prev, nxt in zip(alpha, alpha[1:]):
        prev_start, prev_end = offsets[prev.block_id]
        next_start, _ = offsets[nxt.block_id]
        ratio = (prev_end - next_start) / (prev_end - prev_start)
        assert low / 100 <= ratio <= high / 100, ratio
    assert alpha[0].text.startswith("# Alpha")
    assert all(not p.text.startswith("# Alpha") for p in alpha[1:])
    windows = window_spans(uc.unit_text)
    for window in windows:
        assert len(window.body_of(uc.unit_text)) <= EXTRACT_WINDOW_CHARS
    assert [p.text for p in uc.parents] == [w.text_of(uc.unit_text) for w in windows]


def test_sc4_children_default_to_zero_overlap_on_the_overflow_fixture() -> None:
    # SC4: the same overflow fixture's children default to 0 overlap —
    # the 8% window identity must not be copied onto the ES keys.
    text = "# Alpha\n" + _sentences(60)
    uc = split_unit(_unit(text), doc_type="narrative")
    assert len([p for p in uc.parents if p.title == "Alpha"]) >= 2
    by_id = {p.block_id: p for p in uc.parents}
    for parent in uc.parents:
        kids = _children_of(uc, parent)
        assert kids, "every window carries children"
        offsets = []
        for child in kids:
            rel = parent.text.find(child.text)
            assert rel != -1
            offsets.append((rel, rel + len(child.text)))
        assert offsets == sorted(offsets), "children stay ordered in their window"
    # No adjacent sibling pair overlaps, and — the lock the spec names —
    # no pair's overlap lands in the window band.
    low, high = WINDOW_OVERLAP_TOLERANCE
    pairs = _sibling_overlaps(uc)
    assert len(pairs) >= 2, "precondition: several sibling pairs to measure"
    for overlap, prev_len in pairs:
        assert overlap == 0, "children must not overlap by default"
        assert not (low / 100 * prev_len <= overlap <= high / 100 * prev_len)
    assert by_id  # the parent link surface exists for every child


def test_sc4_narrative_child_overlap_is_at_most_one_sentence() -> None:
    # SC4: a narrative fixture may overlap children by at most one
    # sentence — never the window band.
    text = "# Alpha\n" + _sentences(60)
    uc = split_unit(_unit(text), doc_type="narrative")
    longest = max(
        len(s) for s in re.split(r"(?<=\.)\s+", uc.unit_text) if s
    )
    low, high = WINDOW_OVERLAP_TOLERANCE
    for overlap, prev_len in _sibling_overlaps(uc):
        assert overlap <= longest, (overlap, longest)
        assert not (low / 100 * prev_len <= overlap <= high / 100 * prev_len)


def test_sc4_cross_window_adjacent_children_have_zero_overlap() -> None:
    # SC4 / ADR 0063: the 8% is a WINDOW identity — the adjacent child
    # pair straddling two windows must overlap by 0, exactly like the
    # siblings inside one window. ``_sibling_overlaps`` never measured
    # this pair (it walks within one parent), which is how the window
    # overlap leaked onto the child sequence unnoticed.
    text = "# Alpha\n" + _sentences(60)
    uc = split_unit(_unit(text), doc_type="narrative")
    assert len([p for p in uc.parents if p.title == "Alpha"]) >= 2, (
        "precondition: the fixture overflows into >= 2 windows"
    )
    pairs = _cross_window_child_overlaps(uc)
    assert pairs, "precondition: a child pair straddles the window seam"
    low, high = WINDOW_OVERLAP_TOLERANCE
    for overlap, prev_len in pairs:
        assert overlap == 0, (overlap, prev_len)
        assert not (low / 100 * prev_len <= overlap <= high / 100 * prev_len)


def test_sc4_cross_window_children_are_monotone_over_the_unit_text() -> None:
    # SC11 wire invariant: the envelope body is the unit text and
    # ``chunks[]`` are the children in ordinal order, so the iknow
    # freeze walk (``findSliceInvariantViolations``) must find every
    # child at or after the cursor left by its predecessor.
    text = "# Alpha\n" + _sentences(60)
    uc = split_unit(_unit(text), doc_type="narrative")
    assert len([p for p in uc.parents if p.title == "Alpha"]) >= 2
    assert _cross_window_child_overlaps(uc), "precondition: a cross-window pair"
    _monotone_child_offsets(uc)


def test_sc4_real_overflow_fixture_is_monotone_on_the_wire() -> None:
    # REGRESSION, the reported geometry: the md fixture's overloaded
    # body packs children independently inside each window, so window
    # 2's first child begins ~8% before window 1's last child ends and
    # the wire walk fails at exactly that child.
    sample = (
        Path(__file__).resolve().parents[2]
        / "data/corpus/dataset/documents/md/AAPL-2022Q3__table_dense__md__md.md"
    )
    result = parse_document(
        sample.read_bytes(),
        fmt="md",
        document_id="AAPL-2022Q3__table_dense__md__md",
        version=VERSION,
    )
    document = split_document(result, doc_type="narrative")
    assert len(document.units) > 11, "precondition: the fixture has the unit"
    uc = document.units[11]  # zero-based: the reported unit
    assert len(uc.parents) == 2, "precondition: the reported two-window unit"
    assert _cross_window_child_overlaps(uc), "precondition: a cross-window pair"
    _monotone_child_offsets(uc)


def test_envelope_chunks_walk_monotonically_over_the_body() -> None:
    # The offline mirror of the receiving service's freeze invariant
    # (iknow ``src/tools/slice-invariants.ts``): one envelope per unit,
    # ``body`` = unit text, each ``chunks[i].content`` found at or after
    # the cursor, dense 0..N-1 indexes and a non-empty chunk list. No
    # network — the same walk the live MCP service enforces.
    sample = (
        Path(__file__).resolve().parents[2]
        / "data/corpus/dataset/documents/md/AAPL-2022Q3__table_dense__md__md.md"
    )
    result = parse_document(
        sample.read_bytes(),
        fmt="md",
        document_id="AAPL-2022Q3__table_dense__md__md",
        version=VERSION,
    )
    envelopes = envelopes_of_document(
        split_document(result, doc_type="narrative"),
        valid_from="2026-01-01T00:00:00.000Z",
        valid_until="9999-12-31T23:59:59.999Z",
    )
    assert envelopes, "precondition: the fixture produces envelopes"
    for envelope in envelopes:
        assert envelope.chunks, "every envelope must carry chunks"
        cursor = 0
        for position, chunk in enumerate(envelope.chunks):
            assert chunk.index == position, "chunk indexes are dense 0..N-1"
            found = envelope.body.find(chunk.content, cursor)
            assert found != -1, (
                f"{envelope.source_ref}: chunk {chunk.index} is not a "
                f"contiguous substring of the body at/after cursor {cursor}"
            )
            cursor = found + len(chunk.content)


def test_sc4_table_row_and_clause_children_stay_zero_overlap() -> None:
    # SC4: table rows and atomic clauses keep 0 child overlap.
    rows = "# Table\n" + "\n".join(
        f"| Item {i:02d} | revenue {i} | margin {i} |" for i in range(30)
    )
    clauses = "# Clauses\n" + "\n".join(
        f"{i}. The retained party shall deliver the quarterly report within "
        f"fifteen business days of period end and late delivery triggers the "
        f"remediation clause number {i}."
        for i in range(1, 90)
    )
    for text, doc_type in ((rows, "table_dense"), (clauses, "clause_list")):
        uc = split_unit(_unit(text), doc_type=doc_type)
        low, high = WINDOW_OVERLAP_TOLERANCE
        for overlap, prev_len in _sibling_overlaps(uc):
            assert overlap == 0, (doc_type, overlap)
            assert not (low / 100 * prev_len <= overlap <= high / 100 * prev_len)


def test_sc5_every_child_is_inside_exactly_one_parent_window() -> None:
    # SC5: every child is a contiguous substring of its parent 抽图窗;
    # no child spans two parents. Adjacent windows of one region overlap
    # by the window identity, so a child of window k near a seam may
    # also occur as text inside window k+1's overlap tail — that is the
    # window rule, not a second parent: the block belongs to exactly one
    # window and is packed inside it.
    text = "# Alpha\n" + _sentences(60) + "\n# Beta\n" + _sentences(30)
    uc = split_unit(_unit(text), doc_type="narrative")
    assert len(uc.parents) >= 2
    assert len([p for p in uc.parents if p.title == "Alpha"]) >= 2
    by_id = {p.block_id: p for p in uc.parents}
    for child in uc.children:
        assert child.parent_block_id is not None
        owner = [p for p in uc.parents if p.block_id == child.parent_block_id]
        assert len(owner) == 1, "the parent link must resolve to one window"
        assert child.text in owner[0].text
        # the child sits inside its parent's span — it never crosses the
        # window's start or end into the neighbouring window
        assert owner[0].text.index(child.text) + len(child.text) <= len(owner[0].text)
        assert child.text in uc.unit_text
    # child blocks are distinct per window: no block is shared between
    # two parents
    seen: dict[str, str] = {}
    for child in uc.children:
        assert child.block_id not in seen or seen[child.block_id] == child.parent_block_id
        seen[child.block_id] = child.parent_block_id
    assert all(by_id[pid].kind == KIND_PARENT for pid in seen.values())


def test_sc8_density_sizes_children_not_the_window_cap() -> None:
    # SC8: a high-density fixture packs more / smaller children inside
    # the same 1200-token window; the parent cap stays
    # EXTRACT_WINDOW_CHARS — density does not retarget the window to
    # child size (ADR 0060). The two fixtures below carry real density
    # signals (prose vs table rows), so the triads differ.
    plain = "# Alpha\n" + _sentences(60)
    dense = "# Alpha\n" + "\n".join(
        f"| row {i} | value {i} |" for i in range(200)
    )
    plain_uc = split_unit(_unit(plain), doc_type="narrative")
    dense_uc = split_unit(_unit(dense), doc_type="table_dense")
    assert plain_uc.density == 0 and dense_uc.density == 2, (
        "precondition: the fixtures carry different density levels"
    )
    # The two rows differ (density-sized child packing)…
    assert plain_uc.params != dense_uc.params
    # …but neither row's caps touch the window: every window body stays
    # under the window cap, and the plain row's body even exceeds its
    # own row cap (window cap > row cap here) without being shrunk.
    assert plain_uc.params.max_chars < EXTRACT_WINDOW_CHARS
    plain_bodies = [
        len(w.body_of(plain_uc.unit_text)) for w in window_spans(plain_uc.unit_text)
    ]
    assert max(plain_bodies) == EXTRACT_WINDOW_CHARS
    for uc in (plain_uc, dense_uc):
        for window in window_spans(uc.unit_text):
            assert len(window.body_of(uc.unit_text)) <= EXTRACT_WINDOW_CHARS
    # The dense row packs more (per unit of window body) and smaller
    # children than the plain row.
    plain_per_1k = len(plain_uc.children) * 1000 / len(plain_uc.unit_text)
    dense_per_1k = len(dense_uc.children) * 1000 / len(dense_uc.unit_text)
    assert dense_per_1k > plain_per_1k
    assert max(len(c.text) for c in dense_uc.children) < max(
        len(c.text) for c in plain_uc.children
    )


# ---------------------------------------------------------------------------
# Deterministic identity (T12 consumes; T14 idempotency key depends)
# ---------------------------------------------------------------------------


def test_same_input_same_blocks_and_ids() -> None:
    result = _parsed(_sentences(40))
    first = split_document(result, doc_type="narrative")
    second = split_document(_parsed(_sentences(40)), doc_type="narrative")
    assert first == second
    ids = [b.block_id for u in first.units for b in (*u.parents, *u.children)]
    assert len(ids) == len(set(ids)), "block ids must be unique within a document"


def test_document_chunking_covers_every_unit() -> None:
    text = "\n\n".join(_sentences(6, start=10 * i) for i in range(3))
    result = _parsed(text)  # txt: blank-line paragraphs → 3 units
    assert len(result.units) == 3
    chunking = split_document(result, doc_type="narrative")
    assert len(chunking.units) == len(result.units)
    for parsed, uc in zip(result.units, chunking.units):
        assert uc.locator == parsed.locator
        assert uc.parents
        assert uc.children
        for parent in uc.parents:
            assert parent.text in parsed.text
