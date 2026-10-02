"""Figure-scoped OCR must actually run for a page that has BOTH a text layer and a figure.

``scripts/g4_pipeline/driver.py`` ran one OCR rule per page:
``fin.corpus.ocr.detect_ocr_required``. That rule asks whether the text layer
collapsed while the page still carries images, so on a page with a healthy text
layer it can never answer "yes" — and the driver measured no OCR at all on
``g4-mixed-chart-001`` page 3, whose chart labels exist only as pixels.

The product already has the other half of its own two-rule contract, and says so
at ``src/fin/corpus/parsers/_pages.py:29-33``: the *figure* rule
(``crop_is_legible``) "asks whether one region is a figure whose box is big
enough to hold text drawn as pixels, which is how text inside a chart on a page
that also has a text layer is reached at all". The driver never invoked it, so
a committed ``ocr_expected: true`` was authored and unmeasurable.

This file holds the line that closes that gap: the driver must apply the
product's OWN rule and the product's OWN crop, so the lane measures what the
product does rather than a second OCR engine written here. The rule is imported
from the mounted product, not restated — a local copy of the 160 px floor would
let the harness disagree with the code it claims to measure.

A figure crop is also *not* free: it is a second Tesseract call on a bitmap the
page rule may already have read. The product skips crops once the page rule has
had the whole raster (``_pages.py`` ``_one_pdf_page``), and this driver keeps
that shape, so a scanned page is still recognized once.

The driver is a script that normally runs inside the pinned image, so it is
loaded by path here and only its pure functions are called, as
``test_g4_pipeline_ocr_evidence_contract`` does. Every collaborator is a local
double; the product rule is imported from the real package, because the claim
under test is that the *product's* threshold is what runs.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DRIVER_PATH = PROJECT_ROOT / "scripts" / "g4_pipeline" / "driver.py"

# The committed gold page this contract is about: page 3 of
# ``g4-mixed-chart-001.pdf`` carries a text layer AND a 528 x 328 pt chart, and
# the manifest authors ``ocr_expected: true`` for it. At the core's 200 dpi
# that box's shorter edge is 732 px, over the 160 px floor.
#
# ``page_height_pt`` travels with a recorded box because the record keeps
# Docling's bottom-left origin and the figure rule works in the product's
# top-left space, so the flip needs the page it belongs to. The driver attaches
# it in ``attach_regions``; a box handed over directly carries it too.
GOLD_MIXED_PAGE3_FIGURE_BOX_PT = {
    "l": 64.9, "t": 646.5, "r": 553.5, "b": 382.8, "page_height_pt": 792.0,
}

# A box under the floor, in the shape the gold set measures one: the 33 x 40 pt
# logotype on page 1 of the same case (93 px shorter edge at 200 dpi).
GOLD_MIXED_PAGE1_FIGURE_BOX_PT = {
    "l": 288.8, "t": 470.0, "r": 322.1, "b": 429.5, "page_height_pt": 792.0,
}


def _driver() -> ModuleType:
    spec = importlib.util.spec_from_file_location("g4_pipeline_driver_figure_ocr", DRIVER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _product_rule() -> Any:
    """The product's own figure rule, imported rather than restated."""
    sys.path.insert(0, str(PROJECT_ROOT / "src"))
    from fin.corpus.parsers._pages import crop_is_legible  # noqa: PLC0415

    return crop_is_legible


def _page_png() -> bytes:
    """A real page-sized PNG, so the product's own ``crop_png`` can cut it.

    The figure lane is a crop, not a page read: the bytes handed to the engine
    have to come out of the product's cut, so the test supplies a raster the
    product can actually slice rather than a stub that would let a whole-page
    read pass for a region read.
    """
    import io

    from PIL import Image

    image = Image.new("RGB", (1700, 2200), "white")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


_PAGE_PNG = _page_png()


class _Engine:
    """A Tesseract stand-in that records what it was asked to read."""

    lang = "eng"

    def __init__(self, *, text: str = "Total net sales by fiscal quarter") -> None:
        self._text = text
        self.calls: list[dict[str, Any]] = []

    def recognize(self, *, source: Path, page: int) -> str:
        self.calls.append({"source": Path(source), "page": page})
        return self._text


# ----- the figure rule must run on a page that has a text layer ------------


