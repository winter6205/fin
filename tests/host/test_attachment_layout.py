"""The Heron layout adapter's side of ``fin.corpus.parsers.PageLayoutPort``.

These tests pin what the attachment capability promises the page/region core:
real PDF points from the pinned container's record, and silence — never an
exception — when the container cannot answer. The last group is the load-bearing
one: a layout adapter that raises, or that forwards a box the media box cannot
hold, costs *every* physical page of the document, so these run the real core
over a committed gold PDF rather than over a stand-in.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path
from typing import Final

import pytest

from fin.corpus.errors import PageLayoutError
from fin.corpus.parsers import PageParseResult, PageRegionParser, Region
from fin.host.attachment_layout import (
    PASS_FAILED,
    PASS_MEASURED,
    PASS_PENDING,
    PASS_TIMED_OUT,
    PASS_UNAVAILABLE,
    HeronHarnessAbsentError,
    HeronHarnessAnswerError,
    HeronPassTimeoutError,
    HeronPageLayout,
    HeronRecordUnreadableError,
    HeronStagingRefusedError,
)

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
GOLD_MIXED: Final[Path] = REPO_ROOT / "data/corpus/g4_attachment_gold/g4-mixed-chart-001.pdf"
GOLD_RECORD: Final[Path] = (
    REPO_ROOT
    / "scripts/g4_pipeline/results/g4-mixed-chart-001_20260926T000242Z"
    / "g4-mixed-chart-001.measured.jsonl"
)

# 612 x 792 pt is this fixture's own media box, read with poppler when the gold
# set was authored (manifest ``page_size_pt``), not measured off Docling.
LETTER_PT: Final[tuple[float, float]] = (612.0, 792.0)
# Quoted verbatim from data/corpus/g4_attachment_gold/manifest.json, which the
# gold set was authored against and which fin's parser never contributed to.
GOLD_PAGE1_QUOTE: Final[str] = "SECURITIES AND EXCHANGE COMMISSION"
GOLD_PAGE2_QUOTE: Final[str] = "Total net sales 81,797 82,959 293,787 304,182"
GOLD_PAGE3_QUOTE: Final[str] = (
    "2023 Q2 (April 1, 2023) 94,836; 2023 Q3 (July 1, 2023) 81,797."
)


def _normalised(text: str) -> str:
    """The manifest's own matching form: runs of whitespace collapsed."""
    return " ".join(text.split())


class _StubRunner:
    """A layout pass that answers with a record shaped like the harness's own."""

    def __init__(self, record=None, *, error: BaseException | None = None) -> None:
        self._record = record
        self._error = error
        self.calls = 0

    def read_record(self, *, pdf_path: Path, config) -> dict:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._record or {"pages": []}


def _record(pages: list[dict]) -> dict:
    return {"schema": "fin.g4.t2a.pipeline_measurement/3", "pages": pages}


def _page(page_no: int, *, tables=(), figures=(), counts=None) -> dict:
    return {
        "page_no": page_no,
        "region_counts": counts or {"text": 0, "table": 0, "figure": 0},
        "table_boxes": list(tables),
        "figure_boxes": list(figures),
        "docling_page_size_pt": [612.0, 792.0],
    }


def _box(l: float, t: float, r: float, b: float) -> dict:  # noqa: E741 - the record's own key
    return {"l": l, "t": t, "r": r, "b": b}


class _StubOcr:
    """Recognizes a fixed string, so the figure-crop lane cannot abort a page."""

    def __init__(self, text: str = "recognized") -> None:
        self.text = text
        self.calls: list[str] = []

    def recognize_raster(self, *, png: bytes, reference: str) -> str:
        self.calls.append(reference)
        return self.text


# ---------------------------------------------------------------------------
# the port's own contract
# ---------------------------------------------------------------------------


