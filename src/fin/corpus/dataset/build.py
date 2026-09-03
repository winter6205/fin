"""One-command dataset build + verify (plan T10, SC30).

Pipeline (``uv run python -m fin.corpus.dataset build``):

1. Load the tier-1 registry from ``data/corpus/manifest.json`` and
   verify every frozen PDF's on-disk sha256 against it (drift refuses
   the build — same discipline as :mod:`fin.corpus.ingest`).
2. Read the frozen unit text of every document from the committed
   extracted tree (``data/corpus/extracted/``) — the same real content
   the citation gate will be held against.
3. Classify each document's units into the three required document
   types and export the derived samples (md / txt / html / docx /
   xlsx / pptx) with the deterministic exporters, plus one hard sample
   per format (SC28) built from real content.
4. Assemble the query set: manual gold annotations (span-verified
   against the tree) + deterministic phrase probes, >= 100 queries in
   four classes each >= 10%.
5. Write ``documents/<format>/<sample>.<ext>`` and ``manifest.json``
   with per-sample sha256 records.

``verify_dataset`` reruns the pipeline in memory and compares every
sample hash and the query-set hash against the committed manifest —
重跑不一致样本数 = 0 is the pass condition.

The build never touches the network and never runs under the default
pytest suite (the dataset bytes live under ``data/corpus/dataset/``).
"""

from __future__ import annotations

from fin.corpus._private_fixtures import load_private_fixture

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from fin.corpus.chunking import split_document, split_unit
from fin.corpus.errors import ManifestShaMismatchError
from fin.corpus.dataset.exporters import (
    EXPORTER_VERSION,
    DERIVED_FORMATS,
    DerivedDoc,
    HardTable,
    derived_filename,
    export_derived,
)
from fin.corpus.dataset.manifest import (
    B1_MAX_CHUNKS,
    B1_MIN_CHUNKS,
    B1_MIN_SAMPLES_PER_FORMAT,
    CHUNK_TOTAL_STATUS_SPLIT,
    DATASET_ID,
    MANIFEST_SCHEMA_VERSION,
    compute_counts,
)
from fin.corpus.dataset.pages import (
    DOCUMENT_TYPES,
    NARRATIVE,
    TABLE_DENSE,
    FrozenUnit,
    _NUMBER_TOKEN,
    classify_page,
    detect_repeated_lines,
    ensure_all_types_present,
    is_repeated_page_furniture,
    split_table_row,
)
from fin.corpus.dataset.queries import (
    MANUAL_ANNOTATIONS,
    generate_phrase_queries,
    verify_manual_annotations,
)
from fin.corpus.parsers import parse_document
from fin.corpus.parsers._common import build_unit

PHRASE_QUERY_TARGET = 68
PHRASE_PER_DOC_CAP = 10
HARD_TABLE_MAX_ROWS = 12

LICENSE_PRIMARY = (
    load_private_fixture("7a7398cd926c90c1e340062bb62cb0f75768e8fca7d47051c48ab03241d4e4ee")
)
LICENSE_DERIVED_TEMPLATE = (
    load_private_fixture("1550a026344ee5f62f8080898a54d965195ddf599fcc51edfa52789ea4a12faa")
)

PRIMARY_PDF_GAP = "primary_pdf_below_per_format_floor"

# The B1 per-format floor requires >= 20 samples per format.
# Currently format 'pdf' has 12 frozen private fixture source 10-Q PDFs.
# Synthesizing PDFs is strictly prohibited by §B1 / ACR.
# Until the upstream package is extended with real 10-Q PDFs,
# the gap must be honestly reported in known_gaps.
KNOWN_GAPS: list[dict] = [
    {
        "gap": PRIMARY_PDF_GAP,
        "criterion": f"samples per format >= {B1_MIN_SAMPLES_PER_FORMAT}",
        "detail": (
            "format 'pdf' (tier 1) holds 12 countable samples (< 20); "
            "the >= 20 floor requires real upstream 10-Q PDFs (never synthesized)"
        ),
        "remediation": (
            "extend the upstream tier-1 package with real 10-Q PDFs with source and license, "
            "then rebuild the dataset"
        ),
    }
]


