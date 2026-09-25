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

Two consequences follow for records that cannot be read at all:

* a manifest or catalog entry that cannot be joined to a file on disk is
  a named gap with its own reason, never a dropped row -- a silently
  dropped manifest row would otherwise be reported later as "on disk but
  absent from manifest.json", which is the wrong reason for the hole;
* the two product caps quoted under ``cap_findings`` are read from the
  modules that enforce them at run time, and their ``file:line``
  provenance is derived from that same live object, so a cap the product
  later changes cannot be cited here from a stale copy.  A cap that cannot
  be read blocks the run instead of falling back to a literal.

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
  or corpus records, no PDF at all, no ``pdfinfo`` binary, or a product
  cap that cannot be read from the module that enforces it).  Nothing is
  written and stdout stays empty: the lane not reporting is a different
  fact from the lane observing nothing.
"""

from __future__ import annotations

import ast
import hashlib
import importlib
import inspect
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

# Product caps are quoted, not re-declared, so the fin packages have to be
# importable the same way the product itself imports them.
sys.path.insert(0, str(REPO_ROOT / "src"))

# The measured set is exactly the PDF corpus checkout, not the extracted
# text or the live HTML documents: uploads reach the attachment door as
# whole files, so the unit of interest is the file on disk.
PDF_DIR_REL: Final[str] = "data/corpus/pdf"
MANIFEST_REL: Final[str] = "data/corpus/manifest.json"
CATALOG_REL: Final[str] = "data/corpus/catalog.json"
ARTIFACT_REL: Final[str] = "data/corpus/g4_upload_distribution.json"

# The constant T2a's measurement exists to replace: the thread-attachment
# upload cap enforced in ``fin.host.workspace_http``.  Only its address is
# stated here -- the value and the ``file:line`` provenance are read from
# the live module, so neither can drift from what the product enforces.
ROUTE_ATTACHMENT_CONSTANT: Final[tuple[str, str]] = (
    "fin.host.workspace_http",
    "ATTACHMENT_MAX_BYTES",
)

# A different door, kept separate on purpose: the corpus ingest endpoint
# bound.  Reported as context for the byte comparison above; it is not a
# candidate for the thread-attachment cap and the two must not be conflated.
INGEST_CONSTANT: Final[tuple[str, str]] = (
    "fin.corpus.ingest_endpoint",
    "MAX_INGEST_BYTES",
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


# ------------------------------------------------------------ product caps


def _module_constant_line(module_path: Path, constant_name: str) -> int:
    """Line of the module-level assignment of ``constant_name``.

    Read off the live source rather than quoted from prose, so the recorded
    provenance and the recorded value always describe the same statement.
    """
    try:
        tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
    except (OSError, ValueError) as exc:
        raise MeasurementBlockedError(
            f"cannot parse {module_path} to locate {constant_name}: {exc}"
        ) from exc
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else []
        if isinstance(node, ast.AnnAssign):
            targets = [node.target]
        for target in targets:
            if isinstance(target, ast.Name) and target.id == constant_name:
                return node.lineno
    raise MeasurementBlockedError(
        f"{module_path} does not assign {constant_name} at module level"
    )


def _module_source_path(module_name: str, source_file: str | None) -> Path:
    """Absolute source path of a live module, which must be in this checkout."""
    if source_file is None:
        raise MeasurementBlockedError(f"{module_name} has no readable source file")
    resolved = Path(source_file).resolve()
    try:
        resolved.relative_to(REPO_ROOT)
    except ValueError as exc:
        raise MeasurementBlockedError(
            f"{resolved} is outside {REPO_ROOT}, so its cap cannot be cited"
        ) from exc
    return resolved


def read_product_constant(module_name: str, constant_name: str) -> dict[str, Any]:
    """One product byte cap, taken from the module that enforces it.

    Returns ``{"constant_bytes": int, "source": "file:line NAME"}``.  Every
    failure raises: this is a measurement instrument, and substituting a
    literal here would report a number nobody measured.
    """
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise MeasurementBlockedError(
            f"cannot import {module_name} to read {constant_name}: {exc}"
        ) from exc
    value = getattr(module, constant_name, None)
    if not isinstance(value, int):
        raise MeasurementBlockedError(
            f"{module_name}.{constant_name} is {type(value).__name__}, not a byte count"
        )
    source_path = _module_source_path(module_name, inspect.getsourcefile(module))
    line = _module_constant_line(source_path, constant_name)
    relative = source_path.relative_to(REPO_ROOT).as_posix()
    return {"constant_bytes": value, "source": f"{relative}:{line} {constant_name}"}


def read_product_constants() -> dict[str, dict[str, Any]]:
    """Both quoted caps, keyed by the door they belong to."""
    return {
        "route": read_product_constant(*ROUTE_ATTACHMENT_CONSTANT),
        "ingest": read_product_constant(*INGEST_CONSTANT),
    }


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


def _manifest_pdf_records(
    manifest: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Index the manifest's PDF documents by filename.

    Returns ``(by_filename, gaps)``: a record that cannot be joined to a
    file is a named gap with its own reason, never a silent drop.  A
    dropped record would later surface as "on disk but absent from
    manifest.json", which is the wrong reason for that hole.
    """
    documents = manifest.get("documents")
    if not isinstance(documents, list):
        raise MeasurementBlockedError(
            "manifest has no 'documents' list"
        )
    by_filename: dict[str, dict[str, Any]] = {}
    gaps: list[dict[str, Any]] = []
    for position, record in enumerate(documents):
        if not isinstance(record, dict):
            gaps.append(
                {"manifest_position": position, "gap": "manifest entry is not a JSON object"}
            )
            continue  # EXIT: an unparseable entry has no filename to join on
        filename = record.get("filename")
        if not isinstance(filename, str):
            gaps.append(
                {"manifest_position": position, "gap": "manifest entry has no string 'filename'"}
            )
            continue
        if not filename.lower().endswith(PDF_SUFFIXES):
            continue  # EXIT: outside the measured set by design -- the unit is the uploaded PDF
        by_filename[filename] = record
    return by_filename, gaps


