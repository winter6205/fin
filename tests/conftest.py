"""Corpus fixtures shared by the corpus and host test packages.

``multi_form_corpus`` is the "test corpus includes those forms" half of
spec SC3. The committed frozen 12 are 10-Q only — that is what makes the
SC3 negative case (``form=10-K`` / ``form=8-K`` -> ``{"filings": []}``)
meaningful — so the positive case needs its own tree.

The fixture builds the extracted tree by hand but fills the catalog's
EDGAR-shaped fields through ``fin.corpus.ingest.update_catalog_fields``,
the same local ingest step the CLI runs. That step reads only the
manifest's own metadata: no SEC host is opened, so this fixture is legal
in default pytest.

It yields the ``manifest_path`` / ``extracted_root`` pair as a mapping so
callers can splat it straight into ``list_files`` / ``list_filings``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fin.corpus.ingest import update_catalog_fields


# One row per form the operator should be able to see in the library.
# ``doc_type`` is the local metadata the manifest already carries;
# ``form`` is deliberately absent so the ingest catalog step is the
# thing that writes it.
_MULTI_FORM_ROWS: tuple[tuple[str, str, str], ...] = (
    ("ZEUS-2023-10K", "10-K", "2023 FY"),
    ("ZEUS-2023-8K", "8-K", "2023-05-04"),
    ("ZEUS-2023Q1", "10-Q", "2023 Q1"),
)


def _write_extracted_document(
    extracted_root: Path, document_id: str, version: str
) -> None:
    doc_dir = extracted_root / document_id
    doc_dir.mkdir(parents=True)
    text = f"{document_id} page 1 body"
    (doc_dir / "page-001.json").write_text(
        json.dumps(
            {
                "document_id": document_id,
                "version": version,
                "page": 1,
                "text": text,
            }
        )
    )
    (doc_dir / "passages-001.json").write_text(
        json.dumps(
            {
                "document_id": document_id,
                "version": version,
                "page": 1,
                "passages": [
                    {"text": text, "char_start": 0, "char_end": len(text)}
                ],
            }
        )
    )


@pytest.fixture
def multi_form_corpus(tmp_path: Path) -> dict[str, Path]:
    """A corpus of one 10-K, one 8-K and one 10-Q for the same issuer."""
    documents: list[dict[str, object]] = []
    for index, (document_id, doc_type, period) in enumerate(_MULTI_FORM_ROWS):
        documents.append(
            {
                "document_id": document_id,
                "source_id": "ZEUS",
                "company": "ZEUS",
                "ticker": "ZEUS",
                "cik": "0000000042",
                "accession": f"0000000042-23-00000{index + 1}",
                "filing_date": "2023-05-04",
                "period": period,
                "doc_type": doc_type,
                "filename": f"{document_id}.pdf",
                "bytes": 0,
                "sha256": str(index + 1) * 64,
            }
        )

    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "upstream": {"note": "synthetic; no upstream"},
                "tickers": ["ZEUS"],
                "periods": sorted({row[2] for row in _MULTI_FORM_ROWS}),
                "note": "synthetic multi-form corpus for spec SC3",
                "documents": documents,
            }
        )
    )
    update_catalog_fields(manifest_path=manifest_path)

    extracted_root = tmp_path / "extracted"
    extracted_root.mkdir()
    for doc in documents:
        _write_extracted_document(
            extracted_root, str(doc["document_id"]), str(doc["sha256"])
        )
    (extracted_root / "_index.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "documents": {
                    str(doc["document_id"]): {
                        "page_count": 1,
                        "version": str(doc["sha256"]),
                    }
                    for doc in documents
                },
            }
        )
    )

    return {"manifest_path": manifest_path, "extracted_root": extracted_root}
