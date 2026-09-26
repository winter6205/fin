"""The pinned-pipeline driver's gap contract: a reading that did not happen.

``scripts/g4_pipeline/driver.py`` states its own rule at its head — a failed
measurement is never a zero — and the budget decision is priced from the
quantities it emits. These tests hold that rule at the four places a failure
could still be written as a value: an unreadable image-object count feeding the
repo OCR rule, a failed OCR pass, a Docling export whose attributes moved, and
an absent artifact cache.

The driver is a script that normally runs inside the pinned image, so it is
loaded by path and only its pure functions are called here. Every collaborator
is a local double that raises or withholds exactly one reading.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DRIVER_PATH = PROJECT_ROOT / "scripts" / "g4_pipeline" / "driver.py"


def _driver() -> ModuleType:
    spec = importlib.util.spec_from_file_location("g4_pipeline_driver", DRIVER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _TextPage:
    def __init__(self, text: str) -> None:
        self._text = text

    def get_text_range(self) -> str:
        return self._text


class _Page:
    """A physical page whose object scan either answers or fails."""

    def __init__(self, text: str, *, objects_error: Exception | None) -> None:
        self._text = text
        self._objects_error = objects_error

    def get_size(self) -> tuple[float, float]:
        return (612.0, 792.0)

    def get_textpage(self) -> _TextPage:
        return _TextPage(self._text)

    def get_objects(self, *, filter: Any = None) -> list[Any]:  # noqa: A002
        if self._objects_error is not None:
            raise self._objects_error
        return []


class _Rule:
    """Records that the repo rule was asked, so "never asked" is observable."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, *, text: str, image_count: int, min_text_chars: int) -> bool:
        self.calls.append(
            {"text": text, "image_count": image_count, "min_text_chars": min_text_chars}
        )
        return image_count > 0 and len(text) < min_text_chars


class _Engine:
    lang = "chi_sim+eng"

    def __init__(self, *, fail: bool) -> None:
        self._fail = fail

    def recognize(self, *, source: Path, page: int) -> str:
        if self._fail:
            raise RuntimeError("tesseract exited 1")
        return "text"


class _Bitmap:
    """Enough of a PIL image for the driver's own temp-bitmap path."""

    def save(self, path: Any, format: str = "PNG") -> None:  # noqa: A002
        Path(path).write_bytes(b"\x89PNG\r\n\x1a\n")


class _Size:
    """The pinned ``docling_core...base.Size``: exactly ``width`` and ``height``."""

    def __init__(self, width: float, height: float) -> None:
        self.width = width
        self.height = height


class _PageItem:
    """A pinned ``PageItem``, whose model fields are ``page_no`` / ``size`` / ``image``."""

    def __init__(self, page_no: int, *, with_size: bool = True) -> None:
        self.page_no = page_no
        self.size = _Size(612.0, 792.0) if with_size else None


class _Doc:
    """A Docling export with the named collections, or one whose schema moved."""

    def __init__(
        self,
        *,
        pages: int = 0,
        with_regions: bool = True,
        with_sizes: bool = True,
    ) -> None:
        # ``DoclingDocument.pages`` is ``dict[int, PageItem]`` in the pin, which
        # the driver's page-size read has to iterate by value.
        self.pages: dict[int, _PageItem] = {
            page_no: _PageItem(page_no, with_size=with_sizes)
            for page_no in range(1, pages + 1)
        }
        if not with_regions:
            return
        self.texts: list[Any] = []
        self.tables: list[Any] = []
        self.pictures: list[Any] = []


class _Result:
    status = "SUCCESS"

    def __init__(self, doc: _Doc) -> None:
        self.document = doc


class _Converter:
    def __init__(self, doc: _Doc) -> None:
        self._doc = doc

    def convert(self, path: str) -> _Result:
        return _Result(self._doc)


# ----- an image-object count that could not be read -------------------------


def test_unreadable_image_count_leaves_the_ocr_rule_unanswered() -> None:
    """A failed scan may not select or clear OCR through a fabricated count.

    ``_page_image_objects`` promises ``(None, reason)`` is "not 'no images' and
    must not select the OCR rule", so the rule field has to go null and the rule
    must not be called at all.
    """
    driver = _driver()
    rule = _Rule()
    page = _Page("", objects_error=RuntimeError("no object list"))

    entry = driver._page_structure_entry(page, 1, rule)

    assert entry["pdf_image_objects"] is None
    assert entry["pdf_image_objects_error"]
    assert entry["ocr_required_by_repo_rule"] is None
    assert rule.calls == []


def test_readable_image_count_still_asks_the_rule() -> None:
    driver = _driver()
    rule = _Rule()
    page = _Page("", objects_error=None)

    entry = driver._page_structure_entry(page, 1, rule)

    assert entry["pdf_image_objects"] == 0
    assert entry["ocr_required_by_repo_rule"] is False
    assert rule.calls == [{"text": "", "image_count": 0, "min_text_chars": 1}]


# ----- an OCR pass that failed ------------------------------------------------


def test_failed_ocr_records_no_character_count_rather_than_zero() -> None:
    """A page whose OCR raised has no measured OCR text, which is not zero.

    ``ocr_text_chars_total`` prices the scanned-page cost the page cap rests
    on, so a failure summed as "no text" would read as a cheap page.
    """
    driver = _driver()
    entry: dict[str, object] = {}
    failures: list[str] = []

    driver._apply_page_ocr(
        entry,
        _Bitmap(),
        1,
        _Engine(fail=True),
        "repo_rule",
        None,
        failures,
    )

    assert entry["ocr_used"] is False
    assert entry["ocr_error"]
    assert entry["ocr_text_char_count"] is None


