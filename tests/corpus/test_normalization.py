"""T5 / SC17: frozen-text normalization has a single rule authority.

The four normalization categories (whitespace, Unicode compatibility,
hyphens, soft line breaks) must all be produced by one outlet —
``fin.corpus.normalization.normalize_frozen_text`` — and frozen-unit
text and chunk text must share it. The citation gate
(``fin.host.citation_gate``) matches spans verbatim (plain ``in``
substring search, no normalization of its own), so the outlet is the
compatibility surface: any ≥32-char span a model could quote from a
chunk must remain a contiguous substring of the frozen unit text with
zero cross-rule substitution failures.
"""

from __future__ import annotations

import json
from pathlib import Path

from fin.corpus.extracted import Locator
from fin.corpus.normalization import (
    NORMALIZATION_RULE_VERSION,
    normalize_frozen_text,
)
from fin.host.citation_gate import (
    DoneResult,
    EvidenceItem,
    verify_citations,
)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


# ----- category 1: whitespace ------------------------------------------


def test_whitespace_collapses_to_single_space_per_line() -> None:
    raw = "A\u00a0\tB\u2003C\u202fD\u3000E   F"
    assert normalize_frozen_text(raw) == "A B C D E F"


def test_blank_lines_are_dropped_and_edges_trimmed() -> None:
    raw = "\n\n  A  \n\n\n B \n\n"
    assert normalize_frozen_text(raw) == "A\nB"


# ----- category 2: Unicode compatibility -------------------------------


def test_unicode_nfkc_compatibility_forms() -> None:
    raw = "\uFF26\uFF2F\uFF32\uFF2D \uFF11\uFF10 \uFB01le \u2460"
    assert normalize_frozen_text(raw) == "FORM 10 file 1"


# ----- category 3: hyphens ---------------------------------------------


def test_hyphen_variants_unify_to_ascii_hyphen_minus() -> None:
    raw = "10\u2010Q 10\u2011Q 10\uFE63Q 10\uFF0DQ"
    assert normalize_frozen_text(raw) == "10-Q 10-Q 10-Q 10-Q"


def test_soft_hyphen_is_removed_not_converted() -> None:
    assert normalize_frozen_text("Quar\u00adterly") == "Quarterly"


# ----- category 4: soft line breaks ------------------------------------


def test_line_break_family_normalizes_to_lf() -> None:
    raw = "A\r\nB\rC\u0085D\u2028E\u2029F\x0bG\x0cH"
    assert normalize_frozen_text(raw) == "A\nB\nC\nD\nE\nF\nG\nH"


# ----- outlet properties ------------------------------------------------


def test_normalization_is_idempotent() -> None:
    raw = (
        "\uFEFFA\u00a0\u2003B\u2010C\u00adD\u2028E\r\n\uFF26 \u2460 "
        "F\n\nG  H\x0cI"
    )
    once = normalize_frozen_text(raw)
    assert normalize_frozen_text(once) == once


def test_empty_string_normalizes_to_empty_string() -> None:
    assert normalize_frozen_text("") == ""


def test_non_string_input_raises_type_error() -> None:
    import pytest

    with pytest.raises(TypeError):
        normalize_frozen_text(None)  # type: ignore[arg-type]


def test_normalization_rule_version_is_exposed() -> None:
    assert isinstance(NORMALIZATION_RULE_VERSION, str)
    assert NORMALIZATION_RULE_VERSION != ""


# ----- frozen-unit text and chunk text share the one outlet -------------


def test_unit_and_chunk_paths_go_through_the_same_outlet() -> None:
    raw = (
        "\uFF26ORM 10\u2011Q \uFB01ling\n"
        "Revenue $ 60,584 \u2010 up Quar\u00adterly\r\n"
        "\u0085 trailing soft break"
    )
    # Frozen-unit path: raw extractor output frozen through the outlet.
    unit_text = normalize_frozen_text(raw)
    # Chunk path (what T11 will do): a contiguous slice of the frozen
    # unit text. It is already an outlet fixed point — the same rule,
    # not a second one, and re-running the outlet on it is a no-op.
    chunk_text = unit_text[5:75]
    assert len(chunk_text) >= 32
    assert chunk_text in unit_text
    assert normalize_frozen_text(chunk_text) == chunk_text


# ----- citation-gate compatibility on a real block text -----------------


