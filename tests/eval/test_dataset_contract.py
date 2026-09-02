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
    B1_MAX_CHUNKS,
    B1_MIN_CHUNKS,
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
    # Plan C: EDGAR iXBRL expansion closed the primary tier-1 floor
    # (html_live docs are counted under format="html"); the per-format
    # pdf floor is structural (see per-format threshold entry, marked
    # unenforceable) and is asserted below by
    # ``test_plan_c_b1_floors_closed_after_expand_primary``.
    short = {f: n for f, n in by_format.items() if n < B1_MIN_SAMPLES_PER_FORMAT}
    assert "pdf" in short, f"expected pdf < 20 (structural); got short={short}"

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


# Plan C acceptance: after expand-primary + rebuild, the B1 floors the
# corpus-build surface can close must be closed. tier-1 primary >= 20
# closes via the EDGAR iXBRL expansion (8 new html_live primary docs);
# the chunk band [20000, 120000] closes once those primaries feed the
# derived-export pipeline. The "pdf per-format < 20" and "chunk_total
# below the band" gaps must therefore no longer appear in known_gaps;
# the structural pdf<20 reality is still surfaced via the
# unenforceable threshold-report entry (the only remaining SSOT path
# for limits the corpus-build surface cannot close).
def test_plan_c_b1_floors_closed_after_expand_primary() -> None:
    corpus_dir, _, dataset_dir = default_paths()
    payload = json.loads((dataset_dir / "manifest.json").read_text(encoding="utf-8"))
    counts = payload["counts"]
    known_gaps = {gap["gap"] for gap in payload["known_gaps"]}

    # Tier-1 primary count closed (8 EDGAR iXBRL primary docs added).
    assert counts["by_origin"]["primary"] >= 20, counts["by_origin"]

    # Chunk band closed (more primary docs -> more derived samples ->
    # more child blocks; the T11 splitter re-derives every count).
    assert B1_MIN_CHUNKS <= payload["chunk_total"] <= B1_MAX_CHUNKS, (
        payload["chunk_total"],
        B1_MIN_CHUNKS,
        B1_MAX_CHUNKS,
    )

    # The two pre-Plan-C gaps must no longer be reported.
    assert "primary_pdf_below_per_format_floor" not in known_gaps, known_gaps
    assert "chunk_total_outside_b1_band" not in known_gaps, known_gaps

    # The structural pdf<20 reality must still be visible at the
    # threshold-report layer (the only channel left for limits the
    # corpus-build surface cannot close).
    report = validate_manifest(
        payload, primary_registry=_registry(corpus_dir)
    ).threshold_report
    per_format = next(
        e for e in report if e["criterion"].startswith("samples per format")
    )
    assert per_format["enforceable"] is False
    assert "pdf" in per_format["detail"]

    # Sanity: every format except pdf meets the per-format floor (pdf's
    # structural limit — only the 12 frozen upstream PDFs count — is
    # documented in the unenforceable per-format threshold entry).
    by_format = counts["by_format"]
    assert len(by_format) == 7
    for fmt, n in by_format.items():
        if fmt != "pdf":
            assert n >= B1_MIN_SAMPLES_PER_FORMAT, (fmt, n)
    assert by_format["pdf"] == 12
    assert counts["total"] >= B1_MIN_SOURCE_DOCUMENTS
