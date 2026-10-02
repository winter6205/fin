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
* the product's *figure* rule for the pages that rule cannot select
  (``fin.corpus.parsers._pages.crop_is_legible`` + ``crop_png``), because
  ``detect_ocr_required`` asks whether a text layer collapsed and can never
  answer "yes" on a page that has one — which is the only way a chart's own
  pixel labels are ever read. Both the rule and the cut are the product's own
  functions, so a change to the core moves this measurement with it;
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
import io
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

_PROC_STATUS_FIELDS: Final[tuple[str, ...]] = ("VmHWM", "VmRSS", "VmSwap", "VmPeak")

# filename -> snapshot field, plus whether "max" is a legal reading there. A
# limit file may answer "no limit"; a usage file that says so read nothing.
_CGROUP_SCALAR_FILES: Final[tuple[tuple[str, str, bool], ...]] = (
    ("memory.current", "cgroup_memory_current_bytes", False),
    ("memory.max", "cgroup_memory_max_bytes", True),
    ("memory.swap.current", "cgroup_memory_swap_current_bytes", False),
    ("memory.swap.max", "cgroup_memory_swap_max_bytes", True),
)

_CGROUP_EVENT_FILES: Final[tuple[str, ...]] = ("memory.events", "memory.events.local")
_CGROUP_EVENT_KEYS: Final[frozenset[str]] = frozenset({"oom", "oom_kill", "max_usage"})

# The three region kinds Docling's layout output is bucketed into; the record
# names them in this order.
REGION_KINDS: Final[tuple[str, ...]] = ("text", "table", "figure")

# Where a failed reading already explained itself: every snapshot carries its
# own causes, so a totals-level gap points there rather than repeating them.
_READ_ERRORS_HINT: Final[str] = "see memory_checkpoints[].read_errors"

# The single counter this harness trusts for an OOM kill. Its name goes into
# both the provenance field and the gap reason, so the two cannot drift apart.
OOM_KILL_COUNTER_SOURCE: Final[str] = "cgroup memory.events oom_kill counter"

# Ceiling on an artifact the driver will hash inside a measured run. It is the
# rule the layout lane already used, kept so widening the hashing to the
# table-structure weights does not start hashing the whole cache; a weight above
# it is reported as a named gap rather than as an absent digest.
_MAX_HASHED_ARTIFACT_BYTES: Final[int] = 2 * 1024**3

# The product's figure rule and Region, bound from the mounted source tree.
# They are module-level so the figure lane calls the *product's* function
# rather than a threshold restated here. The bind is a no-op when the product
# is not importable, which is why ``_apply_figure_region_ocr`` refuses rather
# than falling back to a local floor: a locally re-implemented rule would stop
# being a measurement of fin.
crop_is_legible: Any = None
_Region: Any = None
_crop_png: Any = None
_PageRaster: Any = None


def _bind_product_region_rule() -> bool:
    """Bind the product's figure rule and cut from the source tree beside this file.

    Returns False when the product is not importable, which is the only case in
    which the figure lane cannot run; the caller names that rather than
    measuring against a local copy of the rule or the crop.
    """
    global crop_is_legible, _Region, _crop_png, _PageRaster
    src = Path(__file__).resolve().parents[2] / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    try:
        from fin.corpus.parsers._pages import (  # noqa: PLC0415
            PageRaster,
            Region,
            crop_is_legible as rule,
            crop_png,
        )
    except Exception:  # pragma: no cover - a host without the product on its path
        return False
    crop_is_legible = rule
    _Region = Region
    _crop_png = crop_png
    _PageRaster = PageRaster
    return True


_bind_product_region_rule()


def log(msg: str) -> None:
    """Diagnostics on stderr only - stdout carries exactly one JSON line."""
    print(msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# failure recording: a measurement that did not happen is never a zero
# --------------------------------------------------------------------------
def error_text(exc: BaseException, *, prefix: str = "") -> str:
    """The one spelling of a failed reading: ``<Type>: <message>``."""
    return f"{prefix}{type(exc).__name__}: {exc}"


def record_unmeasured(
    entry: dict[str, object], key: str, exc: BaseException, *, prefix: str = ""
) -> None:
    """Write ``{key: None, f"{key}_error": <Type>: <msg>}`` for one failed value."""
    entry[key] = None
    entry[f"{key}_error"] = error_text(exc, prefix=prefix)


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
            error_text(exc, prefix="/proc/self/status: ")
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
            error_text(exc, prefix=f"{name}: "),
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


def _cgroup_peak_with_fallback(errors: list[str]) -> int | None:
    """``memory.peak`` on cgroup v2, falling back to its v1 name.

    The v2 file is missing on a v1 host rather than empty, so the miss that
    licenses the fallback is kept as a read error: a host with neither file has
    to leave the peak null *and* say which two names it looked for.
    """
    peak, error = _read_cgroup_int("memory.peak", allow_unlimited=False)
    if peak is None:  # cgroup v1 shape
        if error is not None:
            errors.append(error)
        peak, error = _read_cgroup_int(
            "memory.max_usage_in_bytes", allow_unlimited=False
        )
    if error is not None:
        errors.append(error)
    return peak


def _cgroup_scalars(errors: list[str]) -> dict[str, int | None]:
    """The four usage-and-limit counters, each gap appended rather than zeroed."""
    scalars: dict[str, int | None] = {}
    for filename, key, is_limit in _CGROUP_SCALAR_FILES:
        value, error = _read_cgroup_int(filename, allow_unlimited=is_limit)
        scalars[key] = value
        if error is not None:
            errors.append(error)
    return scalars


def _cgroup_events(errors: list[str]) -> dict[str, int]:
    """The OOM counters, keyed by the file they were read from."""
    events: dict[str, int] = {}
    for ev in _CGROUP_EVENT_FILES:
        try:
            body = (CGROUP_DIR / ev).read_text(encoding="utf-8")
        except OSError as exc:
            errors.append(
                error_text(exc, prefix=f"{ev}: ")
            )  # EXIT: an absent events file is a gap in the OOM evidence
            continue
        for line in body.splitlines():
            k, _, v = line.partition(" ")
            if k in _CGROUP_EVENT_KEYS:
                try:
                    events[f"{ev}.{k}"] = int(v)
                except ValueError:
                    errors.append(
                        f"{ev}.{k}: unparseable {v.strip()!r}"
                    )  # EXIT: an unreadable OOM counter is not evidence of no OOM
    return events


def memory_snapshot(tag: str) -> MemorySnapshot:
    """One measured snapshot: process + cgroup v2 (v1 fallback included)."""
    proc, errors = _read_proc_status_fields(_PROC_STATUS_FIELDS)
    # Bound in this order, not inline: read_errors is one list every helper
    # appends to, and the order its messages appear in is part of a committed
    # record's leaves.
    peak = _cgroup_peak_with_fallback(errors)
    scalars = _cgroup_scalars(errors)
    events = _cgroup_events(errors)
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
            # EXIT: a distribution the image cannot introspect is no version, so
            # the pin record says null rather than a guessed or echoed one
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
        "cache_total_bytes": None,
        "file_count": None,
        "files": [],
        "layout_files": [],
    }
    if not info["exists"]:
        return info  # EXIT: an absent cache was never walked, so it has no size
    total = 0
    count = 0
    files: list[dict[str, object]] = []
    layout: list[dict[str, object]] = []
    for path in sorted(artifacts_path.rglob("*")):
        if not path.is_file():
            continue
        stat_size = path.stat().st_size
        total += stat_size
        count += 1
        rel = str(path.relative_to(artifacts_path))
        # The lane rule is unchanged from the committed records: hashed layout
        # files go to `layout_files[]`, everything else over 1 MB to `files[]`,
        # and the rest is walked and counted but never reported.
        is_layout = "layout" in rel.lower()
        hashable = stat_size <= _MAX_HASHED_ARTIFACT_BYTES
        if not (is_layout and hashable or stat_size > 1_000_000):
            continue
        # Every reported artifact is hashed, not just the layout ones: a swap
        # of tableformer_accurate.safetensors performed after the build's
        # verify-models gate and during a measurement is otherwise invisible to
        # the run's own record. The hashing read is the size that gets reported,
        # so a payload replaced between the two cannot be described by a size
        # that no longer belongs to the bytes that were hashed.
        entry: dict[str, object] = {"path": rel, "bytes": stat_size}
        if not hashable:
            # An artifact too large to hash is a digest that was not measured,
            # which is a gap with a reason and never an absent key.
            entry["sha256"] = None
            entry["sha256_error"] = (
                f"artifact is {stat_size} bytes, over the "
                f"{_MAX_HASHED_ARTIFACT_BYTES} this harness hashes"
            )
        else:
            try:
                size, digest = sha256_bytes(path)
            except OSError as exc:
                record_unmeasured(entry, "sha256", exc)
            else:
                entry["bytes"] = size
                entry["sha256"] = digest
        (layout if is_layout and hashable else files).append(entry)
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
def _page_image_objects(page: Any) -> tuple[int | None, str | None]:
    """Count a page's image objects without decoding any bitmap.

    The kind constant is read from the pinned library and handed to the
    library's own ``filter``, so the count is the library's answer rather than a
    comparison against a copied-out number. ``(None, reason)`` when it declines
    to answer: an unavailable count is not "no images" and must not select the
    OCR rule.
    """
    try:
        from pypdfium2 import raw

        objects = page.get_objects(filter=[int(raw.FPDF_PAGEOBJ_IMAGE)])
        return len(list(objects)), None
    except Exception as exc:
        return (
            None,
            error_text(exc),
        )  # EXIT: an unreadable object list is a gap, not a zero