def _pdf_gaps(counts: dict) -> list[dict]:
    pdf_count = counts.get("by_format", {}).get("pdf", 0)
    if pdf_count >= B1_MIN_SAMPLES_PER_FORMAT:
        return []
    return [
        {
            "gap": PRIMARY_PDF_GAP,
            "criterion": f"samples per format >= {B1_MIN_SAMPLES_PER_FORMAT}",
            "detail": (
                f"format 'pdf' (tier 1) holds {pdf_count} countable samples (< {B1_MIN_SAMPLES_PER_FORMAT}); "
                "the >= 20 floor requires real upstream 10-Q PDFs (never synthesized)"
            ),
            "remediation": (
                "extend the upstream tier-1 package with real 10-Q PDFs with source and license, "
                "then rebuild the dataset"
            ),
        }
    ]

# The T11 splitter computes chunk counts deterministically; the B1
# chunk band [20000, 120000] scales with the tier-1 corpus size, so
# while the primary corpus is below its own B1 floor the chunk band is
# reported as a known gap with the actual numbers (never fabricated).
CHUNK_GAP_BELOW_BAND = "chunk_total_outside_b1_band"


def _chunk_gaps(chunk_total: int) -> list[dict]:
    """Safety net: report the B1 chunk band only if it falls outside.

    Plan C closes the chunk band once the EDGAR iXBRL expansion is
    rebuilt, so this should normally return ``[]``. Kept as a guard so
    any future corpus regression that drops chunk_total back below the
    band surfaces in the report instead of being silently absorbed.
    """
    if B1_MIN_CHUNKS <= chunk_total <= B1_MAX_CHUNKS:
        return []
    return [
        {
            "gap": CHUNK_GAP_BELOW_BAND,
            "criterion": f"chunks within [{B1_MIN_CHUNKS}, {B1_MAX_CHUNKS}] after split",
            "detail": (
                f"the T11 splitter computed chunk_total={chunk_total}; the band "
                "scales with the tier-1 corpus size (Plan C expansion closed "
                "this band for the committed corpus)"
            ),
            "remediation": (
                "re-run `uv run python -m fin.corpus.dataset expand-primary` "
                "or grow the upstream tier-1 package, then rebuild"
            ),
        }
    ]

_PROJECT_ROOT = Path(__file__).resolve().parents[4]


def project_root() -> Path:
    return _PROJECT_ROOT


def default_paths() -> tuple[Path, Path, Path]:
    corpus_dir = _PROJECT_ROOT / "data" / "corpus"
    return corpus_dir, corpus_dir / "extracted", corpus_dir / "dataset"


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


# --------------------------------------------------------------------------
# Corpus loading
# --------------------------------------------------------------------------


def _load_registry(corpus_dir: Path) -> tuple[dict[str, dict], dict[str, str]]:
    """Read the corpus manifest; verify each tier-1 source's bytes on disk.

    Two tier-1 kinds exist (B1「语料来源三级」一级):

    * ``kind="pdf"`` — the frozen 10-Q PDFs under ``data/corpus/pdf/``;
      their bytes are re-hashed here and drift refuses the build.
    * ``kind="html_live"`` — live EDGAR primary iXBRL documents frozen
      by :mod:`fin.corpus.live_fetch` (no PDF on disk; the version
      sha256 was computed over the official ``.htm`` bytes at freeze
      time and is re-asserted by the extracted-tree version check in
      :func:`_load_units`).

    Returns ``(registry, versions)`` where ``registry`` maps
    document_id -> record (version sha256, filename, kind, period) and
    ``versions`` is the plain document_id -> sha256 map used by
    manifest validation.
    """
    manifest = json.loads((corpus_dir / "manifest.json").read_text(encoding="utf-8"))
    registry: dict[str, dict] = {}
    for doc in manifest["documents"]:
        if doc.get("form") and doc.get("form") != "10-Q":
            continue
        pdf_path = corpus_dir / "pdf" / doc["filename"]
        if pdf_path.is_file():
            actual = sha256_bytes(pdf_path.read_bytes())
            if actual != doc["sha256"]:
                raise ManifestShaMismatchError(
                    f"frozen PDF drift: {pdf_path} sha256 {actual} != manifest {doc['sha256']}"
                )
            kind = "pdf"
        else:
            # Live EDGAR primary iXBRL document: unit artifacts live in
            # the extracted tree; the frozen version is the .htm sha256.
            kind = "html_live"
        registry[doc["document_id"]] = {
            "version": doc["sha256"],
            "filename": doc["filename"],
            "period": doc.get("period", ""),
            "kind": kind,
        }
    versions = {doc_id: rec["version"] for doc_id, rec in registry.items()}
    return registry, versions


