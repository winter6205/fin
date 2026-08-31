"""Opt-in contract test for the committed eval dataset (plan T10, SC27–SC30).

Runs under ``uv run pytest -q -m eval`` only (the tests/eval conftest
deselects eval-marked items from the default run, which therefore never
reads the dataset bytes). This test reads the real corpus — the frozen
tier-1 PDFs, the extracted tree, and ``data/corpus/dataset/`` — and
asserts the dataset contract end to end:

* the committed manifest is exactly what a fresh deterministic rebuild
  produces (byte-identical manifest, SC30);
* ``verify_dataset`` reports mismatch count = 0 and no on-disk drift;
* the manifest validates cleanly against the corpus registry;
* B1 floors are met everywhere except the documented known gaps
  (tier-1 PDF count pending the opt-in EDGAR expansion, and the B1
  chunk band scaling with that same pending expansion — the T11
  splitter computes the real deterministic chunk counts).
"""

from __future__ import annotations

import json

import pytest

from fin.corpus.dataset.build import (
    _dump,
    build_dataset,
    default_paths,
    verify_dataset,
)
from fin.corpus.dataset.manifest import (
    B1_MIN_QUERIES,
    B1_MIN_SAMPLES_PER_FORMAT,
    B1_MIN_SOURCE_DOCUMENTS,
    CHUNK_TOTAL_STATUS_SPLIT,
    HARD_SAMPLE_KINDS,
    validate_manifest,
)

pytestmark = pytest.mark.eval


def _registry(corpus_dir) -> dict[str, str]:
    manifest = json.loads((corpus_dir / "manifest.json").read_text(encoding="utf-8"))
    return {doc["document_id"]: doc["sha256"] for doc in manifest["documents"]}


def test_committed_manifest_matches_fresh_rebuild() -> None:
    corpus_dir, _, dataset_dir = default_paths()
    fresh = build_dataset(corpus_dir=corpus_dir, out_dir=dataset_dir, write=False)
    committed = json.loads((dataset_dir / "manifest.json").read_text(encoding="utf-8"))
    assert fresh == committed, "committed manifest drifted from a fresh rebuild"


def test_verify_dataset_mismatch_count_zero() -> None:
    corpus_dir, _, dataset_dir = default_paths()
    report = verify_dataset(corpus_dir=corpus_dir, dataset_dir=dataset_dir)
    assert report.ok, (report.mismatches, report.problems)
    assert report.mismatch_count == 0
    assert report.checked_samples >= B1_MIN_SOURCE_DOCUMENTS


def test_manifest_validates_against_corpus_registry() -> None:
    corpus_dir, _, dataset_dir = default_paths()
    payload = json.loads((dataset_dir / "manifest.json").read_text(encoding="utf-8"))
    report = validate_manifest(payload, primary_registry=_registry(corpus_dir))
    assert report.ok, report.problems
    assert report.counts["excluded"] == [], report.counts["excluded"]


def test_b1_floors() -> None:
    corpus_dir, _, dataset_dir = default_paths()
    payload = json.loads((dataset_dir / "manifest.json").read_text(encoding="utf-8"))
    counts = payload["counts"]

    # SC27: totals and composition (B1 数据集规模行).
    assert counts["total"] >= B1_MIN_SOURCE_DOCUMENTS
    by_format = counts["by_format"]
    assert len(by_format) == 7
    short = {f: n for f, n in by_format.items() if n < B1_MIN_SAMPLES_PER_FORMAT}
    # The one documented gap: tier-1 PDFs await the opt-in EDGAR expansion.
    assert set(short) == {"pdf"}, f"unexpected formats below the floor: {short}"
    assert by_format["pdf"] == 12
    known_gaps = {gap["gap"] for gap in payload["known_gaps"]}
    assert "primary_pdf_below_per_format_floor" in known_gaps

    # SC28: every format has >= 1 countable hard sample with a real kind.
    for fmt, n in counts["hard_by_format"].items():
        assert n >= 1, f"format {fmt} has no hard sample"
    for sample in payload["documents"]:
        if sample.get("is_hard"):
            assert sample["hard_kind"] in HARD_SAMPLE_KINDS

    # SC29 + B1 查询分档: >= 100 queries, four classes each >= 10%,
    # three document types with manual should-hit annotations.
    queries = payload["queries"]
    assert queries["total"] >= B1_MIN_QUERIES
    by_class: dict[str, int] = {}
    manual_types: set[str] = set()
    for q in queries["items"]:
        by_class[q["query_class"]] = by_class.get(q["query_class"], 0) + 1
        if q["annotation"] == "manual":
            manual_types.add(q["doc_type"])
        # locator identity points at a tier-1 document's frozen version
        assert q["expected_locator"]["version"] in _registry(corpus_dir).values()
        assert q["expected_locator"]["unit"] >= 1
    assert len(by_class) == 4
    for cls, n in by_class.items():
        assert n * 100 // queries["total"] >= 10, f"class {cls} below 10%"
    assert len(manual_types) >= 3, manual_types

    # T11: the splitter computes the real, deterministic chunk counts.
    assert payload["chunk_total_status"] == CHUNK_TOTAL_STATUS_SPLIT
    assert payload["chunk_total"] > 0
    assert payload["chunk_total"] == sum(
        sample["chunk_count"] for sample in payload["documents"]
    )
    assert all(
        isinstance(sample["chunk_count"], int) and sample["chunk_count"] >= 0
        for sample in payload["documents"]
    )
