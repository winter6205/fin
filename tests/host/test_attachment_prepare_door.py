"""What the shipped door answers for a page document, either lane state.

Why this file exists
--------------------
The composition root now deploys the page lane: every service
``_build_default_attachment_service`` builds carries the run's bounds, a
pages port, and a vision port, so the shipped process cannot produce the
refusal this file used to pin through the real builder — a name-declared
page document answered ``attachment_page_lane_unwired`` because no
deployment had a lane. That answer is now owed only by a service wired
without ports (an embedded caller builds the door itself), and this file
pins both sides of that line from the door's point of view:

* a lane deployed: a claimed page document is read — the gold PDF and the
  gold raster freeze their units and reach ``ready``, the upload/retry/
  status doors agree on one field set, and nothing raises past the door;

Door semantics are T2f's throughout the HTTP pins: the retry door only
asks, answering 200 immediately with the wire record of the queued job
(``parsing``), and the preparation settles through the job
(``service.drain_attachment_jobs()``, what the prep worker runs), so the
door cases walk ask -> drain -> settled read-back instead of expecting
the verdict inside the retry request.
* a lane deployed, and what a page reader now judges: an image name's
  claim is taken at its word, so text bytes under ``.png`` end at the
  type the image reader refused (``attachment_type_unsupported``) rather
  than at the old text-ladder success;
* a lane absent: the same document is answered with the deployment code,
  and an image name falls to the content ladder the door always owned —
  the half of the old promise that survives the wiring, because the code
  names a service's wiring, not a deployment the product ships;
* and one answer no wiring changes: no bytes is empty before any reader
  is asked.

Hermetic either way: the page port here is the real core over stub render
and OCR ports with the null layout, and the scan seam is left at its
documented default-open value (no vision port), so no container, OCR
binary, or provider is crossed — those boundaries have their own suites.
The root-built service is read as a wiring fact, never fed a document.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from fin.corpus.object_store import InMemoryObjectStore
from fin.corpus.parsers import NullLayout, PageRegionParser, PageRaster
from fin.host.attachment_budgets import AttachmentBudgets, required_run_calls
from fin.host.attachment_prepare import (
    ATTACHMENT_EMPTY_TEXT,
    ATTACHMENT_NOT_TEXT,
    ATTACHMENT_PAGE_LANE_UNWIRED,
    ATTACHMENT_TYPE_UNSUPPORTED,
    AttachmentPreparationService,
)
from fin.host.attachment_store import InMemoryAttachmentStore
from fin.host.http import (
    get_attachment_budgets,
    get_attachment_service,
    get_thread_store,
)
from fin.host.thread_store import InMemoryThreadStore, default_thread_store
from fin.host.workspace_http import _build_default_attachment_service

REPO_ROOT = Path(__file__).resolve().parents[2]
GOLD_DIR = REPO_ROOT / "data" / "corpus" / "g4_attachment_gold"
MIXED_BYTES = (GOLD_DIR / "g4-mixed-chart-001.pdf").read_bytes()
CHART_BYTES = (GOLD_DIR / "g4-image-chart-001.png").read_bytes()

# The mixed fixture is 3 physical pages (the gold manifest's own count).
MIXED_PAGE_COUNT = 3

# Deployed bounds, priced through the module that prices a run: the lane
# reads preflight (page cap + crop + the one reserved retry) before it
# opens a page, so a door fixture whose totals cannot cover its own caps
# would pin a startup error rather than a door answer.
_CROP_CALLS = 8
_MAX_PAGES = 8
DOOR_BUDGETS = AttachmentBudgets(
    max_upload_bytes=1_048_576,
    max_pdf_pages=_MAX_PAGES,
    max_vision_calls_per_run=required_run_calls(
        AttachmentBudgets(
            max_upload_bytes=1_048_576,
            max_pdf_pages=_MAX_PAGES,
            # Only the page and crop counts price a run, so this stand-in
            # total is what the call returns, never an input to it.
            max_vision_calls_per_run=_MAX_PAGES + _CROP_CALLS,
            max_vision_crop_calls_per_run=_CROP_CALLS,
        )
    ),
    max_vision_crop_calls_per_run=_CROP_CALLS,
)

# Every store the root picks up from the environment: scrubbed, the builder
# takes its documented in-memory degradation instead of opening a pool.
_ROOT_ENV = (
    "FIN_CHECKPOINT_URL",
    "FIN_OBJECT_STORE_URL",
    "FIN_OBJECT_STORE_ENDPOINT",
    "FIN_OBJECT_STORE_BUCKET",
)

_RASTER_PNG = io.BytesIO()
Image.new("RGB", (1, 1)).save(_RASTER_PNG, format="PNG")
RASTER_BYTES = _RASTER_PNG.getvalue()


class StubRenderer:
    """``PageRenderPort`` handing out a 1-pixel raster for every page."""

    def render_page(self, *, page_no: int) -> PageRaster:
        return PageRaster.of(RASTER_BYTES, width_px=1, height_px=1)


class StubOcr:
    """``RasterOcrPort`` answering every read with one usable line."""

    def recognize_raster(self, *, png: bytes, reference: str) -> str:
        return "stub page read"


def _laneless_service(
    threads: InMemoryThreadStore,
) -> AttachmentPreparationService:
    """A door wired without a page lane: the shape the root no longer builds.

    An embedded caller still constructs this; the codes below are pinned
    against it precisely because the shipped builder cannot produce it.
    """
    return AttachmentPreparationService(
        store=InMemoryAttachmentStore(threads), objects=InMemoryObjectStore()
    )


def _deployed_service(
    threads: InMemoryThreadStore,
) -> AttachmentPreparationService:
    """A door with the lane deployed, as the root deploys it, faking only
    the boundaries the default suite may not cross (pixels, OCR, provider).
    """
    return AttachmentPreparationService(
        store=InMemoryAttachmentStore(threads),
        objects=InMemoryObjectStore(),
        budgets=DOOR_BUDGETS,
        pages=PageRegionParser(
            layout=NullLayout(), renderer=StubRenderer(), ocr=StubOcr()
        ),
        # vision stays None: the lane documents that shape as default-open —
        # units freeze, the row publishes, the unscanned gap is the suite's
        # own fact, and no provider import happens here.
    )


@pytest.fixture
def http_app() -> Iterator[FastAPI]:
    from fin.host.http import app

    yield app
    app.dependency_overrides.clear()


@pytest.fixture
def threads() -> InMemoryThreadStore:
    return InMemoryThreadStore()


@pytest.fixture
def client(http_app: FastAPI) -> TestClient:
    return TestClient(http_app)


def _door(http_app: FastAPI, service: AttachmentPreparationService,
          threads: InMemoryThreadStore, title: str) -> dict[str, Any]:
    thread = threads.create_thread(title=title)
    http_app.dependency_overrides[get_thread_store] = lambda: threads
    http_app.dependency_overrides[get_attachment_service] = lambda: service
    http_app.dependency_overrides[get_attachment_budgets] = lambda: DOOR_BUDGETS
    return {"thread_id": thread.id, "service": service}


@pytest.fixture
def laneless_door(
    http_app: FastAPI, threads: InMemoryThreadStore
) -> dict[str, Any]:
    return _door(http_app, _laneless_service(threads), threads,
                 "lane-less door")


@pytest.fixture
def deployed_door(
    http_app: FastAPI, threads: InMemoryThreadStore
) -> dict[str, Any]:
    return _door(http_app, _deployed_service(threads), threads,
                 "deployed lane door")


# ----- the shipped root: the lane is deployed, so its refusal is gone -----


def test_the_shipped_root_builds_no_door_that_can_refuse_a_page_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The old pin was ``(budgets, pages) == (None, None)``; it cannot hold.

    The builder reads the bounds through the same boot singleton
    ``serve()`` gates on and wires the pages port and the vision port on
    every success path, so ``attachment_page_lane_unwired`` is unreachable
    in a built product process: a door without a lane was never one of the
    shapes this root returns.
    """
    for key in _ROOT_ENV:
        monkeypatch.delenv(key, raising=False)
    # The bounds the boot singleton must find, and no Heron knob but the
    # shipped defaults: this pins the door's wiring, not the lane's config
    # reads (the boot suite owns those).
    monkeypatch.setenv("FIN_ATTACHMENT_MAX_BYTES", "1048576")
    monkeypatch.setenv("FIN_ATTACHMENT_MAX_PDF_PAGES", "8")
    monkeypatch.setenv("FIN_ATTACHMENT_VISION_CALLS_PER_RUN", "17")
    monkeypatch.setenv("FIN_ATTACHMENT_VISION_CROP_CALLS_PER_RUN", "8")
    import fin.host.http as http_module

    monkeypatch.setattr(
        http_module, "_http_attachment_budgets_singleton", None, raising=False
    )

    service = _build_default_attachment_service()

    assert service.budgets is not None
    assert service.pages is not None
    assert service.vision is not None
    # And the plain-text promise the door always owned survives the wiring:
    thread = default_thread_store().create_thread(title="root wired text")
    accepted = service.accept(
        thread_id=thread.id,
        filename="notes.txt",
        payload=b"quarterly revenue rose 12%\n",
    )
    ready = service.prepare(accepted.attachment_id)
    assert ready.status == "ready", ready