def test_table_and_figure_boxes_become_regions_in_pdf_points():
    # Docling's record keeps its bbox in bottom-left origin, where ``t`` is the
    # higher edge, so the adapter is the place that flips it onto the core's
    # top-left space: a table spanning y 300..340 of a 792 pt page starts 452 pt
    # from the top, and the chart below it is read first.
    runner = _StubRunner(
        _record([_page(1, tables=[_box(50.0, 340.0, 500.0, 300.0)],
                         figures=[_box(60.0, 646.5, 553.0, 382.8)])])
    )
    layout = HeronPageLayout(pdf_path=GOLD_MIXED, runner=runner)

    regions = layout.page_regions(page_no=1, size_pt=LETTER_PT)

    assert [(r.kind, r.left_pt, r.top_pt, r.right_pt, r.bottom_pt) for r in regions] == [
        ("figure", 60.0, 145.5, 553.0, 409.2),
        ("table", 50.0, 452.0, 500.0, 492.0),
    ]
    assert all(isinstance(r, Region) for r in regions)


def test_regions_carry_no_text_because_the_core_owns_recognition():
    runner = _StubRunner(_record([_page(1, figures=[_box(1.0, 300.0, 90.0, 200.0)])]))

    regions = HeronPageLayout(pdf_path=GOLD_MIXED, runner=runner).page_regions(
        page_no=1, size_pt=LETTER_PT
    )

    assert [(r.text, r.ocr_applied) for r in regions] == [("", False)]


def test_record_order_is_not_the_answer_order():
    # The harness buckets tables and figures separately and in Docling's own
    # order; consumers crop pixels, so a stable reading order is the adapter's
    # job and must not depend on which bucket a region came from.
    runner = _StubRunner(
        _record(
            [
                _page(
                    1,
                    tables=[_box(10.0, 740.0, 300.0, 700.0)],
                    figures=[_box(10.0, 200.0, 300.0, 100.0)],
                )
            ]
        )
    )

    regions = HeronPageLayout(pdf_path=GOLD_MIXED, runner=runner).page_regions(
        page_no=1, size_pt=LETTER_PT
    )

    assert [r.top_pt for r in regions] == sorted(r.top_pt for r in regions)


def test_the_whole_document_pass_runs_once_and_serves_every_page():
    runner = _StubRunner(
        _record([_page(1, figures=[_box(1.0, 1.0, 100.0, 100.0)]), _page(2)])
    )
    layout = HeronPageLayout(pdf_path=GOLD_MIXED, runner=runner)

    for page_no in (1, 2, 3, 1):
        layout.page_regions(page_no=page_no, size_pt=LETTER_PT)

    assert runner.calls == 1


def test_a_page_the_record_never_answered_has_no_regions():
    runner = _StubRunner(_record([_page(1)]))
    layout = HeronPageLayout(pdf_path=GOLD_MIXED, runner=runner)

    assert layout.page_regions(page_no=9, size_pt=LETTER_PT) == ()
    assert layout.page_regions(page_no=1, size_pt=LETTER_PT) == ()


# ---------------------------------------------------------------------------
# the port's input boundary: a bad argument is answered, never raised
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "page_no",
    [0, -1, 1.5, "1", None, True],
)
def test_a_page_number_that_is_not_a_plain_int_answers_no_regions(page_no):
    runner = _StubRunner(_record([_page(1, figures=[_box(1.0, 1.0, 100.0, 100.0)])]))
    layout = HeronPageLayout(pdf_path=GOLD_MIXED, runner=runner)

    assert layout.page_regions(page_no=page_no, size_pt=LETTER_PT) == ()


@pytest.mark.parametrize(
    "size_pt",
    [
        None,
        (),
        (612.0,),
        (612.0, 792.0, 1.0),
        (0.0, 792.0),
        (-612.0, 792.0),
        (612.0, float("nan")),
        (612.0, float("inf")),
        ("612", "792"),
        {"width": 612.0, "height": 792.0},
    ],
)
def test_a_media_box_that_is_not_two_finite_positive_numbers_answers_no_regions(
    size_pt,
):
    runner = _StubRunner(_record([_page(1, figures=[_box(1.0, 1.0, 100.0, 100.0)])]))
    layout = HeronPageLayout(pdf_path=GOLD_MIXED, runner=runner)

    assert layout.page_regions(page_no=1, size_pt=size_pt) == ()


