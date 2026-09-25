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
from pathlib import Path

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


def log(msg: str) -> None:
    """Diagnostics on stderr only - stdout carries exactly one JSON line."""
    print(msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# memory measurement (measured, never inferred)
# --------------------------------------------------------------------------
def _read_proc_status_field(names: tuple[str, ...]) -> dict[str, int | None]:
    out: dict[str, int | None] = {}
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                if key in names:
                    parts = rest.split()
                    if parts:
                        try:
                            out[key] = int(parts[0]) * (1024 if "kB" in parts else 1)
                        except ValueError:
                            out[key] = None
    except OSError as exc:  # pragma: no cover - /proc always present in Linux
        out["proc_status_error"] = None  # type: ignore[assignment]
        log(f"[mem] cannot read /proc/self/status: {exc}")
    return {k: out.get(k) for k in names}


def _read_cgroup_file(name: str) -> int | None:
    path = CGROUP_DIR / name
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if raw == "max":
        return -1
    try:
        return int(raw)
    except ValueError:
        # key/value files such as memory.events
        out: dict[str, int] = {}
        for line in raw.splitlines():
            k, _, v = line.partition(" ")
            try:
                out[k] = int(v)
            except ValueError:
                continue
        return out.get("oom_kill")  # type: ignore[return-value]
    return None


def memory_snapshot(tag: str) -> dict[str, object]:
    """One measured snapshot: process + cgroup v2 (v1 fallback included)."""
    proc = _read_proc_status_field(("VmHWM", "VmRSS", "VmSwap", "VmPeak"))
    peak_v2 = _read_cgroup_file("memory.peak")
    if peak_v2 is None:  # cgroup v1 shape
        peak_v2 = _read_cgroup_file("memory.max_usage_in_bytes")
    events: dict[str, int | None] = {}
    for ev in ("memory.events", "memory.events.local"):
        try:
            body = (CGROUP_DIR / ev).read_text(encoding="utf-8")
        except OSError:
            continue
        for line in body.splitlines():
            k, _, v = line.partition(" ")
            if k in {"oom", "oom_kill", "max_usage"}:
                try:
                    events[f"{ev}.{k}"] = int(v)
                except ValueError:
                    pass
    return {
        "tag": tag,
        "t_monotonic_s": round(time.monotonic(), 3),
        "proc_vmhwm_bytes": proc.get("VmHWM"),
        "proc_vmrss_bytes": proc.get("VmRSS"),
        "proc_vmswap_bytes": proc.get("VmSwap"),
        "proc_vmpeak_bytes": proc.get("VmPeak"),
        "cgroup_memory_peak_bytes": peak_v2,
        "cgroup_memory_current_bytes": _read_cgroup_file("memory.current"),
        "cgroup_memory_max_bytes": _read_cgroup_file("memory.max"),
        "cgroup_memory_swap_current_bytes": _read_cgroup_file("memory.swap.current"),
        "cgroup_memory_swap_max_bytes": _read_cgroup_file("memory.swap.max"),
        **events,
    }


# --------------------------------------------------------------------------
# pin / artifact identity
# --------------------------------------------------------------------------
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
        return info
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
            h = hashlib.sha256()
            with path.open("rb") as fh:
                for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                    h.update(chunk)
            digest = h.hexdigest()
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
def _image_object_type() -> int:
    try:
        from pypdfium2 import raw  # type: ignore[attr-defined]

        return int(raw.FPDF_PAGEOBJ_IMAGE)
    except Exception:
        return 3  # FPDF_PAGEOBJ_IMAGE constant


def _object_kind(obj: object) -> int:
    for attr in ("get_type", "type"):
        val = getattr(obj, attr, None)
        if callable(val):
            try:
                return int(val())
            except Exception:
                continue
        elif val is not None:
            try:
                return int(val)
            except Exception:
                pass
    return -1


def sha256_bytes(path: Path) -> tuple[int, str]:
    h = hashlib.sha256()
    size = 0
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            size += len(chunk)
            h.update(chunk)
    return size, h.hexdigest()


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
                continue
            bucket(int(page_no))["text"] = int(bucket(int(page_no))["text"]) + 1

    for kind, attr in (("table", "tables"), ("figure", "pictures")):
        for item in getattr(doc, attr, []) or []:
            for prov in getattr(item, "prov", None) or []:
                page_no = getattr(prov, "page_no", None)
                if page_no is None:
                    continue
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
        return 2

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

    checkpoints: list[dict[str, object]] = [memory_snapshot("start")]
    file_bytes, file_sha256 = sha256_bytes(pdf_path)

    # ---------------------------------------------------------------- render
    t0 = time.monotonic()
    pdf_doc = pdfium.PdfDocument(str(pdf_path))
    page_count = len(pdf_doc)
    img_type = _image_object_type()
    pages: list[dict[str, object]] = []
    ocr_engine = TesseractEngine(lang=DEFAULT_TESSERACT_LANG)
    if args.write_images:
        try:
            args.image_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log(f"[driver] cannot write images to {args.image_dir}: {exc}")
            args.write_images = False

    for idx in range(page_count):
        page_no = idx + 1
        pt0 = time.monotonic()
        page = pdf_doc[idx]
        width_pt, height_pt = page.get_size()
        textpage = page.get_textpage()
        native_text = textpage.get_text_range() or ""
        try:
            # image_only=True filters on the object type without decoding the
            # bitmaps, which is what fin's rule needs (a count, not a pixel).
            n_images = len(page.get_objects(image_only=True))
        except TypeError:
            # Older pypdfium2 without the image_only shortcut: scan by type.
            try:
                n_images = sum(
                    1 for obj in page.get_objects() if _object_kind(obj) == img_type
                )
            except Exception as exc:  # pragma: no cover - defensive
                log(f"[driver] page {page_no} object scan failed: {exc}")
                n_images = -1
        except Exception as exc:  # pragma: no cover - defensive
            log(f"[driver] page {page_no} object scan failed: {exc}")
            n_images = -1

        entry: dict[str, object] = {
            "page_no": page_no,
            "size_pt": [round(float(width_pt), 2), round(float(height_pt), 2)],
            "native_text_char_count": len(native_text),
            "native_text_codepoints": len(native_text),
            "pdf_image_objects": n_images,
            "ocr_required_by_repo_rule": bool(
                detect_ocr_required(
                    text=native_text, image_count=max(n_images, 0), min_text_chars=1
                )
            ),
            "ocr_used": False,
            "ocr_trigger": None,
            "ocr_lang": None,
            "ocr_text_char_count": 0,
            "ocr_text_sample": None,
            "ocr_error_code": None,
        }

        bitmap = page.render(scale=args.dpi / 72.0)
        pil = bitmap.to_pil()
        entry["rendered_width_px"] = int(pil.width)
        entry["rendered_height_px"] = int(pil.height)
        try:
            dpi_x, dpi_y = bitmap.get_dpi()
            entry["rendered_dpi"] = [round(float(dpi_x), 1), round(float(dpi_y), 1)]
        except Exception:
            entry["rendered_dpi"] = [args.dpi, args.dpi]

        ocr_trigger = "repo_rule" if entry["ocr_required_by_repo_rule"] else None
        if args.force_ocr and ocr_trigger is None:
            # Harness self-check only: exercises the CPU Tesseract boundary on a
            # page the repo rule would not select, so "OCR path is live" is
            # observed rather than assumed even when every committed PDF has a
            # text layer. Never used to decide coverage in a measured run.
            ocr_trigger = "forced_selftest"
        if ocr_trigger is not None:
            entry["ocr_trigger"] = ocr_trigger
            png_path = None
            if args.write_images:
                png_path = args.image_dir / f"page_{page_no:04d}.png"
            else:
                tmp = Path(
                    f"/tmp/g4_page_{os.getpid()}_{page_no:04d}.png"
                )
                png_path = tmp
            pil.save(png_path, format="PNG")
            try:
                ocr_text = ocr_engine.recognize(source=png_path, page=page_no)
                entry["ocr_used"] = True
                entry["ocr_text_char_count"] = len(ocr_text)
                # Short sample so the OCR output is auditable without shipping
                # the whole page text in the measurement line.
                entry["ocr_text_sample"] = " ".join(ocr_text.split())[:160]
            except Exception as exc:  # typed OcrRequiredUnavailableError path
                entry["ocr_error_code"] = getattr(exc, "code", type(exc).__name__)
                entry["ocr_used"] = False
            finally:
                entry["ocr_lang"] = ocr_engine.lang
                if not args.write_images:
                    try:
                        png_path.unlink(missing_ok=True)
                    except OSError:
                        pass
            if args.write_images:
                entry["page_image_png_bytes"] = png_path.stat().st_size
                entry["page_image_png_sha256"] = sha256_bytes(png_path)[1]
        elif args.write_images:
            png_path = args.image_dir / f"page_{page_no:04d}.png"
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
    full_doc_ms = (time.monotonic() - t_full) * 1000.0
    checkpoints.append(memory_snapshot("after_docling_whole_doc"))

    # Docling's export is the authority for region boxes; fall back to the
    # single-page pass below if the whole-document run blew up.
    for entry in pages:
        reg = regions.get(int(entry["page_no"]), {})
        entry["region_counts"] = {
            "text": int(reg.get("text", 0)),
            "table": int(reg.get("table", 0)),
            "figure": int(reg.get("figure", 0)),
        }
        entry["table_boxes"] = reg.get("table_boxes", [])
        entry["figure_boxes"] = reg.get("figure_boxes", [])
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
                slice_error = f"{type(exc).__name__}: {exc}"
                log(f"[driver] slice pass page {page_no} failed: {slice_error}")
                pages[idx]["page_latency_ms"] = None
                pages[idx]["page_latency_mode"] = "single_page_slice_failed"
                break
            finally:
                if one is not None:
                    # Explicit close: gc-based cleanup would pile up one PDF
                    # handle per page inside the 4 GiB ceiling.
                    try:
                        one.close()
                    except Exception:
                        del one
                try:
                    tmp_pdf.unlink(missing_ok=True)
                except OSError:
                    pass
            if idx % 10 == 9:
                checkpoints.append(memory_snapshot(f"slice_pass_page_{page_no}"))
    else:
        # No per-page number available without measurement: stay explicit.
        for entry in pages:
            entry["page_latency_ms"] = None
            entry["page_latency_mode"] = "not_measured"
    checkpoints.append(memory_snapshot("end"))

    final = memory_snapshot("final")
    vmhwm = max(
        [c["proc_vmhwm_bytes"] for c in checkpoints if c["proc_vmhwm_bytes"]]
        + [final["proc_vmhwm_bytes"] or 0]
    )
    cgroup_peak = max(
        [
            c["cgroup_memory_peak_bytes"] or 0
            for c in checkpoints
            if (c["cgroup_memory_peak_bytes"] or 0) > 0
        ]
        + [final["cgroup_memory_peak_bytes"] or 0]
    )
    oom_kill_events = max(
        [
            int(v)
            for c in checkpoints + [final]
            for k, v in c.items()
            if k.startswith("memory.events") and k.endswith("oom_kill") and v is not None
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
                "text": sum(p["region_counts"]["text"] for p in pages),
                "table": sum(p["region_counts"]["table"] for p in pages),
                "figure": sum(p["region_counts"]["figure"] for p in pages),
            },
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
            "cgroup_memory_max_bytes": final.get("cgroup_memory_max_bytes"),
            "cgroup_memory_swap_max_bytes": final.get("cgroup_memory_swap_max_bytes"),
            "cgroup_memory_swap_current_bytes": final.get(
                "cgroup_memory_swap_current_bytes"
            ),
            "proc_vmswap_bytes": final.get("proc_vmswap_bytes"),
            "oom_kill_events_in_cgroup": oom_kill_events,
            "oom_killed": bool(oom_kill_events > 0),
            "oom_killed_source": "cgroup memory.events oom_kill counter",
            "memory_checkpoints": checkpoints + [final],
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
