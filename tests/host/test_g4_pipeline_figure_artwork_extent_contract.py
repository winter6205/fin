"""A figure region must cover the artwork it names, not only what a box detected.

The layout stage returns a *detection* box. On ``g4-mixed-chart-001`` page 3 it
returns ``l 64.9, t 145.5, r 553.5, b 409.2`` pt, while the chart raster the page
actually places is ``l 42.0, t 100.0, r 570.0, b 428.0`` pt — the box is inset on
all four sides, and the chart's title, subtitle and source line lie in the inset
band above it. The figure rule then crops the detection box, OCR reads the axis
and the data labels, and the title is never read. That is the gap
``known_gaps[10]`` records, and the gold manifest's primary
``expected_ocr_quote`` is that title.

The PDF already states where its artwork is: an image XObject's placement is
addressable through the PDF library's own ``get_bounds()``, and this page's
answer (measured with pypdfium2 on the committed fixture) is exactly the
``x 42-570, y 100-428`` the manifest declares. So the placement is not an
assumption to be tuned — it is a reading, and the driver can take it from the
same object scan it already performs for the OCR rule.

What this file holds is that the region the figure rule acts on is the artwork's
real extent whenever the layout box names that artwork, and that this is derived
from the page rather than written down. A constant would satisfy a test that
asserted a number and would be wrong on every other document; the assertions
here are on the *relationship* between the layout box and the placement, using
the committed fixture's own measured values as the case.

The driver is a script that normally runs inside the pinned image, so it is
loaded by path and only its pure functions are called here, as
``test_g4_pipeline_ocr_evidence_contract`` does.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DRIVER_PATH = PROJECT_ROOT / "scripts" / "g4_pipeline" / "driver.py"
GOLD_MIXED = PROJECT_ROOT / "data" / "corpus" / "g4_attachment_gold" / "g4-mixed-chart-001.pdf"

# Measured on the committed fixture with pypdfium2, in the record's own
# bottom-left origin (this is what the driver and the product both read).
#
#   page 3 chart raster : l 42.0, b 364.0, r 570.0, t 692.0  (528 x 328 pt)
#   page 1 logotype     : l 289.5, b 430.0, r 322.5, t 470.0 (33 x 40 pt)
#
# The p3 values are the manifest's own declared placement for the chart
# ("displayed_pt": "528 x 328 (x=42, top y=100)"), so the two sources agree.
GOLD_P3_CHART_BOUNDS = (42.0, 364.0, 570.0, 692.0)
GOLD_P1_LOGO_BOUNDS = (289.5, 430.0, 322.5, 470.0)

# What the layout stage actually returns, in the same origin. The p3 box is
# inset on every side; the p1 box is the logotype itself.
LAYOUT_P3_FIGURE_BOX = {"l": 64.9, "t": 646.5, "r": 553.5, "b": 382.8}
LAYOUT_P1_FIGURE_BOX = {"l": 288.8, "t": 470.0, "r": 322.1, "b": 429.5}


def _driver() -> ModuleType:
    spec = importlib.util.spec_from_file_location("g4_driver_artwork_extent", DRIVER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _Image:
    """A PDF image object that knows only where it is placed."""

    def __init__(self, bounds: tuple[float, float, float, float]) -> None:
        self._bounds = bounds

    def get_bounds(self) -> tuple[float, float, float, float]:
        return self._bounds


class _Page:
    def __init__(self, images: list[_Image]) -> None:
        self._images = images

    def get_objects(self, **kwargs: Any) -> list[Any]:
        return list(self._images)


def _box(bounds: tuple[float, float, float, float]) -> dict[str, float]:
    left, bottom, right, top = bounds
    return {"l": left, "b": bottom, "r": right, "t": top}


# ----- the region must cover the artwork, or the crop loses the title ------


def test_a_figure_box_is_widened_to_the_artwork_it_sits_on() -> None:
    """The inset band above the detection box is where the chart title is.

    The layout box is contained by the raster's placement, so the two name the
    same artwork and the placement is the one that bounds the pixels. Cropping
    the inner box reads the axis and the data labels and silently drops the
    title, which is exactly the observation ``known_gaps[10]`` records.
    """
    driver = _driver()
    placement = _box(GOLD_P3_CHART_BOUNDS)

    widened = driver.artwork_extent(LAYOUT_P3_FIGURE_BOX, [placement])

    assert widened == {
        "l": 42.0, "b": 364.0, "r": 570.0, "t": 692.0,
    }, "the figure region still under-covers the chart raster"


def test_a_box_overlapping_no_image_is_left_alone() -> None:
    """A figure that is not an embedded image keeps the model's own box.

    Charts drawn as vector paths have no image XObject, and a page may carry a
    diagram that is not an image at all. Growing those into some other shape's
    placement would name artwork the model never saw.
    """
    driver = _driver()
    box = dict(LAYOUT_P1_FIGURE_BOX)
    # A real image elsewhere on the page: to the right of the logotype, which
    # spans l 288.8-322.1 and b 429.5-470.0.
    elsewhere = _box((400.0, 430.0, 500.0, 470.0))

    assert driver.artwork_extent(box, [elsewhere]) == box


def test_an_empty_placement_list_leaves_the_box_alone() -> None:
    """A page whose object scan could not answer must not invent an extent."""
    driver = _driver()
    box = dict(LAYOUT_P3_FIGURE_BOX)

    assert driver.artwork_extent(box, []) == box


def test_a_box_is_widened_by_the_smallest_image_that_overlaps_it() -> None:
    """When two images overlap the box, the tight one names the same artwork.

    A large page image can overlap a detection box that a smaller chart also
    overlaps, and the tighter one is the artwork the layout stage was pointing
    at. Choosing by area keeps the region as close to the detected artwork as the
    page's own readings allow, instead of growing the crop to whatever else
    happens to be on the page.
    """
    driver = _driver()
    logo = _box(GOLD_P1_LOGO_BOUNDS)
    page_image = _box((0.0, 0.0, 612.0, 792.0))  # a full-page raster, if one existed

    widened = driver.artwork_extent(dict(LAYOUT_P1_FIGURE_BOX), [page_image, logo])

    assert widened == {"l": 289.5, "b": 430.0, "r": 322.5, "t": 470.0}


def test_a_detector_box_that_overshoots_the_artwork_by_a_fraction_still_matches() -> None:
    """The measured reason the rule is overlap and not strict containment.

    On the committed page 1 the logotype's placement is ``l 289.5`` while the
    layout box is ``l 288.8`` — the detector's box starts 0.7 pt to the LEFT of
    the artwork, so no strict containment would match it and the region would
    stay inset, which is the very defect this fixes. This is measured on the
    fixture, not invented to suit the rule.
    """
    driver = _driver()
    logo = _box(GOLD_P1_LOGO_BOUNDS)

    assert LAYOUT_P1_FIGURE_BOX["l"] < logo["l"], "fixture no longer overshoots"
    assert driver.artwork_extent(dict(LAYOUT_P1_FIGURE_BOX), [logo]) == logo


def test_the_logotype_keeps_its_own_extent_and_stays_illegible() -> None:
    """Widening must not turn a logo into a legible region.

    The p1 logotype is 33 x 40 pt, a 93 px shorter edge at 200 dpi, under the
    product's 160 px floor. If an extent rule grew it, the page would pay a
    Tesseract call to learn that a logo holds no text.
    """
    driver = _driver()
    extent = driver.artwork_extent(
        dict(LAYOUT_P1_FIGURE_BOX), [_box(GOLD_P1_LOGO_BOUNDS)]
    )
    extent = {**extent, "page_height_pt": 792.0}

    assert driver._legible_figure_box(extent) is False, (
        "the 33 x 40 pt logotype was made legible by an extent change"
    )


# ----- the extent is a reading, not a constant ----------------------------


def test_the_extent_comes_from_the_pdf_object_scan_not_a_written_box() -> None:
    """The page is the source of the placement, so the driver must read it.

    An assertion on a constant would pass on this fixture and be wrong on every
    other document, and the driver would have no way to notice. This holds the
    scan: a page whose objects report a placement must produce that placement.
    """
    driver = _driver()
    page = _Page([_Image(GOLD_P3_CHART_BOUNDS)])

    placements = driver.image_placements(page)

    assert [tuple(p[k] for k in ("l", "b", "r", "t")) for p in placements] == [
        GOLD_P3_CHART_BOUNDS
    ]


def test_a_page_whose_scan_fails_reports_no_placement_rather_than_guessing() -> None:
    """An unreadable object list must not become an invented extent.

    Returning nothing leaves the layout box as it was, which is the conservative
    reading: the region is the model's own box, and the record says the page's
    artwork could not be read.
    """
    driver = _driver()

    class _Broken:
        def get_objects(self, **kwargs: Any) -> list[Any]:
            raise RuntimeError("object list unreadable")

    assert driver.image_placements(_Broken()) == []


def test_an_image_with_unreadable_bounds_is_skipped_not_guessed() -> None:
    """A placement the library will not state is a gap, not a zero-sized box."""
    driver = _driver()

    class _NoBounds:
        def get_bounds(self) -> tuple[float, float, float, float]:
            raise RuntimeError("no bounds")

    assert driver.image_placements(_Page([_NoBounds()])) == []


def test_attach_regions_widens_a_measured_figure_box() -> None:
    """The extent is applied where the layout output enters the record.

    ``attach_regions`` is the one place the layout pass's boxes are written onto
    a page, so it is where a box has to become the artwork's extent. Exercised
    through that entry point rather than through ``artwork_extent`` alone,
    because a real run failed here once: the key was declared ``None`` and
    ``dict.setdefault`` returns the existing ``None`` rather than a fresh list.
    """
    driver = _driver()
    entry: dict[str, object] = {
        "page_no": 3,
        "size_pt": [612.0, 792.0],
        "image_placements": [_box(GOLD_P3_CHART_BOUNDS)],
        "figure_boxes_detected": None,
    }
    docling = driver.DoclingPass(
        latency_ms=1.0,
        status="SUCCESS",
        page_count=1,
        error=None,
        regions={3: {"text": 5, "table": 0, "figure": 1,
                     "table_boxes": [], "figure_boxes": [dict(LAYOUT_P3_FIGURE_BOX)]}},
        page_sizes_pt={3: [612.0, 792.0]},
    )
    failures: list[str] = []

    driver.attach_regions([entry], docling, failures)

    assert failures == []
    box = entry["figure_boxes"][0]
    left, bottom, right, top = GOLD_P3_CHART_BOUNDS
    assert (box["l"], box["b"], box["r"], box["t"]) == (left, bottom, right, top)
    # The layout stage's own box is kept beside the widened one.
    assert entry["figure_boxes_detected"] == [
        {k: LAYOUT_P3_FIGURE_BOX[k] for k in ("l", "t", "r", "b")}
    ]


if __name__ == "__main__":  # pragma: no cover - convenience for a focused run
    import pytest

    raise SystemExit(pytest.main([__file__]))