def image_placements(page: Any) -> list[dict[str, float]]:
    """Where this page actually places each of its image XObjects, in points.

    The layout stage reports a *detection* box, which is a model's opinion about
    where artwork is. A PDF already states where its own artwork is: an image
    XObject's placement, in the same bottom-left origin the record uses. Reading
    it from the page is what lets a figure region cover the pixels it names
    instead of only the part of them a box happened to land on — the chart title
    sits in the band above the detection box, so a crop of the box cannot read
    it.

    The kind constant is the pinned library's own, and the placement is its
    ``get_bounds()``, so this is a reading rather than a constant: an image the
    library will not place, and a page whose object list cannot be read, both
    yield nothing here and leave the layout box as it was. Neither guesses.
    """
    placements: list[dict[str, float]] = []
    try:
        from pypdfium2 import raw

        objects = page.get_objects(filter=[int(raw.FPDF_PAGEOBJ_IMAGE)])
        for obj in objects:
            try:
                left, bottom, right, top = (float(v) for v in obj.get_bounds())
            except Exception:
                # EXIT: an object the library will not place is a gap in the
                # evidence, never a zero-sized box that would shrink a region.
                continue
            if right <= left or top <= bottom:
                continue  # EXIT: an inverted placement describes no area
            placements.append(
                {"l": round(left, 1), "b": round(bottom, 1), "r": round(right, 1), "t": round(top, 1)}
            )
    except Exception as exc:
        log(f"[driver] image placement scan failed: {error_text(exc)}")
        return []  # EXIT: an unreadable object list yields no placement, not a guess
    return placements


def artwork_extent(
    box: dict[str, Any], placements: list[dict[str, float]]
) -> dict[str, Any]:
    """The artwork's real extent when the layout box names that artwork.

    A detection box and the image placement it sits on name the same artwork, and
    the placement is the one that bounds the pixels. When they overlap, the
    region becomes the placement, so the figure rule crops the whole chart —
    title, axis and labels together — rather than the inner rectangle a detector
    chose.

    Overlap, not strict containment, is the test, because a detector's box and
    the artwork's placement routinely miss each other by a fraction of a point:
    on this gold set the p1 logotype's box is ``l 288.8`` while its placement is
    ``l 289.5``, which no strict containment would ever match. What keeps two
    nearby figures from being merged is the choice among overlapping placements:
    the *tightest* one wins, so a full-page raster cannot absorb a region on a
    scanned page, and a second figure that only brushes the box loses to the
    one the box is actually inside. A box overlapping no image at all (a vector
    chart, a drawn diagram) is left exactly as the model reported it.
    """
    if not placements:
        return dict(box)  # EXIT: nothing on the page to cover, so nothing changes
    try:
        left, bottom = float(box["l"]), float(box["b"])  # noqa: PLR1702
        right, top = float(box["r"]), float(box["t"])
    except (KeyError, TypeError, ValueError):
        return dict(box)  # EXIT: a box with no four edges is passed through, not grown
    best: tuple[float, dict[str, float]] | None = None
    for place in placements:
        pl, pb = float(place["l"]), float(place["b"])  # noqa: PLR1702
        pr, pt = float(place["r"]), float(place["t"])
        overlap_w = min(right, pr) - max(left, pl)
        overlap_h = min(top, pt) - max(bottom, pb)
        if overlap_w <= 0 or overlap_h <= 0:
            continue  # EXIT: no shared area means no shared artwork
        area = (pr - pl) * (pt - pb)
        if best is None or area < best[0]:
            # Tightest overlapping placement wins: a full-page raster would
            # otherwise grow every region on a scanned page to the whole page.
            best = (area, place)
    if best is None:
        return dict(box)  # EXIT: the box is over no image, so it stays the model's
    widened = dict(box)
    widened.update(
        {
            "l": float(best[1]["l"]),
            "b": float(best[1]["b"]),
            "r": float(best[1]["r"]),
            "t": float(best[1]["t"]),
        }
    )
    return widened


def _png_path(page_no: int, image_dir: Path | None) -> Path:
    """Kept page bitmap path, or a per-process temp path when nothing is kept."""
    if image_dir is not None:
        return image_dir / f"page_{page_no:04d}.png"
    return Path(f"/tmp/g4_page_{os.getpid()}_{page_no:04d}.png")


def _ocr_text_path(page_no: int, image_dir: Path | None) -> Path | None:
    """Sidecar for one page's full recognized text, or None when nothing is kept.

    Named beside ``_png_path``'s bitmap and kept in the same directory, so the
    pixels a quote was read from and the text read off them stay together in the
    run directory.
    """
    if image_dir is None:
        return None  # EXIT: nothing is kept, so there is no artefact to point at
    return image_dir / f"page_{page_no:04d}.ocr.txt"


def _save_ocr_text(
    entry: dict[str, object], ocr_text: str, page_no: int, image_dir: Path | None
) -> None:
    """Persist the whole recognized text so a committed quote is checkable.

    ``ocr_text_sample`` is capped so the single JSON line this harness gates on
    stays bounded, which leaves a page's numeric rows outside the record. The
    gold manifest commits an ``expected_quote`` per page, and on a scanned page
    that quote is the fixture-independent check that OCR did not drop digits, so
    a record that cannot reach the row cannot settle the expectation. The
    sidecar widens the evidence without widening the line, and it is written
    only where the page bitmap is written.
    """
    path = _ocr_text_path(page_no, image_dir)
    if path is None:
        return  # EXIT: no bitmap was kept, so no text artefact is written either
    path.write_text(ocr_text, encoding="utf-8")
    entry["ocr_text_file"] = path.name
    entry["ocr_text_sha256"] = sha256_bytes(path)[1]


def _save_page_png(
    entry: dict[str, object], pil: Any, png_path: Path, *, keep: bool
) -> None:
    """Persist a page bitmap and record the payload a vision call would send.

    ``keep`` is False only for the temp file an OCR call consumes: a bitmap that
    is removed again has no payload size to measure.
    """
    pil.save(png_path, format="PNG")
    if not keep:
        return  # EXIT: a discarded temp bitmap carries no byte measurement
    entry["page_image_png_bytes"] = png_path.stat().st_size
    entry["page_image_png_sha256"] = sha256_bytes(png_path)[1]