def _catalog_page_counts(
    catalog: dict[str, Any],
) -> tuple[dict[str, int | None], list[dict[str, Any]]]:
    """Index catalog page counts by document_id, with a gap per unreadable row."""
    filings = catalog.get("filings")
    if not isinstance(filings, list):
        raise MeasurementBlockedError("catalog has no 'filings' list")
    counts: dict[str, int | None] = {}
    gaps: list[dict[str, Any]] = []
    for position, filing in enumerate(filings):
        if not isinstance(filing, dict):
            gaps.append(
                {
                    "catalog_position": position,
                    "gap": "catalog filing is not a JSON object",
                }
            )
            continue  # EXIT: an unparseable row cannot vouch for any page count
        document_id = filing.get("document_id")
        if not isinstance(document_id, str):
            gaps.append(
                {
                    "catalog_position": position,
                    "gap": "catalog filing has no string 'document_id'",
                }
            )
            continue
        page_count = filing.get("page_count")
        # A non-integer page count stays a ``None`` entry: the per-file
        # record then reports the missing second opinion as a finding.
        counts[document_id] = page_count if isinstance(page_count, int) else None
    return counts, gaps


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


def _bytes_on_disk(filename: str, path: Path, gaps: list[dict[str, Any]]) -> int | None:
    """Size straight from the filesystem; an unstat-able file is a named gap."""
    try:
        return os.stat(path).st_size
    except OSError as exc:
        gaps.append({"filename": filename, "gap": f"cannot stat file: {exc}"})
        return None  # EXIT: the row still ships so the reader can see the hole


def _manifest_bytes(manifest_record: dict[str, Any] | None) -> int | None:
    """The byte count the manifest claims, or ``None`` when it claims none."""
    if not isinstance(manifest_record, dict):
        return None
    recorded = manifest_record.get("bytes")
    return recorded if isinstance(recorded, int) else None


def _bytes_agreement(
    filename: str,
    manifest_record: dict[str, Any] | None,
    bytes_on_disk: int | None,
    gaps: list[dict[str, Any]],
) -> bool:
    """Whether manifest and filesystem agree on this file's size; gaps say why not."""
    if manifest_record is None:
        gaps.append({"filename": filename, "gap": "on disk but absent from manifest.json"})
        return False
    bytes_in_manifest = _manifest_bytes(manifest_record)
    if bytes_in_manifest is None:
        gaps.append({"filename": filename, "gap": "manifest records no integer 'bytes'"})
        return False
    if bytes_on_disk is None:
        return False  # EXIT: the disk side is already a named stat gap, not a second reason
    return bytes_in_manifest == bytes_on_disk


def _joined_catalog_page_count(
    manifest_record: dict[str, Any] | None,
    catalog_pages: dict[str, int | None],
) -> int | None:
    """The catalog's page count for the same document, joined through ``document_id``."""
    document_id = (
        manifest_record.get("document_id") if isinstance(manifest_record, dict) else None
    )
    if not isinstance(document_id, str):
        return None
    return catalog_pages.get(document_id)


