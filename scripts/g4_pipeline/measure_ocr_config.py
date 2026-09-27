"""Measure the CPU-Tesseract configuration the page/region core ships.

Runs where ``tesseract`` and its languages are the pinned ones. It answers
three questions the page/vision budget record left open: which ``--psm`` and
language set to ship, what a standalone image costs to prepare, and what the
shipped constants actually select.

Every timing is per-process CPU time plus major faults around the tesseract
child, not the harness process's own resident peak. Each (case, lane, lang,
psm, dpi) cell is run ``--repeats`` times: the *first* attempt pays the
language-model load into the process (measured at 3x-6x the warm call on a
cold tessdata cache), so only the last attempt is a per-page price.

Quality is reported four ways because character count alone rewards a
language model that emits garbage:

* ``oracle_hits`` — how many committed expected quotes the read reproduced.
* ``cjk_char_count`` — Chinese characters in the output. On an English-only
  page every one of them is a wrong character, whatever else the read got.
* ``ref_token_recall`` / ``junk_token_rate`` — for the scanned lane only,
  where a full independent reference exists: the same bytes were built by
  rasterising a page of a committed real filing, so that page's own text
  layer (read with pdfplumber, never with tesseract) is the reference.
  Recall is the share of reference words the read reproduced; junk rate is
  the share of the read's words that are not in the reference.
* ``wrong_word_rate`` — the same comparison over *every* letter run rather
  than only word-shaped ones, because a model that splits Latin words into
  single letters keeps ``recall`` at 1.0 and ``junk_token_rate`` near zero.

``--dump-dir`` also writes each read's full text next to the records, so a
character count can be checked by eye instead of only by rate.

Usage (from the container):
    python /worktree/scripts/g4_pipeline/measure_ocr_config.py \
        --worktree /worktree --out /out [--psm 3,6,11] [--dpi 150,200]
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import resource
import subprocess
import sys
import time
from pathlib import Path


def emit(record: dict) -> None:
    print(json.dumps(record, separators=(",", ":")), flush=True)


# The scanned fixture's three pages rasterise pages 6, 9 and 10 of this
# committed filing; its text layer is the reference for those same pixels.
SCANNED_REFERENCE = ("data/corpus/pdf/2023_Q3_AAPL.pdf", (6, 9, 10))


def load_expectations(gold_dir: Path) -> dict[tuple[str, int], dict]:
    """Committed per-page expectations, read from the gold manifest.

    Quoted from the dataset rather than copied into this script: the
    manifest is the authority, was authored outside fin, and states per
    physical page which text exists only as pixels. A quote the manifest
    labels as native-layer content is left out of the oracle — a page's text
    layer proves nothing about whether a read of its pixels worked.
    """
    manifest = json.loads((gold_dir / "manifest.json").read_text())
    out: dict[tuple[str, int], dict] = {}
    for case in manifest["cases"]:
        for page in case["per_page"]:
            quotes: list[str] = []
            for key in ("expected_ocr_quote", "expected_quote"):
                quote = page.get(key)
                if not quote:
                    continue
                kind = str(page.get("expected_quote_kind", ""))
                if key == "expected_quote" and "OCR" not in kind:
                    continue
                quotes.append(quote)
            quotes += list(page.get("expected_ocr_quote_alternatives", []))
            out[(case["case_id"], page["page"])] = {
                "quotes": quotes,
                "native_text_chars": page.get("native_text_chars"),
                "ocr_expected": page.get("ocr_expected"),
                "figure_box_pt": _figure_boxes(page),
            }
    return out


# Committed geometry, e.g. "528 x 328 (x=42, top y=100)".
_DISPLAYED_PT = re.compile(
    r"(\d+(?:\.\d+)?)\s*x\s*(\d+(?:\.\d+)?)"
    r"\s*\(x=(?P<x>[\d.]+),\s*top y=(?P<y>[\d.]+)\)"
)


def _figure_boxes(page: dict) -> list[tuple[float, float, float, float]]:
    """Top-left/right-bottom points of each figure the manifest boxes."""
    boxes = []
    for region in page.get("figure_regions", []):
        displayed = str(region.get("displayed_pt", ""))
        match = _DISPLAYED_PT.match(displayed)
        if match is None:
            continue
        width_pt, height_pt = float(match.group(1)), float(match.group(2))
        left, top = float(match.group("x")), float(match.group("y"))
        boxes.append((left, top, left + width_pt, top + height_pt))
    return boxes


_WORD = re.compile(r"[A-Za-z][A-Za-z0-9.,$%'/-]{3,}")
_ANY_WORD = re.compile(r"[A-Za-z]+")
_CJK = re.compile(r"[぀-ヿ㐀-䶿一-鿿]")


def _tokens(text: str) -> list[str]:
    return [t.casefold() for t in _WORD.findall(text)]


def _oracle_quality(text: str, *, oracle: list[str]) -> dict:
    """What the committed quotes and the character budget say about a read.

    Answered without any reference text: an oracle hit is a committed
    expectation reproduced, and the CJK / frozen counts are what the
    product would store, not what a model emitted raw — some language
    models pad Latin words with wide spacing, which a raw count prices but
    :func:`fin.corpus.normalization.normalize_frozen_text` collapses away
    before a unit is written.
    """
    from fin.corpus.normalization import normalize_frozen_text

    flat = " ".join(text.split())
    frozen = normalize_frozen_text(text)
    hits = [quote for quote in oracle if " ".join(quote.split()) in flat]
    return {
        "oracle_quotes": len(oracle),
        "oracle_hits": len(hits),
        "oracle_hit_quotes": hits,
        "cjk_char_count": len(_CJK.findall(text)),
        "frozen_char_count": len(frozen),
        "frozen_word_count": len(_ANY_WORD.findall(frozen)),
    }


def _token_quality(text: str, *, reference: str) -> dict:
    """Token-level agreement with an independent reference text layer.

    Only the scanned lane has a reference, and one measure is not enough:
    a language model that splits Latin words into single letters keeps
    ``ref_token_recall`` at 1.0 and ``junk_token_rate`` near 0, because a
    one-letter run is not a ``_WORD`` match either way. ``wrong_word_rate``
    therefore counts every letter run, so character-level garbage shows up
    as wrong text.
    """
    ref = set(_tokens(reference))
    got = _tokens(text)
    ref_any = {t.casefold() for t in _ANY_WORD.findall(reference)}
    got_any = [t.casefold() for t in _ANY_WORD.findall(text)]
    return {
        "ref_token_count": len(ref),
        "ref_token_recall": round(len(ref & set(got)) / len(ref), 4) if ref else None,
        "junk_token_rate": (
            round(sum(1 for t in got if t not in ref) / len(got), 4) if got else None
        ),
        "word_count": len(got_any),
        "wrong_word_rate": (
            round(sum(1 for t in got_any if t not in ref_any) / len(got_any), 4)
            if got_any
            else None
        ),
    }


def quality(text: str, *, oracle: list[str], reference: str | None) -> dict:
    """The four quality reports of one read, keyed as the record names them."""
    out = _oracle_quality(text, oracle=oracle)
    if reference is not None:
        out.update(_token_quality(text, reference=reference))
    return out


def ocr_once(pil_image, lang: str, psm: str, *, attempt: int, oracle: list[str],
             reference: str | None, dump_to: Path | None = None) -> dict:
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu_before = time.process_time()
    wall_before = time.monotonic()
    config = f"--psm {psm}"
    import pytesseract

    error = None
    text = ""
    try:
        text = pytesseract.image_to_string(pil_image, lang=lang, config=config)
    except Exception as exc:  # a missing language, a crashed child
        error = f"{type(exc).__name__}: {exc}"
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    if dump_to is not None:
        dump_to.write_text(text)
    return {
        "attempt": attempt,
        "lang": lang,
        "psm": psm,
        "config": config,
        "text_char_count": len(text.strip()),
        "text_sample": text.strip()[:120],
        "dumped_text": dump_to.name if dump_to is not None else None,
        **quality(text, oracle=oracle, reference=reference),
        "cpu_time_ms": round((time.process_time() - cpu_before) * 1000, 1),
        "wall_ms": round((time.monotonic() - wall_before) * 1000, 1),
        "child_utime_ms": round((after.ru_utime - before.ru_utime) * 1000, 1),
        "child_stime_ms": round((after.ru_stime - before.ru_stime) * 1000, 1),
        "child_major_faults": after.ru_majflt - before.ru_majflt,
        "child_max_rss_bytes": after.ru_maxrss * 1024,
        "error": error,
    }


def render_page(pdf: Path, page_no: int, dpi: int):
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(str(pdf))
    image = doc[page_no - 1].render(scale=dpi / 72.0).to_pil()
    doc.close()
    return image.convert("RGB")


def reference_text(worktree: Path) -> dict[int, str]:
    """Text layer of the filing pages the scanned fixture was rasterised from."""
    import pdfplumber

    rel, pages = SCANNED_REFERENCE
    out: dict[int, str] = {}
    with pdfplumber.open(worktree / rel) as pdf:
        for page_no in pages:
            out[page_no] = pdf.pages[page_no - 1].extract_text() or ""
    return out


def host_identity() -> dict:
    try:
        version = subprocess.run(
            ["tesseract", "--version"], capture_output=True, text=True, check=False
        ).stdout.strip().splitlines()[0]
        langs = subprocess.run(
            ["tesseract", "--list-langs"], capture_output=True, text=True, check=False
        ).stdout.strip().splitlines()[1:]
    except Exception as exc:  # no binary: the record must say so, not crash
        version, langs = f"unavailable: {type(exc).__name__}", []
    return {
        "tesseract_version": version,
        "tesseract_langs": langs,
        "python": platform.python_version(),
        "machine": f"{platform.machine()} / {os.cpu_count()} cores",
        "load_avg": list(os.getloadavg()),
    }


def _comma_list(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


def _comma_ints(raw: str) -> list[int]:
    return [int(item) for item in _comma_list(raw)]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the ladder, keeping the comma lists as the lists they name."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--worktree", type=Path, default=Path("/worktree"))
    ap.add_argument("--out", type=Path, default=Path("/out"))
    ap.add_argument("--psm", default="3,6,11")
    ap.add_argument("--dpi", default="200")
    ap.add_argument("--langs", default="eng,chi_sim+eng")
    ap.add_argument("--repeats", type=int, default=2)
    ap.add_argument("--scanned-pages", default="1")
    ap.add_argument("--dump-dir", type=Path, default=None)
    ap.add_argument("--lanes", default="standalone_image,figure_crop,scanned_full_page")
    ap.add_argument("--fin-src", type=Path, default=Path("/worktree/src"))
    args = ap.parse_args(argv)
    args.psms = _comma_list(args.psm)
    args.dpis = _comma_ints(args.dpi)
    args.langs = _comma_list(args.langs)
    args.lanes = _comma_list(args.lanes)
    args.scanned_pages = _comma_ints(args.scanned_pages)
    return args


def _dump_path(
    dump_dir: Path, *, case_id: str, lane: str, page_no: int,
    extra: dict | None, lang: str, psm: str, attempt: int,
) -> Path:
    """One name per (case, lane, dpi, lang, psm, attempt) cell."""
    dpi = extra.get("dpi") if extra else "na"
    return dump_dir / (
        f"{case_id}_{lane}_p{page_no}_dpi{dpi}"
        f"_{lang.replace('+', '-')}_psm{psm}_a{attempt}.txt"
    )


def run_cell(
    image, *, case_id: str, lane: str, page_no: int, reference: str | None,
    extra: dict | None, args: argparse.Namespace,
    expects: dict[tuple[str, int], dict],
) -> None:
    """Measure one (case, lane, page) cell across the lang × psm × attempt grid."""
    if lane not in args.lanes:
        return
    expectation = expects[(case_id, page_no)]
    assert expectation["quotes"], (
        f"the manifest names no OCR expectation for {case_id} page {page_no}"
    )
    for lang in args.langs:
        for psm in args.psms:
            for attempt in range(1, args.repeats + 1):
                dump = None
                if args.dump_dir is not None:
                    args.dump_dir.mkdir(parents=True, exist_ok=True)
                    dump = _dump_path(
                        args.dump_dir, case_id=case_id, lane=lane,
                        page_no=page_no, extra=extra, lang=lang, psm=psm,
                        attempt=attempt,
                    )
                record = ocr_once(
                    image, lang, psm, attempt=attempt,
                    oracle=expectation["quotes"], reference=reference,
                    dump_to=dump,
                )
                record.update(
                    case_id=case_id, lane=lane, page_no=page_no,
                    page_native_text_chars=expectation["native_text_chars"],
                    page_ocr_expected=expectation["ocr_expected"],
                )
                if extra:
                    record.update(extra)
                emit(record)


def emit_run_header(args: argparse.Namespace, *, constants: dict) -> None:
    """The first record: shipped constants and the host they were measured on."""
    emit(
        {
            "schema": "fin.g4.t2d.ocr_config_measurement/1",
            "shipped_constants": {
                **constants,
                "requested_dpi": args.dpis,
                "psm_ladder": args.psms,
                "langs": args.langs,
                "repeats": args.repeats,
            },
            "host": host_identity(),
            "load_avg_1m_before": os.getloadavg()[0],
        }
    )


def emit_run_footer() -> None:
    emit({"schema": "fin.g4.t2d.ocr_config_measurement/1", "done": True,
          "load_avg_1m_after": os.getloadavg()[0], "host_after": host_identity()})


def measure_standalone_image(gold: Path, args: argparse.Namespace,
                             expects: dict) -> None:
    """1. the standalone image: the only preparation cost no record has."""
    import PIL.Image

    with PIL.Image.open(gold / "g4-image-chart-001.png") as source_image:
        source_image.load()
        run_cell(
            source_image.convert("RGB"),
            case_id="g4-image-chart-001",
            lane="standalone_image",
            page_no=1,
            reference=None,
            extra={"pixels": list(source_image.size)},
            args=args,
            expects=expects,
        )


def measure_figure_crop(gold: Path, args: argparse.Namespace, expects: dict,
                        *, min_edge_px: int) -> None:
    """2. the figure crop on a native-text page: the committed box of the
    mixed page's third page, which is where the region rule fires."""
    for dpi in args.dpis:
        page = render_page(gold / "g4-mixed-chart-001.pdf", 3, dpi)
        scale = dpi / 72.0
        box_pt = expects[("g4-mixed-chart-001", 3)]["figure_box_pt"][0]
        box = tuple(int(round(v * scale)) for v in box_pt)
        piece = page.crop(box)
        run_cell(
            piece,
            case_id="g4-mixed-chart-001",
            lane="figure_crop",
            page_no=3,
            reference=None,
            extra={
                "dpi": dpi,
                "crop_pixels": list(piece.size),
                "crop_box_pt": list(box_pt),
                "legible": min(piece.size) >= min_edge_px,
            },
            args=args,
            expects=expects,
        )


