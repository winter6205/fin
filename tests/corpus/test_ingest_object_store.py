"""Integration tests for raw object-store ingest wiring."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from fin.corpus.catalog import list_filings
from fin.corpus.extracted import load_extracted_tree
from fin.corpus.ingest import run_ingest
from fin.corpus.metadata_store import InMemoryMetadataStore
from fin.corpus.object_store import InMemoryObjectStore


def _tiny_pdf() -> bytes:
    content = (
        b"BT /F1 14 Tf 72 720 Td (Overview) Tj "
        b"0 -24 Td (Revenue increased) Tj ET"
    )
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length "
        + str(len(content)).encode()
        + b" >>\nstream\n"
        + content
        + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, obj in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{number} 0 obj\n".encode())
        output.extend(obj)
        output.extend(b"\nendobj\n")
    xref_offset = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode())
    output.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n".encode()
    )
    return bytes(output)


def _fixture(tmp_path: Path) -> tuple[Path, Path, Path, bytes]:
    payload = _tiny_pdf()
    pdf_dir = tmp_path / "pdf"
    pdf_dir.mkdir()
    (pdf_dir / "fixture.pdf").write_bytes(payload)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "documents": [
                    {
                        "document_id": "TEST-2026",
                        "company": "TEST",
                        "period": "2026 Q1",
                        "filename": "fixture.pdf",
                        "sha256": hashlib.sha256(payload).hexdigest(),
                        "form": "10-Q",
                    }
                ]
            }
        )
    )
    return manifest_path, pdf_dir, tmp_path / "extracted", payload


def test_ingest_stores_raw_bytes_and_keeps_locator_reads_local(
    tmp_path: Path,
) -> None:
    manifest_path, pdf_dir, extracted_root, payload = _fixture(tmp_path)
    object_store = InMemoryObjectStore()

    result = run_ingest(
        manifest_path=manifest_path,
        pdf_dir=pdf_dir,
        extracted_root=extracted_root,
        object_store=object_store,
    )

    raw_key = "filings/TEST-2026/fixture.pdf"
    assert result.raw_storage == "object_store"
    assert object_store.get(raw_key) == payload
    row = json.loads(manifest_path.read_text())["documents"][0]
    assert row["raw_object"]["key"] == raw_key
    assert row["raw_object"]["sha256"] == row["sha256"]

    pages = load_extracted_tree(manifest_path, extracted_root)
    assert pages["TEST-2026"][0].locator.document_id == "TEST-2026"
    assert list_filings(
        manifest_path=manifest_path,
        extracted_root=extracted_root,
    )["filings"][0]["document_id"] == "TEST-2026"


def test_ingest_persists_raw_pointer_in_metadata_store(tmp_path: Path) -> None:
    manifest_path, pdf_dir, extracted_root, payload = _fixture(tmp_path)
    object_store = InMemoryObjectStore()
    metadata_store = InMemoryMetadataStore()

    run_ingest(
        manifest_path=manifest_path,
        pdf_dir=pdf_dir,
        extracted_root=extracted_root,
        object_store=object_store,
        metadata_store=metadata_store,
    )

    pointer = metadata_store.get_pointer("TEST-2026").object
    assert pointer.key == "filings/TEST-2026/fixture.pdf"
    assert pointer.sha256 == hashlib.sha256(payload).hexdigest()


def test_ingest_without_store_reports_documented_local_fallback(
    tmp_path: Path,
) -> None:
    manifest_path, pdf_dir, extracted_root, _ = _fixture(tmp_path)

    result = run_ingest(
        manifest_path=manifest_path,
        pdf_dir=pdf_dir,
        extracted_root=extracted_root,
    )

    assert result.raw_storage == "local_fallback"
    assert "raw_object" not in json.loads(manifest_path.read_text())[
        "documents"
    ][0]
    assert (extracted_root / "TEST-2026" / "page-001.json").is_file()