def test_a_legible_figure_region_is_read_even_with_a_healthy_text_layer(
    tmp_path: Path,
) -> None:
    """The committed counterexample: native text present, chart labels in pixels.

    ``detect_ocr_required`` can never answer "yes" for this page, so if the
    driver asks only that question the manifest's ``ocr_expected: true`` is
    unmeasurable. The figure rule is the product's own other half and it does
    answer "yes" for this box.
    """
    driver = _driver()
    engine = _Engine()
    entry: dict[str, object] = {
        "page_no": 3,
        "ocr_used": False,
        "ocr_text_char_count": 0,
        "native_text_char_count": 334,
        "ocr_required_by_repo_rule": False,
    }
    failures: list[str] = []

    driver._apply_figure_region_ocr(
        entry,
        [GOLD_MIXED_PAGE3_FIGURE_BOX_PT],
        _PAGE_PNG,
        page_no=3,
        dpi=200,
        image_dir=tmp_path,
        ocr_engine=engine,
        failures=failures,
    )

    assert failures == []
    assert len(engine.calls) == 1, "the figure region was never read"
    assert entry["figure_ocr_regions_read"] == 1
    assert entry["figure_ocr_text_char_count"] == len("Total net sales by fiscal quarter")
    assert "Total net sales by fiscal quarter" in str(entry["figure_ocr_text_sample"])


def test_the_figure_read_survives_beside_the_page_bitmap(tmp_path: Path) -> None:
    """A committed chart quote has to be reachable in the artefact.

    The manifest's ``expected_ocr_quote`` for this page is the chart TITLE, and
    Tesseract reads a bar chart axis-first, so the title can sit past a
    160-character window — the same reachability defect the page-level sidecar
    was written for, one rule down. Without the sidecar the expectation is
    unfalsifiable however correct the read was.
    """
    driver = _driver()
    title = "Total net sales by fiscal quarter"
    engine = _Engine(text=f"120,000 100,000 80,000 {'x' * 200} {title}")
    entry: dict[str, object] = {
        "page_no": 3,
        "ocr_used": False,
        "ocr_text_char_count": 0,
        "ocr_required_by_repo_rule": False,
    }
    failures: list[str] = []

    driver._apply_figure_region_ocr(
        entry,
        [GOLD_MIXED_PAGE3_FIGURE_BOX_PT],
        _PAGE_PNG,
        page_no=3,
        dpi=200,
        image_dir=tmp_path,
        ocr_engine=engine,
        failures=failures,
    )

    assert failures == []
    sidecar = tmp_path / str(entry["figure_ocr_text_file"])
    assert sidecar.is_file()
    assert title in sidecar.read_text(encoding="utf-8")
    assert entry["figure_ocr_text_sha256"] == driver.sha256_bytes(sidecar)[1]
    # The bounded sample is still bounded: the line does not grow with the crop.
    assert len(str(entry["figure_ocr_text_sample"])) == 160


def test_nothing_kept_means_no_figure_sidecar(tmp_path: Path) -> None:
    """Without ``--write-images`` the run keeps no pixels, so it keeps no text."""
    driver = _driver()
    engine = _Engine()
    entry: dict[str, object] = {
        "page_no": 3,
        "ocr_used": False,
        "ocr_text_char_count": 0,
        "ocr_required_by_repo_rule": False,
    }
    failures: list[str] = []

    driver._apply_figure_region_ocr(
        entry,
        [GOLD_MIXED_PAGE3_FIGURE_BOX_PT],
        _PAGE_PNG,
        page_no=3,
        dpi=200,
        image_dir=None,
        ocr_engine=engine,
        failures=failures,
    )

    assert entry["figure_ocr_regions_read"] == 1
    assert entry["figure_ocr_text_file"] is None
    assert entry["figure_ocr_text_sha256"] is None


def test_a_figure_region_under_the_legibility_floor_is_not_read(tmp_path: Path) -> None:
    """A logo holds no text, and paying a Tesseract call to learn that is waste.

    This is the product's own floor, not a driver-chosen one: the gold set
    measures a 33 x 40 pt logotype at 93 px against the same 160 px the core
    applies, and it must not be cropped.
    """
    driver = _driver()
    engine = _Engine()
    entry: dict[str, object] = {"page_no": 1, "ocr_used": False, "ocr_text_char_count": 0}
    failures: list[str] = []

    driver._apply_figure_region_ocr(
        entry,
        [GOLD_MIXED_PAGE1_FIGURE_BOX_PT],
        _PAGE_PNG,
        page_no=1,
        dpi=200,
        image_dir=tmp_path,
        ocr_engine=engine,
        failures=failures,
    )

    assert failures == []
    assert engine.calls == []
    assert entry["figure_ocr_regions_read"] == 0
    assert entry["figure_ocr_text_char_count"] == 0


