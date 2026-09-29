"""The composition root's page lane: wiring facts and the per-document glue.

Why this file exists
--------------------
T2d shipped the page/region core and the Heron adapter but left
``_build_default_attachment_service`` unwired on purpose; this file covers
what the wiring itself owes:

* the deployed-state facts of the root builder — which config reads are
  boot facts (bounds, malformed Heron knobs) and which absences stay legal
  (every optional Heron knob, the harness, the image, the credential), so
  booting the lane never requires a container, an OCR binary, or a model;
* the composition root's own adapter, ``HeronPageLaneParser``: the core
  binds its layout once while ``HeronPageLayout`` caches one pass for one
  file, so the adapter stages a private copy per PDF, and that copy — one
  thread's filing — must not outlive the call that wrote it;
* the run-pricing seam the lane's preparations charge through: one
  injected ``VisionCallLedger`` spans every preparation of a run, while
  the default keeps each preparation on its own fresh ledger.

Hermetic by construction: the runner seam is a fake, so nothing here ever
spawns the harness, and the vision client is only inspected as an
unconstructed object, never invoked.
"""

from __future__ import annotations

import hashlib
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from fin.corpus.errors import UnsupportedFormatParseError
from fin.corpus.object_store import InMemoryObjectStore
from fin.corpus.ocr import OcrRequiredUnavailableError
from fin.corpus.parsers import (
    NullLayout,
    PageRaster,
    PageRegionParser,
)
from fin.host.attachment_budgets import (
    ATTACHMENT_VISION_BUDGET_EXCEEDED,
    AttachmentBudgetConfigError,
    AttachmentBudgets,
    VisionCallLedger,
    required_run_calls,
)
from fin.host.attachment_layout import (
    DEFAULT_STAGING_DIR,
    HeronHarnessAbsentError,
    HeronLayoutConfig,
    HeronLayoutConfigError,
)
from fin.host.attachment_prepare import (
    ATTACHMENT_PAGE_LANE_UNWIRED,
    AttachmentPreparationService,
)
from fin.host.attachment_store import (
    AttachmentRecord,
    InMemoryAttachmentStore,
)
from fin.host.attachment_vision import (
    ChatModelVisionAdapter,
    VisionCallResult,
    VisionUsage,
)
from fin.host.thread_store import InMemoryThreadStore
from fin.host.workspace_http import (
    HeronPageLaneParser,
    _build_default_attachment_service,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
GOLD_DIR = REPO_ROOT / "data" / "corpus" / "g4_attachment_gold"
NATIVE_PDF = GOLD_DIR / "g4-native-text-001.pdf"
NATIVE_BYTES = NATIVE_PDF.read_bytes()
CHART_PNG = GOLD_DIR / "g4-image-chart-001.png"
CHART_BYTES = CHART_PNG.read_bytes()

# Every store the root picks up from the environment: scrubbed, the builder
# takes its documented in-memory degradation instead of opening a pool.
_ROOT_ENV = (
    "FIN_CHECKPOINT_URL",
    "FIN_OBJECT_STORE_URL",
    "FIN_OBJECT_STORE_ENDPOINT",
    "FIN_OBJECT_STORE_BUCKET",
)

# The six Heron knobs, all optional: scrubbed, the builder takes the
# shipped defaults named in ``.env.example``.
_HERON_ENV = (
    "FIN_ATTACHMENT_HERON_SCRIPT",
    "FIN_ATTACHMENT_HERON_STAGING_DIR",
    "FIN_ATTACHMENT_HERON_TIMEOUT_S",
    "FIN_ATTACHMENT_HERON_IMAGE",
    "FIN_ATTACHMENT_HERON_MODEL_CACHE",
    "FIN_ATTACHMENT_HERON_SLICE_PASS",
)

# Test fixtures, not the deployed numbers; the run total is derived through
# the module that prices a run so this set cannot drift from the
# page + crop + retry-reserve relationship the boot check enforces.
_FIXTURE_BOUNDS = {
    "FIN_ATTACHMENT_MAX_BYTES": "1048576",
    "FIN_ATTACHMENT_MAX_PDF_PAGES": "8",
    "FIN_ATTACHMENT_VISION_CALLS_PER_RUN": str(
        required_run_calls(
            AttachmentBudgets(
                max_upload_bytes=1_048_576,
                max_pdf_pages=8,
                max_vision_calls_per_run=17,
                max_vision_crop_calls_per_run=8,
            )
        )
    ),
    "FIN_ATTACHMENT_VISION_CROP_CALLS_PER_RUN": "8",
}


class RecordingRunner:
    """``LayoutRecordRunner`` witnessing the path each document was staged at."""

    def __init__(
        self,
        *,
        fail_with: Exception | None = None,
        record: Mapping[str, object] | None = None,
    ) -> None:
        self.calls: list[tuple[Path, bool, str]] = []
        self._fail_with = fail_with
        # The record a successful pass answers with; ``None`` keeps the
        # original empty answer, which the region tests replace with the
        # one page's boxes the geometry under test needs.
        self._record = record

    def read_record(
        self, *, pdf_path: Path, config: HeronLayoutConfig
    ) -> Mapping[str, object]:
        # Witnessed from inside the call: the staged file must exist and
        # must hold exactly the bytes the upload carried.
        exists = pdf_path.is_file()
        digest = (
            hashlib.sha256(pdf_path.read_bytes()).hexdigest() if exists else ""
        )
        self.calls.append((pdf_path, exists, digest))
        if self._fail_with is not None:
            raise self._fail_with
        return dict(self._record) if self._record is not None else {}


@pytest.fixture
def clean_root_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scrubbed stores and Heron knobs; the four bounds deployed as fixtures."""
    for key in _ROOT_ENV + _HERON_ENV:
        monkeypatch.delenv(key, raising=False)
    for key, value in _FIXTURE_BOUNDS.items():
        monkeypatch.setenv(key, value)
    import fin.host.http as http_module

    monkeypatch.setattr(
        http_module, "_http_attachment_budgets_singleton", None, raising=False
    )


# ----- the boot facts of the wired root -----


def test_the_root_boots_with_the_page_lane_deployed(
    clean_root_env: None,
) -> None:
    """A builder that succeeds has a lane: bounds, a pages port, a vision port.

    Absent optional config is not an opt-out: every Heron knob unset still
    boots the lane on its shipped defaults, because with no harness image
    the per-document pass answers its own named state and the document
    keeps its native-text, render, OCR, and scan path.
    """
    service = _build_default_attachment_service()

    assert isinstance(service, AttachmentPreparationService)
    assert service.budgets is not None
    assert isinstance(service.pages, HeronPageLaneParser)
    assert isinstance(service.vision, ChatModelVisionAdapter)
    assert service.pages.heron_config == HeronLayoutConfig.from_environ(
        environ={}
    )
    assert service.pages.heron_config.staging_dir == DEFAULT_STAGING_DIR


def test_booting_the_lane_touches_no_container_no_binary_no_provider(
    clean_root_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deployment is a config fact, not a capability probe.

    A subprocess spawn, a pytesseract import that succeeds, or an HTTP
    client at boot would each make every unrelated request depend on
    machinery the lane only needs at preparation; the builder must not
    cross any of those boundaries.
    """
    import subprocess

    def _no_spawn(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("booting the page lane must not spawn anything")

    monkeypatch.setattr(subprocess, "Popen", _no_spawn)
    monkeypatch.setattr(subprocess, "run", _no_spawn)

    service = _build_default_attachment_service()

    assert service.pages is not None
    # The vision client is built lazily on the first scan, never here.
    assert service.vision._client is None


def test_a_malformed_heron_knob_fails_the_build_rather_than_guessing(
    clean_root_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The template's own promise, now a boot fact.

    A timeout spelled ``1e3`` is a confusable cap; falling back to the
    default would turn an operator's typo into an unbounded wait nobody
    configured, so the build names it and stops.
    """
    monkeypatch.setenv("FIN_ATTACHMENT_HERON_TIMEOUT_S", "1e3")

    with pytest.raises(HeronLayoutConfigError):
        _build_default_attachment_service()


def test_a_missing_bound_refuses_the_build_before_any_door_is_served(
    clean_root_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bounds are the boot singleton's read, so the root shares its gate.

    A page lane without a stated page cap could only parse against a
    guessed bound, which is the one thing the lane's pricing forbids.
    """
    monkeypatch.delenv("FIN_ATTACHMENT_MAX_PDF_PAGES")

    with pytest.raises(AttachmentBudgetConfigError):
        _build_default_attachment_service()


def test_a_deployment_declaring_no_bounds_builds_the_unwired_text_door(
    clean_root_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate's nothing-declared branch: no opt-in, no crash, no page lane.

    With none of the four ``FIN_ATTACHMENT_*`` keys set the root ships
    T2d's shipped text-only door: text keeps working and a page document
    answers the pinned unwired code, because a lane that declared no bound
    has nothing to price pages against. The key names come from the module
    that owns them, so this branch cannot drift from what the gate reads.
    """
    from fin.host.attachment_budgets import BOUND_ENV_KEYS
    from fin.host.thread_store import default_thread_store

    for key, _ in BOUND_ENV_KEYS:
        monkeypatch.delenv(key)

    service = _build_default_attachment_service()

    assert service.budgets is None
    assert service.pages is None

    thread = default_thread_store().create_thread(title="unwired root")
    text_ready = service.prepare(
        service.accept(
            thread_id=thread.id,
            filename="notes.txt",
            payload=b"quarterly revenue rose 12%\n",
        ).attachment_id
    )
    assert text_ready.status == "ready", text_ready

    page_failed = service.prepare(
        service.accept(
            thread_id=thread.id,
            filename="filing.pdf",
            payload=NATIVE_BYTES,
        ).attachment_id
    )
    assert page_failed.status == "failed", page_failed
    assert page_failed.failure_reason == ATTACHMENT_PAGE_LANE_UNWIRED


def test_the_lane_serves_a_text_document_without_the_byte_store(
    clean_root_env: None,
) -> None:
    """The documented in-memory degradation survives the wiring.

    With no object store configured the root builds anyway (logged
    degradation) and a plain text document still reaches ``ready`` — the
    lane is additive to the ladder the door always owned, not a new
    precondition for it.
    """
    from fin.host.thread_store import default_thread_store

    service = _build_default_attachment_service()

    thread = default_thread_store().create_thread(title="wired root text")
    accepted = service.accept(
        thread_id=thread.id,
        filename="notes.txt",
        payload=b"quarterly revenue rose 12%\n",
    )
    ready = service.prepare(accepted.attachment_id)

    assert ready.status == "ready", ready
    assert [unit.text for unit in service.units(ready.attachment_id)] == [
        "quarterly revenue rose 12%\n"
    ]


# ----- the composition root's own adapter -----


def _parser(runner: RecordingRunner) -> HeronPageLaneParser:
    return HeronPageLaneParser(
        config=HeronLayoutConfig.from_environ(environ={}), runner=runner
    )


def test_one_pdf_earns_one_staged_copy_holding_its_own_bytes() -> None:
    """The pass is whole-document and the file is per-document.

    ``HeronPageLayout`` caches its pass for one path, so the adapter may
    not share a staged copy across documents; and a private filing must
    not outlive the preparation that wrote it, so the copy is gone the
    moment the extract returns.
    """
    runner = RecordingRunner()
    parser = _parser(runner)
    digest = hashlib.sha256(NATIVE_BYTES).hexdigest()

    result = parser.extract_pdf(
        source_bytes=NATIVE_BYTES,
        source_sha256=digest,
        document_id="doc-a",
        version="1",
    )

    staged_paths = [call[0] for call in runner.calls]
    assert all(exists for _, exists, _ in runner.calls)
    assert runner.calls[0][2] == digest
    assert all(path.suffix == ".pdf" for path in staged_paths)
    assert all(not path.exists() for path in staged_paths)
    assert result.pages  # native text survived the Heron lane being empty


def test_two_documents_never_share_one_staged_copy() -> None:
    """Distinct documents, distinct paths: the layout cache cannot straddle."""
    runner = RecordingRunner()
    parser = _parser(runner)

    parser.extract_pdf(
        source_bytes=NATIVE_BYTES,
        source_sha256=hashlib.sha256(NATIVE_BYTES).hexdigest(),
        document_id="doc-a",
        version="1",
    )
    parser.extract_pdf(
        source_bytes=NATIVE_BYTES,
        source_sha256=hashlib.sha256(NATIVE_BYTES).hexdigest(),
        document_id="doc-b",
        version="1",
    )

    assert len(runner.calls) == 2
    assert runner.calls[0][0] != runner.calls[1][0]


def test_a_host_without_the_harness_still_serves_the_page_lane() -> None:
    """The absent container is a named pass state, not a deployment failure.

    Regions are additive metadata, so the Heron refusal drops only the
    boxes: the document keeps its native text and freezes its units.
    """
    runner = RecordingRunner(
        fail_with=HeronHarnessAbsentError(Path("/absent/run_measured.sh"))
    )
    parser = _parser(runner)

    result = parser.extract_pdf(
        source_bytes=NATIVE_BYTES,
        source_sha256=hashlib.sha256(NATIVE_BYTES).hexdigest(),
        document_id="doc-a",
        version="1",
    )

    assert runner.calls  # the pass was asked for and named its absence
    assert [call[1] for call in runner.calls] == [True]
    assert result.pages
    assert not any(page.regions for page in result.pages)


# ----- the boxes a pass finds are left where the crop lane reads them ----


def test_a_pass_leaves_its_page_boxes_in_the_bound_sink() -> None:
    """Without this write there is no crop to take, only the whole page.

    The layout already produced the geometry; persisting it is what makes a
    later question's crop inspection possible at all. The box below is the
    chart the committed gold manifest records on page 3 of the mixed-chart
    case — 528 x 328 pt at x=42, top y=100 on a 612 x 792 pt media box —
    expressed in the record's own bottom-left space the adapter flips back
    to top-left.
    """
    runner = RecordingRunner(
        record={
            "pages": [
                {
                    # The native-text gold fixture is one page, 612 x 792 pt.
                    "page_no": 1,
                    "figure_boxes": [
                        # 792 - 100 = 692 and 792 - 428 = 364, the flip of
                        # the mixed-chart manifest's page-3 chart box.
                        {"l": 42.0, "t": 692.0, "r": 570.0, "b": 364.0}
                    ],
                }
            ]
        }
    )
    stored: dict[str, Any] = {}
    parser = _parser(runner)
    parser.region_sink = lambda **kwargs: (
        stored.update(kwargs) or len(kwargs["regions"])
    )

    parser.extract_pdf(
        source_bytes=NATIVE_BYTES,
        source_sha256=hashlib.sha256(NATIVE_BYTES).hexdigest(),
        document_id="doc-chart",
        version="3",
    )

    # The sink received the same version the extract ran under, and one box
    # in the core's own top-left PDF-point space, not the record's flipped
    # coordinates — so a crop taken from it lands where Heron said.
    assert stored["attachment_id"] == "doc-chart"
    assert stored["version"] == 3  # the version the extract ran under
    boxes = stored["regions"]
    assert len(boxes) == 1
    assert (boxes[0].page, boxes[0].kind) == (1, "figure")
    assert (boxes[0].left_pt, boxes[0].right_pt) == (42.0, 570.0)
    assert (boxes[0].top_pt, boxes[0].bottom_pt) == (100.0, 428.0)


def test_a_lane_with_no_sink_still_parses_and_keeps_no_boxes() -> None:
    """A sink is additive metadata, never a preparation precondition.

    With no store bound there is nowhere to leave a box, and the parse still
    has to return its pages, text, and rasters — the crop lane then reads
    the absence as the whole-page fallback.
    """
    parser = _parser(RecordingRunner())

    result = parser.extract_pdf(
        source_bytes=NATIVE_BYTES,
        source_sha256=hashlib.sha256(NATIVE_BYTES).hexdigest(),
        document_id="doc-a",
        version="1",
    )

    assert parser.region_sink is None
    assert result.pages


def test_a_sink_that_refuses_does_not_fail_the_preparation() -> None:
    """Regions are additive: a store fault loses boxes, not a document.

    The text, units, and rasters the same pass produced are still good, and
    the row's typed failure has not been earned by a missing box.
    """

    def refusing(**_kwargs: Any) -> int:
        raise RuntimeError("region table gone")

    parser = _parser(RecordingRunner())
    parser.region_sink = refusing

    result = parser.extract_pdf(
        source_bytes=NATIVE_BYTES,
        source_sha256=hashlib.sha256(NATIVE_BYTES).hexdigest(),
        document_id="doc-a",
        version="1",
    )

    assert result.pages


def test_the_wired_root_binds_its_store_as_the_region_sink(
    clean_root_env: None,
) -> None:
    """The deployed lane and the store that owns the rows are one wiring.

    Without this binding the page lane would parse and then throw its boxes
    away, leaving every later question with the whole page.
    """
    service = _build_default_attachment_service()

    assert isinstance(service.pages, HeronPageLaneParser)
    assert service.pages.region_sink == service.store.record_regions


def test_a_staged_copy_is_gone_even_when_the_document_cannot_be_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refused claim leaks nothing.

    Bytes without a PDF signature under a PDF claim end before any page
    opens, but the private copy was already written — the same guard that
    removes a good document's copy must remove this one's on the way out.
    """
    monkeypatch.setattr(
        "fin.host.workspace_http.tempfile.gettempdir", lambda: str(tmp_path)
    )
    parser = _parser(RecordingRunner())

    with pytest.raises(UnsupportedFormatParseError):
        parser.extract_pdf(
            source_bytes=b"not a pdf at all",
            source_sha256=hashlib.sha256(b"not a pdf at all").hexdigest(),
            document_id="doc-x",
            version="1",
        )

    assert list(tmp_path.glob("fin-attachment-*.pdf")) == []


def _extract_chart(parser: HeronPageLaneParser) -> Any:
    return parser.extract_image(
        source_bytes=CHART_BYTES,
        source_sha256=hashlib.sha256(CHART_BYTES).hexdigest(),
        document_id="img-a",
        version="1",
    )


@pytest.fixture(autouse=True)
def _no_pytesseract_leak() -> None:
    """Undo any real ``pytesseract`` import this module causes.

    Both construction paths here build the adapter's own
    ``TesseractEngine`` — ``_parser`` directly, and
    ``_build_default_attachment_service`` through the composition root —
    and that engine imports the optional extra for real. Leaving the
    module in ``sys.modules`` fails the sibling
    ``test_default_pytest_does_not_import_pytesseract`` in
    tests/corpus/test_ocr.py whenever this file is collected first; that
    contract is about ``fin.corpus.ocr`` not importing the extra when
    *it* is imported, and it cannot account for a sibling file
    instantiating an engine. Autouse because every test here that builds
    a parser or the root leaks, not just the image-lane ones.
    """
    yield
    for name in [
        m for m in sys.modules if m == "pytesseract" or m.startswith("pytesseract.")
    ]:
        sys.modules.pop(name, None)


def test_a_standalone_image_without_ocr_stages_no_heron_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no OCR engine the lane ends at the typed exit, staging nothing.

    ``TesseractEngine`` resolves its ``pytesseract`` import in
    ``__init__``, so the absent-extra case is pinned by blocking the
    import BEFORE the adapter builds its engine — the same
    ``sys.modules`` idiom ``tests/corpus/test_ocr.py`` uses. Driving it
    from the import, rather than from whatever this host happens to have
    installed, is what makes the case deterministic: the old form
    asserted the refusal unconditionally and so failed on any host where
    the optional ``[ocr]`` extra was present.
    """
    monkeypatch.setitem(sys.modules, "pytesseract", None)

    runner = RecordingRunner()
    parser = _parser(runner)

    with pytest.raises(OcrRequiredUnavailableError) as excinfo:
        _extract_chart(parser)

    # Bound to the raised instance, not to the class attribute: the class
    # constant compared against its own definition proves nothing about
    # which exit this call took.
    assert excinfo.value.code == "ocr_required_unavailable"
    assert runner.calls == []


def test_a_standalone_image_with_ocr_stages_no_heron_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With a readable engine the lane returns the upload itself, still unstaged.

    The other leg of the same invariant, and the one that had no coverage
    at all: the refusal above proves nothing on a host where OCR works.
    Here a recording stand-in for the OCR port answers with real text, so
    the lane builds its one page, the page's raster is byte-identical to
    the upload (no container PDF was synthesized to hold the image), and
    the Heron runner is still never called.
    """
    class _ReadableOcr:
        """A recording ``RasterOcrPort``: no binary, no extra, real text."""

        def __init__(self) -> None:
            self.calls: list[bytes] = []

        def recognize_raster(self, *, png: bytes, reference: str) -> str:
            self.calls.append(png)
            return "quarterly revenue rose 12%\n"

    engine = _ReadableOcr()
    monkeypatch.setattr(
        "fin.host.workspace_http.TesseractEngine", lambda *a, **k: engine
    )

    runner = RecordingRunner()
    parser = _parser(runner)

    result = _extract_chart(parser)

    assert engine.calls == [CHART_BYTES]
    assert result.source_page_count == 1
    assert result.truncated is False
    assert len(result.pages) == 1
    page = result.pages[0]
    # The raster IS the upload: this is the "no container synthesized"
    # claim, and it is the assertion a synthesized-PDF regression breaks.
    assert page.raster.png == CHART_BYTES
    assert "12%" in page.scanned_text
    assert runner.calls == []


# ----- the run-pricing seam of the page lane -----

# The smallest legal run, derived through the module that prices one: one
# admitted page, one crop of headroom, and the one reserved retry — three
# full-page scans fit, a fourth does not.
_LEDGER_BUDGETS = AttachmentBudgets(
    max_upload_bytes=1_048_576,
    max_pdf_pages=1,
    max_vision_calls_per_run=required_run_calls(
        AttachmentBudgets(
            max_upload_bytes=1_048_576,
            max_pdf_pages=1,
            max_vision_calls_per_run=2,
            max_vision_crop_calls_per_run=1,
        )
    ),
    max_vision_crop_calls_per_run=1,
)


class _ChartRaster:
    """``PageRenderPort``: one fixed raster per page (a real PNG's bytes)."""

    def render_page(self, *, page_no: int) -> PageRaster:
        return PageRaster.of(CHART_BYTES, width_px=1, height_px=1)


class _AnsweringVision:
    """``VisionPort`` that answers every scan; the ledger is what these pin.

    Success on every page keeps the refusal these tests assert a pricing
    fact of the ledger, never a provider outage.
    """

    def __init__(self) -> None:
        self.targets: list[Any] = []

    def inspect(
        self,
        *,
        image: bytes,
        target: Any,
        media_type: str,
        question: str | None = None,
    ) -> VisionCallResult:
        self.targets.append(target)
        return VisionCallResult(
            text="No chart, plot, or table is present on this page.",
            target=target,
            model_id="lane-fake-vision-1",
            media_type=media_type,
            usage=VisionUsage(11, 7, 18),
        )


def _ledger_lane_service(
    *, call_ledger: VisionCallLedger | None = None
) -> tuple[AttachmentPreparationService, str]:
    threads = InMemoryThreadStore()
    thread = threads.create_thread(title="ledger lane")
    service = AttachmentPreparationService(
        store=InMemoryAttachmentStore(threads),
        objects=InMemoryObjectStore(),
        budgets=_LEDGER_BUDGETS,
        pages=PageRegionParser(
            layout=NullLayout(), renderer=_ChartRaster(), ocr=None
        ),
        vision=_AnsweringVision(),
        call_ledger=call_ledger,
    )
    return service, thread.id


def _prepare_one_page_pdf(
    service: AttachmentPreparationService, thread_id: str
) -> AttachmentRecord:
    accepted = service.accept(
        thread_id=thread_id,
        filename="filing.pdf",
        payload=NATIVE_BYTES,
    )
    return service.prepare(accepted.attachment_id)


def test_one_injected_ledger_prices_every_preparation_against_the_run() -> None:
    """A run-wide ledger is what makes the deployed total a run total.

    The budget admits exactly three single-page scans: with one ledger
    injected, preparations one to three spend it and the fourth is refused
    by the same seam — the reserve belongs to the run, not to any one
    document, which is what ADR 0089's pricing promises.
    """
    ledger = VisionCallLedger(_LEDGER_BUDGETS)
    service, thread_id = _ledger_lane_service(call_ledger=ledger)

    first_three = [
        _prepare_one_page_pdf(service, thread_id).status for _ in range(3)
    ]
    fourth = _prepare_one_page_pdf(service, thread_id)

    assert first_three == ["ready", "ready", "ready"]
    assert fourth.status == "failed"
    assert fourth.failure_reason == ATTACHMENT_VISION_BUDGET_EXCEEDED
    assert ledger.calls_spent == 3


def test_without_an_injected_ledger_each_preparation_prices_itself() -> None:
    """The default stays per-preparation: no hidden run-wide sharing.

    The same three-call budget admits four single-page documents when each
    preparation charges a fresh ledger — an automatically shared one would
    have refused the fourth on an allowance spent by the first three, so
    this pins that the seam defaults to the shipped shape and only an
    explicit injection spans preparations.
    """
    service, thread_id = _ledger_lane_service()

    records = [
        _prepare_one_page_pdf(service, thread_id) for _ in range(4)
    ]

    assert [record.status for record in records] == ["ready"] * 4
    assert service.call_ledger is None
