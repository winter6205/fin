#!/usr/bin/env python3
"""T2a pinned-pipeline measurement driver.

Runs *only* inside the pinned image built by ``Dockerfile`` (docling==2.130.0,
CPU-only torch, Tesseract on CPU) under the 4 GiB ceiling enforced by
``run_measured.sh``. It measures the candidate pipeline defined by
``docs/adr/0089-lazy-page-region-attachment-preparation.md``:

* Docling's standard PDF pipeline with the Heron layout model, producing
  text / table / figure region boxes with coordinates;
* CPU Tesseract OCR for pages that are image-only, using the *repo's own*
  decision rule and language string (``fin.corpus.ocr.detect_ocr_required`` /
  ``fin.corpus.ocr.TesseractEngine``, imported from the read-only worktree
  mount) so the measurement exercises the same rule ADR 0054 freezes;
* a full-page bitmap render of EVERY physical page with its pixel dimensions
  and DPI, because the page-cap / vision-call budget decision depends on the
  size of the image a vision call would carry;
* measured peak memory (process ``VmHWM`` *and* the cgroup's ``memory.peak``),
  measured latency, and the OOM signal from ``memory.events``.

It deliberately does NOT call any vision provider (another worker owns that
probe) and does NOT load a local VLM or a separate picture classifier, exactly
as ADR 0089 forbids: picture classification/description, code and formula
enrichment are switched off, and ``do_ocr`` is off inside Docling because OCR
is the explicit, per-page, repo-rule-gated Tesseract step above.

Output: exactly ONE machine-readable JSON line on stdout. All diagnostics go to
stderr, so ``run_measured.sh`` can ``tee`` the line.

One-physical-page-per-unit invariant (``src/fin/corpus/parsers/_pdf.py``): the
``pages`` array is indexed by 1-based physical page number and every physical
page gets exactly one entry, including blank ones. ``--max-pages`` is only a
harness safety valve; exceeding it is reported as ``page_limit_exceeded`` and
is NOT silently truncated, mirroring ``attachment_page_limit_exceeded``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

# Packages whose *resolved* version is part of the pin record. Anything that
# cannot be introspected is recorded as null rather than guessed.
PINNED_PACKAGES = [
    "docling",
    "docling-slim",
    "docling-core",
    "docling-ibm-models",
    "docling-parse",
    "torch",
    "torchvision",
    "transformers",
    "opencv-python",
    "opencv-python-headless",
    "pypdfium2",
    "huggingface-hub",
    "accelerate",
    "numpy",
    "scipy",
    "pillow",
    "pytesseract",
    "rapidocr",
    "onnxruntime",
]

CGROUP_DIR = Path("/sys/fs/cgroup")

# The cgroup's own spelling of "this limit is off". Recorded as the number the
# committed measurements already carry, and kept distinct from ``None``, which
# means the file answered nothing at all.
CGROUP_NO_LIMIT_TEXT: Final[str] = "max"
CGROUP_NO_LIMIT_BYTES: Final[int] = -1

# pypdfium2's FPDF_PAGEOBJ_IMAGE enum value, used only when the raw module
# cannot be imported; no other object kind counts as an image.
FPDF_PAGEOBJ_IMAGE: Final[int] = 3

_PROC_STATUS_FIELDS: Final[tuple[str, ...]] = ("VmHWM", "VmRSS", "VmSwap", "VmPeak")


def log(msg: str) -> None:
    """Diagnostics on stderr only - stdout carries exactly one JSON line."""
    print(msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# memory measurement (measured, never inferred)
# --------------------------------------------------------------------------
def _read_proc_status_fields(
    names: tuple[str, ...],
) -> tuple[dict[str, int | None], list[str]]:
    """Requested ``/proc/self/status`` fields in bytes, plus every read failure.

    A field is ``None`` only together with a named reason, so an unread memory
    number can never be read back as a measured zero.
    """
    values: dict[str, int | None] = {name: None for name in names}
    errors: list[str] = []
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                if key not in values:
                    continue
                parts = rest.split()
                if not parts:
                    errors.append(f"/proc/self/status {key}: empty value")
                    continue  # EXIT: an empty field is no reading, not a zero
                try:
                    values[key] = int(parts[0]) * (1024 if "kB" in parts else 1)
                except ValueError:
                    errors.append(
                        f"/proc/self/status {key}: unparseable {parts[0]!r}"
                    )  # EXIT: text that is not a number is a failed read, not a zero
    except OSError as exc:
        errors.append(
            f"/proc/self/status: {type(exc).__name__}: {exc}"
        )  # EXIT: no /proc means no process peak, named rather than guessed
        log(f"[mem] cannot read /proc/self/status: {exc}")
    return values, errors


def _read_cgroup_int(
    name: str, *, allow_unlimited: bool
) -> tuple[int | None, str | None]:
    """One cgroup integer as ``(value, error)``; at most one of them is set.

    ``allow_unlimited`` marks a limit file, where ``max`` is a real answer. In a
    counter file the same text answers nothing, so it becomes a named gap rather
    than a peak.
    """
    try:
        raw = (CGROUP_DIR / name).read_text(encoding="utf-8").strip()
    except OSError as exc:
        return (
            None,
            f"{name}: {type(exc).__name__}: {exc}",
        )  # EXIT: an absent cgroup file is a gap, never a zero
    if raw == CGROUP_NO_LIMIT_TEXT:
        if allow_unlimited:
            return (
                CGROUP_NO_LIMIT_BYTES,
                None,
            )  # EXIT: "no limit" is a reading, but never a byte count
        return (
            None,
            f"{name}: reported {CGROUP_NO_LIMIT_TEXT!r}, not a measured number",
        )  # EXIT: an unbounded counter measured nothing, so it is not a peak
    try:
        return int(raw), None
    except ValueError:
        return (
            None,
            f"{name}: not an integer: {raw[:60]!r}",
        )  # EXIT: unexpected content is a failed read, not a zero


@dataclass(slots=True)
class MemorySnapshot:
    """One process + cgroup reading, with every failed sub-read named.

    Byte fields are ``int | None`` so the peak is only ever the max of real
    readings; ``read_errors`` carries which file or field gave nothing.
    """

    tag: str
    t_monotonic_s: float
    proc_vmhwm_bytes: int | None
    proc_vmrss_bytes: int | None
    proc_vmswap_bytes: int | None
    proc_vmpeak_bytes: int | None
    cgroup_memory_peak_bytes: int | None
    cgroup_memory_current_bytes: int | None
    cgroup_memory_max_bytes: int | None
    cgroup_memory_swap_current_bytes: int | None
    cgroup_memory_swap_max_bytes: int | None
    events: dict[str, int] = field(default_factory=dict)
    read_errors: list[str] = field(default_factory=list)

    def to_record(self) -> dict[str, object]:
        """The flat JSON shape the committed measurements carry."""
        out: dict[str, object] = {
            "tag": self.tag,
            "t_monotonic_s": self.t_monotonic_s,
            "proc_vmhwm_bytes": self.proc_vmhwm_bytes,
            "proc_vmrss_bytes": self.proc_vmrss_bytes,
            "proc_vmswap_bytes": self.proc_vmswap_bytes,
            "proc_vmpeak_bytes": self.proc_vmpeak_bytes,
            "cgroup_memory_peak_bytes": self.cgroup_memory_peak_bytes,
            "cgroup_memory_current_bytes": self.cgroup_memory_current_bytes,
            "cgroup_memory_max_bytes": self.cgroup_memory_max_bytes,
            "cgroup_memory_swap_current_bytes": self.cgroup_memory_swap_current_bytes,
            "cgroup_memory_swap_max_bytes": self.cgroup_memory_swap_max_bytes,
            **self.events,
        }
        # Errors are omitted when there are none: the whole record is one line.
        if self.read_errors:
            out["read_errors"] = list(self.read_errors)
        return out


def memory_snapshot(tag: str) -> MemorySnapshot:
    """One measured snapshot: process + cgroup v2 (v1 fallback included)."""
    proc, errors = _read_proc_status_fields(_PROC_STATUS_FIELDS)

    peak, error = _read_cgroup_int("memory.peak", allow_unlimited=False)
    if peak is None:  # cgroup v1 shape
        if error is not None:
            errors.append(error)
        peak, error = _read_cgroup_int(
            "memory.max_usage_in_bytes", allow_unlimited=False
        )
    if error is not None:
        errors.append(error)

    scalars: dict[str, int | None] = {}
    for filename, key, is_limit in (
        ("memory.current", "cgroup_memory_current_bytes", False),
        ("memory.max", "cgroup_memory_max_bytes", True),
        ("memory.swap.current", "cgroup_memory_swap_current_bytes", False),
        ("memory.swap.max", "cgroup_memory_swap_max_bytes", True),
    ):
        value, error = _read_cgroup_int(filename, allow_unlimited=is_limit)
        scalars[key] = value
        if error is not None:
            errors.append(error)

    events: dict[str, int] = {}
    for ev in ("memory.events", "memory.events.local"):
        try:
            body = (CGROUP_DIR / ev).read_text(encoding="utf-8")
        except OSError as exc:
            errors.append(
                f"{ev}: {type(exc).__name__}: {exc}"
            )  # EXIT: an absent events file is a gap in the OOM evidence
            continue
        for line in body.splitlines():
            k, _, v = line.partition(" ")
            if k in {"oom", "oom_kill", "max_usage"}:
                try:
                    events[f"{ev}.{k}"] = int(v)
                except ValueError:
                    errors.append(
                        f"{ev}.{k}: unparseable {v.strip()!r}"
                    )  # EXIT: an unreadable OOM counter is not evidence of no OOM
    return MemorySnapshot(
        tag=tag,
        t_monotonic_s=round(time.monotonic(), 3),
        proc_vmhwm_bytes=proc.get("VmHWM"),
        proc_vmrss_bytes=proc.get("VmRSS"),
        proc_vmswap_bytes=proc.get("VmSwap"),
        proc_vmpeak_bytes=proc.get("VmPeak"),
        cgroup_memory_peak_bytes=peak,
        events=events,
        read_errors=errors,
        **scalars,
    )


# --------------------------------------------------------------------------
# pin / artifact identity
# --------------------------------------------------------------------------
def sha256_bytes(path: Path) -> tuple[int, str]:
    """Bytes read and their digest, streamed so file size does not matter."""
    h = hashlib.sha256()
    size = 0
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            size += len(chunk)
            h.update(chunk)
    return size, h.hexdigest()


def resolved_versions() -> dict[str, object]:
    from importlib import metadata

    out: dict[str, object] = {}
    for pkg in PINNED_PACKAGES:
        try:
            out[pkg] = metadata.version(pkg)
        except metadata.PackageNotFoundError:
            out[pkg] = None
    import torch

    out["_torch_build"] = {
        "torch_version": torch.__version__,
        "torch_cuda_version_attr": torch.version.cuda,
        "torch_cuda_available": bool(torch.cuda.is_available()),
        "torch_threads": int(torch.get_num_threads()),
        "python_version": sys.version.split()[0],
    }
    return out


def artifact_identity(artifacts_path: Path) -> dict[str, object]:
    """Name / size / sha256 of what actually landed in the mounted cache."""
    info: dict[str, object] = {
        "artifacts_path": str(artifacts_path),
        "exists": artifacts_path.is_dir(),
        "cache_total_bytes": 0,
        "file_count": 0,
        "files": [],
        "layout_files": [],
    }
    if not info["exists"]:
        return info  # EXIT: an absent cache is the finding; the zeros say "nothing landed"
    total = 0
    count = 0
    files: list[dict[str, object]] = []
    layout: list[dict[str, object]] = []
    for path in sorted(artifacts_path.rglob("*")):
        if not path.is_file():
            continue
        size = path.stat().st_size
        total += size
        count += 1
        rel = str(path.relative_to(artifacts_path))
        # sha256 only for the layout weights that define the Heron pin; hashing
        # the whole cache would dominate the measurement run.
        digest = None
        if "layout" in rel.lower() and size <= 2 * 1024**3:
            digest = sha256_bytes(path)[1]
        entry = {"path": rel, "bytes": size}
        if digest:
            entry["sha256"] = digest
            layout.append(entry)
        elif size > 1_000_000:
            files.append(entry)
    info["cache_total_bytes"] = total
    info["file_count"] = count
    info["files"] = files
    info["layout_files"] = layout
    return info


# --------------------------------------------------------------------------
# PDF structure / rendering (pypdfium2 is Docling 2.130's own PDF backend, so
# the render and text-layer numbers below come from the same library the
# pinned pipeline uses internally)
# --------------------------------------------------------------------------
def _image_object_type() -> tuple[int, str | None]:
    """The image page-object kind, plus why it was not read from the library."""
    try:
        from pypdfium2 import raw

        return int(raw.FPDF_PAGEOBJ_IMAGE), None
    except Exception as exc:
        return (
            FPDF_PAGEOBJ_IMAGE,
            f"pypdfium2.raw unreadable, image kind assumed to be "
            f"{FPDF_PAGEOBJ_IMAGE}: {type(exc).__name__}: {exc}",
        )  # EXIT: a pinned constant, but the record must say it was not measured


def _object_kind(obj: object) -> int | None:
    """A page object's kind, or None when neither accessor answers."""
    for attr in ("get_type", "type"):
        val = getattr(obj, attr, None)
        if callable(val):
            try:
                return int(val())
            except Exception:
                continue  # EXIT: this accessor refused; the other may still answer
        elif val is not None:
            try:
                return int(val)
            except Exception:
                continue  # EXIT: an uncoercible kind is no knowledge of the kind
    return None  # EXIT: unknown kind, so the object is neither image nor not-image