# ----- the driver's threshold is the product's, not a local copy -----------


def test_the_driver_uses_the_products_own_legibility_rule() -> None:
    """A restated 160 px floor would let the harness disagree with the product.

    The two answers must come from the same function object, so a change to
    ``MIN_OCR_REGION_EDGE_PX`` moves the measurement with the code it measures.
    """
    driver = _driver()
    assert driver.crop_is_legible is _product_rule()


def test_load_repo_ocr_hands_back_both_rules_and_main_unpacks_them() -> None:
    """The figure rule is returned, and every caller unpacks the shape it returns.

    A measured run reached the container and died on ``ValueError: too many
    values to unpack`` because the return gained a fourth value and one call
    site kept unpacking three. The run's ``json_lines`` was 0 and its exit was
    1, so this is caught by the arity check below rather than by a green
    container: the shape of what ``main`` receives is part of the contract.
    """
    driver = _driver()
    src = DRIVER_PATH.read_text()
    tree = ast.parse(src)

    returns = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "load_repo_ocr"
    ]
    assert len(returns) == 1
    annotation = ast.unparse(returns[0].returns)
    arity = annotation.count("Any") + annotation.count("str")

    unpackings = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and isinstance(node.targets[0], ast.Tuple)
        and any(
            isinstance(target, ast.Name) and target.id == "detect_ocr_required"
            for target in node.targets[0].elts
        )
        and isinstance(node.value, ast.Call)
        and getattr(node.value.func, "id", None) == "load_repo_ocr"
    ]
    assert len(unpackings) == 1, "load_repo_ocr must have exactly one call site"
    targets = unpackings[0].targets[0]
    assert isinstance(targets, ast.Tuple), "its result is unpacked, not indexed"
    assert len(targets.elts) == arity, (
        f"load_repo_ocr returns {arity} values ({annotation}) but the call site "
        f"unpacks {len(targets.elts)}"
    )
    assert driver.crop_is_legible is not None


# ----- the page rule and the figure rule must not both read one bitmap -----


def test_a_page_already_read_whole_is_not_read_again_by_region(tmp_path: Path) -> None:
    """Whole-page OCR supersedes the crops; the product's own ordering.

    ``_pages.py`` reads the full raster when the page rule fires and skips the
    figure crops, so the same bitmap is never recognized twice for nothing
    added. Charging a scanned gold case a second Tesseract pass per figure would
    also misprice the OCR cost the budget decision is read from.
    """
    driver = _driver()
    engine = _Engine()
    entry: dict[str, object] = {
        "page_no": 1,
        "ocr_used": True,
        "ocr_text_char_count": 5565,
        "ocr_required_by_repo_rule": True,
    }
    failures: list[str] = []

    driver._apply_figure_region_ocr(
        entry,
        [GOLD_MIXED_PAGE3_FIGURE_BOX_PT],
        _PAGE_PNG,
        page_no=1,
        dpi=200,
        image_dir=tmp_path,
        ocr_engine=engine,
        failures=failures,
    )

    assert engine.calls == []
    # Zero regions were *read*, and that is a real answer: the figure rule ran
    # and the page rule had already had the whole raster.
    assert entry["figure_ocr_regions_read"] == 0
    assert entry["figure_ocr_skipped_reason"] == "page_ocr_already_read_whole_page"


# ----- a failed crop read is reported, never counted as empty text ---------