def measure_scanned_pages(gold: Path, args: argparse.Namespace, expects: dict,
                          refs: dict[int, str]) -> None:
    """3. scanned pages, whole: the cost that prices a filing. Reference
    text is the source filing's own text layer for the page these pixels
    came from."""
    for dpi in args.dpis:
        for page_no in args.scanned_pages:
            page = render_page(gold / "g4-scanned-001.pdf", page_no, dpi)
            run_cell(
                page,
                case_id="g4-scanned-001",
                lane="scanned_full_page",
                page_no=page_no,
                reference=refs[SCANNED_REFERENCE[1][page_no - 1]],
                extra={
                    "dpi": dpi,
                    "page_pixels": list(page.size),
                    "reference_page": SCANNED_REFERENCE[1][page_no - 1],
                },
                args=args,
                expects=expects,
            )


def main() -> int:
    args = parse_args()

    sys.path.insert(0, str(args.fin_src))
    from fin.corpus.parsers._pages import (  # shipped constants, not copies
        MIN_OCR_REGION_EDGE_PX,
        PAGE_RENDER_DPI,
    )

    gold = args.worktree / "data/corpus/g4_attachment_gold"
    refs = reference_text(args.worktree)
    expects = load_expectations(gold)

    emit_run_header(
        args,
        constants={
            "page_render_dpi": PAGE_RENDER_DPI,
            "min_ocr_region_edge_px": MIN_OCR_REGION_EDGE_PX,
        },
    )
    measure_standalone_image(gold, args, expects)
    measure_figure_crop(gold, args, expects, min_edge_px=MIN_OCR_REGION_EDGE_PX)
    measure_scanned_pages(gold, args, expects, refs)
    emit_run_footer()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
