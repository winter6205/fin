"""Tests for the eval dataset builder (plan T10) — small fixtures only.

These tests never read ``data/corpus`` (the real corpus bytes stay out
of the default ``uv run pytest -q`` per SC1 / plan T10 acceptance):
every fixture is a tiny synthetic-but-explicit corpus directory under
``tmp_path`` whose extracted texts are crafted to classify cleanly into
the three required document types.

The manual-annotation module-level tuple and the phrase-probe target
are monkeypatched to fixture equivalents, so no real annotation is
exercised here — the real annotations are verified at build time
against the real extracted tree (eval-marked contract test).
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from fin.corpus.dataset import build as build_mod
from fin.corpus.dataset.build import (
    build_dataset,
    verify_dataset,
)
from fin.corpus.dataset.exporters import (
    DerivedDoc,
    FrozenUnit,
    export_docx,
    export_derived,
    export_html,
    export_markdown,
    export_pptx,
    export_text,
    export_xlsx,
)
from fin.corpus.dataset.manifest import (
    compute_counts,
    is_countable,
    validate_manifest,
)
from fin.corpus.dataset.pages import (
    CLAUSE_LIST,
    NARRATIVE,
    TABLE_DENSE,
    classify_page,
    detect_repeated_lines,
    is_repeated_page_furniture,
    split_table_row,
)
from fin.corpus.dataset.queries import generate_phrase_queries

# --------------------------------------------------------------------------
# Fixture corpus
# --------------------------------------------------------------------------

_HEADER = "Table of Contents"
_FOOTER = "testcorp.example/footer"

_TABLE_TEXT = "\n".join(
    [
        _HEADER,
        "TEST CONSOLIDATED STATEMENTS",
        "(In millions)",
        "Three Months Ended",
        "Total net sales 1,234 1,100",
        "Net income 234 210",
        "Gross margin 567 500",
        _FOOTER,
    ]
)

_CLAUSE_TEXT = "\n".join(
    [
        _HEADER,
        "CERTIFICATION",
        "I, Test Officer, certify that:",
        "1. I have reviewed this quarterly report with reasonable scrutiny;",
        "2. Based on my knowledge the statements are not misleading in any material respect;",
        "3. All financial statements and controls have been reviewed by the registrant;",
        _FOOTER,
    ]
)

_NARRATIVE_LINES = [
    _HEADER,
    "Management's Discussion and Analysis",
    "The company continued to execute its long-term strategy with disciplined investment across all segments this quarter.",
    "Operating efficiency improved while the company maintained headcount discipline and kept hiring plans modest.",
    "The macroeconomic environment remained uncertain but demand for the product portfolio stayed resilient overall.",
    _FOOTER,
]
_NARRATIVE_TEXT = "\n".join(_NARRATIVE_LINES)


def _sha256_hex(payload: bytes) -> str:
    import hashlib

    return hashlib.sha256(payload).hexdigest()


PDF_BYTES = b"%PDF-1.4 fixture corpus bytes\n"
PDF_SHA = _sha256_hex(PDF_BYTES)


def _make_corpus(tmp_path: Path) -> tuple[Path, Path]:
    """Write a 2-document corpus dir; returns (corpus_dir, out_dir)."""
    corpus = tmp_path / "corpus"
    (corpus / "pdf").mkdir(parents=True)
    extracted = corpus / "extracted"
    docs = {
        "TESTA-2022Q3": {1: _TABLE_TEXT, 2: _CLAUSE_TEXT, 3: _NARRATIVE_TEXT},
        "TESTB-2022Q3": {1: _TABLE_TEXT, 2: _CLAUSE_TEXT, 3: _NARRATIVE_TEXT},
    }
    manifest_docs = []
    index_docs: dict[str, dict] = {}
    for doc_id, pages in docs.items():
        (corpus / "pdf" / f"{doc_id}.pdf").write_bytes(PDF_BYTES)
        index_docs[doc_id] = {"page_count": len(pages), "version": PDF_SHA}
        manifest_docs.append(
            {
                "document_id": doc_id,
                "filename": f"{doc_id}.pdf",
                "sha256": PDF_SHA,
                "period": "2022 Q3",
            }
        )
        doc_dir = extracted / doc_id
        doc_dir.mkdir(parents=True)
        for page, text in pages.items():
            (doc_dir / f"page-{page:03d}.json").write_text(
                json.dumps(
                    {
                        "document_id": doc_id,
                        "page": page,
                        "text": text,
                        "version": PDF_SHA,
                    }
                ),
                encoding="utf-8",
            )
    (corpus / "manifest.json").write_text(
        json.dumps({"documents": manifest_docs}), encoding="utf-8"
    )
    (extracted / "_index.json").write_text(
        json.dumps({"documents": index_docs}), encoding="utf-8"
    )
    return corpus, tmp_path / "dataset"


@pytest.fixture
def small_annotations(monkeypatch):
    """Fixture manual annotations: one per document type."""
    from fin.corpus.dataset.pages import FrozenUnit  # noqa: F401  (typing aid)

    anns = (
        {
            "query_id": "q-factoid-001",
            "query_class": "factoid",
            "doc_type": "table_dense",
            "document_id": "TESTA-2022Q3",
            "unit": 1,
            "question": "What were Test Corp's total net sales?",
            "evidence_span": "Total net sales 1,234 1,100",
        },
        {
            "query_id": "q-narr-001",
            "query_class": "narrative",
            "doc_type": "narrative",
            "document_id": "TESTA-2022Q3",
            "unit": 3,
            "question": "How did the company describe its investment posture?",
            "evidence_span": "disciplined investment across all segments",
        },
        {
            "query_id": "q-narr-002",
            "query_class": "narrative",
            "doc_type": "clause_list",
            "document_id": "TESTB-2022Q3",
            "unit": 2,
            "question": "What does the certification say about review?",
            "evidence_span": "1. I have reviewed this quarterly report",
        },
        {
            "query_id": "q-cross-001",
            "query_class": "cross_period",
            "doc_type": "table_dense",
            "document_id": "TESTB-2022Q3",
            "unit": 1,
            "question": "How did total net sales change period over period?",
            "evidence_span": "Total net sales 1,234 1,100",
        },
    )
    monkeypatch.setattr(build_mod, "MANUAL_ANNOTATIONS", anns)
    monkeypatch.setattr(build_mod, "PHRASE_QUERY_TARGET", 6)
    return anns


@pytest.fixture
def built(tmp_path, small_annotations):
    corpus, out = _make_corpus(tmp_path)
    payload = build_dataset(corpus_dir=corpus, out_dir=out, write=True)
    return corpus, out, payload


# --------------------------------------------------------------------------
# Page helpers
# --------------------------------------------------------------------------


class TestPageHelpers:
    def test_classify_page_three_types(self):
        assert classify_page(_TABLE_TEXT) == TABLE_DENSE
        assert classify_page(_CLAUSE_TEXT) == CLAUSE_LIST
        assert classify_page(_NARRATIVE_TEXT) == NARRATIVE

    def test_classify_is_integer_deterministic(self):
        # Same input twice, same output; no float thresholds involved.
        assert classify_page(_TABLE_TEXT) == classify_page(_TABLE_TEXT)

    def test_split_table_row_cells(self):
        cells = split_table_row("Total net sales $ 19,442 $ 21,744 $ 79,082")
        assert cells == ["Total net sales", "$ 19,442", "$ 21,744", "$ 79,082"]

    def test_repeated_lines_detected(self):
        units = [
            FrozenUnit(unit=1, text=_TABLE_TEXT),
            FrozenUnit(unit=2, text=_CLAUSE_TEXT),
            FrozenUnit(unit=3, text=_NARRATIVE_TEXT),
        ]
        header, footer = detect_repeated_lines(units)
        assert header == _HEADER
        assert footer == _FOOTER
        assert is_repeated_page_furniture(units)


# --------------------------------------------------------------------------
# Exporters: determinism + structural validity
# --------------------------------------------------------------------------


def _zip_member(payload: bytes, name: str) -> bytes:
    import zipfile

    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        return zf.read(name)


def _small_doc() -> DerivedDoc:
    units = (FrozenUnit(unit=1, text=_TABLE_TEXT), FrozenUnit(unit=2, text=_NARRATIVE_TEXT))
    return DerivedDoc(
        sample_id="TESTA-2022Q3__table_dense",
        fmt="md",
        document_id="TESTA-2022Q3",
        version=PDF_SHA,
        doc_type=TABLE_DENSE,
        units=units,
    )


class TestExporters:
    def test_text_formats_deterministic_and_verbatim(self):
        doc = _small_doc()
        for exporter in (export_markdown, export_text, export_html):
            first = exporter(doc)
            second = exporter(doc)
            assert first == second
            assert _TABLE_TEXT.encode() in first  # verbatim frozen text
            assert PDF_SHA.encode() in first  # citation identity points back

    def test_ooxml_deterministic(self):
        doc = _small_doc()
        for exporter in (export_docx, export_xlsx, export_pptx):
            assert exporter(doc) == exporter(doc)

    def test_ooxml_members_are_well_formed(self):
        import zipfile
        from xml.dom.minidom import parseString

        doc = _small_doc()
        required = {
            "docx": {"[Content_Types].xml", "_rels/.rels", "word/document.xml"},
            "xlsx": {"[Content_Types].xml", "_rels/.rels", "xl/workbook.xml"},
            "pptx": {
                "[Content_Types].xml",
                "_rels/.rels",
                "ppt/presentation.xml",
                "ppt/slideMasters/slideMaster1.xml",
                "ppt/slideLayouts/slideLayout1.xml",
                "ppt/theme/theme1.xml",
                "ppt/slides/slide1.xml",
            },
        }
        for fmt, exporter in (("docx", export_docx), ("xlsx", export_xlsx), ("pptx", export_pptx)):
            payload = exporter(doc)
            with zipfile.ZipFile(io.BytesIO(payload)) as zf:
                assert zf.testzip() is None  # CRCs valid
                names = set(zf.namelist())
                assert required[fmt] <= names
                for name in names:
                    if name.endswith((".xml", ".rels")):
                        parseString(zf.read(name))  # XML well-formed

    def test_hard_variants_embed_real_difficulty(self):
        from fin.corpus.dataset.exporters import HARD_MERGED_CELLS, HARD_MULTI_COLUMN

        base = _small_doc()
        table = build_mod.build_hard_table(base.units[0])

        def _variant(fmt: str, **extra) -> DerivedDoc:
            return DerivedDoc(
                sample_id=base.sample_id,
                fmt=fmt,
                document_id=base.document_id,
                version=base.version,
                doc_type=base.doc_type,
                units=base.units,
                hard_kind=extra.get("hard_kind"),
                hard_table=extra.get("hard_table"),
            )

        docx_payload = export_derived(
            _variant("docx", hard_kind=HARD_MERGED_CELLS, hard_table=table)
        )
        docx_xml = _zip_member(docx_payload, "word/document.xml")
        assert b"gridSpan" in docx_xml  # merged title cell
        xlsx_payload = export_derived(
            _variant("xlsx", hard_kind=HARD_MERGED_CELLS, hard_table=table)
        )
        assert any(
            b"mergeCells" in _zip_member(xlsx_payload, name)
            for name in ("xl/worksheets/sheet1.xml", "xl/worksheets/sheet2.xml")
        )
        html_payload = export_derived(
            _variant("html", hard_kind=HARD_MULTI_COLUMN)
        )
        assert b"column-count: 2" in html_payload


# --------------------------------------------------------------------------
# Counting discipline
# --------------------------------------------------------------------------


class TestCounting:
    def test_synthetic_and_unsourced_excluded(self, built):
        corpus, out, payload = built
        samples = list(payload["documents"])
        base_total = payload["counts"]["total"]
        synthetic = {
            "sample_id": "fake__synthetic",
            "format": "md",
            "origin": "synthetic",
            "synthetic": True,
            "sha256": "0" * 64,
            "license": "n/a",
        }
        no_license = {
            "sample_id": "fake__no_license",
            "format": "md",
            "origin": "derived",
            "sha256": "1" * 64,
            "derived_from": {"document_id": "TESTA-2022Q3", "version": PDF_SHA},
        }
        no_source = {
            "sample_id": "fake__no_source",
            "format": "md",
            "origin": "derived",
            "sha256": "2" * 64,
            "license": "n/a",
        }
        samples.extend([synthetic, no_license, no_source])
        counts = compute_counts(samples)
        assert counts["total"] == base_total  # none of the three counted
        reasons = {e["sample_id"]: e["reason"] for e in counts["excluded"]}
        assert reasons["fake__synthetic"] == "synthetic"
        assert reasons["fake__no_license"] == "missing_license"
        assert reasons["fake__no_source"] == "missing_source"
        ok, reason = is_countable(synthetic)
        assert not ok and reason == "synthetic"

    def test_derived_samples_carry_source_and_license(self, built):
        _, _, payload = built
        derived = [s for s in payload["documents"] if s["origin"] == "derived"]
        assert derived
        for sample in derived:
            ok, _ = is_countable(sample)
            assert ok
            assert sample["license"].startswith("Derived (tier 3)")
            assert sample["derived_from"]["version"] == PDF_SHA

    def test_all_samples_countable_in_fixture(self, built):
        _, _, payload = built
        assert payload["counts"]["excluded"] == []
        assert payload["counts"]["total"] == len(payload["documents"])


# --------------------------------------------------------------------------
# Build determinism + verify (SC30 discipline on fixtures)
# --------------------------------------------------------------------------


class TestBuildDeterminism:
    def test_two_builds_identical(self, tmp_path, small_annotations):
        corpus, out = _make_corpus(tmp_path)
        first = build_dataset(corpus_dir=corpus, out_dir=out / "one", write=True)
        second = build_dataset(corpus_dir=corpus, out_dir=out / "two", write=True)
        assert (out / "one" / "manifest.json").read_bytes() == (
            out / "two" / "manifest.json"
        ).read_bytes()
        assert first["counts"] == second["counts"]
        one_files = sorted(p.name for p in (out / "one" / "documents").rglob("*") if p.is_file())
        two_files = sorted(p.name for p in (out / "two" / "documents").rglob("*") if p.is_file())
        assert one_files == two_files

    def test_verify_ok_after_build(self, built):
        corpus, out, payload = built
        report = verify_dataset(corpus_dir=corpus, dataset_dir=out)
        assert report.ok, (report.mismatches, report.problems)
        assert report.mismatch_count == 0
        assert report.checked_samples == payload["counts"]["total"]

    def test_verify_detects_committed_sha_drift(self, built):
        corpus, out, _ = built
        manifest_path = out / "manifest.json"
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        target = next(
            s for s in payload["documents"] if s["origin"] == "derived"
        )
        target["sha256"] = "f" * 64
        manifest_path.write_text(json.dumps(payload), encoding="utf-8")
        report = verify_dataset(corpus_dir=corpus, dataset_dir=out)
        assert not report.ok
        assert any(m["kind"] == "sha256" for m in report.mismatches)

    def test_verify_detects_on_disk_drift(self, built):
        corpus, out, _ = built
        manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
        sample = next(s for s in manifest["documents"] if s["origin"] == "derived")
        path = Path(sample["path"])
        # sample["path"] is repo-relative; rewrite it to a tmp-relative file
        (out / "documents" / sample["format"] / Path(path).name).write_bytes(b"tampered")
        report = verify_dataset(corpus_dir=corpus, dataset_dir=out)
        assert not report.ok
        assert any("on-disk drift" in p for p in report.problems)


# --------------------------------------------------------------------------
# Manifest validation
# --------------------------------------------------------------------------


class TestManifestValidation:
    def test_valid_manifest_has_no_problems(self, built):
        corpus, out, payload = built
        registry = {d: PDF_SHA for d in ("TESTA-2022Q3", "TESTB-2022Q3")}
        report = validate_manifest(payload, primary_registry=registry)
        assert report.ok, report.problems

    def test_missing_hard_sample_flagged(self, built):
        corpus, out, payload = built
        payload["documents"] = [
            s for s in payload["documents"] if not (s.get("is_hard") and s["format"] == "md")
        ]
        report = validate_manifest(payload)
        assert not report.ok
        assert any("format md" in p for p in report.problems)

    def test_derived_from_must_point_at_registry(self, built):
        _, _, payload = built
        sample = next(
            s for s in payload["documents"] if s["origin"] == "derived"
        )
        sample["derived_from"] = {"document_id": "GHOST-2022Q3", "version": PDF_SHA}
        registry = {d: PDF_SHA for d in ("TESTA-2022Q3", "TESTB-2022Q3")}
        report = validate_manifest(payload, primary_registry=registry)
        assert not report.ok
        assert any("GHOST-2022Q3" in p for p in report.problems)

    def test_query_class_share_floor(self, built):
        _, _, payload = built
        items = payload["queries"]["items"]
        skewed = [dict(q, query_class="phrase") for q in items]
        payload["queries"]["items"] = skewed
        report = validate_manifest(payload)
        assert not report.ok
        assert any("query class share" in p for p in report.problems)

    def test_manual_annotation_coverage_required(self, built):
        _, _, payload = built
        for q in payload["queries"]["items"]:
            if q["annotation"] == "manual":
                q["annotation"] = "derived_span"
        report = validate_manifest(payload)
        assert not report.ok
        assert any("manual" in p and "should-hit" in p for p in report.problems)


# --------------------------------------------------------------------------
# Phrase probes
# --------------------------------------------------------------------------


class TestPhraseProbes:
    def test_deterministic_and_exact_substrings(self, tmp_path):
        units = {
            "TESTA-2022Q3": [
                FrozenUnit(unit=1, text=_TABLE_TEXT),
                FrozenUnit(unit=2, text=_CLAUSE_TEXT),
                FrozenUnit(unit=3, text=_NARRATIVE_TEXT),
            ]
        }
        first = generate_phrase_queries(units, target=3)
        second = generate_phrase_queries(units, target=3)
        assert first == second
        joined = "\n".join(u.text for u in units["TESTA-2022Q3"])
        for q in first:
            assert q["question"] in joined  # exact frozen substring
            assert q["annotation"] == "derived_span"

    def test_shortfall_raises(self, tmp_path):
        units = {"TESTA-2022Q3": [FrozenUnit(unit=1, text=_TABLE_TEXT)]}
        with pytest.raises(ValueError):
            generate_phrase_queries(units, target=5)