def test_a_failed_figure_read_names_its_failure_and_keeps_no_text(
    tmp_path: Path,
) -> None:
    """A crop that could not be read says nothing about the page.

    Zero characters with no error would read as "the engine ran and the chart
    holds no text", which is a different claim from "the read failed" — the
    same distinction ``_pages.py`` draws between ``ocr_empty`` and a typed
    failure.
    """
    driver = _driver()

    class _Failing:
        lang = "eng"

        def recognize(self, *, source: Path, page: int) -> str:
            raise RuntimeError("tesseract exited 1")

    entry: dict[str, object] = {
        "page_no": 3,
        "ocr_used": False,
        "ocr_text_char_count": 0,
        "ocr_required_by_repo_rule": False,
    }
    failures: list[str] = []

    driver._apply_figure_region_ocr(
        entry,
        [GOLD_MIXED_PAGE3_FIGURE_BOX_PT],
        _PAGE_PNG,
        page_no=3,
        dpi=200,
        image_dir=tmp_path,
        ocr_engine=_Failing(),
        failures=failures,
    )

    # A read that did not happen is null, never zero: zero would say the crop
    # was offered to the engine and the engine read nothing from it.
    assert entry["figure_ocr_regions_read"] is None
    assert entry["figure_ocr_text_char_count"] is None
    assert entry["figure_ocr_error"] is not None
    assert len(failures) == 1


# ----- the record must distinguish "not asked" from "asked, read nothing" ---


def test_a_page_entry_declines_its_figure_ocr_keys_before_the_pass() -> None:
    """The keys exist as null on a page whose regions were never read.

    The record's rule is that a reading which did not happen is never a zero,
    so a reader must not have to infer the difference from a missing key.
    """
    driver = _driver()
    rule_calls: list[dict[str, Any]] = []

    def _rule(*, text: str, image_count: int, min_text_chars: int) -> bool:
        rule_calls.append({"text": text})
        return False

    class _Page:
        def get_size(self) -> tuple[float, float]:
            return (612.0, 792.0)

        def get_textpage(self) -> Any:
            class _TextPage:
                def get_text_range(self) -> str:
                    return "Apple Inc. CONDENSED CONSOLIDATED"

            return _TextPage()

        def get_objects(self, *, filter: Any = None) -> list[Any]:  # noqa: A002
            class _Image:
                pass

            return [_Image()]

    entry = driver._page_structure_entry(_Page(), 3, _rule)

    assert entry["figure_ocr_regions_read"] is None
    assert entry["figure_ocr_text_char_count"] is None
    assert entry["figure_ocr_text_sample"] is None
    assert entry["figure_ocr_error"] is None
    assert entry["figure_ocr_skipped_reason"] is None


def test_a_page_with_no_figure_regions_reports_zero_reads_not_a_gap(
    tmp_path: Path,
) -> None:
    """A page with no figure region has a real answer, and it is zero.

    The figure rule ran, found nothing above the floor, and returned nothing to
    read. That is a measurement, not a missing one.
    """
    driver = _driver()
    engine = _Engine()
    entry: dict[str, object] = {
        "page_no": 2,
        "ocr_used": False,
        "ocr_text_char_count": 0,
        "ocr_required_by_repo_rule": False,
    }
    failures: list[str] = []

    driver._apply_figure_region_ocr(
        entry, [], _PAGE_PNG, page_no=2, dpi=200, image_dir=tmp_path,
        ocr_engine=engine, failures=failures,
    )

    assert failures == []
    assert engine.calls == []
    assert entry["figure_ocr_regions_read"] == 0
    assert entry["figure_ocr_text_char_count"] == 0
    assert entry["figure_ocr_error"] is None


def test_a_figure_box_the_raster_cannot_crop_is_named_not_ignored(
    tmp_path: Path,
) -> None:
    """A box with no readable edges is a gap in the record, not a silent skip."""
    driver = _driver()
    engine = _Engine()
    entry: dict[str, object] = {
        "page_no": 3,
        "ocr_used": False,
        "ocr_text_char_count": 0,
        "ocr_required_by_repo_rule": False,
    }
    failures: list[str] = []

    driver._apply_figure_region_ocr(
        entry,
        [{"l": None, "t": 1.0, "r": 2.0, "b": 3.0}],
        _PAGE_PNG,
        page_no=3,
        dpi=200,
        image_dir=tmp_path,
        ocr_engine=engine,
        failures=failures,
    )

    assert engine.calls == []
    assert len(failures) == 1
    assert entry["figure_ocr_regions_read"] is None
    assert entry["figure_ocr_text_char_count"] is None


if __name__ == "__main__":  # pragma: no cover - convenience for a focused run
    raise SystemExit(pytest.main([__file__]))