def test_a_pdf_path_that_is_not_a_path_is_refused_at_construction():
    for bad in ("", "not-a-path", None, 7, GOLD_MIXED.with_suffix(".txt")):
        with pytest.raises((TypeError, ValueError)):
            HeronPageLayout(pdf_path=bad, runner=_StubRunner())


# ---------------------------------------------------------------------------
# a malformed record is answered with nothing, never raised
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "record",
    [
        {},
        {"pages": None},
        {"pages": {}},
        {"pages": "not a list"},
        {"pages": [None]},
        {"pages": [{"page_no": 1, "figure_boxes": None, "table_boxes": None}]},
        {"pages": [_page(1, figures=[None])]},
        {"pages": [_page(1, figures=["figure"])]},
        {"pages": [_page(1, figures=[{"l": 1}])]},
        {"pages": [_page(1, figures=[{"l": "1", "t": "2", "r": "3", "b": "4"}])]},
        {"pages": [_page(1, figures=[{"l": 1.0, "t": float("nan"), "r": 9.0, "b": 9.0}])]},
        {"pages": [_page(1, figures=[_box(1.0, 792.0, 100.0, 792.0)])]},
        {"pages": [_page(1, figures=[_box(1.0, 700.0, 100.0, 900.0)])]},
        {"pages": [_page(1, figures=[_box(1.0, 1.0, 1.0, 100.0)])]},
        {"pages": [{"page_no": "1", "figure_boxes": [_box(1.0, 1.0, 10.0, 10.0)]}]},
        {"pages": [_page(1, counts={"text": "many"})]},
    ],
)
def test_a_record_that_does_not_match_the_harness_shape_answers_no_regions(record):
    layout = HeronPageLayout(pdf_path=GOLD_MIXED, runner=_StubRunner(record))

    assert layout.page_regions(page_no=1, size_pt=LETTER_PT) == ()


class _RawLayout:
    """A layout lane that forwards whatever boxes it was handed, unsanitized."""

    def __init__(self, *regions: Region) -> None:
        self._regions = regions

    def page_regions(self, *, page_no: int, size_pt) -> tuple[Region, ...]:
        return self._regions


def test_control_a_box_the_media_box_cannot_hold_costs_the_whole_document():
    # Why the adapter sanitizes instead of forwarding: the core names such a box
    # with PageLayoutError, and that error aborts extract_pdf -- so one bad box
    # on page 1 suppresses all three pages, regions and text layers alike.
    overhang = Region("figure", 1.0, 1.0, 100.0, 900.0)
    inverted = Region("figure", 1.0, 700.0, 100.0, 700.0)
    for box in (overhang, inverted):
        parser = PageRegionParser(layout=_RawLayout(box), ocr=_StubOcr())
        with pytest.raises(PageLayoutError):
            parser.extract_pdf(
                source_bytes=GOLD_MIXED.read_bytes(),
                source_sha256="0" * 64,
                document_id="g4-mixed-chart-001",
                version="1",
            )