def _page_image_objects(page: Any, img_type: int) -> tuple[int | None, str | None]:
    """Count a page's image objects without decoding any bitmap.

    ``image_only=True`` filters on the object type, which is what fin's rule
    needs (a count, not a pixel). ``(None, reason)`` when no API shape answers:
    an unavailable count is not "no images" and must not select the OCR rule.
    """
    try:
        return len(page.get_objects(image_only=True)), None
    except TypeError as exc:  # older pypdfium2 has no image_only shortcut
        try:
            count = 0
            unreadable = 0
            for obj in page.get_objects():
                kind = _object_kind(obj)
                if kind is None:
                    unreadable += 1
                elif kind == img_type:
                    count += 1
        except Exception as retry_exc:
            return (
                None,
                f"type scan after {type(exc).__name__}: {exc}; "
                f"{type(retry_exc).__name__}: {retry_exc}",
            )  # EXIT: no count at all, so nothing is recorded as zero images
        if unreadable:
            return (
                count,
                f"{unreadable} objects with unreadable kind were not counted",
            )  # EXIT: a partial count is labelled as one
        return count, None
    except Exception as exc:
        return (
            None,
            f"{type(exc).__name__}: {exc}",
        )  # EXIT: an unreadable object list is a gap, not a zero


def _apply_page_ocr(
    entry: dict[str, object],
    pil: Any,
    page_no: int,
    ocr_engine: Any,
    ocr_trigger: str,
    image_dir: Path | None,
    failures: list[str],
) -> None:
    """One rendered page through PNG -> the repo's Tesseract engine, recorded.

    ``image_dir`` persists the PNG so the vision payload is measured; ``None``
    means a temp file removed after the call.
    """
    png_path = (
        image_dir / f"page_{page_no:04d}.png"
        if image_dir is not None
        else Path(f"/tmp/g4_page_{os.getpid()}_{page_no:04d}.png")
    )
    entry["ocr_trigger"] = ocr_trigger
    pil.save(png_path, format="PNG")
    try:
        ocr_text = ocr_engine.recognize(source=png_path, page=page_no)
    except Exception as exc:  # fin.corpus.ocr's typed OcrRequiredUnavailableError
        entry["ocr_error_code"] = getattr(exc, "code", type(exc).__name__)
        entry["ocr_error"] = (
            f"{type(exc).__name__}: {exc}"  # EXIT: a code alone would hide why the page has no text
        )
        entry["ocr_used"] = False
    else:
        entry["ocr_used"] = True
        entry["ocr_text_char_count"] = len(ocr_text)
        # Short sample so the OCR output is auditable without shipping
        # the whole page text in the measurement line.
        entry["ocr_text_sample"] = " ".join(ocr_text.split())[:160]
    finally:
        entry["ocr_lang"] = ocr_engine.lang
        if image_dir is None:
            try:
                png_path.unlink(missing_ok=True)
            except OSError as exc:
                failures.append(
                    f"page {page_no}: temp PNG {png_path.name} not removed: "
                    f"{type(exc).__name__}: {exc}"
                )  # EXIT: a leaked bitmap is not a gap in a number, but not invisible either
    if image_dir is not None:
        entry["page_image_png_bytes"] = png_path.stat().st_size
        entry["page_image_png_sha256"] = sha256_bytes(png_path)[1]