def _load_units(extracted_root: Path, registry: dict[str, dict]) -> dict[str, list[FrozenUnit]]:
    """Read every frozen unit text from the committed extracted tree."""
    index = json.loads((extracted_root / "_index.json").read_text(encoding="utf-8"))
    documents = index["documents"]
    units_by_doc: dict[str, list[FrozenUnit]] = {}
    for doc_id in sorted(registry):
        if doc_id not in documents:
            raise ManifestShaMismatchError(
                f"extracted tree has no record for {doc_id}; run ingest first"
            )
        page_count = int(documents[doc_id]["page_count"])
        expected_version = str(documents[doc_id]["version"])
        if expected_version != registry[doc_id]["version"]:
            raise ManifestShaMismatchError(
                f"extracted tree version for {doc_id} ({expected_version}) does not "
                f"match the frozen PDF ({registry[doc_id]['version']})"
            )
        units: list[FrozenUnit] = []
        for page in range(1, page_count + 1):
            payload = json.loads(
                (extracted_root / doc_id / f"page-{page:03d}.json").read_text(encoding="utf-8")
            )
            units.append(FrozenUnit(unit=int(payload["page"]), text=str(payload["text"])))
        units_by_doc[doc_id] = units
    return units_by_doc


def pages_by_doc(units_by_doc: dict[str, list[FrozenUnit]]) -> dict[str, dict[int, str]]:
    return {
        doc_id: {u.unit: u.text for u in units}
        for doc_id, units in units_by_doc.items()
    }


# --------------------------------------------------------------------------
# Derived sample planning
# --------------------------------------------------------------------------


def build_hard_table(unit: FrozenUnit) -> HardTable:
    """Take a real table (title + numeric rows) out of one frozen unit."""
    title = ""
    for line in unit.text.splitlines():
        letters = [c for c in line if c.isalpha()]
        if len(letters) >= 8 and line == line.upper() and not _NUMBER_TOKEN.search(line):
            title = line.strip()
            break
    rows: list[tuple[str, ...]] = []
    for line in unit.text.splitlines():
        if len(_NUMBER_TOKEN.findall(line)) >= 2:
            cells = split_table_row(line)
            if len(cells) >= 2:
                rows.append(tuple(cells))
        if len(rows) >= HARD_TABLE_MAX_ROWS:
            break
    if not title or not rows:
        raise ValueError(f"unit {unit.unit} of hard source has no usable real table")
    return HardTable(source_unit=unit.unit, title=title, rows=tuple(rows))


