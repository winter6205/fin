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

import pytest

from fin.corpus.chunking import (
    KIND_CHILD,
    KIND_PARENT,
    split_document,
    split_unit,
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
    for parent in uc.parents:
        assert len(parent.text) <= expected.max_chars
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
                page=uc.locator.unit,
            ),
            page_text=block.text,
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
    sentences = "".join(
        f"Part {i:03d} of the filing describes item {i:03d} with metric "
        f"{i * 7:04d} and value {i * 13:05d}. "
        for i in range(40)
    )
    uc = split_unit(_unit(sentences), doc_type="narrative")
    assert len(uc.parents) >= 2
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
                page=uc.locator.unit,
            ),
            page_text=block.text,
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
    parents_by_id = {p.block_id: p for p in uc.parents}
    for child in uc.children:
        assert child.text in parents_by_id[child.parent_block_id].text


def test_char_cap_hard_split_without_natural_boundaries() -> None:
    # One 10k-char line: no headings, no sentence punctuation — only
    # the char_cap rank can bound the pieces.
    text = ("revenue " * 1250).strip()
    uc = split_unit(_unit(text), doc_type="narrative")
    assert max(len(p.text) for p in uc.parents) <= uc.params.max_chars
    assert max(len(c.text) for c in uc.children) <= uc.params.max_chars
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