def test_the_adapter_survives_a_record_whose_boxes_would_abort_the_parse():
    runner = _StubRunner(
        _record(
            [
                _page(
                    1,
                    tables=[_box(-5.0, 340.0, 700.0, 300.0)],
                    figures=[_box(1.0, 700.0, 100.0, 900.0), _box(10.0, 200.0, 300.0, 100.0)],
                )
            ]
        )
    )

    regions = HeronPageLayout(pdf_path=GOLD_MIXED, runner=runner).page_regions(
        page_no=1, size_pt=LETTER_PT
    )

    # Overhanging edges are clamped to the media box rather than dropped: the
    # region is real and only its edge is a rounding artifact. The first figure
    # box, whose recorded top edge sits above the page, is the one dropped --
    # flipping it would have put its bottom edge above its top.
    assert [(r.kind, r.left_pt, r.right_pt) for r in regions] == [
        ("table", 0.0, 612.0),
        ("figure", 10.0, 300.0),
    ]
    parser = PageRegionParser(layout=HeronPageLayout(pdf_path=GOLD_MIXED, runner=runner),
                              ocr=_StubOcr())
    result = parser.extract_pdf(
        source_bytes=GOLD_MIXED.read_bytes(),
        source_sha256="0" * 64,
        document_id="g4-mixed-chart-001",
        version="1",
        max_pages=1,
    )
    assert len(result.pages) == 1


@pytest.mark.parametrize(
    "error,state",
    [
        (OSError("docker socket refused"), PASS_UNAVAILABLE),
        (TimeoutError("harness exceeded its wall-clock cap"), PASS_TIMED_OUT),
        (RuntimeError("harness exited non-zero"), PASS_FAILED),
        (ValueError("record is not JSON"), PASS_FAILED),
    ],
)
def test_every_runner_failure_shape_reports_its_own_state_and_answers_nothing(
    error, state
):
    layout = HeronPageLayout(pdf_path=GOLD_MIXED, runner=_StubRunner(error=error))

    assert layout.page_regions(page_no=1, size_pt=LETTER_PT) == ()
    assert layout.pass_state == state
    assert state in layout.failure_detail


def test_a_failed_pass_is_not_retried_page_after_page():
    runner = _StubRunner(error=OSError("no image"))
    layout = HeronPageLayout(pdf_path=GOLD_MIXED, runner=runner)

    for page_no in (1, 2, 3, 4, 5):
        assert layout.page_regions(page_no=page_no, size_pt=LETTER_PT) == ()

    assert runner.calls == 1


# ---------------------------------------------------------------------------
# the invariant: regions are additive, so no page is ever allowed to disappear
# ---------------------------------------------------------------------------


_QUIET_LANE_RUNNERS = [
    pytest.param(_StubRunner(error=FileNotFoundError("harness not installed")),
                 id="unavailable"),
    pytest.param(_StubRunner(error=TimeoutError("cap")), id="timeout"),
    pytest.param(_StubRunner(_record([])), id="empty-record"),
    pytest.param(_StubRunner(_record([_page(1), _page(2), _page(3)])),
                 id="zero-regions"),
    pytest.param(_StubRunner({"pages": [{"page_no": 1, "oops": 1}]}),
                 id="garbage-record"),
]


def _quiet_lane_result(runner) -> PageParseResult:
    """Extract the mixed gold PDF with a layout lane that answers nothing."""
    parser = PageRegionParser(
        layout=HeronPageLayout(pdf_path=GOLD_MIXED, runner=runner), ocr=_StubOcr()
    )
    return parser.extract_pdf(
        source_bytes=GOLD_MIXED.read_bytes(),
        source_sha256="0" * 64,
        document_id="g4-mixed-chart-001",
        version="1",
    )


@pytest.mark.parametrize("runner", _QUIET_LANE_RUNNERS)
def test_the_core_still_emits_every_page_when_the_layout_lane_answers_nothing(
    runner,
):
    result = _quiet_lane_result(runner)
    assert [p.page_no for p in result.pages] == [1, 2, 3]
    assert [p.page_count for p in result.pages] == [3, 3, 3]
    # Nothing was suppressed and nothing was invented: no regions, every page.
    assert [p.regions for p in result.pages] == [(), (), ()]
    assert len(result.frozen_units()) == 3


@pytest.mark.parametrize("runner", _QUIET_LANE_RUNNERS)
def test_every_page_keeps_its_raster_when_the_layout_lane_answers_nothing(
    runner,
):
    result = _quiet_lane_result(runner)
    for page in result.pages:
        assert page.raster.png.startswith(b"\x89PNG")
        assert page.raster.width_px > 0 and page.raster.height_px > 0