def plan_derived_docs(
    registry: dict[str, dict],
    units_by_doc: dict[str, list[FrozenUnit]],
) -> list[DerivedDoc]:
    """Plan every derived sample: 3 doc-type slices + 1 hard doc per format."""
    plans: list[DerivedDoc] = []
    buckets_by_doc: dict[str, dict[str, list[FrozenUnit]]] = {}
    furniture_by_doc: dict[str, tuple[str, str]] = {}
    for doc_id in sorted(registry):
        version = registry[doc_id]["version"]
        buckets = classify_doc_units(units_by_doc[doc_id])
        buckets_by_doc[doc_id] = buckets
        furniture_by_doc[doc_id] = detect_repeated_lines(units_by_doc[doc_id])
        for doc_type in DOCUMENT_TYPES:
            plans.append(
                DerivedDoc(
                    sample_id=f"{doc_id}__{doc_type}",
                    fmt="",  # filled per format below
                    document_id=doc_id,
                    version=version,
                    doc_type=doc_type,
                    units=tuple(buckets[doc_type]),
                )
            )
    # Hard samples: one per derived format, all cut from the first
    # document (sorted) so the derivation is stable and inspectable.
    hard_doc = sorted(registry)[0]
    hard_version = registry[hard_doc]["version"]
    narrative_units = tuple(buckets_by_doc[hard_doc][NARRATIVE])
    table_unit = buckets_by_doc[hard_doc][TABLE_DENSE][0]
    hard_header, hard_footer = furniture_by_doc[hard_doc]
    hard_specs: list[tuple[str, str, dict]] = [
        ("md", "header_footer", {"header": hard_header, "footer": hard_footer}),
        ("txt", "header_footer", {"header": hard_header, "footer": hard_footer}),
        ("pptx", "header_footer", {"header": hard_header, "footer": hard_footer}),
        ("html", "multi_column", {}),
        ("docx", "merged_cells", {"table": build_hard_table(table_unit)}),
        ("xlsx", "merged_cells", {"table": build_hard_table(table_unit)}),
    ]
    for fmt, kind, extra in hard_specs:
        plans.append(
            DerivedDoc(
                sample_id=f"{hard_doc}__hard-{kind}",
                fmt=fmt,
                document_id=hard_doc,
                version=hard_version,
                doc_type=NARRATIVE if kind != "merged_cells" else TABLE_DENSE,
                units=narrative_units if kind != "merged_cells" else (table_unit,),
                hard_kind=kind,
                header_line=extra.get("header", ""),
                footer_line=extra.get("footer", ""),
                hard_table=extra.get("table"),
            )
        )
    # Type-slice plans (empty fmt) expand into one plan per derived
    # format; hard plans already carry their format.
    expanded: list[DerivedDoc] = []
    for plan in plans:
        if plan.fmt:
            expanded.append(plan)
            continue
        for fmt in DERIVED_FORMATS:
            expanded.append(
                DerivedDoc(
                    sample_id=f"{plan.sample_id}__{fmt}",
                    fmt=fmt,
                    document_id=plan.document_id,
                    version=plan.version,
                    doc_type=plan.doc_type,
                    units=plan.units,
                    hard_kind=plan.hard_kind,
                    header_line=plan.header_line,
                    footer_line=plan.footer_line,
                    hard_table=plan.hard_table,
                )
            )
    return expanded


def classify_doc_units(units: list[FrozenUnit]) -> dict[str, list[FrozenUnit]]:
    """Classify + guaranteed non-empty buckets for one document.

    EDGAR iXBRL primary documents are frozen as a single unit per
    ADR 0028 (``the partition itself is still the trivial one — the
    whole document is unit 1``), so they cannot donate across buckets
    via :func:`ensure_all_types_present`. When the document carries
    exactly one non-empty unit, mirror that unit into every type
    bucket so the derived export still runs (the same real frozen text
    serves as the source for each ``doc_type`` slice). Multi-unit
    documents (the private fixture source frozen PDFs) keep their original
    classification + redistribution behavior unchanged.

    Invariant: for ``len(with_text) == 1`` documents the bucket
    contents are intentionally identical across the three
    ``doc_type`` slices (not a bug). The doc_type distinction is
    irrelevant for single-unit docs because the source text has only
    one structural shape to export.
    """
    classified = {t: b for t, b in _classify_buckets(units).items()}
    with_text = [u for u in units if u.text.strip()]
    if len(with_text) == 1:
        only = with_text[0]
        for t in DOCUMENT_TYPES:
            if not classified[t]:
                classified[t].append(only)
    return ensure_all_types_present(classified)


def _classify_buckets(units: list[FrozenUnit]) -> dict[str, list[FrozenUnit]]:
    buckets: dict[str, list[FrozenUnit]] = {t: [] for t in DOCUMENT_TYPES}
    for u in sorted((u for u in units if u.text.strip()), key=lambda u: u.unit):
        buckets[classify_page(u.text)].append(u)
    return buckets


# --------------------------------------------------------------------------
# Query assembly
# --------------------------------------------------------------------------