# ----- the deployed lane reads the documents the lane-less door refused -----


@pytest.mark.parametrize(
    ("filename", "payload", "unit_count"),
    [
        pytest.param(
            "g4-mixed-chart-001.pdf", MIXED_BYTES, MIXED_PAGE_COUNT,
            id="gold-pdf",
        ),
        pytest.param(
            "g4-image-chart-001.png", CHART_BYTES, 1, id="gold-image",
        ),
    ],
)
def test_a_deployed_lane_freezes_the_page_document_it_once_refused(
    threads: InMemoryThreadStore,
    filename: str,
    payload: bytes,
    unit_count: int,
) -> None:
    """The other half of the flip: the same bytes now come back ``ready``.

    One unit per physical page for the PDF, one from the raster's own
    pixels for the image — the answers the page core gives over stub
    ports; the door's job here is that a lane-deployed preparation reaches
    ``ready`` with its units and keeps the row consistent.
    """
    service = _deployed_service(threads)
    thread = threads.create_thread(title="deployed lane reads pages")

    accepted = service.accept(
        thread_id=thread.id, filename=filename, payload=payload
    )
    ready = service.prepare(accepted.attachment_id)

    assert ready.status == "ready", ready
    assert ready.failure_reason is None, ready
    assert ready.searchable is True, ready
    assert ready.unit_count == unit_count, ready
    assert service.status(accepted.attachment_id) == ready


