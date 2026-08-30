"""Boundary tests for the v1 ingest command and the extracted tree loader.

The five boundary classes come from the defensive-contract-validator
axis: empty, negative, overflow, concurrent, exception. Together they
exercise the typed-error contract (ADR 0020) that the host graph, the
retrieve adapter, and the citation gate will rely on.

The real-ingest tests share a session-scoped fixture that runs
``run_ingest`` once against the committed corpus (~2.5 minutes on the
full 12 PDFs). Per-test mutations on that tree are restored in a
``finally`` block so subsequent tests see an intact tree.
"""

from __future__ import annotations

import ast
import concurrent.futures
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from fin.corpus.errors import (
    IngestMissingError,
    MalformedExtractedIndexError,
    ManifestShaMismatchError,
)
from fin.corpus.extracted import Locator, load_extracted_tree
from fin.corpus.ingest import run_ingest


# ---------- helpers ----------


def _project_data_dir() -> Path:
    """Absolute path to the project's committed ``data/corpus`` dir.

    The worktree root is the parent of ``src/fin/corpus/``; the frozen
    PDFs and manifest live under ``data/corpus/``.
    """
    return Path(__file__).resolve().parents[2] / "data" / "corpus"


def _write_manifest(manifest_path: Path, documents: list[dict]) -> None:
    payload = {
        "upstream": {
            "repo": "test",
            "commit": "test",
            "path": "test",
            "license": "test",
        },
        "tickers": [],
        "periods": [],
        "note": "boundary-test fixture",
        "documents": documents,
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(payload))


def _copy_pdf_into(src_pdf: Path, dst_pdf: Path) -> str:
    """Copy ``src_pdf`` to ``dst_pdf``; return sha256 of the copy."""
    shutil.copyfile(src_pdf, dst_pdf)
    return _sha256_file(dst_pdf)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _hash_tree(root: Path) -> dict[str, str]:
    """Recursive sha256 of every file under ``root``, keyed by
    relative path. Used to assert byte-level idempotency.
    """
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            out[str(path.relative_to(root))] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return out


def _real_manifest() -> tuple[Path, Path, list[dict]]:
    """Point at the real committed manifest + PDFs.

    Returns ``(manifest_path, pdf_dir, documents)``.
    """
    data_dir = _project_data_dir()
    manifest_path = data_dir / "manifest.json"
    pdf_dir = data_dir / "pdf"
    documents = json.loads(manifest_path.read_text())["documents"]
    return manifest_path, pdf_dir, documents


# ---------- session-scoped real-ingest fixture ----------