def assemble_queries(
    units_by_doc: dict[str, list[FrozenUnit]],
    versions: dict[str, str],
) -> list[dict]:
    """Manual gold + deterministic phrase probes, locator versions filled."""
    problems = verify_manual_annotations(MANUAL_ANNOTATIONS, pages_by_doc(units_by_doc))
    if problems:
        raise ValueError(
            "manual annotations failed verification against the extracted tree:\n"
            + "\n".join(problems)
        )
    items: list[dict] = []
    for ann in MANUAL_ANNOTATIONS:
        doc_id = ann["document_id"]
        unit = ann["unit"]
        page_text = next(u.text for u in units_by_doc[doc_id] if u.unit == unit)
        items.append(
            {
                "query_id": ann["query_id"],
                "question": ann["question"],
                "query_class": ann["query_class"],
                "doc_type": classify_page(page_text),
                "expected_locator": {
                    "document_id": doc_id,
                    "version": versions[doc_id],
                    "unit": unit,
                },
                "annotation": "manual",
                "evidence_span": ann["evidence_span"],
            }
        )
    probes = generate_phrase_queries(
        units_by_doc, target=PHRASE_QUERY_TARGET, per_doc_cap=PHRASE_PER_DOC_CAP
    )
    for probe in probes:
        doc_id = probe["document_id"]
        items.append(
            {
                "query_id": probe["query_id"],
                "question": probe["question"],
                "query_class": probe["query_class"],
                "doc_type": probe["doc_type"],
                "expected_locator": {
                    "document_id": doc_id,
                    "version": versions[doc_id],
                    "unit": probe["unit"],
                },
                "annotation": probe["annotation"],
                "evidence_span": probe["evidence_span"],
            }
        )
    return items


# --------------------------------------------------------------------------
# Build + verify
# --------------------------------------------------------------------------


@dataclass
class DatasetVerifyReport:
    ok: bool
    checked_samples: int
    mismatches: list[dict] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def mismatch_count(self) -> int:
        return len(self.mismatches)


# --------------------------------------------------------------------------
# Chunk counting (T11 splitter)
# --------------------------------------------------------------------------


def count_derived_chunks(payload: bytes, *, fmt: str, sample_id: str, sha256: str, doc_type: str) -> int:
    """Child-block count of one derived sample: parse the exported
    bytes with the T6 entry point, split with the T11 splitter, count
    the hit set (children only — parents ride along, they are not
    pushed as independent blocks)."""
    result = parse_document(
        payload, fmt=fmt, document_id=sample_id, version=sha256
    )
    return len(split_document(result, doc_type=doc_type).hittable_blocks())


def count_primary_chunks(
    units: list[FrozenUnit], *, fmt: str, document_id: str, version: str
) -> int:
    """Child-block count of one primary sample.

    The committed extracted tree is the frozen-unit SSOT the build
    reads; each unit is re-frozen through the single construction
    point (idempotent for already-normalized text — it only repairs
    the few pre-T5 tree pages that still carry un-normalized
    characters) and split with the T11 splitter. Empty-text pages
    carry no frozen unit and produce no chunks. The eval chunk
    contract (``tests/eval/test_chunking_contract.py``) independently
    re-parses the primary bytes via ``parse_document`` and asserts the
    same per-sample counts, so any drift between the tree and the
    parser fails the eval suite (verified equal for the frozen 12).
    """
    total = 0
    for unit in units:
        if not unit.text.strip():
            continue
        parsed = build_unit(
            fmt=fmt,
            source_sha256=version,
            document_id=document_id,
            version=version,
            unit=unit.unit,
            text=unit.text,
        )
        total += len(split_unit(parsed, doc_type="mixed").children)
    return total