def test_the_deployed_door_answers_200_and_agrees_with_itself(
    client: TestClient, deployed_door: dict[str, Any]
) -> None:
    """Upload, retry, status, list over a lane-deployed service, two stages.

    T2f moved preparation behind the durable job, so the door's promise
    is walked as an operator walks it: the retry answers 200 immediately
    with the queued promise — ``parsing``, one pending job, nothing
    searchable — and the drain (what the prep worker does) settles the
    row. The self-agreement rule survives the split intact at both
    stages: in-request the retry/status/list doors answer one identical
    field set for the parsing row, and after the drain the status and
    list doors answer one identical field set for the settled ready row —
    the "cannot disagree with the preparation that published the row"
    property, restated over the job. ``TestClient`` re-raises an
    unhandled server exception by default, so any escape still fails
    this case, exactly as it did under the synchronous door.
    """
    thread_id = deployed_door["thread_id"]
    service: AttachmentPreparationService = deployed_door["service"]

    uploaded = client.post(
        f"/api/threads/{thread_id}/attachments",
        params={"filename": "g4-mixed-chart-001.pdf"},
        content=MIXED_BYTES,
    )
    assert uploaded.status_code == 200, uploaded.text
    attachment_id = uploaded.json()["attachment_id"]

    retried = client.post(
        f"/api/threads/{thread_id}/attachments/{attachment_id}/retry"
    )

    assert retried.status_code == 200, retried.text
    asked = retried.json()
    # The door only asks: parsing, still no version, nothing searchable.
    assert asked["status"] == "parsing", asked
    assert asked["failure_reason"] is None, asked
    assert asked["searchable"] is False, asked
    assert asked["version"] == 0, asked
    # …and the ask enqueued exactly one job for this attachment.
    jobs = service.jobs.incomplete()
    assert [job.attachment_id for job in jobs] == [attachment_id], jobs
    assert len(jobs) == 1 and jobs[0].state == "pending", jobs

    # In-request self-agreement: no door disagrees with the retry answer.
    shown_now = client.get(
        f"/api/threads/{thread_id}/attachments/{attachment_id}"
    )
    assert shown_now.status_code == 200, shown_now.text
    assert shown_now.json() == asked, shown_now.json()
    listed_now = client.get(f"/api/threads/{thread_id}/attachments")
    assert listed_now.status_code == 200, listed_now.text
    assert asked in listed_now.json()["attachments"], listed_now.json()

    # The drain settles the promised work: exactly one job, then nothing
    # left incomplete, and the success the old synchronous door used to
    # answer in-request.
    assert service.drain_attachment_jobs() == 1
    assert service.jobs.incomplete() == ()

    shown = client.get(
        f"/api/threads/{thread_id}/attachments/{attachment_id}"
    )
    assert shown.status_code == 200, shown.text
    settled = shown.json()
    assert settled["status"] == "ready", settled
    assert settled["failure_reason"] is None, settled
    assert settled["searchable"] is True, settled
    listed = client.get(f"/api/threads/{thread_id}/attachments")
    assert listed.status_code == 200, listed.text
    assert [
        row
        for row in listed.json()["attachments"]
        if row["attachment_id"] == attachment_id
    ] == [settled], listed.json()
    # One field set across the transition: every door still serializes
    # the same columns, only their values settled.
    assert set(settled) == set(asked), (settled, asked)