@pytest.mark.parametrize("runner", _QUIET_LANE_RUNNERS)
def test_every_page_keeps_its_native_text_when_the_layout_lane_answers_nothing(
    runner,
):
    result = _quiet_lane_result(runner)
    # The gold manifest's own quotes, matched the way it says to match them
    # (whitespace-normalised containment): what a citation is quoted from is the
    # text layer, and a layout lane that answered nothing cannot touch it.
    assert GOLD_PAGE1_QUOTE in _normalised(result.pages[0].native_text)
    assert GOLD_PAGE2_QUOTE in _normalised(result.pages[1].native_text)
    assert GOLD_PAGE3_QUOTE in _normalised(result.pages[2].native_text)


def test_the_gold_record_through_the_adapter_keeps_every_page_and_its_regions():
    record = json.loads(GOLD_RECORD.read_text(encoding="utf-8"))
    runner = _StubRunner(record)
    parser = PageRegionParser(
        layout=HeronPageLayout(pdf_path=GOLD_MIXED, runner=runner), ocr=_StubOcr()
    )

    result = parser.extract_pdf(
        source_bytes=GOLD_MIXED.read_bytes(),
        source_sha256="0" * 64,
        document_id="g4-mixed-chart-001",
        version="1",
    )

    assert len(result.pages) == 3
    kinds = [[r.kind for r in p.regions] for p in result.pages]
    assert kinds == [["figure"], [], ["figure"]]
    assert all(p.raster.png for p in result.pages)


def test_the_committed_heron_boxes_land_inside_the_media_box_and_where_the_manifest_says():
    # Independent expectation: the manifest says page 1's only non-text region is
    # the Apple logotype, displayed at 33 x 40 pt and "horizontally centred near
    # the vertical middle" of a 612 x 792 page. A box that flips into the wrong
    # origin lands in the top or bottom band and fails this.
    record = json.loads(GOLD_RECORD.read_text(encoding="utf-8"))
    layout = HeronPageLayout(pdf_path=GOLD_MIXED, runner=_StubRunner(record))

    regions = layout.page_regions(page_no=1, size_pt=LETTER_PT)

    assert len(regions) == 1
    box = regions[0]
    width, height = box.size_pt
    assert 30.0 <= width <= 36.0
    assert 37.0 <= height <= 44.0
    assert abs((box.left_pt + box.right_pt) / 2 - 306.0) <= 6.0
    assert 320.0 <= box.top_pt <= 470.0
    assert 0.0 <= box.left_pt and box.right_pt <= 613.0
    assert 0.0 <= box.top_pt and box.bottom_pt <= 793.0


def test_the_adapter_reads_the_record_it_was_handed_and_not_a_second_engine():
    record = json.loads(GOLD_RECORD.read_text(encoding="utf-8"))
    layout = HeronPageLayout(pdf_path=GOLD_MIXED, runner=_StubRunner(record))

    total = sum(
        len(layout.page_regions(page_no=p, size_pt=LETTER_PT)) for p in (1, 2, 3)
    )

    assert total == 2


class _BlockingRunner(_StubRunner):
    """A pass that stays inside the container boundary until the test lets go."""

    def __init__(self, record=None) -> None:
        super().__init__(record)
        self.inside = threading.Event()
        self.release = threading.Event()

    def read_record(self, *, pdf_path: Path, config) -> dict:
        self.calls += 1
        self.inside.set()
        assert self.release.wait(timeout=10), "the pass was never released"
        return self._record or {"pages": []}