def _apply_page_ocr(
    entry: dict[str, object],
    pil: Any,
    page_no: int,
    ocr_engine: Any,
    ocr_trigger: str,
    image_dir: Path | None,
    failures: list[str],
) -> None:
    """One rendered page through PNG -> the repo's Tesseract engine, recorded."""
    png_path = _png_path(page_no, image_dir)
    entry["ocr_trigger"] = ocr_trigger
    _save_page_png(entry, pil, png_path, keep=image_dir is not None)
    try:
        ocr_text = ocr_engine.recognize(source=png_path, page=page_no)
    except Exception as exc:  # fin.corpus.ocr's typed OcrRequiredUnavailableError
        entry["ocr_error_code"] = getattr(exc, "code", type(exc).__name__)
        entry["ocr_error"] = (
            error_text(exc)  # EXIT: a code alone would hide why the page has no text
        )
        entry["ocr_used"] = False
        # No character count exists for a pass that produced nothing, and a zero
        # here would sum into ocr_text_chars_total as a cheap page.
        entry["ocr_text_char_count"] = None
    else:
        entry["ocr_used"] = True
        entry["ocr_text_char_count"] = len(ocr_text)
        # Short sample so the OCR output is auditable without shipping
        # the whole page text in the measurement line.
        entry["ocr_text_sample"] = " ".join(ocr_text.split())[:160]
        _save_ocr_text(entry, ocr_text, page_no, image_dir)
    finally:
        entry["ocr_lang"] = ocr_engine.lang
        if image_dir is None:
            try:
                png_path.unlink(missing_ok=True)
            except OSError as exc:
                failures.append(
                    error_text(
                        exc,
                        prefix=f"page {page_no}: temp PNG {png_path.name} not removed: ",
                    )
                )  # EXIT: a leaked bitmap is not a gap in a number, but not invisible either


def _figure_region_record(entry: dict[str, object], **values: object) -> None:
    """Declare this page's figure-OCR keys before any region is read.

    The record's rule is that a reading which did not happen is never a zero, so
    a page that has not been through the figure rule spells its keys as null
    rather than leaving a reader to infer the difference from a missing key.
    """
    entry.update(
        {
            "figure_ocr_regions_read": None,
            "figure_ocr_text_char_count": None,
            "figure_ocr_text_sample": None,
            "figure_ocr_text_file": None,
            "figure_ocr_text_sha256": None,
            "figure_ocr_error": None,
            "figure_ocr_skipped_reason": None,
        }
    )
    entry.update(values)


def _apply_figure_region_ocr(
    entry: dict[str, object],
    figure_boxes: list[dict[str, object]] | None,
    page_png: bytes,
    *,
    page_no: int,
    dpi: int,
    image_dir: Path | None,
    ocr_engine: Any,
    failures: list[str],
) -> None:
    """Read the pixels of every figure region the product's rule calls legible.

    The page rule (``detect_ocr_required``) asks whether a page's text layer
    collapsed while it still carries images, so on a page with a healthy text
    layer it can never answer "yes" and the chart's own labels -- which exist
    only as pixels -- are never read. The product's *figure* rule
    (``fin.corpus.parsers._pages.crop_is_legible``) is the other half of its own
    two-rule contract and is what reaches that content
    (``src/fin/corpus/parsers/_pages.py``: "which is how text inside a chart on
    a page that also has a text layer is reached at all"). Both the rule and the
    cut (``crop_png``) are the product's own functions rather than a threshold
    and a crop re-implemented here, so a change to ``MIN_OCR_REGION_EDGE_PX`` or
    to the crop's bounds moves this measurement with the code it measures.

    A page the page rule already read whole is not re-read per region: the
    product skips crops in that case (``_pages.py`` ``_one_pdf_page``), and
    recognizing the same bitmap twice would misprice the OCR cost.
    """
    if entry.get("ocr_used"):
        # EXIT: the page rule has had every pixel on this page; its crops would
        # recognize the same bitmap again for nothing added. The rule ran and
        # selected nothing to read, which is a real answer, not a gap.
        _figure_region_record(
            entry,
            figure_ocr_regions_read=0,
            figure_ocr_text_char_count=0,
            figure_ocr_skipped_reason="page_ocr_already_read_whole_page",
        )
        return

    if crop_is_legible is None or _Region is None or _crop_png is None:
        # EXIT: without the product's own rule this would be a locally chosen
        # floor, which is a guess about the product rather than a measurement.
        reason = "product_figure_rule_unavailable"
        _figure_region_record(entry, figure_ocr_skipped_reason=reason)
        failures.append(f"figure region OCR skipped: {reason}")
        return

    boxes = list(figure_boxes or [])
    # A figure box the record could not describe is a gap in the evidence, not
    # a region that happened to be too small: it is named before the rule runs,
    # so a malformed box cannot be read as a page with no legible figure.
    malformed = [box for box in boxes if not isinstance(box, dict) or not _box_readable(box)]
    legible = [box for box in boxes if _legible_figure_box(box)]
    if malformed:
        reason = (
            f"page {page_no}: {len(malformed)} figure box(es) the record did not "
            "describe in four finite edges"
        )
        _figure_region_record(
            entry,
            figure_ocr_regions_read=None,
            figure_ocr_text_char_count=None,
            figure_ocr_error=reason,
        )
        failures.append(reason)
        return
    if not legible:
        # EXIT: the rule ran and selected nothing, which is a measurement.
        _figure_region_record(
            entry, figure_ocr_regions_read=0, figure_ocr_text_char_count=0
        )
        return

    raster = _PageRaster.of(page_png, width_px=_png_width(page_png), height_px=_png_height(page_png))
    texts: list[str] = []
    for box in legible:
        region = _region_from_box(box)
        try:
            crop = _crop_png(region, raster=raster, dpi=dpi)
        except Exception as exc:  # fin.corpus.errors.PageRenderError
            # EXIT: a box the raster cannot show names itself; it is not a box
            # that happened to hold no text.
            reason = error_text(
                exc, prefix=f"page {page_no}: figure region crop failed: "
            )
            _figure_region_record(
                entry,
                figure_ocr_regions_read=None,
                figure_ocr_text_char_count=None,
                figure_ocr_error=reason,
            )
            failures.append(reason)
            return
        crop_path = _png_path(page_no, None).with_name(
            f"g4_figure_{os.getpid()}_{page_no:04d}.png"
        )
        try:
            crop_path.write_bytes(crop)
            text = ocr_engine.recognize(source=crop_path, page=page_no)
        except Exception as exc:  # fin.corpus.ocr's typed OcrRequiredUnavailableError
            # EXIT: a crop that could not be read says nothing about the page;
            # zero characters with no error would read as "read, held no text".
            reason = error_text(
                exc, prefix=f"page {page_no}: figure region OCR failed: "
            )
            _figure_region_record(
                entry,
                figure_ocr_regions_read=None,
                figure_ocr_text_char_count=None,
                figure_ocr_error=reason,
            )
            failures.append(reason)
            return
        finally:
            try:
                crop_path.unlink(missing_ok=True)
            except OSError as exc:
                failures.append(
                    error_text(
                        exc,
                        prefix=f"page {page_no}: figure crop {crop_path.name} not removed: ",
                    )
                )  # EXIT: a leaked crop is not a gap in a number, but not invisible
        if text and text.strip():
            texts.append(text)

    joined = " ".join(texts)
    _figure_region_record(
        entry,
        figure_ocr_regions_read=len(legible),
        figure_ocr_text_char_count=len(joined),
        # Bounded like the page-level sample, so the single JSON line stays
        # bounded however much a chart's labels read back.
        figure_ocr_text_sample=" ".join(joined.split())[:160] or None,
    )
    _save_figure_ocr_text(entry, joined, page_no, image_dir)