# ----- what the deployed lane now judges that the lane-less door did not -----


def test_an_image_name_is_taken_at_its_word_by_the_reader_it_claims(
    threads: InMemoryThreadStore,
) -> None:
    """The old text-ladder success flips once a reader owns the name.

    With no lane the door answered an image name by content, because the
    content ladder was the only reader; with the lane deployed the name's
    claim is judged by the image reader, and bytes carrying no image
    signature end at the named type the reader refused — the same verdict
    any undecodable raster earns. The bytes stay, exactly as they did for
    the refusal this replaced.
    """
    service = _deployed_service(threads)
    thread = threads.create_thread(title="image name, text bytes, lane on")

    accepted = service.accept(
        thread_id=thread.id,
        filename="chart.png",
        payload="quarterly revenue rose 12%\n".encode("utf-8"),
    )
    failed = service.prepare(accepted.attachment_id)

    assert failed.status == "failed", failed
    assert failed.failure_reason == ATTACHMENT_TYPE_UNSUPPORTED, failed
    assert service.objects.get(accepted.object_key) == (
        "quarterly revenue rose 12%\n".encode("utf-8")
    )


@pytest.mark.parametrize("laneless", [True, False], ids=["laneless", "deployed"])
def test_no_bytes_is_empty_before_any_reader_decides(
    threads: InMemoryThreadStore, laneless: bool
) -> None:
    """One answer wiring cannot move: an empty payload is empty by name.

    The preflight exits on no bytes before the lane is consulted and
    before any reader is picked up, so the same blank upload reads the
    same way on both doors.
    """
    service = (
        _laneless_service(threads) if laneless else _deployed_service(threads)
    )
    thread = threads.create_thread(title="image name, no bytes")

    accepted = service.accept(
        thread_id=thread.id, filename="chart.png", payload=b""
    )
    failed = service.prepare(accepted.attachment_id)

    assert failed.status == "failed", failed
    assert failed.failure_reason == ATTACHMENT_EMPTY_TEXT, failed


# ----- the lane-less door: codes of a wiring the root no longer ships -----