def test_pages_asked_while_one_is_crossing_the_boundary_share_that_crossing():
    # Preparation can reach one document from more than one worker, and the
    # container cold start dominates this lane, so a second crossing is a defect.
    # The three late pages are started only once the first is provably inside
    # the pass, so this fails loudly rather than flakily if the lock is dropped.
    runner = _BlockingRunner(
        _record([_page(1, figures=[_box(1.0, 100.0, 50.0, 50.0)])])
    )
    layout = HeronPageLayout(pdf_path=GOLD_MIXED, runner=runner)
    answers: list[tuple[Region, ...]] = []

    def ask() -> None:
        answers.append(layout.page_regions(page_no=1, size_pt=LETTER_PT))

    first = threading.Thread(target=ask)
    first.start()
    assert runner.inside.wait(timeout=10), "no page ever crossed the boundary"
    late = [threading.Thread(target=ask) for _ in range(3)]
    for thread in late:
        thread.start()
    runner.release.set()
    first.join(timeout=20)
    for thread in late:
        thread.join(timeout=20)

    assert not any(thread.is_alive() for thread in [first, *late])
    assert runner.calls == 1
    assert len(answers) == 4 and all(answers)


def test_the_harness_box_bound_is_reported_rather_than_hidden():
    # The record caps coordinates per page and kind so its single JSON line
    # stays bounded while its counts stay whole, so a caller has to be able to
    # see that the boxes it read are a prefix.
    entry = _page(1, figures=[_box(1.0, 100.0, 50.0, 50.0)], counts={"figure": 41})
    entry["figure_boxes_capped"] = True
    layout = HeronPageLayout(pdf_path=GOLD_MIXED, runner=_StubRunner(_record([entry])))

    assert len(layout.page_regions(page_no=1, size_pt=LETTER_PT)) == 1
    assert layout.capped_pages == frozenset({1})


def test_a_capped_box_bound_is_unknown_rather_than_absent_before_the_pass():
    # An empty set is the pass's answer that no page was truncated; before the
    # pass there is no answer at all, and a caller that read the two alike would
    # trust "nothing capped" out of a document it never measured.
    layout = HeronPageLayout(pdf_path=GOLD_MIXED, runner=_StubRunner(_record([])))

    assert layout.capped_pages is None
    assert layout.pass_state == PASS_PENDING
    layout.page_regions(page_no=1, size_pt=LETTER_PT)
    assert layout.capped_pages == frozenset()


def test_a_record_that_names_no_regions_answers_as_measured_not_as_broken():
    # "The model found none" and "the engine never answered" are different facts,
    # and only the second one carries a failure detail.
    layout = HeronPageLayout(pdf_path=GOLD_MIXED, runner=_StubRunner(_record([])))

    assert layout.page_regions(page_no=1, size_pt=LETTER_PT) == ()
    assert layout.pass_state == PASS_MEASURED
    assert layout.failure_detail is None
    assert layout.capped_pages == frozenset()


def test_a_capped_pass_is_asked_again_instead_of_becoming_the_documents_answer():
    # The cap is the one failure that reported nothing about the document, and on
    # a busy host it is the expected case: caching it as terminal would strand
    # every page of a filing whose only sin was that the container was slow.
    class _TimeoutRunner:
        def __init__(self) -> None:
            self.calls = 0

        def read_record(self, *, pdf_path: Path, config) -> dict:
            self.calls += 1
            raise HeronPassTimeoutError(subprocess.TimeoutExpired(cmd=["bash"], timeout=1.0))

    runner = _TimeoutRunner()
    layout = HeronPageLayout(pdf_path=GOLD_MIXED, runner=runner)

    assert layout.page_regions(page_no=1, size_pt=LETTER_PT) == ()
    assert layout.pass_state == PASS_TIMED_OUT
    assert runner.calls == 1

    assert layout.page_regions(page_no=2, size_pt=LETTER_PT) == ()
    assert runner.calls == 2
    assert layout.pass_state == PASS_TIMED_OUT
    assert "heron_pass_timeout" in (layout.failure_detail or "")