def _bytes_per_page(bytes_on_disk: int | None, pages: int | None) -> float | None:
    """Mean bytes per page, and ``None`` whenever that ratio would have to be invented."""
    if bytes_on_disk is None or pages is None or pages <= 0:
        return None
    return round(bytes_on_disk / pages, 2)


def _record_disagreements(
    record: dict[str, Any],
    manifest_record: dict[str, Any] | None,
    sha256: str | None,
    findings: list[dict[str, Any]],
) -> None:
    """Append one finding row for every cross-check this file failed."""
    if not record["bytes_agree"] and record["bytes_on_disk"] is not None:
        findings.append(
            {
                "check": "bytes",
                "filename": record["filename"],
                "value_on_disk": record["bytes_on_disk"],
                "value_in_manifest": record["bytes_in_manifest"],
            }
        )
    if not record["page_count_agree"]:
        findings.append(
            {
                "check": "page_count",
                "filename": record["filename"],
                "value_from_pdfinfo": record["page_count_pdfinfo"],
                "value_in_catalog": record["page_count_catalog"],
            }
        )
    recorded_sha = manifest_record.get("sha256") if isinstance(manifest_record, dict) else None
    if sha256 is not None and isinstance(recorded_sha, str) and recorded_sha != sha256:
        findings.append(
            {
                "check": "sha256",
                "filename": record["filename"],
                "value_computed": sha256,
                "value_in_manifest": recorded_sha,
            }
        )


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
    bytes_on_disk = _bytes_on_disk(filename, path, gaps)
    sha256 = sha256_of(path) if bytes_on_disk is not None else None
    page_count_pdfinfo, page_error = pdfinfo_page_count(pdfinfo, path)
    if page_count_pdfinfo is None:
        gaps.append({"filename": filename, "gap": f"pdfinfo: {page_error}"})
        # EXIT: a refused page count is a gap in its own right; the byte lanes still measure
    page_count_catalog = _joined_catalog_page_count(manifest_record, catalog_pages)
    # This check appends gaps, so it runs before the record is assembled
    # rather than as a side effect inside the literal.
    bytes_agree = _bytes_agreement(filename, manifest_record, bytes_on_disk, gaps)
    record: dict[str, Any] = {
        "filename": filename,
        "bytes_on_disk": bytes_on_disk,
        "bytes_in_manifest": _manifest_bytes(manifest_record),
        "bytes_agree": bytes_agree,
        "sha256": sha256,
        "page_count_pdfinfo": page_count_pdfinfo,
        "page_count_catalog": page_count_catalog,
        "page_count_agree": (
            page_count_pdfinfo is not None
            and isinstance(page_count_catalog, int)
            and page_count_pdfinfo == page_count_catalog
        ),
        "bytes_per_page": _bytes_per_page(bytes_on_disk, page_count_pdfinfo),
    }
    _record_disagreements(record, manifest_record, sha256, findings)
    return record


# ---------------------------------------------------------------- building


def _pdf_filenames() -> list[str]:
    """The measured set: every PDF file directly under the corpus PDF directory."""
    pdf_dir = REPO_ROOT / PDF_DIR_REL
    if not pdf_dir.is_dir():
        raise MeasurementBlockedError(f"{PDF_DIR_REL} is not a directory")
    filenames = sorted(
        entry.name
        for entry in os.scandir(pdf_dir)
        if entry.is_file() and entry.name.lower().endswith(PDF_SUFFIXES)
    )
    if not filenames:
        raise MeasurementBlockedError(
            f"{PDF_DIR_REL} holds no PDF to measure"
        )  # keeps an empty corpus from reporting a zero-length distribution
    return filenames