# --------------------------------------------------------------------------
# Docling + Heron
# --------------------------------------------------------------------------
def build_converter(artifacts_path: Path, layout_preset: str, threads: int):
    """Pinned standard-PDF pipeline: Heron layout + TableFormer, CPU, no OCR
    engine inside Docling, no VLM, no picture classifier (ADR 0089)."""
    from docling.datamodel.accelerator_options import AcceleratorOptions
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import (
        LayoutObjectDetectionOptions,
        PdfPipelineOptions,
    )
    from docling.document_converter import DocumentConverter, PdfFormatOption

    layout_note = f"from_preset({layout_preset!r})"
    try:
        layout_options = LayoutObjectDetectionOptions.from_preset(layout_preset)
    except Exception as exc:
        # The default LayoutObjectDetectionOptions is already the Heron spec;
        # fall back to it but record that the explicit preset call failed.
        layout_note = f"from_preset failed ({type(exc).__name__}: {exc}); default"
        layout_options = LayoutObjectDetectionOptions()
        # EXIT: the built pipeline is still Heron, but the record says the
        # preset was not what selected it

    opts = PdfPipelineOptions(
        artifacts_path=artifacts_path,
        # ADR 0089: no local VLM, no separate picture-classification model.
        do_picture_classification=False,
        do_picture_description=False,
        do_code_enrichment=False,
        do_formula_enrichment=False,
        do_table_structure=True,
        # OCR is the explicit per-page Tesseract step gated by the repo rule.
        do_ocr=False,
        # Page bitmaps are produced by the render pass above (same pypdfium2
        # backend), so nothing is held twice while the document tree is alive.
        generate_page_images=False,
        generate_picture_images=False,
        layout_options=layout_options,
        accelerator_options=AcceleratorOptions(device="cpu", num_threads=threads),
    )
    fmt_option = PdfFormatOption(pipeline_options=opts)
    conv = DocumentConverter(format_options={InputFormat.PDF: fmt_option})
    spec = getattr(layout_options, "model_spec", None)
    identity = {
        "layout_selection": layout_note,
        "pipeline_cls": getattr(
            getattr(fmt_option, "pipeline_cls", None), "__name__", None
        ),
        "backend_cls": getattr(getattr(fmt_option, "backend", None), "__name__", None),
        "model_spec_name": getattr(spec, "name", None),
        "repo_id": getattr(spec, "repo_id", None),
        "revision": getattr(spec, "revision", None),
        "engine_overrides": {
            str(k): getattr(v, "repo_id", None)
            for k, v in (getattr(spec, "engine_overrides", None) or {}).items()
        },
        "do_ocr": opts.do_ocr,
        "do_table_structure": opts.do_table_structure,
        "do_picture_classification": opts.do_picture_classification,
        "do_picture_description": opts.do_picture_description,
        "do_code_enrichment": opts.do_code_enrichment,
        "do_formula_enrichment": opts.do_formula_enrichment,
        "device": str(opts.accelerator_options.device),
        "num_threads": opts.accelerator_options.num_threads,
        "layout_batch_size": opts.layout_batch_size,
        "table_batch_size": opts.table_batch_size,
    }
    return conv, identity