def _new_samples(
    registry: dict[str, dict],
    units_by_doc: dict[str, list[FrozenUnit]],
) -> tuple[list[dict], dict[str, bytes]]:
    """Build sample records + the derived bytes (in memory)."""
    samples: list[dict] = []
    derived_bytes: dict[str, bytes] = {}
    for doc_id in sorted(registry):
        rec = registry[doc_id]
        kind = rec.get("kind", "pdf")
        samples.append(
            {
                "sample_id": f"{doc_id}__primary",
                "format": "pdf" if kind == "pdf" else "html",
                "origin": "primary",
                "tier": 1,
                "path": f"data/corpus/pdf/{rec['filename']}"
                if kind == "pdf"
                else f"data/corpus/extracted/{doc_id}",
                "bytes": None,  # filled by the writer
                "sha256": rec["version"],
                "license": LICENSE_PRIMARY,
                "doc_type": "mixed",
                "is_hard": is_repeated_page_furniture(units_by_doc[doc_id]),
                "hard_kind": "header_footer"
                if is_repeated_page_furniture(units_by_doc[doc_id])
                else None,
                "document_id": doc_id,
                "source_kind": kind,
                "period": rec["period"],
                "chunk_count": count_primary_chunks(
                    units_by_doc[doc_id],
                    fmt="pdf" if kind == "pdf" else "html",
                    document_id=doc_id,
                    version=rec["version"],
                ),
            }
        )
    for plan in plan_derived_docs(registry, units_by_doc):
        payload = export_derived(plan)
        sample_id = f"{plan.sample_id}__{plan.fmt}"
        derived_bytes[sample_id] = payload
        samples.append(
            {
                "sample_id": sample_id,
                "format": plan.fmt,
                "origin": "derived",
                "tier": 3,
                "path": f"data/corpus/dataset/documents/{plan.fmt}/"
                + derived_filename(sample_id, plan.fmt),
                "bytes": len(payload),
                "sha256": sha256_bytes(payload),
                "license": LICENSE_DERIVED_TEMPLATE.format(
                    document_id=plan.document_id, version=plan.version
                ),
                "doc_type": plan.doc_type,
                "is_hard": plan.hard_kind is not None,
                "hard_kind": plan.hard_kind,
                "derived_from": {
                    "document_id": plan.document_id,
                    "version": plan.version,
                },
                "source_units": [{"unit": u.unit} for u in plan.units],
                "unit_count": len(plan.units),
                "chunk_count": count_derived_chunks(
                    payload,
                    fmt=plan.fmt,
                    sample_id=sample_id,
                    sha256=sha256_bytes(payload),
                    doc_type=plan.doc_type,
                ),
            }
        )
    return samples, derived_bytes


def _dump(payload: object) -> bytes:
    return (
        json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    ).encode("utf-8")


def _manifest_payload(
    registry: dict[str, dict],
    units_by_doc: dict[str, list[FrozenUnit]],
    samples: list[dict],
    queries: list[dict],
    derived_bytes: dict[str, bytes] | None,
) -> dict:
    counts = compute_counts(samples)
    queries_payload = {
        "total": len(queries),
        "note": "全量集 >= 100 题、四类各 >= 10%（B1 查询分档）；CI 契约集 40 题由 T22 圈定",
        "items_sha256": sha256_bytes(_dump(queries)),
        "items": queries,
    }
    chunk_total = sum(int(s["chunk_count"]) for s in samples)
    payload = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "dataset_id": DATASET_ID,
        "exporter_version": EXPORTER_VERSION,
        "built_from": {
            "corpus_manifest": "data/corpus/manifest.json",
            "extracted_root": "data/corpus/extracted",
            "primary_documents": sorted(registry),
        },
        "b1_thresholds": {
            "min_source_documents": 140,
            "min_formats": 7,
            "min_samples_per_format": 20,
            "min_chunks": 20000,
            "max_chunks": 120000,
            "min_queries": 100,
            "min_query_class_share_percent": 10,
            "source": "specs/corpus-retrieval.md §B1 数据集规模 / 查询分档（逐字口径）",
        },
        "chunk_total": chunk_total,
        "chunk_total_status": CHUNK_TOTAL_STATUS_SPLIT,
        "known_gaps": _pdf_gaps(counts) + _chunk_gaps(chunk_total),
        "counts": counts,
        "queries": queries_payload,
        "documents": samples,
    }
    return payload


def build_dataset(
    *,
    corpus_dir: Path | None = None,
    out_dir: Path | None = None,
    write: bool = True,
) -> dict:
    """Run the full pipeline; optionally write files + manifest.

    Returns the manifest payload. Deterministic: two calls on the same
    corpus produce identical manifest bytes and identical sample files.
    """
    if corpus_dir is None or out_dir is None:
        d_corpus, d_extracted, d_dataset = default_paths()
        corpus_dir = corpus_dir or d_corpus
        out_dir = out_dir or d_dataset
    registry, _ = _load_registry(corpus_dir)
    units_by_doc = _load_units(corpus_dir / "extracted", registry)
    samples, derived_bytes = _new_samples(registry, units_by_doc)
    # Primary bytes/size come from the verified on-disk PDFs; live HTML
    # documents take the byte count recorded by the freeze.
    for sample in samples:
        if sample["origin"] == "primary":
            if sample["source_kind"] == "pdf":
                pdf = corpus_dir / "pdf" / registry[sample["document_id"]]["filename"]
                sample["bytes"] = pdf.stat().st_size
            else:
                sample["bytes"] = sum(
                    len(u.text.encode("utf-8"))
                    for u in units_by_doc[sample["document_id"]]
                )
    queries = assemble_queries(units_by_doc, {d: r["version"] for d, r in registry.items()})
    payload = _manifest_payload(registry, units_by_doc, samples, queries, derived_bytes)
    if write:
        docs_dir = out_dir / "documents"
        for sample in samples:
            if sample["origin"] != "derived":
                continue
            fmt = sample["format"]
            target = docs_dir / fmt / Path(sample["path"]).name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(derived_bytes[sample["sample_id"]])
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "manifest.json").write_bytes(_dump(payload))
    return payload