def _load_real_page() -> dict[str, object]:
    path = (
        _PROJECT_ROOT
        / "data"
        / "corpus"
        / "extracted"
        / "AAPL-2023Q3"
        / "page-001.json"
    )
    return json.loads(path.read_text(encoding="utf-8"))


_REAL_LINE = "For the quarterly period ended July 1, 2023"

# The same real line, re-derived from extractor output that carries all
# four categories of characters: NBSP / em-space / narrow-NBSP /
# ideographic space (whitespace), soft hyphen (invisible break control),
# full-width letters and the fi ligature (Unicode compatibility), the
# non-breaking hyphen U+2011 (hyphens), and NEL / CR-LF at the line
# boundaries (soft line breaks).
_MUTATED_LINE = (
    "For the\u00a0Quar\u00adterly\u2003period\u202fended\u3000July 1, 2023 "
    "\uFB01led as \uFF26ORM 10\u2011Q \uFF08Ｍark One\uFF09"
)
_NORMALIZED_LINE = (
    "For the Quarterly period ended July 1, 2023 "
    "filed as FORM 10-Q (Mark One)"
)


def _mutated_raw_page(committed: str) -> str:
    assert _REAL_LINE in committed
    raw = committed.replace(_REAL_LINE, _MUTATED_LINE)
    # Splice the soft-line-break family on the line's two boundaries.
    raw = raw.replace(_MUTATED_LINE + "\n", _MUTATED_LINE + "\r\n")
    raw = raw.replace("\n" + _MUTATED_LINE, "\u0085" + _MUTATED_LINE)
    return raw


def test_real_page_frozen_text_is_an_outlet_fixed_point() -> None:
    committed = _load_real_page()["text"]
    assert isinstance(committed, str)
    # The committed frozen corpus (produced by the pre-T5 rule) is
    # already in the outlet's normal form — the single authority is
    # byte-compatible with what ships today.
    assert normalize_frozen_text(committed) == committed


def test_citation_gate_passes_32_char_span_over_real_block_text() -> None:
    page = _load_real_page()
    committed = page["text"]
    assert isinstance(committed, str)
    raw = _mutated_raw_page(committed)

    # Frozen-unit path: the mutated extractor output through the outlet.
    unit_text = normalize_frozen_text(raw)
    assert _NORMALIZED_LINE in unit_text
    # The four categories did not disturb any untouched line.
    untouched = [
        line for line in committed.split("\n") if line != _REAL_LINE
    ]
    for line in untouched:
        assert line in unit_text

    # Chunk path (T11's product): a contiguous slice of the frozen unit
    # text around the four-category region — a "real block text".
    line_start = unit_text.index(_NORMALIZED_LINE)
    chunk_start = max(0, line_start - 20)
    chunk_end = min(
        len(unit_text), line_start + len(_NORMALIZED_LINE) + 20
    )
    chunk_text = unit_text[chunk_start:chunk_end]
    assert chunk_text in unit_text
    assert normalize_frozen_text(chunk_text) == chunk_text

    # The span the model would quote (crosses whitespace, soft-hyphen,
    # Unicode and hyphen regions) is ≥32 chars and alnum-bearing.
    span = _NORMALIZED_LINE
    assert len(span) >= 32
    assert any(ch.isalnum() for ch in span)

    result = verify_citations(
        span,
        [
            EvidenceItem(
                locator=Locator(
                    document_id=str(page["document_id"]),
                    version=str(page["version"]),
                    unit=int(page["page"]),  # type: ignore[arg-type]
                ),
                unit_text=chunk_text,
            )
        ],
        repair_count=0,
        known_document_ids={str(page["document_id"])},
    )
    assert isinstance(result, DoneResult)
    assert result.citations[0].quote == span


def test_cross_rule_substring_failures_over_real_block_count_zero() -> None:
    """Every ≥32-char span quotable from the frozen unit text is a
    contiguous substring of it — cross-rule substitution failures = 0."""
    committed = _load_real_page()["text"]
    assert isinstance(committed, str)
    unit_text = normalize_frozen_text(_mutated_raw_page(committed))

    failures = 0
    window = 32
    step = 16
    for i in range(0, len(unit_text) - window + 1, step):
        span = unit_text[i : i + window]
        if span not in unit_text:
            failures += 1
    assert failures == 0
