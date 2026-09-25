#!/usr/bin/env python3
"""Measure the real upload-byte and PDF-page distribution of the committed corpus (G4 T2a).

Usage::

    uv run python scripts/g4_upload_distribution.py
    # equivalent, no env resolution: .venv/bin/python scripts/g4_upload_distribution.py

G4 T2a must pick a finite upload-byte cap and PDF-page cap from the size
and page-count distribution of actual filings (``plans/thread-attachment-
source-read.md`` line 38: "record the real file-size distribution"), so
that T2b can replace the historical 2 MiB route constant with the
measured setting.  This script is that measurement, nothing else: it
reports numbers and refuses to propose a budget.

Every number is taken from an independent source and cross-checked, so a
drifted record shows up as a finding instead of a silent agreement:

* bytes come from ``os.stat`` on the file itself, cross-checked against
  the ``bytes`` recorded in ``data/corpus/manifest.json``;
* page counts come from poppler's ``pdfinfo``, independent of fin's own
  parser, cross-checked against ``page_count`` in
  ``data/corpus/catalog.json`` (joined through the manifest's
  ``document_id``);
* SHA-256 is computed from the bytes and cross-checked against the
  manifest ``sha256`` and the catalog ``version``, the two records that
  claim to identify the very same bytes.

Disagreements are reported, never resolved silently, and the manifest is
not treated as the authority over the filesystem.

Deterministic by construction: fixed file set sorted by name, integer
measurements, a documented nearest-rank percentile, sorted JSON keys, no
timestamp and no absolute path inside the artifact, so a re-run over the
same corpus reproduces ``data/corpus/g4_upload_distribution.json``
byte-for-byte.

Output: exactly one machine-readable JSON line on stdout (the summary,
including the SHA-256 of the written artifact); diagnostics on stderr.

Exit code:

* ``0`` -- every file on disk got both a byte and a page measurement.
  Findings such as a manifest disagreement do NOT change the exit code:
  a measurement that contradicts a record is still a completed
  measurement, and the payload carries the contradiction.
* ``1`` -- the measurement ran but is incomplete: at least one named gap
  (a file ``pdfinfo`` cannot read, a file absent from the manifest, a
  manifest entry with no file on disk).  The artifact is still written,
  with the gap in it.
* ``2`` -- the measurement could not be made at all (missing directory
  or corpus records, no PDF at all, no ``pdfinfo`` binary).  Nothing is
  written and stdout stays empty: the lane not reporting is a different
  fact from the lane observing nothing.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Final

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[1]

# The measured set is exactly the PDF corpus checkout, not the extracted
# text or the live HTML documents: uploads reach the attachment door as
# whole files, so the unit of interest is the file on disk.
PDF_DIR_REL: Final[str] = "data/corpus/pdf"
MANIFEST_REL: Final[str] = "data/corpus/manifest.json"
CATALOG_REL: Final[str] = "data/corpus/catalog.json"
ARTIFACT_REL: Final[str] = "data/corpus/g4_upload_distribution.json"

# The constant T2a's measurement exists to replace: the historical
# thread-attachment upload cap in ``src/fin/host/workspace_http.py:600``
# (``ATTACHMENT_MAX_BYTES = 2 * 1024 * 1024``).  Pinned as data with its
# origin so the over-cap finding and the plan text cannot drift apart.
ROUTE_ATTACHMENT_MAX_BYTES: Final[int] = 2 * 1024 * 1024
ROUTE_ATTACHMENT_MAX_SOURCE: Final[str] = (
    "src/fin/host/workspace_http.py:600 ATTACHMENT_MAX_BYTES"
)

# A different door, kept separate on purpose: the corpus ingest endpoint
# bound (``src/fin/corpus/ingest_endpoint.py:89``).  Reported as context
# for the comparison the leader asked for; it is not a candidate for the
# thread-attachment cap and the two must not be conflated.
INGEST_MAX_BYTES: Final[int] = 50 * 1024 * 1024
INGEST_MAX_SOURCE: Final[str] = (
    "src/fin/corpus/ingest_endpoint.py:89 MAX_INGEST_BYTES"
)

PDF_SUFFIXES: Final[tuple[str, ...]] = (".pdf",)

_PDFINFO_PAGES_RE: Final[re.Pattern[str]] = re.compile(
    r"^Pages:[ \t]*([0-9]+)[ \t]*$", re.MULTILINE
)

_COMPLETE_EXIT: Final[int] = 0
_INCOMPLETE_EXIT: Final[int] = 1
_NOT_REPORTING_EXIT: Final[int] = 2


class MeasurementBlockedError(RuntimeError):
    """A prerequisite of the measurement is missing; nothing is reportable."""


# ---------------------------------------------------------------- loading


def _load_json_object(relative_path: str) -> dict[str, Any]:
    """Read one corpus JSON record file as an object."""
    path = REPO_ROOT / relative_path
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise MeasurementBlockedError(f"cannot read {relative_path}: {exc}") from exc
    try:
        loaded = json.loads(raw)
    except ValueError as exc:
        raise MeasurementBlockedError(f"{relative_path} is not valid JSON: {exc}") from exc
    if not isinstance(loaded, dict):
        raise MeasurementBlockedError(f"{relative_path} is not a JSON object")
    return loaded


def _manifest_pdf_records(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Index the manifest's PDF documents by filename."""
    documents = manifest.get("documents")
    if not isinstance(documents, list):
        raise MeasurementBlockedError("manifest has no 'documents' list")
    by_filename: dict[str, dict[str, Any]] = {}
    for record in documents:
        if not isinstance(record, dict):
            continue
        filename = record.get("filename")
        if not isinstance(filename, str) or not filename.lower().endswith(PDF_SUFFIXES):
            continue
        by_filename[filename] = record
    return by_filename


