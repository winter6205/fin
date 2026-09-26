#!/usr/bin/env python3
"""Screen every committed corpus page for a solid ink block.

The claim this script exists to keep checkable is `known_gaps[0]` of
`data/corpus/g4_attachment_gold/manifest.json`: the committed filings contain no
native data chart, and the only solid graphic in them is the Apple logotype on
the four AAPL cover pages. A layout model's `figure` recall cannot be judged from
a corpus that has nothing to find, so the product rule "scan the whole page,
including pages where Heron found no figure box" rests on this negative.

A chart drawn as bars, thick lines or filled areas survives erosion by a square
neighbourhood: its strokes are wider than a few pixels. Text does not — a glyph
stroke at 50 dpi is one or two pixels wide, so eroding the dark-pixel mask by a
5x5 or 9x9 square removes it and leaves only solid filled regions.

Rendering is by poppler's `pdftoppm`, not by any library fin depends on, so the
screen is independent of the parser whose recall it bounds.

Usage:
    uv run --no-sync python scripts/g4_ink_screen.py \
        [--corpus-dir data/corpus/pdf] [--out data/corpus/g4_ink_screen.json]
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

import numpy as np

SCHEMA: Final = "fin.g4.t2a.ink_screen/1"
DPI: Final = 50
EROSION_RADII: Final = (5, 9)

# A page below this dark-pixel fraction is blank enough that "no solid block"
# is not a statement about its content, so it is excluded from the population
# the surviving fractions are reported over.
MATERIAL_INK_FRACTION: Final = 0.001

# A pixel at or below this 8-bit grey value counts as ink.
DARK_MAX: Final = 128

TOOLS: Final = {
    "renderer": "pdftoppm (poppler), -gray -r 50 -png",
    "screen": "numpy erosion of the dark-pixel mask by square neighbourhoods",
}


class ScreenError(RuntimeError):
    """A required input or tool is missing: no artifact is written."""


@dataclass(frozen=True)
class PageScreen:
    pdf: str
    page: int
    pixels: list[int]
    dark_fraction: float
    #: surviving solid fraction per erosion side length, keyed "5" / "9"
    solid_fraction: dict[str, float]

    @property
    def has_material_ink(self) -> bool:
        return self.dark_fraction >= MATERIAL_INK_FRACTION

    def to_entry(self) -> dict[str, Any]:
        return {
            "pdf": self.pdf,
            "page": self.page,
            "width_px": self.pixels[0],
            "height_px": self.pixels[1],
            "dark_fraction": round(self.dark_fraction, 6),
            "solid_5x5_fraction": round(self.solid_fraction["5"], 6),
            "solid_9x9_fraction": round(self.solid_fraction["9"], 6),
        }


def pdf_page_count(pdf: Path) -> int:
    out = subprocess.run(
        ["pdfinfo", str(pdf)], check=True, capture_output=True, text=True
    ).stdout
    match = re.search(r"^Pages:\s+(\d+)\s*$", out, re.MULTILINE)
    if match is None:
        raise ScreenError(f"pdfinfo reported no page count for {pdf.name}")
    return int(match.group(1))


def render_page(pdf: Path, page: int, workdir: Path) -> Path:
    # pdftoppm is 1-based with -f/-l and names its output <root>-<page>.png.
    root = workdir / f"{pdf.stem}_{page:04d}"
    subprocess.run(
        ["pdftoppm", "-gray", "-r", str(DPI), "-png",
         "-f", str(page), "-l", str(page), str(pdf), str(root)],
        check=True,
        capture_output=True,
    )
    produced = sorted(workdir.glob(f"{root.name}*.png"))
    if not produced:
        raise ScreenError(f"pdftoppm wrote no bitmap for {pdf.name} page {page}")
    return produced[0]


def _eroded_solid_fraction(dark: np.ndarray, side: int) -> float:
    """Fraction of page pixels that are the centre of an all-ink `side` square.

    The integral-image sum over the mask answers every window in one pass; a
    window is solid exactly when its sum equals its area.
    """
    radius = side // 2
    inner = dark[radius : dark.shape[0] - radius, radius : dark.shape[1] - radius]
    if inner.size == 0:
        return 0.0
    padded = np.zeros((dark.shape[0] + 1, dark.shape[1] + 1), dtype=np.int64)
    np.cumsum(np.cumsum(dark.astype(np.int64), axis=0), axis=1, out=padded[1:, 1:])
    top, left = np.meshgrid(
        np.arange(dark.shape[0] - side + 1), np.arange(dark.shape[1] - side + 1)
    )
    window_sum = (
        padded[top + side, left + side]
        - padded[top, left + side]
        - padded[top + side, left]
        + padded[top, left]
    )
    return float(np.count_nonzero(window_sum == side * side)) / float(
        dark.shape[0] * dark.shape[1]
    )


def screen_page(png: Path, pdf_name: str, page: int) -> PageScreen:
    bitmap = np.asarray(_read_grey(png))
    if bitmap.ndim != 2:
        raise ScreenError(f"{png.name} is not a single-channel bitmap")
    dark = (bitmap <= DARK_MAX).astype(np.uint8)
    height, width = bitmap.shape
    return PageScreen(
        pdf=pdf_name,
        page=page,
        pixels=[int(width), int(height)],
        dark_fraction=float(np.count_nonzero(dark)) / float(height * width),
        solid_fraction={
            str(side): _eroded_solid_fraction(dark, side) for side in EROSION_RADII
        },
    )


def _read_grey(png: Path) -> Any:
    from PIL import Image

    with Image.open(png) as handle:
        return handle.convert("L")


def screen_corpus(pdf_dir: Path) -> list[PageScreen]:
    pdfs = sorted(pdf_dir.glob("*.pdf"))
    if not pdfs:
        raise ScreenError(f"no PDFs found under {pdf_dir}")
    screens: list[PageScreen] = []
    with tempfile.TemporaryDirectory(prefix="g4-ink-") as tmp:
        workdir = Path(tmp)
        for pdf in pdfs:
            for page in range(1, pdf_page_count(pdf) + 1):
                bitmap = render_page(pdf, page, workdir)
                screens.append(screen_page(bitmap, pdf.name, page))
                bitmap.unlink()
    return screens


def _has_solid_block(screen: PageScreen) -> bool:
    return any(screen.solid_fraction[str(side)] > 0.0 for side in EROSION_RADII)


def _peaks(inked: list[PageScreen]) -> dict[str, float]:
    return {
        str(side): max((s.solid_fraction[str(side)] for s in inked), default=0.0)
        for side in EROSION_RADII
    }


def aggregate(screens: list[PageScreen]) -> dict[str, Any]:
    inked = [s for s in screens if s.has_material_ink]
    with_block = [s for s in inked if _has_solid_block(s)]
    return {
        "pages_rendered": len(screens),
        "pdf_count": len({s.pdf for s in screens}),
        "pages_with_material_ink": len(inked),
        "pages_without_material_ink": len(screens) - len(inked),
        "pages_with_any_solid_block": len(with_block),
        "page_names_with_any_solid_block": sorted(
            f"{s.pdf} p{s.page}" for s in with_block
        ),
        "max_solid_fraction": {k: round(v, 6) for k, v in _peaks(inked).items()},
        "solid_5x5_fraction_of_inked_pages": round(len(with_block) / len(inked), 6)
        if inked
        else None,
        "nonzero_solid_fractions": [
            {
                "pdf": s.pdf,
                "page": s.page,
                "solid_5x5_fraction": round(s.solid_fraction["5"], 6),
                "solid_9x9_fraction": round(s.solid_fraction["9"], 6),
            }
            for s in with_block
        ],
    }


def _tool_versions() -> dict[str, str]:
    """Record what produced the screen, so the artifact proves its own basis."""
    from PIL import Image

    rendered = subprocess.run(
        ["pdftoppm", "-v"], capture_output=True, text=True
    ).stderr.splitlines()
    return {
        "pdftoppm": rendered[0].strip() if rendered else "unknown",
        "numpy": np.__version__,
        "pillow": Image.__version__,
        "python": sys.version.split()[0],
    }


def build_artifact(screens: list[PageScreen], measured_at: str) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "measured_at_utc": measured_at,
        "question": "Does the committed corpus contain any native solid graphic "
        "a layout model could box as a data chart?",
        "bounds": {
            "dpi": DPI,
            "erosion_sides": list(EROSION_RADII),
            "dark_pixel_max": DARK_MAX,
            "material_ink_min_dark_fraction": MATERIAL_INK_FRACTION,
            "denominator_of_every_fraction": "page pixels (height x width)",
        },
        "tools": TOOLS,
        "tool_versions": _tool_versions(),
        "summary": aggregate(screens),
        "pages": [s.to_entry() for s in screens],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--corpus-dir", default="data/corpus/pdf", type=Path)
    parser.add_argument("--out", default="data/corpus/g4_ink_screen.json", type=Path)
    args = parser.parse_args(argv)
    try:
        screens = screen_corpus(args.corpus_dir)
    except (ScreenError, subprocess.CalledProcessError) as failure:
        print(f"ERROR: ink screen did not run: {failure}", file=sys.stderr)
        return 1
    artifact = build_artifact(screens, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    args.out.write_text(json.dumps(artifact, indent=1) + "\n", encoding="utf-8")
    print(json.dumps(artifact["summary"], indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