def test_a_failed_ocr_keeps_the_file_total_out_of_the_sum() -> None:
    driver = _driver()
    pages = [
        {"ocr_required_by_repo_rule": True, "ocr_used": True,
         "native_text_char_count": 0, "ocr_text_char_count": None},
        {"ocr_required_by_repo_rule": True, "ocr_used": False,
         "native_text_char_count": 0, "ocr_text_char_count": 12},
    ]

    assert driver.page_rule_counts(pages)["ocr_text_chars_total"] is None


def test_a_rule_gap_keeps_the_page_count_out_of_the_sum() -> None:
    driver = _driver()
    pages = [
        {"ocr_required_by_repo_rule": None, "ocr_used": False,
         "native_text_char_count": 5, "ocr_text_char_count": 0},
        {"ocr_required_by_repo_rule": True, "ocr_used": True,
         "native_text_char_count": 0, "ocr_text_char_count": 7},
    ]

    counts = driver.page_rule_counts(pages)
    assert counts["pages_needing_ocr"] is None
    assert counts["native_text_chars_total"] == 5


# ----- a Docling export whose attributes moved --------------------------------


def test_missing_docling_collections_fail_the_region_pass(tmp_path: Path) -> None:
    """Zero regions must mean Docling found none, not that the read moved.

    A defaulted attribute would leave ``docling_error`` null and
    ``regions_measured`` true, so ``regions_total`` would certify ``0/0/0`` for
    a document the pass never actually read.
    """
    driver = _driver()

    passed = driver.run_docling_pass(_Converter(_Doc()), tmp_path / "x.pdf", [])
    assert passed.error is None
    assert passed.regions_measured is True

    broken = driver.run_docling_pass(
        _Converter(_Doc(with_regions=False)), tmp_path / "x.pdf", []
    )
    assert broken.error is not None
    assert broken.regions_measured is False
    assert broken.status is None
    assert broken.page_count is None
    assert broken.regions == {}


def test_page_sizes_are_read_from_the_pages_mapping(tmp_path: Path) -> None:
    """``pages`` is a mapping keyed by page number, so the items are its values.

    Iterating the mapping itself yields ``int`` page numbers, which carry no
    ``size``: the pass would then report a successful conversion that answered
    nothing, and the render-cost evidence the page cap rests on would be absent
    from every committed record.
    """
    driver = _driver()

    passed = driver.run_docling_pass(
        _Converter(_Doc(pages=2)), tmp_path / "x.pdf", []
    )

    assert passed.page_count == 2
    assert passed.page_sizes_pt == {1: [612.0, 792.0], 2: [612.0, 792.0]}


def test_an_unanswered_size_lane_is_named_rather_than_left_null(
    tmp_path: Path,
) -> None:
    """Per-page nulls need the record to say which lane failed to read.

    ``docling_page_size_pt`` is null here while ``docling_page_count`` answered
    3, which is exactly the shape the driver's own gap rule forbids: a reading
    that never happened is a gap, and a gap has to be named.
    """
    driver = _driver()
    passed = driver.run_docling_pass(
        _Converter(_Doc(pages=3, with_sizes=False)), tmp_path / "x.pdf", []
    )
    pages: list[dict[str, object]] = [{"page_no": n} for n in (1, 2, 3)]
    failures: list[str] = []

    driver.attach_regions(pages, passed, failures)

    assert [entry["docling_page_size_pt"] for entry in pages] == [None, None, None]
    assert len(failures) == 1
    assert "docling_page_size_pt" in failures[0]
    assert "3" in failures[0]


def test_a_fully_answered_size_lane_names_no_gap(tmp_path: Path) -> None:
    driver = _driver()
    passed = driver.run_docling_pass(
        _Converter(_Doc(pages=2)), tmp_path / "x.pdf", []
    )
    pages: list[dict[str, object]] = [{"page_no": n} for n in (1, 2)]
    failures: list[str] = []

    driver.attach_regions(pages, passed, failures)

    assert [entry["docling_page_size_pt"] for entry in pages] == [
        [612.0, 792.0],
        [612.0, 792.0],
    ]
    assert failures == []


def test_a_failed_docling_pass_names_its_failure_once(tmp_path: Path) -> None:
    """The pass error is already the gap; the size lane adds no duplicate.

    Two entries for one failure would read as two independent breaks in the
    harness, and every page is null here because nothing was measured at all.
    """
    driver = _driver()
    passed = driver.run_docling_pass(
        _Converter(_Doc(with_regions=False)), tmp_path / "x.pdf", []
    )
    pages: list[dict[str, object]] = [{"page_no": 1}]
    failures: list[str] = []

    driver.attach_regions(pages, passed, failures)

    assert pages[0]["docling_page_size_pt"] is None
    assert failures == []


def test_absent_artifact_cache_is_a_gap_not_a_zero(tmp_path: Path) -> None:
    driver = _driver()

    info = driver.artifact_identity(tmp_path / "never-mounted")

    assert info["exists"] is False
    assert info["cache_total_bytes"] is None
    assert info["file_count"] is None