@pytest.mark.parametrize(
    ("filename", "payload", "reason"),
    [
        pytest.param(
            "g4-mixed-chart-001.pdf", MIXED_BYTES, ATTACHMENT_PAGE_LANE_UNWIRED,
            id="gold-pdf",
        ),
        pytest.param(
            "g4-image-chart-001.png", CHART_BYTES, ATTACHMENT_NOT_TEXT,
            id="gold-image",
        ),
    ],
)
def test_the_lane_less_door_still_names_its_code_instead_of_escaping(
    client: TestClient,
    laneless_door: dict[str, Any],
    filename: str,
    payload: bytes,
    reason: str,
) -> None:
    """The refusal belongs to a service's wiring, not to this deployment.

    A door built without ports (only an embedded caller does that today)
    still owes the same narrow promise: a 200 whose row carries the code
    its family earns — the page cap and wired ports the PDF prices
    against, the content judgement the one-page image gets without them —
    with the bytes kept for a retry under a wired lane. T2f restates the
    promise in two stages: the retry door answers the ask (``parsing``,
    one pending job, the stored reason cleared like any retried row), and
    the drain settles the named failure onto the row — the same typed
    code the synchronous door used to answer in-request, with nothing
    published as parsed. What changed is only that the shipped root can
    no longer hand the door such a service.
    """
    thread_id = laneless_door["thread_id"]
    service: AttachmentPreparationService = laneless_door["service"]

    uploaded = client.post(
        f"/api/threads/{thread_id}/attachments",
        params={"filename": filename},
        content=payload,
    )
    assert uploaded.status_code == 200, uploaded.text
    attachment_id = uploaded.json()["attachment_id"]

    retried = client.post(
        f"/api/threads/{thread_id}/attachments/{attachment_id}/retry"
    )

    assert retried.status_code == 200, retried.text
    asked = retried.json()
    assert asked["status"] == "parsing", asked
    assert asked["failure_reason"] is None, asked
    assert asked["searchable"] is False, asked
    jobs = service.jobs.incomplete()
    assert len(jobs) == 1 and jobs[0].state == "pending", jobs

    # In-request self-agreement, same as the old synchronous door's
    # shown==retried check: the status door reads the ask the retry made.
    asked_now = client.get(
        f"/api/threads/{thread_id}/attachments/{attachment_id}"
    )
    assert asked_now.status_code == 200, asked_now.text
    assert asked_now.json() == asked, asked_now.json()

    assert service.drain_attachment_jobs() == 1
    assert service.jobs.incomplete() == ()

    shown = client.get(
        f"/api/threads/{thread_id}/attachments/{attachment_id}"
    )
    assert shown.status_code == 200, shown.text
    settled = shown.json()
    assert settled["status"] == "failed", settled
    assert settled["failure_reason"] == reason, settled
    assert settled["searchable"] is False, settled
    # The failure named a code, not a partial parse: nothing was ever
    # published as units or postings for this row. (A lane-less service
    # has no page port at all — ``service.pages is None`` is the wiring
    # fact that makes the PDF's refusal the unwired lane, so no renderer
    # could have been asked even in principle.)
    assert service.pages is None
    assert service.units(attachment_id) == ()
    assert service.store.indexed_versions(attachment_id) == ()


def test_the_lane_less_door_still_reads_an_image_name_by_content(
    threads: InMemoryThreadStore,
) -> None:
    """The content ladder is the lane-less answer, and stays pinned as one.

    A standalone image waits on no lane economics, so a door without a
    page lane judges its name's claim by content — bytes that decode as
    UTF-8 become text the way any no-suffix upload does. The deployed
    door replaced this verdict with the reader's own (above); this row
    keeps both halves honest: which promise the absence of a lane earns,
    and which promise only a present lane can make.
    """
    service = _laneless_service(threads)
    thread = threads.create_thread(title="image name, text bytes, no lane")

    accepted = service.accept(
        thread_id=thread.id,
        filename="chart.png",
        payload="quarterly revenue rose 12%\n".encode("utf-8"),
    )
    ready = service.prepare(accepted.attachment_id)

    assert ready.status == "ready", ready
    assert ready.failure_reason is None, ready
    assert [unit.text for unit in service.units(ready.attachment_id)] == [
        "quarterly revenue rose 12%\n"
    ]