def docling_builtin_limits() -> dict[str, object]:
    """What the pinned library itself caps by default (input to the T2a
    byte/page-cap decision: sys.maxsize means fin must set the bound)."""
    from docling.datamodel.settings import DocumentLimits, settings

    dl = DocumentLimits()
    return {
        "DocumentLimits.max_num_pages": dl.max_num_pages,
        "DocumentLimits.max_file_size": dl.max_file_size,
        "settings.perf.page_batch_size": settings.perf.page_batch_size,
        "settings.perf.doc_batch_size": settings.perf.doc_batch_size,
        "settings.artifacts_path": str(settings.artifacts_path),
    }


def region_counts_from_document(doc) -> dict[int, dict[str, object]]:
    """Bucket Docling's layout output per physical page: text/table/figure."""
    per_page: dict[int, dict[str, object]] = {}

    def bucket(page_no: int) -> dict[str, object]:
        return per_page.setdefault(
            int(page_no),
            {"text": 0, "table": 0, "figure": 0, "table_boxes": [], "figure_boxes": []},
        )

    for item in getattr(doc, "texts", []) or []:
        for prov in getattr(item, "prov", None) or []:
            page_no = getattr(prov, "page_no", None)
            if page_no is None:
                continue  # EXIT: provenance with no page number has no page to charge
            bucket(int(page_no))["text"] = int(bucket(int(page_no))["text"]) + 1

    for kind, attr in (("table", "tables"), ("figure", "pictures")):
        for item in getattr(doc, attr, []) or []:
            for prov in getattr(item, "prov", None) or []:
                page_no = getattr(prov, "page_no", None)
                if page_no is None:
                    continue  # EXIT: provenance with no page number has no page to charge
                b = bucket(int(page_no))
                b[kind] = int(b[kind]) + 1
                box = getattr(prov, "bbox", None)
                if box is not None:
                    key = f"{kind}_boxes"
                    if len(b[key]) < 40:  # keep the single JSON line bounded
                        b[key].append(
                            {
                                "l": round(float(box.l), 1),
                                "t": round(float(box.t), 1),
                                "r": round(float(box.r), 1),
                                "b": round(float(box.b), 1),
                            }
                        )
                    else:
                        b[f"{kind}_boxes_capped"] = True
    return per_page


# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pdf", required=True, type=Path)
    ap.add_argument("--dpi", type=int, default=200)
    ap.add_argument("--artifacts", type=Path, default=Path("/models"))
    ap.add_argument(
        "--layout-preset",
        default="layout_heron_default",
        help="Docling layout object-detection preset id (ADR 0089 pins Heron; "
        "'layout_heron_default' is the Heron RT-DETR spec)",
    )
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument(
        "--fin-src",
        type=Path,
        default=Path("/worktree/src"),
        help="read-only worktree checkout that owns fin.corpus.ocr (SSOT for "
        "the OCR decision rule and language string)",
    )
    ap.add_argument(
        "--write-images",
        action="store_true",
        help="also persist each rendered page bitmap (PNG) so the vision-call "
        "payload size is measurable, not estimated",
    )
    ap.add_argument(
        "--force-ocr",
        action="store_true",
        help="self-check only: run CPU Tesseract on every rendered page even "
        "where the repo rule does not select OCR, to prove the OCR boundary "
        "is live. Not a coverage claim.",
    )
    ap.add_argument("--image-dir", type=Path, default=Path("/out/pages"))
    ap.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="harness safety valve; over-limit is reported, never silently cut",
    )
    ap.add_argument(
        "--slice-pass",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="per-page latency from single-page conversions (Docling's own "
        "whole-document latency is always measured too)",
    )
    args = ap.parse_args()

    pdf_path: Path = args.pdf
    if not pdf_path.is_file():
        log(f"[driver] no such PDF: {pdf_path}")
        return 2  # EXIT: nothing was measured, so no line is printed at all

    # The repo's OCR rule and language string, imported from the read-only
    # mount. Hard failure if absent: a locally re-implemented rule would stop
    # being a measurement of fin.
    sys.path.insert(0, str(args.fin_src))
    from fin.corpus.ocr import (  # noqa: E402
        DEFAULT_TESSERACT_LANG,
        TesseractEngine,
        detect_ocr_required,
    )
    import pypdfium2 as pdfium  # noqa: E402

    checkpoints: list[MemorySnapshot] = [memory_snapshot("start")]
    harness_failures: list[str] = []
    file_bytes, file_sha256 = sha256_bytes(pdf_path)

    # ---------------------------------------------------------------- render
    t0 = time.monotonic()
    pdf_doc = pdfium.PdfDocument(str(pdf_path))
    page_count = len(pdf_doc)
    img_type, img_type_error = _image_object_type()
    if img_type_error is not None:
        harness_failures.append(img_type_error)
    pages: list[dict[str, object]] = []
    ocr_engine = TesseractEngine(lang=DEFAULT_TESSERACT_LANG)
    image_dir: Path | None = args.image_dir if args.write_images else None
    if image_dir is not None:
        try:
            image_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log(f"[driver] cannot write images to {image_dir}: {exc}")
            harness_failures.append(
                f"--write-images off: cannot create {image_dir}: "
                f"{type(exc).__name__}: {exc}"
            )  # EXIT: page bitmaps then carry no byte measurement at all
            image_dir = None

    for idx in range(page_count):
        page_no = idx + 1
        pt0 = time.monotonic()
        page = pdf_doc[idx]
        width_pt, height_pt = page.get_size()
        textpage = page.get_textpage()
        native_text = textpage.get_text_range() or ""
        n_images, images_error = _page_image_objects(page, img_type)
        if images_error is not None:
            log(f"[driver] page {page_no} object scan failed: {images_error}")

        entry: dict[str, object] = {
            "page_no": page_no,
            "size_pt": [round(float(width_pt), 2), round(float(height_pt), 2)],
            "native_text_char_count": len(native_text),
            "native_text_codepoints": len(native_text),
            "pdf_image_objects": n_images,
            "pdf_image_objects_error": images_error,
            # The rule's own "no image reference" input is 0, so a failed scan
            # is readable only through pdf_image_objects_error.
            "ocr_required_by_repo_rule": bool(
                detect_ocr_required(
                    text=native_text,
                    image_count=n_images if n_images is not None else 0,
                    min_text_chars=1,
                )
            ),
            "ocr_used": False,
            "ocr_trigger": None,
            "ocr_lang": None,
            "ocr_text_char_count": 0,
            "ocr_text_sample": None,
            "ocr_error_code": None,
            "ocr_error": None,
        }

        bitmap = page.render(scale=args.dpi / 72.0)
        pil = bitmap.to_pil()
        entry["rendered_width_px"] = int(pil.width)
        entry["rendered_height_px"] = int(pil.height)
        try:
            dpi_x, dpi_y = bitmap.get_dpi()
            entry["rendered_dpi"] = [round(float(dpi_x), 1), round(float(dpi_y), 1)]
        except Exception as exc:
            entry["rendered_dpi"] = None
            entry["rendered_dpi_error"] = (
                f"{type(exc).__name__}: {exc}"  # EXIT: the requested DPI is not a measured one
            )

        ocr_trigger = "repo_rule" if entry["ocr_required_by_repo_rule"] else None
        if args.force_ocr and ocr_trigger is None:
            # Harness self-check only: exercises the CPU Tesseract boundary on a
            # page the repo rule would not select, so "OCR path is live" is
            # observed rather than assumed even when every committed PDF has a
            # text layer. Never used to decide coverage in a measured run.
            ocr_trigger = "forced_selftest"
        if ocr_trigger is not None:
            _apply_page_ocr(
                entry,
                pil,
                page_no,
                ocr_engine,
                ocr_trigger,
                image_dir,
                harness_failures,
            )
        elif image_dir is not None:
            png_path = image_dir / f"page_{page_no:04d}.png"
            pil.save(png_path, format="PNG")
            entry["page_image_png_bytes"] = png_path.stat().st_size

        del pil, bitmap
        entry["render_latency_ms"] = round((time.monotonic() - pt0) * 1000.0, 1)
        pages.append(entry)

    render_ms = (time.monotonic() - t0) * 1000.0
    checkpoints.append(memory_snapshot("after_render_and_ocr"))

    if args.max_pages is not None and page_count > args.max_pages:
        # attachment_page_limit_exceeded shape: reported, never silently cut.
        log(f"[driver] page limit exceeded: {page_count} > {args.max_pages}")

    # ------------------------------------------------------- Docling + Heron
    conv, pipeline_identity = build_converter(
        args.artifacts, args.layout_preset, args.threads
    )
    checkpoints.append(memory_snapshot("after_converter_init"))

    t_full = time.monotonic()
    full_error = None
    docling_status = None
    docling_page_count = None
    regions: dict[int, dict[str, object]] = {}
    doc_page_sizes: dict[int, list[float]] = {}
    res = None
    try:
        res = conv.convert(str(pdf_path))
        docling_status = str(getattr(res, "status", None))
        doc = res.document
        docling_page_count = len(getattr(doc, "pages", []) or [])
        regions = region_counts_from_document(doc)
        for p in getattr(doc, "pages", []) or []:
            no = getattr(p, "number", None)
            size = getattr(p, "size", None)
            if no is not None and size is not None:
                doc_page_sizes[int(no)] = [
                    round(float(size.width), 2),
                    round(float(size.height), 2),
                ]
    except Exception as exc:
        full_error = f"{type(exc).__name__}: {exc}"
        log(f"[driver] Docling whole-document pass failed: {full_error}")
        # EXIT: this pass is the only region source, so regions go None below
    full_doc_ms = (time.monotonic() - t_full) * 1000.0
    checkpoints.append(memory_snapshot("after_docling_whole_doc"))

    # Docling's export is the authority for region boxes, and a pass that blew
    # up measured no regions at all, which is not the same fact as zero regions.
    regions_measured = full_error is None
    for entry in pages:
        reg = regions.get(int(entry["page_no"]), {})
        entry["region_counts"] = {
            kind: (int(reg.get(kind, 0)) if regions_measured else None)
            for kind in ("text", "table", "figure")
        }  # EXIT: on a measured page an absent bucket really is zero
        entry["table_boxes"] = reg.get("table_boxes", []) if regions_measured else None
        entry["figure_boxes"] = (
            reg.get("figure_boxes", []) if regions_measured else None
        )
        entry["docling_page_size_pt"] = doc_page_sizes.get(int(entry["page_no"]))

    # ------------------------------------------------- per-page latency pass
    slice_mode = False
    slice_error = None
    per_page_ms: list[float] = []
    if args.slice_pass:
        slice_mode = True
        for idx in range(page_count):
            page_no = idx + 1
            one = None
            tmp_pdf = Path(f"/tmp/g4_slice_{os.getpid()}_{page_no:04d}.pdf")
            t_pg = time.monotonic()
            try:
                one = pdfium.PdfDocument.new()
                one.import_pages(pdf_doc, pages=[idx])
                one.save(str(tmp_pdf))
                conv.convert(str(tmp_pdf))
                ms = (time.monotonic() - t_pg) * 1000.0
                per_page_ms.append(ms)
                pages[idx]["page_latency_ms"] = round(ms, 1)
                pages[idx]["page_latency_mode"] = "single_page_slice"
            except Exception as exc:
                slice_error = f"page {page_no}: {type(exc).__name__}: {exc}"
                log(f"[driver] slice pass page {page_no} failed: {slice_error}")
                pages[idx]["page_latency_ms"] = None
                pages[idx]["page_latency_mode"] = "single_page_slice_failed"
                for rest in pages[idx + 1 :]:
                    rest["page_latency_ms"] = None
                    rest["page_latency_mode"] = "not_measured_after_slice_failure"
                # EXIT: pages after the stop are named as unmeasured instead of
                # leaving their keys out, so a short n in the stats is not a
                # silent shrink of the document
                break
            finally:
                if one is not None:
                    # Explicit close: gc-based cleanup would pile up one PDF
                    # handle per page inside the 4 GiB ceiling.
                    try:
                        one.close()
                    except Exception as exc:
                        harness_failures.append(
                            f"slice page {page_no}: document close failed: "
                            f"{type(exc).__name__}: {exc}"
                        )  # EXIT: a handle left open would inflate the next peak
                        del one
                try:
                    tmp_pdf.unlink(missing_ok=True)
                except OSError as exc:
                    harness_failures.append(
                        f"slice page {page_no}: temp PDF not removed: "
                        f"{type(exc).__name__}: {exc}"
                    )  # EXIT: a slice left on disk is a cost, not a measurement
            if idx % 10 == 9:
                checkpoints.append(memory_snapshot(f"slice_pass_page_{page_no}"))
    else:
        # No per-page number available without measurement: stay explicit.
        for entry in pages:
            entry["page_latency_ms"] = None
            entry["page_latency_mode"] = "not_measured"
    checkpoints.append(memory_snapshot("end"))

    final = memory_snapshot("final")
    # A failed read is None and drops out of the peak; a limit file's "no limit"
    # reading is CGROUP_NO_LIMIT_BYTES and must never enter this max at all.
    vmhwm = max(
        [c.proc_vmhwm_bytes for c in checkpoints if c.proc_vmhwm_bytes]
        + [final.proc_vmhwm_bytes or 0]
    )
    cgroup_peak = max(
        [
            c.cgroup_memory_peak_bytes or 0
            for c in checkpoints
            if (c.cgroup_memory_peak_bytes or 0) > 0
        ]
        + [final.cgroup_memory_peak_bytes or 0]
    )
    oom_kill_events = max(
        [
            int(v)
            for c in checkpoints + [final]
            for k, v in c.events.items()
            if k.startswith("memory.events") and k.endswith("oom_kill")
        ]
        or [0]
    )

    latencies = [p.get("page_latency_ms") for p in pages]
    latencies = [x for x in latencies if isinstance(x, (int, float))]

    record = {
        "schema": "fin.g4.t2a.pipeline_measurement/1",
        "file": {
            "path": str(pdf_path),
            "name": pdf_path.name,
            "bytes": file_bytes,
            "sha256": file_sha256,
        },
        "page_count": page_count,
        "requested_dpi": args.dpi,
        "max_pages": args.max_pages,
        "page_limit_exceeded": bool(
            args.max_pages is not None and page_count > args.max_pages
        ),
        "pages": pages,
        "totals": {
            "render_and_ocr_latency_ms": round(render_ms, 1),
            "docling_whole_doc_latency_ms": round(full_doc_ms, 1),
            "docling_status": docling_status,
            "docling_page_count": docling_page_count,
            "docling_error": full_error,
            "slice_pass_enabled": slice_mode,
            "slice_pass_error": slice_error,
            "total_latency_ms": round(
                render_ms + full_doc_ms + sum(per_page_ms), 1
            ),
            "page_latency_ms_stats": {
                "n": len(latencies),
                "note": "single-page conversions on an already-initialised "
                "pipeline (DocumentConverter caches the initialised "
                "StandardPdfPipeline), i.e. warm steady-state per-page cost",
                "mean": round(statistics.fmean(latencies), 1) if latencies else None,
                "median": round(statistics.median(latencies), 1) if latencies else None,
                "p95": (
                    round(statistics.quantiles(latencies, n=20)[-1], 1)
                    if len(latencies) >= 20
                    else (round(max(latencies), 1) if latencies else None)
                ),
                "max": round(max(latencies), 1) if latencies else None,
            },
            "regions_total": {
                kind: (
                    sum(int(p["region_counts"][kind]) for p in pages)
                    if regions_measured
                    else None
                )
                for kind in ("text", "table", "figure")
            },  # EXIT: no sum over an unmeasured pass, so a null is never a zero
            "pages_needing_ocr": sum(
                1 for p in pages if p["ocr_required_by_repo_rule"]
            ),
            "pages_with_ocr_applied": sum(1 for p in pages if p["ocr_used"]),
            "pages_with_native_text": sum(
                1 for p in pages if p["native_text_char_count"] > 0
            ),
            "native_text_chars_total": sum(
                p["native_text_char_count"] for p in pages
            ),
            "ocr_text_chars_total": sum(p["ocr_text_char_count"] for p in pages),
            "peak_rss_vmhwm_bytes": vmhwm,
            "cgroup_peak_bytes": cgroup_peak,
            "cgroup_memory_max_bytes": final.cgroup_memory_max_bytes,
            "cgroup_memory_swap_max_bytes": final.cgroup_memory_swap_max_bytes,
            "cgroup_memory_swap_current_bytes": final.cgroup_memory_swap_current_bytes,
            "proc_vmswap_bytes": final.proc_vmswap_bytes,
            "oom_kill_events_in_cgroup": oom_kill_events,
            "oom_killed": bool(oom_kill_events > 0),
            "oom_killed_source": "cgroup memory.events oom_kill counter",
            "harness_failures": harness_failures,
            "memory_checkpoints": [c.to_record() for c in checkpoints + [final]],
        },
        "pin_manifest": resolved_versions(),
        "pipeline_identity": pipeline_identity,
        "docling_builtin_limits": docling_builtin_limits(),
        "heron_artifact": {**artifact_identity(args.artifacts), **pipeline_identity},
        "repo_rule_provenance": {
            "ocr_decision": "fin.corpus.ocr.detect_ocr_required(min_text_chars=1)",
            "ocr_engine": "fin.corpus.ocr.TesseractEngine",
            "tesseract_lang_default": DEFAULT_TESSERACT_LANG,
            "page_unit_rule": "one frozen unit per physical page (fin.corpus.parsers._pdf)",
        },
    }
    sys.stdout.write(json.dumps(record, separators=(",", ":"), ensure_ascii=True) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