def test_a_pass_that_answered_is_never_asked_again_by_a_later_page():
    # The retry above is bounded to the one shape that has no answer in it: a
    # state that named a real failure is cached, or a 72-page filing would pay
    # 72 cold starts for an image that is not installed.
    for state_error in (
        HeronHarnessAbsentError(
            Path("/nonexistent/checkout/scripts/g4_pipeline/run_measured.sh")
        ),
        HeronHarnessAnswerError("layout harness exit=1: no image"),
        HeronRecordUnreadableError("out.jsonl", "no per-page list"),
        HeronStagingRefusedError("staging dir is outside the harness mount"),
    ):
        runner = _StubRunner(error=state_error)
        layout = HeronPageLayout(pdf_path=GOLD_MIXED, runner=runner)

        for page_no in (1, 2, 3, 4, 5):
            assert layout.page_regions(page_no=page_no, size_pt=LETTER_PT) == ()

        assert runner.calls == 1, state_error.code
        assert state_error.code in (layout.failure_detail or "")


def test_reading_the_pass_state_does_not_wait_behind_the_container():
    # The state and the box bound are what a caller polls while a page is being
    # prepared, so a read must not queue behind the very pass it is asking about.
    runner = _BlockingRunner(_record([_page(1, figures=[_box(1.0, 100.0, 50.0, 50.0)])]))
    layout = HeronPageLayout(pdf_path=GOLD_MIXED, runner=runner)
    first = threading.Thread(
        target=lambda: layout.page_regions(page_no=1, size_pt=LETTER_PT)
    )
    first.start()
    assert runner.inside.wait(timeout=10), "no page ever crossed the boundary"

    started = time.monotonic()
    assert layout.pass_state == PASS_PENDING
    assert layout.capped_pages is None
    assert layout.failure_detail is None
    waited = time.monotonic() - started

    runner.release.set()
    first.join(timeout=20)
    assert waited < 1.0, f"a state read blocked {waited:.3f}s behind the pass"


def test_a_page_that_cannot_be_answered_leaves_the_pass_state_alone():
    # The container did report and the record did index; only this box's
    # arithmetic failed. Collapsing the two states would erase the difference a
    # caller has to act on, and a retry-per-page would pay for the cold start
    # once per page.
    class _HostileBox(dict):
        def get(self, *args, **kwargs):
            raise RuntimeError("a box that misbehaves on read")

    runner = _StubRunner(_record([_page(1, figures=[_HostileBox()])]))
    layout = HeronPageLayout(pdf_path=GOLD_MIXED, runner=runner)

    assert layout.page_regions(page_no=1, size_pt=LETTER_PT) == ()
    assert layout.pass_state == PASS_MEASURED
    assert "a box that misbehaves on read" in (layout.answer_failure_detail or "")
    assert runner.calls == 1


def test_a_record_that_cannot_be_indexed_is_one_failure_and_not_one_per_page():
    runner = _StubRunner({"pages": _Unindexable()})
    layout = HeronPageLayout(pdf_path=GOLD_MIXED, runner=runner)

    for page_no in (1, 2, 3):
        assert layout.page_regions(page_no=page_no, size_pt=LETTER_PT) == ()

    assert runner.calls == 1
    assert layout.pass_state == PASS_FAILED


def test_a_shape_that_extends_a_named_one_reports_that_shape_and_not_a_crash():
    # The state table is keyed by the declared shapes; a subclass reached it as a
    # KeyError, and because that raise happens inside the failure path it left the
    # pass ``pending`` -- so every later page paid for the cold start again and the
    # one-crossing-per-document promise was silently void.
    class _Derived(HeronHarnessAnswerError):
        pass

    for page_no in (1, 2, 3):
        runner = _StubRunner(error=_Derived("layout harness exit=1: no image"))
        layout = HeronPageLayout(pdf_path=GOLD_MIXED, runner=runner)

        assert layout.page_regions(page_no=page_no, size_pt=LETTER_PT) == ()
        assert layout.pass_state == PASS_FAILED
        assert runner.calls == 1, "a second page crossed the boundary again"


class _Unindexable(list):
    def __iter__(self):
        raise RuntimeError("a page list that cannot be walked")
