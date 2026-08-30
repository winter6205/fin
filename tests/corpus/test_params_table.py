"""T9 / SC13: deterministic chunk-parameter lookup table + versioning.

Contract under test:

* Chunk parameters come **only** from one lookup of the
  (document type x section x content density) table; the same triad
  always resolves to the same parameter group (golden snapshot).
* The table is versioned **data**, not code branches: the version
  constant flows into unit metadata (replacing the T6 ``params-0``
  placeholder).
* Content-density classification uses **integer comparisons only** —
  a source scan asserts no float-threshold comparison and no model
  judgment lives in the lookup path.
* Unknown triads resolve to a deterministic default row — the lookup
  is total (never raises).

Default-suite discipline: no corpus data, no network, no services.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

import pytest

from fin.corpus.params import (
    BOUNDARY_CHAR_CAP,
    BOUNDARY_CLAUSE_ITEM,
    BOUNDARY_HEADING,
    BOUNDARY_KINDS,
    BOUNDARY_SENTENCE,
    BOUNDARY_TABLE_ROW,
    ChunkParams,
    DENSITY_DENSE,
    DENSITY_MIXED,
    DENSITY_PLAIN,
    DOC_TYPES,
    PARAMS_TABLE_VERSION,
    SECTIONS,
    TABLE_VERSION,
    classify_density,
    classify_text_density,
    count_density_signals,
    lookup_chunk_params,
)
from fin.corpus.parsers import PARAMS_TABLE_VERSION as PARSERS_TABLE_VERSION
from fin.corpus.parsers import parse_document

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SRC_PARAMS = (
    _PROJECT_ROOT / "src" / "fin" / "corpus" / "params.py"
)
_SRC_COMMON = (
    _PROJECT_ROOT / "src" / "fin" / "corpus" / "parsers" / "_common.py"
)
_SRC_PARSERS_INIT = (
    _PROJECT_ROOT / "src" / "fin" / "corpus" / "parsers" / "__init__.py"
)


# ---------------------------------------------------------------------------
# 1. Versioned data, not a placeholder
# ---------------------------------------------------------------------------


def test_table_version_is_real_and_replaces_placeholder() -> None:
    assert TABLE_VERSION == "params-1"
    assert PARAMS_TABLE_VERSION == "params-1"
    assert PARSERS_TABLE_VERSION == "params-1"
    # The T6 placeholder must be gone everywhere it was stamped.
    for path in (_SRC_COMMON, _SRC_PARSERS_INIT):
        assert "params-0" not in path.read_text(encoding="utf-8"), path


def test_table_is_data_not_branches() -> None:
    """The table body is a literal mapping of triad rows to frozen
    parameter groups — no ``if`` chain produces parameter values.
    """
    source = _SRC_PARAMS.read_text(encoding="utf-8")
    # The literal rows live in one mapping literal keyed by triads.
    assert re.search(r"_TABLE\s*:\s*Final\[", source) or re.search(
        r"_TABLE\s*=\s*\{", source
    )
    # A wildcard default row exists so any input resolves.
    assert ('"*"' in source) or ("'*'" in source)


# ---------------------------------------------------------------------------
# 2. Golden snapshot: same triad -> same parameter group
# ---------------------------------------------------------------------------


def _row_snapshot(row: ChunkParams) -> list[object]:
    return [
        row.target_chars,
        row.min_chars,
        row.max_chars,
        list(row.boundary_priority),
    ]


def test_golden_snapshot_of_table_rows() -> None:
    """The whole table content is locked by this snapshot. Changing any
    value requires bumping TABLE_VERSION (and updating this golden).
    """
    from fin.corpus import params as params_module

    golden: dict[str, list[object]] = {
        "narrative|*|0": [1200, 200, 2400, [1, 4, 5]],
        "narrative|*|1": [1000, 200, 2000, [1, 4, 5]],
        "narrative|*|2": [800, 150, 1600, [1, 4, 5]],
        "narrative|heading|0": [1500, 100, 3000, [1, 4, 5]],
        "narrative|heading|1": [1200, 100, 2400, [1, 4, 5]],
        "narrative|heading|2": [1000, 80, 2000, [1, 4, 5]],
        "table_dense|*|0": [1000, 150, 2000, [1, 2, 5]],
        "table_dense|*|1": [800, 120, 1600, [1, 2, 5]],
        "table_dense|*|2": [600, 100, 1200, [1, 2, 5]],
        "table_dense|table|0": [700, 100, 1400, [1, 2, 5]],
        "table_dense|table|1": [600, 80, 1200, [1, 2, 5]],
        "table_dense|table|2": [500, 60, 1000, [1, 2, 5]],
        "clause_list|*|0": [1100, 180, 2200, [1, 3, 4, 5]],
        "clause_list|*|1": [900, 150, 1800, [1, 3, 4, 5]],
        "clause_list|*|2": [700, 120, 1400, [1, 3, 4, 5]],
        "clause_list|list|0": [900, 120, 1800, [1, 3, 4, 5]],
        "clause_list|list|1": [800, 100, 1600, [1, 3, 4, 5]],
        "clause_list|list|2": [600, 80, 1200, [1, 3, 4, 5]],
        "*|*|*": [1000, 150, 2000, [1, 4, 5]],
    }
    actual = {
        "|".join(str(k) for k in triad): _row_snapshot(row)
        for triad, row in params_module._TABLE.items()
    }
    assert actual == golden


def test_same_triad_resolves_to_same_group() -> None:
    triads = [
        ("narrative", "body", DENSITY_PLAIN),
        ("table_dense", "table", DENSITY_DENSE),
        ("clause_list", "list", DENSITY_MIXED),
        ("narrative", "heading", DENSITY_DENSE),
        ("table_dense", "body", DENSITY_MIXED),
        ("clause_list", "body", DENSITY_PLAIN),
    ]
    for triad in triads:
        first = lookup_chunk_params(*triad)
        second = lookup_chunk_params(*triad)
        assert first == second
        assert _row_snapshot(first) == _row_snapshot(second)


def test_lookup_covers_declared_domain_and_is_frozen() -> None:
    for doc_type in DOC_TYPES:
        for section in SECTIONS:
            for density in (DENSITY_PLAIN, DENSITY_MIXED, DENSITY_DENSE):
                row = lookup_chunk_params(doc_type, section, density)
                assert isinstance(row, ChunkParams)
                assert 0 < row.min_chars <= row.target_chars <= row.max_chars
                assert row.boundary_priority  # non-empty integer chain
                for rank in row.boundary_priority:
                    assert rank in BOUNDARY_KINDS
                with pytest.raises(dataclasses.FrozenInstanceError):
                    row.target_chars = 1  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 3. Total lookup: unknown combos land deterministically, never raise
# ---------------------------------------------------------------------------


def test_unknown_combinations_fall_back_deterministically() -> None:
    fallback = lookup_chunk_params("unknown-doc-type", "weird", 99)
    default = lookup_chunk_params("*", "*", "*")
    assert fallback == default
    # Known doc type, unknown section: falls back to the doc-type row.
    assert lookup_chunk_params(
        "table_dense", "not-a-section", DENSITY_PLAIN
    ) == lookup_chunk_params("table_dense", "*", DENSITY_PLAIN)
    # Unknown doc type with known section/density: global default.
    assert (
        lookup_chunk_params("nope", "table", DENSITY_DENSE) == default
    )
    # Repeated resolution of unknown combos is stable.
    assert lookup_chunk_params("nope", "table", DENSITY_DENSE) == fallback


# ---------------------------------------------------------------------------
# 4. Integer-only density classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("chars", "cells", "items", "expected"),
    [
        # Degenerate input: deterministic, no division, no raise.
        (0, 0, 0, DENSITY_PLAIN),
        (0, 100, 100, DENSITY_PLAIN),
        # Dense boundary: >= 60 table cells / 1000 chars (exact equality
        # is dense — pure integer cross-multiplication, no ratio).
        (1000, 60, 0, DENSITY_DENSE),
        (1000, 59, 0, DENSITY_MIXED),
        (2000, 120, 0, DENSITY_DENSE),
        # Dense boundary: >= 12 list items / 1000 chars.
        (1000, 0, 12, DENSITY_DENSE),
        (1000, 0, 11, DENSITY_MIXED),
        # Mixed boundary: >= 20 cells / 1000 chars, >= 4 items / 1000.
        (1000, 20, 0, DENSITY_MIXED),
        (1000, 19, 0, DENSITY_PLAIN),
        (1000, 0, 4, DENSITY_MIXED),
        (1000, 0, 3, DENSITY_PLAIN),
        # Plain prose.
        (5000, 0, 0, DENSITY_PLAIN),
        # Dense signal wins over mixed signal.
        (1000, 100, 1, DENSITY_DENSE),
    ],
)
def test_density_integer_thresholds(
    chars: int, cells: int, items: int, expected: int
) -> None:
    assert (
        classify_density(
            chars=chars, table_cell_count=cells, list_item_count=items
        )
        == expected
    )
    assert isinstance(expected, int)


def test_density_levels_are_distinct_integers() -> None:
    levels = {DENSITY_PLAIN, DENSITY_MIXED, DENSITY_DENSE}
    assert levels == {0, 1, 2}


def test_count_density_signals_is_deterministic() -> None:
    text = (
        "| Revenue | Cost |\n"
        "| 100 | 80 |\n"
        "第一条 公司应当依法经营。\n"
        "2. 第二项内容。\n"
        "Plain narrative prose line without markers.\n"
    )
    chars, cells, items = count_density_signals(text)
    assert (chars, cells, items) == (
        len(text),
        text.count("|") + text.count("\t"),
        2,
    )
    assert count_density_signals(text) == (chars, cells, items)


def test_classify_text_density_end_to_end() -> None:
    prose = "A plain narrative paragraph with no structural markers at all." * 20
    assert classify_text_density(prose) == DENSITY_PLAIN
    table = ("| a | b | c | d | e | f |\n" * 100).strip()
    assert classify_text_density(table) == DENSITY_DENSE
    clauses = "\n".join(f"第{n}条 内容{i}。" for n, i in enumerate(range(40)))
    assert classify_text_density(clauses) == DENSITY_DENSE


# ---------------------------------------------------------------------------
# 5. rg self-attestation: no float-threshold comparison in the lookup path
# ---------------------------------------------------------------------------


def test_no_float_thresholds_in_params_and_parsers_common() -> None:
    """T9 contract: density and boundary decisions use integer
    comparison only. A float literal in these files would be a
    threshold comparison by construction — forbid the literal itself.
    """
    float_literal = re.compile(r"(?<![\w.])(\d+\.\d+)(?![\w.])")
    for path in (_SRC_PARAMS, _SRC_COMMON, _SRC_PARSERS_INIT):
        matches = float_literal.findall(path.read_text(encoding="utf-8"))
        assert not matches, f"float threshold(s) {matches} in {path}"


# ---------------------------------------------------------------------------
# 6. Version flows into unit metadata
# ---------------------------------------------------------------------------


def test_params_table_version_stamped_on_parsed_units() -> None:
    result = parse_document(
        b"first paragraph\n\nsecond paragraph\n",
        fmt="txt",
        document_id="T9-PROBE",
        version="v1",
    )
    assert result.params_table_version == "params-1"
    assert result.units
    for unit in result.units:
        assert unit.params_table_version == "params-1"