def verify_dataset(
    *,
    corpus_dir: Path | None = None,
    dataset_dir: Path | None = None,
) -> DatasetVerifyReport:
    """Rebuild in memory and compare against the committed manifest (SC30)."""
    if corpus_dir is None or dataset_dir is None:
        d_corpus, _, d_dataset = default_paths()
        corpus_dir = corpus_dir or d_corpus
        dataset_dir = dataset_dir or d_dataset
    manifest_path = dataset_dir / "manifest.json"
    committed = json.loads(manifest_path.read_text(encoding="utf-8"))
    registry, _ = _load_registry(corpus_dir)
    units_by_doc = _load_units(corpus_dir / "extracted", registry)
    samples, derived_bytes = _new_samples(registry, units_by_doc)
    for sample in samples:
        if sample["origin"] == "primary":
            if sample["source_kind"] == "pdf":
                pdf = corpus_dir / "pdf" / registry[sample["document_id"]]["filename"]
                sample["bytes"] = pdf.stat().st_size
            else:
                sample["bytes"] = sum(
                    len(u.text.encode("utf-8"))
                    for u in units_by_doc[sample["document_id"]]
                )
    queries = assemble_queries(units_by_doc, {d: r["version"] for d, r in registry.items()})
    fresh = _manifest_payload(registry, units_by_doc, samples, queries, derived_bytes)

    committed_by_id = {s["sample_id"]: s for s in committed["documents"]}
    mismatches: list[dict] = []
    problems: list[str] = []
    for sample in fresh["documents"]:
        sid = sample["sample_id"]
        recorded = committed_by_id.get(sid)
        if recorded is None:
            mismatches.append({"sample_id": sid, "kind": "missing_from_committed_manifest"})
            continue
        if recorded.get("sha256") != sample["sha256"]:
            mismatches.append(
                {
                    "sample_id": sid,
                    "kind": "sha256",
                    "committed": recorded.get("sha256"),
                    "rebuilt": sample["sha256"],
                }
            )
        elif recorded.get("bytes") != sample["bytes"]:
            mismatches.append(
                {
                    "sample_id": sid,
                    "kind": "bytes",
                    "committed": recorded.get("bytes"),
                    "rebuilt": sample["bytes"],
                }
            )
    for sid in sorted(set(committed_by_id) - {s["sample_id"] for s in fresh["documents"]}):
        mismatches.append({"sample_id": sid, "kind": "extra_in_committed_manifest"})
    # On-disk bytes of every derived file must hash to its manifest record.
    for sample in fresh["documents"]:
        if sample["origin"] != "derived":
            continue
        on_disk = dataset_dir / "documents" / sample["format"] / Path(sample["path"]).name
        if not on_disk.is_file():
            problems.append(f"derived file missing on disk: {on_disk}")
            continue
        disk_hash = sha256_bytes(on_disk.read_bytes())
        if disk_hash != sample["sha256"]:
            problems.append(
                f"on-disk drift: {on_disk} sha256 {disk_hash} != manifest "
                f"{sample['sha256']}"
            )
    if committed["queries"]["items_sha256"] != fresh["queries"]["items_sha256"]:
        problems.append("query set hash differs from rebuilt query set")
    return DatasetVerifyReport(
        ok=not mismatches and not problems,
        checked_samples=len(fresh["documents"]),
        mismatches=mismatches,
        problems=problems,
    )