@pytest.fixture(scope="session")
def real_ingested_tree(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Run ``run_ingest`` once against the real corpus and share the
    populated tree across tests.

    Real ingest is ~2.5 minutes for all 12 PDFs; running it inside a
    session-scoped fixture makes the test suite tractable while still
    exercising the public ``run_ingest`` entry point over the actual
    frozen data. Tests that need to mutate the tree restore files in a
    ``finally`` block so the next test sees an intact tree.
    """
    manifest_path, pdf_dir, _ = _real_manifest()
    extracted_root = tmp_path_factory.mktemp("real-ingest") / "extracted"
    run_ingest(
        manifest_path=manifest_path,
        pdf_dir=pdf_dir,
        extracted_root=extracted_root,
    )
    return extracted_root


# ---------- boundary 1: empty ----------


def test_run_ingest_with_empty_manifest_writes_empty_tree(tmp_path: Path) -> None:
    """Empty manifest must succeed deterministically with no extracted files.

    This is the "empty input" boundary: ``run_ingest`` must NOT raise
    when the manifest lists zero documents. The resulting tree has only
    the ``_index.json`` metadata file and is byte-stable across runs.
    """
    manifest_path = tmp_path / "manifest.json"
    pdf_dir = tmp_path / "pdf"
    extracted_root = tmp_path / "extracted"
    pdf_dir.mkdir(parents=True, exist_ok=True)

    _write_manifest(manifest_path, documents=[])

    first = run_ingest(
        manifest_path=manifest_path,
        pdf_dir=pdf_dir,
        extracted_root=extracted_root,
    )
    second = run_ingest(
        manifest_path=manifest_path,
        pdf_dir=pdf_dir,
        extracted_root=extracted_root,
    )

    assert first.documents == 0
    assert first.pages == 0
    assert first.pages_written == 0
    # Second run must skip everything because nothing was written.
    assert second.pages == 0
    assert second.pages_written == 0
    assert second.pages_skipped == 0
    # Empty tree has no document dirs, only the index metadata.
    assert (extracted_root / "_index.json").exists()
    doc_dirs = [p for p in extracted_root.iterdir() if p.is_dir()]
    assert doc_dirs == []


# ---------- boundary 2: negative / missing file ----------


def test_run_ingest_missing_pdf_raises_typed_error(tmp_path: Path) -> None:
    """Manifest references a PDF that does not exist.

    The boundary is "negative / missing file": ``run_ingest`` must raise
    the typed ``ManifestShaMismatchError`` (a subclass of the
    ``IngestMissingError`` family) and not silently create the tree.
    """
    manifest_path = tmp_path / "manifest.json"
    pdf_dir = tmp_path / "pdf"
    extracted_root = tmp_path / "extracted"
    pdf_dir.mkdir(parents=True, exist_ok=True)

    _write_manifest(
        manifest_path,
        documents=[
            {
                "document_id": "TEST-2023Q1",
                "filename": "missing.pdf",
                "sha256": "0" * 64,
            }
        ],
    )

    with pytest.raises(ManifestShaMismatchError):
        run_ingest(
            manifest_path=manifest_path,
            pdf_dir=pdf_dir,
            extracted_root=extracted_root,
        )

    # No tree should have been created — the typed error short-circuits.
    assert not (extracted_root / "TEST-2023Q1").exists()


def test_run_ingest_malformed_existing_index_fails_closed(
    tmp_path: Path,
) -> None:
    """A present malformed index is corruption, not a missing index.

    Ingest must raise before it computes or writes a replacement index.
    The original bytes are the evidence needed to repair the extracted
    tree, so they must remain untouched.
    """
    manifest_path = tmp_path / "manifest.json"
    pdf_dir = tmp_path / "pdf"
    extracted_root = tmp_path / "extracted"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    _write_manifest(manifest_path, documents=[])

    extracted_root.mkdir()
    damaged = extracted_root / "_index.json"
    original = b"{ this is not valid json"
    damaged.write_bytes(original)

    with pytest.raises(MalformedExtractedIndexError):
        run_ingest(
            manifest_path=manifest_path,
            pdf_dir=pdf_dir,
            extracted_root=extracted_root,
        )

    assert damaged.read_bytes() == original


def test_ingest_process_returns_nonzero_for_malformed_existing_index(
    tmp_path: Path,
) -> None:
    """The ingest CLI exposes the typed corruption failure as non-zero."""
    manifest_path = tmp_path / "manifest.json"
    pdf_dir = tmp_path / "pdf"
    extracted_root = tmp_path / "extracted"
    facts_root = tmp_path / "facts"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    _write_manifest(manifest_path, documents=[])

    extracted_root.mkdir()
    damaged = extracted_root / "_index.json"
    original = b"{ this is not valid json"
    damaged.write_bytes(original)

    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "fin.corpus.ingest",
            "--manifest",
            str(manifest_path),
            "--pdf-dir",
            str(pdf_dir),
            "--extracted-root",
            str(extracted_root),
            "--facts-root",
            str(facts_root),
            "--ocr-engine",
            "none",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert process.returncode != 0
    assert "MalformedExtractedIndexError" in process.stderr
    assert damaged.read_bytes() == original


def test_run_ingest_sha_drift_raises_typed_error(tmp_path: Path) -> None:
    """Manifest sha256 does not match the on-disk PDF.

    Boundary class "negative": the operator's ``data/corpus/pdf/`` has
    drifted (a byte was added or modified). ``run_ingest`` must refuse
    to extract from the drifted source rather than silently re-extract.
    """
    _, pdf_dir, real_docs = _real_manifest()
    # Copy the first real PDF into the temp pdf_dir.
    sample = real_docs[0]
    src_pdf = pdf_dir / sample["filename"]
    test_pdf_dir = tmp_path / "pdf"
    test_pdf_dir.mkdir(parents=True, exist_ok=True)
    test_pdf = test_pdf_dir / sample["filename"]
    actual_sha = _copy_pdf_into(src_pdf, test_pdf)
    # Mutate one byte so the sha no longer matches.
    test_pdf.write_bytes(test_pdf.read_bytes() + b" ")
    assert _sha256_file(test_pdf) != actual_sha

    manifest_path = tmp_path / "manifest.json"
    _write_manifest(
        manifest_path,
        documents=[
            {
                "document_id": sample["document_id"],
                "filename": sample["filename"],
                "sha256": actual_sha,
            }
        ],
    )
    extracted_root = tmp_path / "extracted"

    with pytest.raises(ManifestShaMismatchError):
        run_ingest(
            manifest_path=manifest_path,
            pdf_dir=test_pdf_dir,
            extracted_root=extracted_root,
        )


# ---------- boundary 3: overflow (partial extraction) ----------


def test_load_extracted_tree_missing_page_raises_typed_error(
    real_ingested_tree: Path,
) -> None:
    """Loader fails fast with ``IngestMissingError`` when the tree is partial.

    Boundary "overflow": delete one page file from a populated tree,
    then ask the loader to validate. The loader must refuse with the
    typed error and not silently produce a truncated tree.
    """
    manifest_path, _, _ = _real_manifest()
    doc_id = sorted(p.name for p in real_ingested_tree.iterdir() if p.is_dir())[0]
    doc_dir = real_ingested_tree / doc_id
    page_files = sorted(p for p in doc_dir.glob("page-*.json"))
    assert len(page_files) >= 2
    victim = page_files[len(page_files) // 2]
    backup = victim.read_bytes()
    try:
        victim.unlink()
        with pytest.raises(IngestMissingError):
            load_extracted_tree(
                manifest_path=manifest_path,
                extracted_root=real_ingested_tree,
            )
    finally:
        victim.write_bytes(backup)


# ---------- boundary 4: concurrent ----------


def test_concurrent_run_ingest_yields_identical_tree(
    real_ingested_tree: Path,
) -> None:
    """Concurrent ``run_ingest`` reruns on a populated tree must be
    byte-identical to the original.

    Boundary "concurrent": per ADR 0020, ingest is idempotent and safe
    to re-run; under the defensive-contract validator this includes
    the case where multiple workers race to write the same tree. We
    use the session-scoped real tree as the baseline, copy it to a
    fresh ``tmp_path``, and then run four concurrent reruns against
    the copy. The fast-skip path means each rerun only does a sha
    check + filesystem existence check (~milliseconds per doc), so
    the four-way race is tractable.
    """
    manifest_path, pdf_dir, _ = _real_manifest()
    concurrent_root = real_ingested_tree.parent / "concurrent-copy"
    if concurrent_root.exists():
        shutil.rmtree(concurrent_root)
    shutil.copytree(real_ingested_tree, concurrent_root)

    # First ingest against an empty tree (with the same manifest
    # content but a separate extracted_root) is not what we test
    # here — the real assertion is that the *concurrent reruns* are
    # all no-ops and the final tree matches the baseline byte-for-byte.
    def _rerun() -> int:
        result = run_ingest(
            manifest_path=manifest_path,
            pdf_dir=pdf_dir,
            extracted_root=concurrent_root,
        )
        return result.pages_written

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        writes = list(pool.map(lambda _: _rerun(), range(4)))

    # Every concurrent rerun must be a no-op (zero new page files
    # written). This is the contract the host graph relies on.
    assert writes == [0, 0, 0, 0]
    # Final tree must be byte-identical to the baseline.
    assert _hash_tree(real_ingested_tree) == _hash_tree(concurrent_root)


# ---------- boundary 5: exception (malformed JSON) ----------


def test_load_extracted_tree_malformed_json_raises_typed_error(
    real_ingested_tree: Path,
) -> None:
    """Loader raises ``IngestMissingError`` on malformed JSON.

    Boundary "exception": the loader must NOT silently skip a corrupted
    file. The same typed error covers missing AND malformed artifacts,
    so callers can rely on ``IngestMissingError`` as the single
    recovery signal.
    """
    manifest_path, _, _ = _real_manifest()
    doc_id = sorted(p.name for p in real_ingested_tree.iterdir() if p.is_dir())[0]
    doc_dir = real_ingested_tree / doc_id
    page_files = sorted(p for p in doc_dir.glob("page-*.json"))
    victim = page_files[len(page_files) // 2]
    backup = victim.read_bytes()
    try:
        victim.write_text("{ this is not valid json")
        with pytest.raises(IngestMissingError):
            load_extracted_tree(
                manifest_path=manifest_path,
                extracted_root=real_ingested_tree,
            )
    finally:
        victim.write_bytes(backup)


# ---------- ask-path smoke ----------


def test_extracted_module_does_not_import_pdfplumber() -> None:
    """The ask-path loader must not depend on ``pdfplumber`` (ADR 0020).

    Boundary class "negative" applied to the read-side contract: if
    this assertion ever fails, the loader has acquired a runtime
    dependency that pulls PDF extraction into the hot path. We parse
    the source as AST and look for ``import pdfplumber`` /
    ``from pdfplumber`` statements — string mentions in docstrings are
    allowed.
    """
    from fin.corpus import extracted as extracted_mod

    source_path = Path(extracted_mod.__file__)
    tree = ast.parse(source_path.read_text())
    offending: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == "pdfplumber":
                    offending.append((node.lineno, f"import {alias.name}"))
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module.split(".")[0] == "pdfplumber":
                offending.append(
                    (node.lineno, f"from {node.module} import ...")
                )
    assert offending == [], (
        "fin.corpus.extracted must not import pdfplumber; "
        f"offending: {offending}"
    )


def test_real_ingest_produces_deterministic_artifacts(
    real_ingested_tree: Path,
) -> None:
    """A second run of ``run_ingest`` against the real corpus must be
    a no-op at the byte level (ADR 0020 idempotency rule).
    """
    manifest_path, pdf_dir, _ = _real_manifest()

    before = _hash_tree(real_ingested_tree)
    second = run_ingest(
        manifest_path=manifest_path,
        pdf_dir=pdf_dir,
        extracted_root=real_ingested_tree,
    )
    after = _hash_tree(real_ingested_tree)

    assert second.pages_written == 0
    assert before == after


def test_real_ingest_writes_locator_and_passages(
    real_ingested_tree: Path,
) -> None:
    """Spot-check that the committed artifacts carry the locator shape
    documented in ADR 0006 and the per-page passage list expected by
    the BM25 adapter (T4).
    """
    manifest_path, _, _ = _real_manifest()

    tree = load_extracted_tree(
        manifest_path=manifest_path,
        extracted_root=real_ingested_tree,
    )

    # Every key is a document_id from the manifest, every value has at
    # least one page, and the locator matches the manifest's sha.
    manifest_docs = json.loads(manifest_path.read_text())["documents"]
    expected = {d["document_id"]: d["sha256"] for d in manifest_docs}
    assert set(tree.keys()) == set(expected.keys())

    sample_doc_id = sorted(tree.keys())[0]
    sample_pages = tree[sample_doc_id]
    assert len(sample_pages) >= 1

    first_page = sample_pages[0]
    assert isinstance(first_page.locator, Locator)
    assert first_page.locator.document_id == sample_doc_id
    assert first_page.locator.version == expected[sample_doc_id]
    assert first_page.locator.page == 1
    assert first_page.text  # non-empty
    assert first_page.passages  # at least one passage
    # Passage offsets are half-open into the page text.
    for p in first_page.passages:
        assert p.char_start >= 0
        assert p.char_end > p.char_start
        assert first_page.text[p.char_start:p.char_end] == p.text