def _measure_corpus(
    pdfinfo: str,
    filenames: list[str],
    manifest_records: dict[str, dict[str, Any]],
    catalog_pages: dict[str, int | None],
    gaps: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Measure every file on disk, then close the loop on manifest-only records."""
    findings: list[dict[str, Any]] = []
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
        # EXIT: named, so a missing file never reads as a smaller corpus
    return records, findings


def _distribution_block(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Byte and page spread over every file that could be measured at all."""
    byte_values = sorted(
        record["bytes_on_disk"] for record in records if record["bytes_on_disk"] is not None
    )
    page_values = sorted(
        record["page_count_pdfinfo"]
        for record in records
        if record["page_count_pdfinfo"] is not None
    )
    return {
        "count": len(byte_values),
        "min": byte_values[0] if byte_values else None,
        "p50": nearest_rank(byte_values, 0.50),
        "p90": nearest_rank(byte_values, 0.90),
        "max": byte_values[-1] if byte_values else None,
        "total": sum(byte_values),
        "max_page_count": page_values[-1] if page_values else None,
        "min_page_count": page_values[0] if page_values else None,
    }


def _files_over_limit(records: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Files measurably larger than ``limit``; an unmeasured file exceeds nothing."""
    return [
        {"filename": record["filename"], "bytes": record["bytes_on_disk"]}
        for record in records
        if record["bytes_on_disk"] is not None and record["bytes_on_disk"] > limit
    ]


def _page_count_record(pair: tuple[int, str] | None) -> dict[str, Any] | None:
    """One ``(page_count, filename)`` pair as an artifact record."""
    # EXIT: no readable page at all is null in the payload, never a page count of zero
    return None if pair is None else {"page_count": pair[0], "filename": pair[1]}


def _page_extremes_block(records: list[dict[str, Any]]) -> dict[str, Any]:
    """The smallest and largest page counts seen, each carrying its own file.

    ``(page_count, filename)`` pairs so the extremes travel with the file
    they came from; ``default=None`` keeps an unreadable-corpus case a null
    in the payload rather than a traceback.
    """
    page_pairs = [
        (record["page_count_pdfinfo"], record["filename"])
        for record in records
        if record["page_count_pdfinfo"] is not None and record["page_count_pdfinfo"] > 0
    ]
    return {
        "smallest_page_count_observed": _page_count_record(min(page_pairs, default=None)),
        "largest_page_count_observed": _page_count_record(max(page_pairs, default=None)),
    }


def _cap_findings_block(
    records: list[dict[str, Any]], constants: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """The measured distribution read against the caps the product itself enforces."""
    route = constants["route"]
    ingest = constants["ingest"]
    over_route = _files_over_limit(records, route["constant_bytes"])
    extremes = _page_extremes_block(records)
    return {
        "route_constant_bytes": route["constant_bytes"],
        "route_constant_source": route["source"],
        "files_over_route_constant": over_route,
        "count_over_route_constant": len(over_route),
        "smallest_page_count_observed": extremes["smallest_page_count_observed"],
        "largest_page_count_observed": extremes["largest_page_count_observed"],
        "separate_ingest_door": {
            "constant_bytes": ingest["constant_bytes"],
            "source": ingest["source"],
            "note": (
                "Corpus-ingest endpoint bound, a different door from the thread "
                "attachment upload; recorded for comparison only, not a candidate "
                "for the attachment cap."
            ),
            "files_over_this_bound": [
                item["filename"] for item in _files_over_limit(records, ingest["constant_bytes"])
            ],
        },
    }


def _disagreement_block(findings: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """The failed cross-checks, grouped by the check that failed."""
    return {
        name: [item for item in findings if item["check"] == name]
        for name in ("bytes", "page_count", "sha256")
    }


def build_artifact() -> dict[str, Any]:
    """Measure the whole committed PDF corpus into the artifact object."""
    pdfinfo = shutil.which("pdfinfo")
    if pdfinfo is None:
        raise MeasurementBlockedError(
            "pdfinfo (poppler) is not on PATH"
        )

    filenames = _pdf_filenames()
    # Both quoted caps come out of the modules that enforce them, so neither
    # number nor its provenance can be a copy that time forgot.
    constants = read_product_constants()
    manifest_records, manifest_gaps = _manifest_pdf_records(_load_json_object(MANIFEST_REL))
    catalog_pages, catalog_gaps = _catalog_page_counts(_load_json_object(CATALOG_REL))
    gaps: list[dict[str, Any]] = [*manifest_gaps, *catalog_gaps]
    records, findings = _measure_corpus(
        pdfinfo, filenames, manifest_records, catalog_pages, gaps
    )

    return {
        "schema_version": 1,
        "ticket": "G4 T2a",
        "measured_set": {
            "directory": PDF_DIR_REL,
            "files_on_disk": len(filenames),
            "pdf_documents_in_manifest": len(manifest_records),
        },
        "percentile_rule": "nearest rank: value at rank ceil(p * n) of the sorted values",
        "files": records,
        "aggregate": _distribution_block(records),
        "disagreements": _disagreement_block(findings),
        "gaps": gaps,
        "cap_findings": _cap_findings_block(records, constants),
        "notes": [
            "Numbers only: this artifact deliberately proposes no budget value.",
            "2023_FY_GOOG.pdf is 624 B with 1 page, a placeholder-shaped fixture "
            "rather than a full 10-K, and is the minimum of both distributions.",
        ],
    }


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