def _box_readable(box: object) -> bool:
    """Whether a recorded box carries four finite numeric edges.

    Only readability is checked here, not order: the record keeps Docling's
    bottom-left origin, so a well-formed box has ``t > b`` and the order is
    judged after the flip onto the product's own Region.
    """
    if not isinstance(box, dict):
        return False
    try:
        values = [float(box[field]) for field in ("l", "t", "r", "b")]  # noqa: PLR1702
    except (KeyError, TypeError, ValueError):
        return False
    return all(value == value and value not in (float("inf"), float("-inf")) for value in values)


def _region_from_box(box: dict[str, object]) -> Any:
    """One recorded figure box as the product's own Region value.

    The record keeps Docling's bottom-left origin, so ``t`` is the higher edge
    and the product's top-left space is reached by flipping it, the same flip
    ``src/fin/host/attachment_layout.py`` performs for the same record.
    """
    height = float(box.get("page_height_pt") or 792.0)
    left, right = float(box["l"]), float(box["r"])  # noqa: PLR1702
    top, bottom = height - float(box["t"]), height - float(box["b"])
    return _Region(
        kind="figure", left_pt=left, top_pt=top, right_pt=right, bottom_pt=bottom
    )


def _png_size(png: bytes) -> tuple[int, int]:
    """A PNG's own pixel size, read from its IHDR rather than guessed."""
    if len(png) < 24 or png[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("figure lane needs the page's own PNG bytes")
    width = int.from_bytes(png[16:20], "big")
    height = int.from_bytes(png[20:24], "big")
    return width, height


def _png_width(png: bytes) -> int:
    return _png_size(png)[0]


def _png_height(png: bytes) -> int:
    return _png_size(png)[1]


def _save_figure_ocr_text(
    entry: dict[str, object], ocr_text: str, page_no: int, image_dir: Path | None
) -> None:
    """Persist a page's whole figure-region read beside its page bitmap.

    The same reasoning as the page-level sidecar applies one rule down: the
    manifest's ``expected_ocr_quote`` for a chart page is the title and the
    period labels, and a title sits at the TOP of a read that Tesseract returns
    axis-first, so it can land outside a 160-character window. A committed
    expectation that the artefact cannot reach is an expectation nothing can
    settle, so the full text is written next to the pixels it was read from and
    named with its digest. The bounded sample in the JSON line is unchanged.
    """
    if image_dir is None:
        return  # EXIT: no bitmap was kept, so no text artefact is written either
    path = image_dir / f"page_{page_no:04d}.figure-ocr.txt"
    path.write_text(ocr_text, encoding="utf-8")
    entry["figure_ocr_text_file"] = path.name
    entry["figure_ocr_text_sha256"] = sha256_bytes(path)[1]


def _legible_figure_box(box: object) -> bool:
    """Whether one recorded figure box passes the product's own legibility rule.

    A box whose four edges the record did not answer in numbers is not legible
    and not merely small: there is no place to cut, so the caller names it.
    """
    if not isinstance(box, dict):
        return False
    try:
        float(box["l"]), float(box["t"]), float(box["r"]), float(box["b"])  # noqa: PLR1702
    except (KeyError, TypeError, ValueError):
        return False
    if crop_is_legible is None or _Region is None:
        return False
    # The record keeps Docling's bottom-left origin, so ``t`` is the HIGHER edge
    # and the box is well formed when ``t > b``; the order is checked after the
    # flip, on the product's own Region, rather than against the record's.
    region = _region_from_box(box)
    if region.right_pt <= region.left_pt or region.bottom_pt <= region.top_pt:
        return False
    return bool(crop_is_legible(region))


@dataclass(frozen=True, slots=True)
class RenderPass:
    """One entry per physical page, plus the render window that produced them."""

    pages: list[dict[str, object]]
    latency_ms: float


def _page_structure_entry(
    page: Any, page_no: int, detect_ocr_required: Any
) -> dict[str, object]:
    """What the page itself says, before any bitmap is produced."""
    width_pt, height_pt = page.get_size()
    textpage = page.get_textpage()
    native_text = textpage.get_text_range() or ""
    n_images, images_error = _page_image_objects(page)
    if images_error is not None:
        log(f"[driver] page {page_no} object scan failed: {images_error}")
    # Where the page places its own artwork, so a layout box can be widened to
    # the pixels it names. Read here, beside the image count the OCR rule needs,
    # because both come from the same object scan.
    placements = image_placements(page)
    # The rule cannot be asked without its image input: running it on a
    # fabricated 0 would let a failed scan clear or select OCR, and
    # pages_needing_ocr is one of the quantities the page cap is priced from.
    rule = None
    if n_images is not None:
        rule = bool(
            detect_ocr_required(
                text=native_text, image_count=n_images, min_text_chars=1
            )
        )
    return {
        "page_no": page_no,
        "size_pt": [round(float(width_pt), 2), round(float(height_pt), 2)],
        "native_text_char_count": len(native_text),
        "pdf_image_objects": n_images,
        "pdf_image_objects_error": images_error,
        # Read from the page, not from the layout pass: the placement is a fact
        # of the document, and the figure pass runs before the regions exist.
        "image_placements": placements,
        "figure_boxes_detected": None,
        "ocr_required_by_repo_rule": rule,
        "ocr_used": False,
        "ocr_trigger": None,
        "ocr_lang": None,
        "ocr_text_char_count": 0,
        "ocr_text_sample": None,
        "ocr_text_file": None,
        "ocr_text_sha256": None,
        "ocr_error_code": None,
        "ocr_error": None,
        # The figure rule runs after the layout pass, because a region has to
        # exist before it can be asked whether its pixels are worth a read.
        "figure_ocr_regions_read": None,
        "figure_ocr_text_char_count": None,
        "figure_ocr_text_sample": None,
        "figure_ocr_text_file": None,
        "figure_ocr_text_sha256": None,
        "figure_ocr_error": None,
        "figure_ocr_skipped_reason": None,
    }


def _apply_render_geometry(
    page: Any, entry: dict[str, object], dpi: int
) -> tuple[Any, Any]:
    """Full-page bitmap at the requested scale; the caller drops it after OCR."""
    bitmap = page.render(scale=dpi / 72.0)
    pil = bitmap.to_pil()
    entry["rendered_width_px"] = int(pil.width)
    entry["rendered_height_px"] = int(pil.height)
    try:
        dpi_x, dpi_y = bitmap.get_dpi()
        entry["rendered_dpi"] = [round(float(dpi_x), 1), round(float(dpi_y), 1)]
    except Exception as exc:
        record_unmeasured(
            entry, "rendered_dpi", exc
        )  # EXIT: the requested DPI is not a measured one
    return bitmap, pil


def _ocr_trigger(
    entry: dict[str, object], force_ocr: bool, failures: list[str]
) -> str | None:
    """Which rule asked for CPU Tesseract on this page, if any.

    ``forced_selftest`` exercises the OCR boundary on a page the repo rule would
    not select, so "the OCR path is live" is observed rather than assumed. It is
    never used to decide coverage in a measured run. A page whose scan failed
    has no answer to trigger on, and forcing it would charge an OCR cost the
    rule never asked for, so the gap is named and the page is left alone.
    """
    rule = entry["ocr_required_by_repo_rule"]
    if rule is None:
        failures.append(
            f"page {entry['page_no']}: no OCR decision, the image-object scan "
            f"failed ({entry['pdf_image_objects_error']})"
        )
        return None  # EXIT: an unanswered rule is a gap in the record, not a no
    if rule:
        return "repo_rule"  # EXIT: the frozen rule selected OCR for this page
    if force_ocr:
        return "forced_selftest"
    return None  # EXIT: no rule asked, so no OCR cost is charged to this page


def prepare_image_dir(image_dir: Path | None, failures: list[str]) -> Path | None:
    """Create the bitmap directory, or switch the PNG measurement off by name."""
    if image_dir is None:
        return None  # EXIT: no --write-images, so no page carries PNG bytes at all
    try:
        image_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log(f"[driver] cannot write images to {image_dir}: {exc}")
        failures.append(
            error_text(exc, prefix=f"--write-images off: cannot create {image_dir}: ")
        )  # EXIT: page bitmaps then carry no byte measurement at all
        return None
    return image_dir


def render_pages(
    *, started_at: float, pdf_doc: Any, page_count: int, dpi: int, force_ocr: bool,
    image_dir: Path | None, ocr_engine: Any, detect_ocr_required: Any, failures: list[str],
) -> RenderPass:
    """Render every physical page and run the repo-gated OCR step on those it selects.

    ``started_at`` is when the document was opened, not when this was called: the
    render window the record reports includes the file's own parse.
    """
    image_dir = prepare_image_dir(image_dir, failures)

    pages: list[dict[str, object]] = []
    for idx in range(page_count):
        page_no = idx + 1
        pt0 = time.monotonic()
        page = pdf_doc[idx]
        entry = _page_structure_entry(page, page_no, detect_ocr_required)
        bitmap, pil = _apply_render_geometry(page, entry, dpi)
        trigger = _ocr_trigger(entry, force_ocr, failures)
        if trigger is not None:
            _apply_page_ocr(entry, pil, page_no, ocr_engine, trigger, image_dir, failures)
        elif image_dir is not None:
            _save_page_png(entry, pil, _png_path(page_no, image_dir), keep=True)
        del pil, bitmap
        entry["render_latency_ms"] = round((time.monotonic() - pt0) * 1000.0, 1)
        pages.append(entry)

    return RenderPass(pages=pages, latency_ms=(time.monotonic() - started_at) * 1000.0)


def run_figure_region_pass(
    *,
    pages: list[dict[str, object]],
    pdf_doc: Any,
    dpi: int,
    image_dir: Path | None,
    ocr_engine: Any,
    failures: list[str],
) -> None:
    """Apply the product's figure rule to every page, once regions are known.

    This runs after the layout pass because the question it asks is about a
    region: a page's chart labels are pixels inside a box Docling has to have
    found first. The page is rendered again for the pages that have a legible
    figure box, because the crop is cut from the same full-page raster the page
    rule would have used, so a region read and a page read are the same pixels.
    """
    for idx, entry in enumerate(pages):
        page_no = int(entry["page_no"])
        boxes = entry.get("figure_boxes")
        if boxes is None:
            # EXIT: the region source failed; there is no box to ask about, and
            # the null stays rather than becoming a zero.
            _figure_region_record(entry)
            continue
        legible = any(_legible_figure_box(box) for box in boxes)
        if not legible and not entry.get("ocr_used"):
            # EXIT: nothing above the floor, and the page rule did not read the
            # page either. The rule ran and selected nothing, which is a
            # measurement, so no render is charged for it.
            _apply_figure_region_ocr(
                entry, list(boxes), b"", page_no=page_no, dpi=dpi,
                image_dir=image_dir, ocr_engine=ocr_engine, failures=failures,
            )
            continue
        if not legible:
            _apply_figure_region_ocr(
                entry, list(boxes), b"", page_no=page_no, dpi=dpi,
                image_dir=image_dir, ocr_engine=ocr_engine, failures=failures,
            )
            continue
        try:
            bitmap = pdf_doc[idx].render(scale=dpi / 72.0)
            png = bitmap.to_pil()
            buffer = io.BytesIO()
            png.save(buffer, format="PNG")
            page_png = buffer.getvalue()
            del png, bitmap
        except Exception as exc:
            reason = error_text(
                exc, prefix=f"page {page_no}: figure-region render failed: "
            )
            _figure_region_record(
                entry, figure_ocr_text_char_count=None, figure_ocr_error=reason
            )
            failures.append(reason)
            continue
        _apply_figure_region_ocr(
            entry, list(boxes), page_png, page_no=page_no, dpi=dpi,
            image_dir=image_dir, ocr_engine=ocr_engine, failures=failures,
        )


# --------------------------------------------------------------------------
# Docling + Heron
# --------------------------------------------------------------------------
def build_converter(
    artifacts_path: Path, layout_preset: str, threads: int
) -> tuple[Any, dict[str, object]]:
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
        layout_note = f"from_preset failed ({error_text(exc)}); default"
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


def _record_region_box(bucket: dict[str, Any], kind: str, box: Any) -> None:
    """One region's coordinates, with the record's own bound kept in view.

    Forty boxes per page and kind bounds the single JSON line the harness may
    print; reaching the bound is recorded as ``*_boxes_capped`` rather than
    dropping coordinates silently.
    """
    key = f"{kind}_boxes"
    if len(bucket[key]) >= 40:
        bucket[f"{key}_capped"] = True
        return  # EXIT: the line stays bounded, and the cap is visible in the page
    bucket[key].append(
        {
            "l": round(float(box.l), 1),
            "t": round(float(box.t), 1),
            "r": round(float(box.r), 1),
            "b": round(float(box.b), 1),
        }
    )


def region_counts_from_document(doc) -> dict[int, dict[str, object]]:
    """Bucket Docling's layout output per physical page: text/table/figure.

    ``texts``, ``tables`` and ``pictures`` are read as attributes and not
    defaulted to empty: a Docling release that renamed one would otherwise
    report zero regions for a document it parsed successfully, and
    ``regions_total`` is entitled to certify ``0 / 0 / 0`` only over a pass that
    actually read them. The ``AttributeError`` reaches ``run_docling_pass``,
    which fails the pass and sends the whole region lane to null.
    """
    per_page: dict[int, dict[str, object]] = {}

    def bucket(page_no: int) -> dict[str, object]:
        return per_page.setdefault(
            int(page_no),
            {"text": 0, "table": 0, "figure": 0, "table_boxes": [], "figure_boxes": []},
        )

    def charge(items: Any, kind: str, *, boxed: bool) -> None:
        for item in items:
            for prov in item.prov or []:
                page_no = getattr(prov, "page_no", None)
                if page_no is None:
                    continue  # EXIT: provenance with no page number has no page to charge
                entry = bucket(page_no)
                entry[kind] = int(entry[kind]) + 1
                if boxed and getattr(prov, "bbox", None) is not None:
                    _record_region_box(entry, kind, prov.bbox)

    charge(doc.texts, "text", boxed=False)
    charge(doc.tables, "table", boxed=True)
    charge(doc.pictures, "figure", boxed=True)
    return per_page


@dataclass(frozen=True, slots=True)
class DoclingPass:
    """The whole-document pass, the record's only source of regions.

    ``error`` set means nothing here was measured: ``status`` / ``page_count``
    are null and the region fields go null, never zero.
    """

    latency_ms: float
    status: str | None
    page_count: int | None
    error: str | None
    regions: dict[int, dict[str, object]]
    page_sizes_pt: dict[int, list[float]]

    @property
    def regions_measured(self) -> bool:
        """Docling's export is the region authority, so its failure is load-bearing."""
        return self.error is None

    def to_totals(self) -> dict[str, object]:
        """The totals fields this pass owns, named as the committed lines name them."""
        return {
            "docling_whole_doc_latency_ms": round(self.latency_ms, 1),
            "docling_status": self.status,
            "docling_page_count": self.page_count,
            "docling_error": self.error,
        }


def docling_page_sizes(doc: Any) -> dict[int, list[float]]:
    """Docling's own per-page size in points, keyed by physical page number.

    ``DoclingDocument.pages`` is ``dict[int, PageItem]`` and the pinned
    ``PageItem``'s fields are ``page_no`` / ``size`` / ``image``, both read off
    the image rather than from memory. So the pages are the mapping's *values*:
    iterating the mapping itself yields ``int`` page numbers, which have no
    ``size``, and a successful conversion would then answer nothing for every
    page. A page whose ``size`` is missing is left unanswered rather than
    guessed, and ``attach_regions`` names that gap in the record.
    """
    sizes: dict[int, list[float]] = {}
    for page in doc.pages.values():
        if page.size is None:
            continue  # EXIT: nothing to read, so nothing to answer
        sizes[int(page.page_no)] = [
            round(float(page.size.width), 2),
            round(float(page.size.height), 2),
        ]
    return sizes


def run_docling_pass(
    conv: Any, pdf_path: Path, checkpoints: list[MemorySnapshot]
) -> DoclingPass:
    """Convert the whole document once with the pinned pipeline and take its regions."""
    t_full = time.monotonic()
    status: str | None = None
    doc_page_count: int | None = None
    error: str | None = None
    regions: dict[int, dict[str, object]] = {}
    sizes: dict[int, list[float]] = {}
    try:
        res = conv.convert(str(pdf_path))
        doc = res.document
        status = str(res.status)
        doc_page_count = len(doc.pages)
        regions = region_counts_from_document(doc)
        sizes = docling_page_sizes(doc)
    except Exception as exc:
        error = error_text(exc)
        log(f"[driver] Docling whole-document pass failed: {error}")
        # EXIT: this pass is the only region source, so everything it owns goes
        # null rather than leaving a half-read lane beside the failure that says
        # it was not read.
        status = None
        doc_page_count = None
        regions = {}
        sizes = {}
    latency_ms = (time.monotonic() - t_full) * 1000.0
    checkpoints.append(memory_snapshot("after_docling_whole_doc"))
    return DoclingPass(
        latency_ms=latency_ms,
        status=status,
        page_count=doc_page_count,
        error=error,
        regions=regions,
        page_sizes_pt=sizes,
    )


def attach_regions(
    pages: list[dict[str, object]], docling: DoclingPass, failures: list[str]
) -> None:
    """Give every physical page its Docling regions, or name why there are none."""
    measured = docling.regions_measured
    for entry in pages:
        page_no = int(entry["page_no"])
        reg = docling.regions.get(page_no, {})
        entry["region_counts"] = {
            kind: (int(reg.get(kind, 0)) if measured else None) for kind in REGION_KINDS
        }  # EXIT: on a measured page an absent bucket really is zero
        entry["table_boxes"] = reg.get("table_boxes", []) if measured else None
        entry["figure_boxes"] = reg.get("figure_boxes", []) if measured else None
        entry["docling_page_size_pt"] = docling.page_sizes_pt.get(page_no)
        # The figure rule works in the product's top-left space, so a figure
        # box carries the page it belongs to: the record keeps Docling's
        # bottom-left origin and the flip is what makes a recorded box a place
        # the product can crop. Only figure boxes need it — a table box is
        # never cropped by the figure rule.
        height_pt = (
            entry.get("docling_page_size_pt") or entry.get("size_pt") or [None, 792.0]
        )[1]
        for box in entry["figure_boxes"] or []:
            if not isinstance(box, dict):
                continue
            box["page_height_pt"] = height_pt
            # The layout stage reports where it DETECTED the artwork; the page
            # reports where the artwork IS. A detection box inset inside the
            # image's own placement leaves the chart's title outside the crop,
            # so the region the figure rule acts on is the placement the page
            # states, widened from the detection rather than written down here.
            # The model's own box is kept beside it so the record can show what
            # the layout stage said and what the page said.
            detections = entry.get("figure_boxes_detected")
            if detections is None:
                # EXIT: the key is spelled as null on a page that has not been
                # through the layout pass; the first box found starts the list.
                detections = []
                entry["figure_boxes_detected"] = detections
            detections.append(
                {k: box[k] for k in ("l", "t", "r", "b") if k in box}
            )
            box.update(
                {
                    k: v
                    for k, v in artwork_extent(
                        {k: box[k] for k in ("l", "t", "r", "b") if k in box},
                        list(entry.get("image_placements") or []),
                    ).items()
                }
            )
    # A whole-document pass that counted pages but read no size is the one case
    # where per-page nulls carry no explanation: ``docling_error`` is null
    # because nothing raised. Naming it keeps ``null`` meaning "not measured"
    # rather than letting it mean "measured to be nothing".
    unanswered = sum(1 for e in pages if e["docling_page_size_pt"] is None)
    if measured and unanswered:
        failures.append(
            f"docling_page_size_pt: no size read for {unanswered} of {len(pages)} pages"
        )


# --------------------------------------------------------------------------
# per-page latency pass (single-page conversions on the warm pipeline)
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class SlicePass:
    """Warm steady-state per-page cost, plus the reason the pass stopped early."""

    enabled: bool
    error: str | None
    latencies_ms: list[float]

    def to_totals(self) -> dict[str, object]:
        return {"slice_pass_enabled": self.enabled, "slice_pass_error": self.error}


def _discard_slice_doc(one: Any, page_no: int, failures: list[str]) -> None:
    """Close a slice document now; gc would hold one handle per page under the ceiling."""
    try:
        one.close()
    except Exception as exc:
        failures.append(
            error_text(exc, prefix=f"slice page {page_no}: document close failed: ")
        )  # EXIT: a handle left open would inflate the next peak


def _discard_slice_pdf(tmp_pdf: Path, page_no: int, failures: list[str]) -> None:
    """Remove one slice file; a slice left on disk is a cost, not a measurement."""
    try:
        tmp_pdf.unlink(missing_ok=True)
    except OSError as exc:
        failures.append(
            error_text(exc, prefix=f"slice page {page_no}: temp PDF not removed: ")
        )  # EXIT: a slice left on disk is a cost, not a measurement


def _slice_one_page(
    pdfium_mod: Any, pdf_doc: Any, conv: Any, idx: int, page_no: int, failures: list[str],
) -> float:
    """Convert one physical page alone and return the measured milliseconds."""
    tmp_pdf = Path(f"/tmp/g4_slice_{os.getpid()}_{page_no:04d}.pdf")
    one = None
    t_pg = time.monotonic()
    try:
        one = pdfium_mod.PdfDocument.new()
        one.import_pages(pdf_doc, pages=[idx])
        one.save(str(tmp_pdf))
        conv.convert(str(tmp_pdf))
        return (time.monotonic() - t_pg) * 1000.0
    finally:
        if one is not None:
            _discard_slice_doc(one, page_no, failures)
            # Dropped here rather than left to the next rebinding: a document
            # that outlives its own iteration holds pages inside the 4 GiB ceiling.
            del one
        _discard_slice_pdf(tmp_pdf, page_no, failures)


def _stop_slice_pass(
    pages: list[dict[str, object]], idx: int, page_no: int, exc: BaseException,
) -> str:
    """Null the page that failed and name every later page as unmeasured."""
    pages[idx]["page_latency_ms"] = None
    pages[idx]["page_latency_mode"] = "single_page_slice_failed"
    for rest in pages[idx + 1 :]:
        rest["page_latency_ms"] = None
        rest["page_latency_mode"] = "not_measured_after_slice_failure"
    # EXIT: pages after the stop are named as unmeasured instead of leaving their
    # keys out, so a short n in the stats is not a silent shrink of the document
    return error_text(exc, prefix=f"page {page_no}: ")


def run_slice_pass(
    *, pdfium_mod: Any, pdf_doc: Any, conv: Any, pages: list[dict[str, object]],
    page_count: int, checkpoints: list[MemorySnapshot], failures: list[str],
    enabled: bool,
) -> SlicePass:
    """Time every page through a single-page conversion, until one of them fails."""
    if not enabled:
        # No per-page number available without measurement: stay explicit.
        for entry in pages:
            entry["page_latency_ms"] = None
            entry["page_latency_mode"] = "not_measured"
        return SlicePass(enabled=False, error=None, latencies_ms=[])

    latencies: list[float] = []
    error: str | None = None
    for idx in range(page_count):
        page_no = idx + 1
        try:
            ms = _slice_one_page(pdfium_mod, pdf_doc, conv, idx, page_no, failures)
        except Exception as exc:
            error = _stop_slice_pass(pages, idx, page_no, exc)
            log(f"[driver] slice pass page {page_no} failed: {error}")
            break
        pages[idx]["page_latency_ms"] = round(ms, 1)
        pages[idx]["page_latency_mode"] = "single_page_slice"
        latencies.append(ms)
        if idx % 10 == 9:
            checkpoints.append(memory_snapshot(f"slice_pass_page_{page_no}"))
    return SlicePass(enabled=True, error=error, latencies_ms=latencies)


# --------------------------------------------------------------------------
# the record: one JSON line, keyed as the committed measurements are
# --------------------------------------------------------------------------
def page_limit_exceeded(max_pages: int | None, page_count: int) -> bool:
    """``--max-pages`` is a harness valve: over-limit is reported, never cut.

    Mirrors fin's ``attachment_page_limit_exceeded`` shape rather than truncating.
    """
    return max_pages is not None and page_count > max_pages


def _p95(values: list[float]) -> float | None:
    """Quantiles need 20 samples; below that the observed maximum is reported."""
    if not values:
        return None  # EXIT: no measured page means no percentile, not a zero
    if len(values) >= 20:
        return round(statistics.quantiles(values, n=20)[-1], 1)
    return round(max(values), 1)


def page_latency_stats(pages: list[dict[str, object]]) -> dict[str, object]:
    """Distribution over the pages that measured a per-page latency at all."""
    values = [
        x for x in (p.get("page_latency_ms") for p in pages) if isinstance(x, (int, float))
    ]
    return {
        "n": len(values),
        "note": "single-page conversions on an already-initialised "
        "pipeline (DocumentConverter caches the initialised "
        "StandardPdfPipeline), i.e. warm steady-state per-page cost",
        "mean": round(statistics.fmean(values), 1) if values else None,
        "median": round(statistics.median(values), 1) if values else None,
        "p95": _p95(values),
        "max": round(max(values), 1) if values else None,
    }


def regions_total(pages: list[dict[str, object]], measured: bool) -> dict[str, object]:
    """Region totals per kind, or all-null when the only region source failed."""
    if not measured:
        # EXIT: no sum over an unmeasured pass, so a null is never a zero
        return {kind: None for kind in REGION_KINDS}
    return {
        kind: sum(int(p["region_counts"][kind]) for p in pages) for kind in REGION_KINDS
    }


def _sum_or_gap(values: list[int | None]) -> int | None:
    """Total over pages that each answered, or null when one of them did not.

    Skipping an unread page would price a filing whose OCR failed as a filing
    whose OCR returned nothing, and those are different facts about the same
    page count.
    """
    total = 0
    for value in values:
        if value is None:
            return None  # EXIT: one unanswered page makes the file total unread
        total += value
    return total


def _answered(flag: object) -> int | None:
    """A tri-state rule answer as a count: 1, 0, or null for "not asked"."""
    if flag is None:
        return None
    return 1 if flag else 0


def page_rule_counts(pages: list[dict[str, object]]) -> dict[str, object]:
    """How many pages the rule selected, and how much text the passes produced."""
    return {
        "pages_needing_ocr": _sum_or_gap(
            [_answered(p["ocr_required_by_repo_rule"]) for p in pages]
        ),
        "pages_with_ocr_applied": sum(1 for p in pages if p["ocr_used"]),
        "pages_with_native_text": sum(1 for p in pages if p["native_text_char_count"] > 0),
        "native_text_chars_total": _sum_or_gap(
            [p["native_text_char_count"] for p in pages]
        ),
        "ocr_text_chars_total": _sum_or_gap(
            [p["ocr_text_char_count"] for p in pages]
        ),
        # The figure rule's own totals. They are the second half of the
        # product's two-rule contract: the page rule above cannot answer "yes"
        # on a page with a text layer, and a chart's labels are pixels.
        "figure_ocr_regions_read": _sum_or_gap(
            [p.get("figure_ocr_regions_read") for p in pages]
        ),
        "figure_ocr_text_chars_total": _sum_or_gap(
            [p.get("figure_ocr_text_char_count") for p in pages]
        ),
    }


def _gap_reason(source: str) -> str:
    """Why a whole measurement is missing: no snapshot ever read ``source``.

    The per-snapshot cause is already carried by every snapshot that failed, so
    the reason points there instead of repeating it once per peak.
    """
    return f"no snapshot read {source}; {_READ_ERRORS_HINT}"


def _peak_or_gap(key: str, readings: list[int], source: str) -> dict[str, object]:
    """One peak as ``{key: max(readings)}``, or as ``None`` plus its reason.

    The ``<key>_error`` sibling exists only together with the ``None``, so a run
    that did read its snapshots keeps the key tree the committed measurements
    carry.
    """
    if readings:
        return {key: max(readings)}
    # EXIT: nothing read is a gap in the evidence, never a reading of zero
    return {key: None, f"{key}_error": _gap_reason(source)}


def peak_fields(samples: list[MemorySnapshot]) -> dict[str, object]:
    """Peaks over every snapshot taken during the run.

    A failed read is None and drops out of the max; a limit file's "no limit"
    reading is CGROUP_NO_LIMIT_BYTES and must never enter this max at all.
    """
    vmhwm = [c.proc_vmhwm_bytes for c in samples if c.proc_vmhwm_bytes]
    cgroup = [
        c.cgroup_memory_peak_bytes for c in samples if (c.cgroup_memory_peak_bytes or 0) > 0
    ]
    return {
        **_peak_or_gap("peak_rss_vmhwm_bytes", vmhwm, "/proc/self/status VmHWM"),
        **_peak_or_gap("cgroup_peak_bytes", cgroup, "cgroup memory.peak"),
    }


def oom_fields(samples: list[MemorySnapshot]) -> dict[str, object]:
    """The OOM signal, taken from the cgroup counter alone.

    A counter that answered 0 is evidence that nothing was killed. A counter no
    snapshot managed to read is evidence of nothing at all, so both the count
    and the bool derived from it go ``None`` with the reason, rather than the
    "no OOM happened" a pair of zeros would assert.
    """
    events = [
        int(v)
        for c in samples
        for k, v in c.events.items()
        if k.startswith("memory.events") and k.endswith("oom_kill")
    ]
    if not events:
        # EXIT: an unread counter cannot answer in either direction
        reason = _gap_reason(OOM_KILL_COUNTER_SOURCE)
        return {
            "oom_kill_events_in_cgroup": None,
            "oom_kill_events_in_cgroup_error": reason,
            "oom_killed": None,
            "oom_killed_error": reason,
        }
    peak = max(events)
    return {"oom_kill_events_in_cgroup": peak, "oom_killed": bool(peak > 0)}


def build_totals(
    *, render: RenderPass, docling: DoclingPass, slice_pass: SlicePass,
    checkpoints: list[MemorySnapshot], final: MemorySnapshot,
    harness_failures: list[str],
) -> dict[str, object]:
    """The record's totals block: what was measured, plus what failed to be."""
    pages = render.pages
    samples = checkpoints + [final]
    oom = oom_fields(samples)
    counter_read = oom["oom_kill_events_in_cgroup"] is not None
    # Naming the counter as the source is only truthful when a snapshot actually
    # read it; an unread one points at the reason the gap carries instead.
    oom_source = OOM_KILL_COUNTER_SOURCE if counter_read else "not read; see oom_killed_error"
    return {
        "render_and_ocr_latency_ms": round(render.latency_ms, 1),
        **docling.to_totals(),
        **slice_pass.to_totals(),
        "total_latency_ms": round(
            render.latency_ms + docling.latency_ms + sum(slice_pass.latencies_ms), 1
        ),
        # The sum above covers three measured phases only. Converter init, the
        # model load and the file open sit outside it, so the end-to-end span is
        # recorded as its own reading rather than letting the phase sum be read
        # as the cost of a run.
        "wall_clock_ms": round(
            (final.t_monotonic_s - checkpoints[0].t_monotonic_s) * 1000.0, 1
        ),
        "page_latency_ms_stats": page_latency_stats(pages),
        "regions_total": regions_total(pages, docling.regions_measured),
        **page_rule_counts(pages),
        **peak_fields(samples),
        "cgroup_memory_max_bytes": final.cgroup_memory_max_bytes,
        "cgroup_memory_swap_max_bytes": final.cgroup_memory_swap_max_bytes,
        "cgroup_memory_swap_current_bytes": final.cgroup_memory_swap_current_bytes,
        "proc_vmswap_bytes": final.proc_vmswap_bytes,
        **oom,
        "oom_killed_source": oom_source,
        "harness_failures": harness_failures,
        "memory_checkpoints": [c.to_record() for c in samples],
    }


def build_record(
    *, pdf_path: Path, artifacts_path: Path, requested_dpi: int, max_pages: int | None,
    file_bytes: int, file_sha256: str, page_count: int, render: RenderPass,
    docling: DoclingPass, slice_pass: SlicePass, checkpoints: list[MemorySnapshot],
    final: MemorySnapshot, harness_failures: list[str],
    pipeline_identity: dict[str, object], tesseract_lang: str,
) -> dict[str, object]:
    """Assemble the whole measurement line the harness contract emits."""
    return {
        # /1 kept an unread quantity as a zero in four places; /2 makes every
        # failed reading null, which is what the committed records cannot show;
        # /3 reads the Docling page sizes off the pages *mapping*, so a record on
        # /2 or earlier has a null ``docling_page_size_pt`` beside a page count
        # that answered, and its size evidence is the pypdfium2 ``size_pt`` only.
        "schema": "fin.g4.t2a.pipeline_measurement/3",
        "file": {
            "path": str(pdf_path),
            "name": pdf_path.name,
            "bytes": file_bytes,
            "sha256": file_sha256,
        },
        "page_count": page_count,
        "requested_dpi": requested_dpi,
        "max_pages": max_pages,
        "page_limit_exceeded": page_limit_exceeded(max_pages, page_count),
        "pages": render.pages,
        "totals": build_totals(
            render=render, docling=docling, slice_pass=slice_pass,
            checkpoints=checkpoints, final=final, harness_failures=harness_failures,
        ),
        "pin_manifest": resolved_versions(),
        "pipeline_identity": pipeline_identity,
        "docling_builtin_limits": docling_builtin_limits(),
        "heron_artifact": {**artifact_identity(artifacts_path), **pipeline_identity},
        "repo_rule_provenance": {
            "ocr_decision": "fin.corpus.ocr.detect_ocr_required(min_text_chars=1)",
            "figure_ocr_decision": "fin.corpus.parsers._pages.crop_is_legible + crop_png",
            "ocr_engine": "fin.corpus.ocr.TesseractEngine",
            "tesseract_lang_default": tesseract_lang,
            "page_unit_rule": "one frozen unit per physical page (fin.corpus.parsers._pdf)",
        },
    }


def emit(record: dict[str, object]) -> None:
    """Exactly one machine-readable JSON line on stdout; diagnostics stay on stderr."""
    sys.stdout.write(json.dumps(record, separators=(",", ":"), ensure_ascii=True) + "\n")
    sys.stdout.flush()


# --------------------------------------------------------------------------
# CLI / phase wiring
# --------------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    """The driver's CLI: the PDF under test plus the harness safety valves."""
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
    return ap.parse_args()


def load_repo_ocr(fin_src: Path) -> tuple[Any, Any, str, Any]:
    """fin.corpus.ocr's rule, engine and language string, read off the mount.

    Hard failure if absent: a locally re-implemented rule would stop being a
    measurement of fin. The product's *figure* rule
    (``fin.corpus.parsers._pages.crop_is_legible``) is returned alongside it,
    for the reason the module docstring names: it is how text inside a chart on
    a page that also has a text layer is reached at all.
    """
    sys.path.insert(0, str(fin_src))
    from fin.corpus.ocr import (  # noqa: E402
        DEFAULT_TESSERACT_LANG,
        TesseractEngine,
        detect_ocr_required,
    )
    from fin.corpus.parsers._pages import (  # noqa: E402
        Region,
        crop_is_legible,
    )

    # Bound at module scope so the figure lane measures the product's own rule
    # and can build the product's own Region; see their declaration above.
    globals()["crop_is_legible"] = crop_is_legible
    globals()["_Region"] = Region

    return (
        detect_ocr_required,
        TesseractEngine(lang=DEFAULT_TESSERACT_LANG),
        DEFAULT_TESSERACT_LANG,
        crop_is_legible,
    )


def main() -> int:
    args = parse_args()
    pdf_path: Path = args.pdf
    if not pdf_path.is_file():
        log(f"[driver] no such PDF: {pdf_path}")
        return 2  # EXIT: nothing was measured, so no line is printed at all

    detect_ocr_required, ocr_engine, tesseract_lang, _figure_rule = load_repo_ocr(
        args.fin_src
    )
    import pypdfium2 as pdfium  # noqa: E402

    checkpoints: list[MemorySnapshot] = [memory_snapshot("start")]
    harness_failures: list[str] = []
    file_bytes, file_sha256 = sha256_bytes(pdf_path)

    started_at = time.monotonic()
    pdf_doc = pdfium.PdfDocument(str(pdf_path))
    page_count = len(pdf_doc)
    render = render_pages(
        started_at=started_at, pdf_doc=pdf_doc, page_count=page_count, dpi=args.dpi,
        force_ocr=args.force_ocr, image_dir=args.image_dir if args.write_images else None,
        ocr_engine=ocr_engine, detect_ocr_required=detect_ocr_required,
        failures=harness_failures,
    )
    checkpoints.append(memory_snapshot("after_render_and_ocr"))

    if page_limit_exceeded(args.max_pages, page_count):
        log(f"[driver] page limit exceeded: {page_count} > {args.max_pages}")

    conv, pipeline_identity = build_converter(
        args.artifacts, args.layout_preset, args.threads
    )
    checkpoints.append(memory_snapshot("after_converter_init"))

    docling = run_docling_pass(conv, pdf_path, checkpoints)
    attach_regions(render.pages, docling, harness_failures)

    # The product's figure rule needs the regions the layout pass just found,
    # so it runs here rather than inside the render pass above.
    run_figure_region_pass(
        pages=render.pages, pdf_doc=pdf_doc, dpi=args.dpi,
        image_dir=args.image_dir if args.write_images else None,
        ocr_engine=ocr_engine, failures=harness_failures,
    )
    checkpoints.append(memory_snapshot("after_figure_region_ocr"))

    slice_pass = run_slice_pass(
        pdfium_mod=pdfium, pdf_doc=pdf_doc, conv=conv, pages=render.pages,
        page_count=page_count, checkpoints=checkpoints, failures=harness_failures,
        enabled=args.slice_pass,
    )
    checkpoints.append(memory_snapshot("end"))
    final = memory_snapshot("final")

    emit(
        build_record(
            pdf_path=pdf_path, artifacts_path=args.artifacts, requested_dpi=args.dpi,
            max_pages=args.max_pages, file_bytes=file_bytes, file_sha256=file_sha256,
            page_count=page_count, render=render, docling=docling,
            slice_pass=slice_pass, checkpoints=checkpoints, final=final,
            harness_failures=harness_failures, pipeline_identity=pipeline_identity,
            tesseract_lang=tesseract_lang,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