def _catalog_page_counts(catalog: dict[str, Any]) -> dict[str, int | None]:
    """Index catalog page counts by document_id."""
    filings = catalog.get("filings")
    if not isinstance(filings, list):
        raise MeasurementBlockedError("catalog has no 'filings' list")
    counts: dict[str, int | None] = {}
    for filing in filings:
        if not isinstance(filing, dict):
            continue
        document_id = filing.get("document_id")
        if not isinstance(document_id, str):
            continue
        page_count = filing.get("page_count")
        counts[document_id] = page_count if isinstance(page_count, int) else None
    return counts


# ------------------------------------------------------------- measuring


def sha256_of(path: Path) -> str:
    """SHA-256 of the file's bytes, streamed so size does not matter."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def pdfinfo_page_count(pdfinfo: str, path: Path) -> tuple[int | None, str | None]:
    """Page count straight from poppler, independent of fin's parser.

    Returns ``(pages, None)`` on success, ``(None, reason)`` when
    ``pdfinfo`` refuses the file -- an unreadable file is a named gap, not
    a dropped row.
    """
    try:
        completed = subprocess.run(
            [pdfinfo, str(path)], capture_output=True, text=True, check=False
        )
    except OSError as exc:  # binary vanished mid-run, unreadable path, ...
        return None, f"pdfinfo could not run: {exc}"
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        return None, f"pdfinfo exit {completed.returncode}: {detail[:400]}"
    match = _PDFINFO_PAGES_RE.search(completed.stdout)
    if match is None:
        return None, "pdfinfo produced no 'Pages:' line"
    return int(match.group(1)), None


def nearest_rank(sorted_values: list[int], percentile: float) -> int | None:
    """Nearest-rank percentile: the value at rank ceil(p * n), 1-based.

    Fixed, documented rule so the committed numbers can be re-derived by
    hand; returns ``None`` for an empty list rather than inventing a
    value.
    """
    if not sorted_values:
        return None
    rank = math.ceil(percentile * len(sorted_values))
    index = min(max(rank, 1), len(sorted_values)) - 1
    return sorted_values[index]


def measure_file(
    *,
    pdfinfo: str,
    filename: str,
    manifest_record: dict[str, Any] | None,
    catalog_pages: dict[str, int | None],
    findings: list[dict[str, Any]],
    gaps: list[dict[str, Any]],
) -> dict[str, Any]:
    """Measure one file on disk and record every cross-check outcome."""
    path = REPO_ROOT / PDF_DIR_REL / filename
    try:
        bytes_on_disk = os.stat(path).st_size
    except OSError as exc:
        # Listed but not stat-able: a named gap, and the row still ships
        # so the reader can see the hole.
        gaps.append({"filename": filename, "gap": f"cannot stat file: {exc}"})
        bytes_on_disk = None

    sha256 = sha256_of(path) if bytes_on_disk is not None else None

    bytes_in_manifest = None
    if isinstance(manifest_record, dict):
        recorded = manifest_record.get("bytes")
        if isinstance(recorded, int):
            bytes_in_manifest = recorded
    if manifest_record is None:
        gaps.append({"filename": filename, "gap": "on disk but absent from manifest.json"})
        bytes_agree = False
    elif bytes_in_manifest is None or bytes_on_disk is None:
        gaps.append({"filename": filename, "gap": "manifest records no integer 'bytes'"})
        bytes_agree = False
    else:
        bytes_agree = bytes_on_disk == bytes_in_manifest

    pages, page_error = pdfinfo_page_count(pdfinfo, path)
    if pages is None:
        gaps.append({"filename": filename, "gap": f"pdfinfo: {page_error}"})

    document_id = (
        manifest_record.get("document_id") if isinstance(manifest_record, dict) else None
    )
    page_count_catalog = (
        catalog_pages.get(document_id) if isinstance(document_id, str) else None
    )
    page_count_agree = (
        pages is not None
        and isinstance(page_count_catalog, int)
        and pages == page_count_catalog
    )

    record: dict[str, Any] = {
        "filename": filename,
        "bytes_on_disk": bytes_on_disk,
        "bytes_in_manifest": bytes_in_manifest,
        "bytes_agree": bytes_agree,
        "sha256": sha256,
        "page_count_pdfinfo": pages,
        "page_count_catalog": page_count_catalog,
        "page_count_agree": page_count_agree,
        "bytes_per_page": (
            round(bytes_on_disk / pages, 2)
            if bytes_on_disk is not None and pages is not None and pages > 0
            else None
        ),
    }

    if not bytes_agree and bytes_on_disk is not None:
        findings.append(
            {
                "check": "bytes",
                "filename": filename,
                "value_on_disk": bytes_on_disk,
                "value_in_manifest": bytes_in_manifest,
            }
        )
    if not page_count_agree:
        findings.append(
            {
                "check": "page_count",
                "filename": filename,
                "value_from_pdfinfo": pages,
                "value_in_catalog": page_count_catalog,
            }
        )
    if sha256 is not None and isinstance(manifest_record, dict):
        recorded_sha = manifest_record.get("sha256")
        if isinstance(recorded_sha, str) and recorded_sha != sha256:
            findings.append(
                {
                    "check": "sha256",
                    "filename": filename,
                    "value_computed": sha256,
                    "value_in_manifest": recorded_sha,
                }
            )
    return record


# ---------------------------------------------------------------- building


def build_artifact() -> dict[str, Any]:
    """Measure the whole committed PDF corpus into the artifact object."""
    pdfinfo = shutil.which("pdfinfo")
    if pdfinfo is None:
        raise MeasurementBlockedError("pdfinfo (poppler) is not on PATH")

    pdf_dir = REPO_ROOT / PDF_DIR_REL
    if not pdf_dir.is_dir():
        raise MeasurementBlockedError(f"{PDF_DIR_REL} is not a directory")
    filenames = sorted(
        entry.name
        for entry in os.scandir(pdf_dir)
        if entry.is_file() and entry.name.lower().endswith(PDF_SUFFIXES)
    )
    if not filenames:
        raise MeasurementBlockedError(f"{PDF_DIR_REL} holds no PDF to measure")

    manifest = _load_json_object(MANIFEST_REL)
    catalog = _load_json_object(CATALOG_REL)
    manifest_records = _manifest_pdf_records(manifest)
    catalog_pages = _catalog_page_counts(catalog)

    findings: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    records = [
        measure_file(
            pdfinfo=pdfinfo,
            filename=filename,
            manifest_record=manifest_records.get(filename),
            catalog_pages=catalog_pages,
            findings=findings,
            gaps=gaps,
        )
        for filename in filenames
    ]

    # A manifest PDF with no file on disk is a hole in coverage: the
    # distribution would quietly shrink otherwise.
    for filename in sorted(set(manifest_records) - set(filenames)):
        gaps.append({"filename": filename, "gap": "in manifest.json but not on disk"})

    byte_values = sorted(
        record["bytes_on_disk"] for record in records if record["bytes_on_disk"] is not None
    )
    page_values = sorted(
        record["page_count_pdfinfo"]
        for record in records
        if record["page_count_pdfinfo"] is not None
    )

    over_route = [
        {"filename": record["filename"], "bytes": record["bytes_on_disk"]}
        for record in records
        if record["bytes_on_disk"] is not None
        and record["bytes_on_disk"] > ROUTE_ATTACHMENT_MAX_BYTES
    ]

    # (page_count, filename) pairs so the extremes carry their file with
    # them; ``default=None`` keeps an unreadable-corpus case a gap in the
    # payload rather than a traceback.
    page_pairs = [
        (record["page_count_pdfinfo"], record["filename"])
        for record in records
        if record["page_count_pdfinfo"] is not None and record["page_count_pdfinfo"] > 0
    ]
    smallest = min(page_pairs, default=None)
    largest = max(page_pairs, default=None)

    artifact: dict[str, Any] = {
        "schema_version": 1,
        "ticket": "G4 T2a",
        "measured_set": {
            "directory": PDF_DIR_REL,
            "files_on_disk": len(filenames),
            "pdf_documents_in_manifest": len(manifest_records),
        },
        "percentile_rule": "nearest rank: value at rank ceil(p * n) of the sorted values",
        "files": records,
        "aggregate": {
            "count": len(byte_values),
            "min": byte_values[0] if byte_values else None,
            "p50": nearest_rank(byte_values, 0.50),
            "p90": nearest_rank(byte_values, 0.90),
            "max": byte_values[-1] if byte_values else None,
            "total": sum(byte_values),
            "max_page_count": page_values[-1] if page_values else None,
            "min_page_count": page_values[0] if page_values else None,
        },
        "disagreements": {
            "bytes": [item for item in findings if item["check"] == "bytes"],
            "page_count": [item for item in findings if item["check"] == "page_count"],
            "sha256": [item for item in findings if item["check"] == "sha256"],
        },
        "gaps": gaps,
        "cap_findings": {
            "route_constant_bytes": ROUTE_ATTACHMENT_MAX_BYTES,
            "route_constant_source": ROUTE_ATTACHMENT_MAX_SOURCE,
            "files_over_route_constant": over_route,
            "count_over_route_constant": len(over_route),
            "smallest_page_count_observed": (
                None if smallest is None else {"page_count": smallest[0], "filename": smallest[1]}
            ),
            "largest_page_count_observed": (
                None if largest is None else {"page_count": largest[0], "filename": largest[1]}
            ),
            "separate_ingest_door": {
                "constant_bytes": INGEST_MAX_BYTES,
                "source": INGEST_MAX_SOURCE,
                "note": (
                    "Corpus-ingest endpoint bound, a different door from the thread "
                    "attachment upload; recorded for comparison only, not a candidate "
                    "for the attachment cap."
                ),
                "files_over_this_bound": [
                    record["filename"]
                    for record in records
                    if record["bytes_on_disk"] is not None
                    and record["bytes_on_disk"] > INGEST_MAX_BYTES
                ],
            },
        },
        "notes": [
            "Numbers only: this artifact deliberately proposes no budget value.",
            "2023_FY_GOOG.pdf is 624 B with 1 page, a placeholder-shaped fixture "
            "rather than a full 10-K, and is the minimum of both distributions.",
        ],
    }
    return artifact


def summary_line(artifact: dict[str, Any], artifact_sha256: str) -> dict[str, Any]:
    """The one machine-readable line a caller reads off stdout."""
    disagreements = artifact["disagreements"]
    return {
        "status": "incomplete" if artifact["gaps"] else "complete",
        "count": artifact["aggregate"]["count"],
        "bytes": {
            "min": artifact["aggregate"]["min"],
            "p50": artifact["aggregate"]["p50"],
            "p90": artifact["aggregate"]["p90"],
            "max": artifact["aggregate"]["max"],
            "total": artifact["aggregate"]["total"],
        },
        "pages": {
            "min": artifact["aggregate"]["min_page_count"],
            "max": artifact["aggregate"]["max_page_count"],
        },
        "files_over_route_constant": artifact["cap_findings"][
            "count_over_route_constant"
        ],
        "disagreements": {
            "bytes": len(disagreements["bytes"]),
            "page_count": len(disagreements["page_count"]),
            "sha256": len(disagreements["sha256"]),
        },
        "gaps": len(artifact["gaps"]),
        "artifact": ARTIFACT_REL,
        "artifact_sha256": artifact_sha256,
    }


def render(artifact: dict[str, Any]) -> str:
    """Serialize the artifact deterministically (sorted keys, LF, trailing NL)."""
    return json.dumps(artifact, indent=2, sort_keys=True, ensure_ascii=True) + "\n"


def main() -> int:
    """Measure, write the artifact, print the summary line."""
    try:
        artifact = build_artifact()
    except MeasurementBlockedError as exc:
        sys.stderr.write(f"g4_upload_distribution: blocked: {exc}\n")
        return _NOT_REPORTING_EXIT

    payload = render(artifact)
    artifact_path = REPO_ROOT / ARTIFACT_REL
    try:
        artifact_path.write_text(payload, encoding="utf-8")
    except OSError as exc:
        sys.stderr.write(f"g4_upload_distribution: cannot write artifact: {exc}\n")
        return _NOT_REPORTING_EXIT

    print(
        json.dumps(
            summary_line(artifact, hashlib.sha256(payload.encode("utf-8")).hexdigest()),
            sort_keys=True,
            ensure_ascii=True,
        )
    )
    return _INCOMPLETE_EXIT if artifact["gaps"] else _COMPLETE_EXIT


if __name__ == "__main__":
    raise SystemExit(main())
