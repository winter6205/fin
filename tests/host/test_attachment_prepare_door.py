"""What the shipped door answers for a page document it cannot read.

Why this file exists
--------------------
The page/region core can read PDFs and images, but the process the product
ships builds its attachment service with no page budget and no extraction
ports (``_build_default_attachment_service`` passes only a store and a byte
store). That unwired state is what an operator actually runs, so the
promise it has to keep is narrow and was never pinned anywhere: a name-declared
page document is stored, answered with a named failure the row keeps, and
nothing raises past the door. The refusal is judged per family: a ``.pdf``
is the multi-page source whose pricing the missing lane owns, so it is
named as a deployment fact, while a standalone image waits on no lane
economics and the text-only door answers it by content — the promise this
file also re-pins, because an earlier repair had the refusal preempt that
content judgement for image bytes too.

Both halves are read from the real thing rather than from a hand-wired
service: the first cases call the composition root's own builder, the later
ones drive the real upload, retry, and status routes. Hermetic either way —
in-memory stores, no model, no provider, no OCR binary, no page ever
rasterized.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fin.corpus.object_store import InMemoryObjectStore
from fin.host.attachment_budgets import AttachmentBudgets
from fin.host.attachment_prepare import (
    ATTACHMENT_EMPTY_TEXT,
    ATTACHMENT_NOT_TEXT,
    ATTACHMENT_PAGE_LANE_UNWIRED,
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

# The door reads its upload bound through its own dependency, so the route
# needs a deployed number. It is unrelated to the answer under test: no
# bound here is ever handed to the service.
DOOR_BUDGETS = AttachmentBudgets(
    max_upload_bytes=1_048_576,
    max_pdf_pages=8,
    max_vision_calls_per_run=16,
    max_vision_crop_calls_per_run=8,
)

# Every store the root picks up from the environment: scrubbed, the builder
# takes its documented in-memory degradation instead of opening a pool.
_ROOT_ENV = (
    "FIN_CHECKPOINT_URL",
    "FIN_OBJECT_STORE_URL",
    "FIN_OBJECT_STORE_ENDPOINT",
    "FIN_OBJECT_STORE_BUCKET",
)

# The refusal is judged per family, so each row names the verdict its own
# family must get on this deployment: the multi-page document the missing
# lane was priced for is named as a deployment fact, and the standalone
# image — one page, no lane economics to wait on — is named by content.
PAGE_DOCUMENTS = [
    pytest.param(
        "g4-mixed-chart-001.pdf", MIXED_BYTES, ATTACHMENT_PAGE_LANE_UNWIRED,
        id="gold-pdf",
    ),
    pytest.param(
        "g4-image-chart-001.png", CHART_BYTES, ATTACHMENT_NOT_TEXT,
        id="gold-image",
    ),
]


def _unwired_service(
    threads: InMemoryThreadStore,
) -> AttachmentPreparationService:
    """The service exactly as the composition root builds it."""
    return AttachmentPreparationService(
        store=InMemoryAttachmentStore(threads), objects=InMemoryObjectStore()
    )


@pytest.fixture
def http_app() -> Iterator[FastAPI]:
    from fin.host.http import app

    yield app
    app.dependency_overrides.clear()


@pytest.fixture
def unwired_door(
    http_app: FastAPI, threads: InMemoryThreadStore
) -> dict[str, Any]:
    thread = threads.create_thread(title="unwired lane door")
    service = _unwired_service(threads)
    http_app.dependency_overrides[get_thread_store] = lambda: threads
    http_app.dependency_overrides[get_attachment_service] = lambda: service
    http_app.dependency_overrides[get_attachment_budgets] = lambda: DOOR_BUDGETS
    return {"thread_id": thread.id, "service": service}


@pytest.fixture
def threads() -> InMemoryThreadStore:
    return InMemoryThreadStore()


@pytest.fixture
def client(http_app: FastAPI) -> TestClient:
    return TestClient(http_app)


# ----- the composition root itself -----


@pytest.mark.parametrize(("filename", "payload", "reason"), PAGE_DOCUMENTS)
def test_the_shipped_root_answers_a_page_document_with_a_code(
    monkeypatch: pytest.MonkeyPatch,
    filename: str,
    payload: bytes,
    reason: str,
) -> None:
    """No 500 out of the builder the product actually runs.

    The row is the answer: ``failed``, carrying the code its family earns on
    this deployment, with the bytes kept so a later preparation — under a
    wired lane or a reader this door has for these bytes — can take them.
    """
    for key in _ROOT_ENV:
        monkeypatch.delenv(key, raising=False)
    service = _build_default_attachment_service()
    assert (service.budgets, service.pages) == (None, None)
    thread = default_thread_store().create_thread(title="root unwired")

    accepted = service.accept(
        thread_id=thread.id, filename=filename, payload=payload
    )
    assert accepted.status == "uploaded", accepted

    failed = service.prepare(accepted.attachment_id)

    assert failed.status == "failed", failed
    assert failed.failure_reason == reason, failed
    assert failed.searchable is False, failed
    assert service.status(accepted.attachment_id) == failed
    # A refusal that judged the deployment or the content, not the upload:
    # the bytes stay either way.
    assert service.objects.get(accepted.object_key) == payload


# ----- the HTTP door -----


@pytest.mark.parametrize(("filename", "payload", "reason"), PAGE_DOCUMENTS)
def test_the_page_door_answers_200_with_the_code_not_an_exception(
    client: TestClient,
    unwired_door: dict[str, Any],
    filename: str,
    payload: bytes,
    reason: str,
) -> None:
    """Upload, retry, and status over the shipped unwired service.

    ``TestClient`` re-raises an unhandled server exception by default, so
    this case fails on the escape itself — a 500 could not be asserted past.
    """
    thread_id = unwired_door["thread_id"]

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
    body = retried.json()
    assert body["status"] == "failed", body
    assert body["failure_reason"] == reason, body
    assert body["searchable"] is False, body
    # One field set for one row: the status door cannot disagree with the
    # retry that wrote the reason.
    shown = client.get(
        f"/api/threads/{thread_id}/attachments/{attachment_id}"
    )
    assert shown.status_code == 200, shown.text
    assert shown.json() == body, shown.json()
    listed = client.get(f"/api/threads/{thread_id}/attachments")
    assert listed.status_code == 200, listed.text
    assert body in listed.json()["attachments"], listed.json()


# ----- the content judgement the unwired door still owes an image name -----


def test_an_image_named_payload_the_door_can_read_is_read_as_text(
    threads: InMemoryThreadStore,
) -> None:
    """The refusal judges families, not filenames.

    A standalone image waits on no lane economics, so on this deployment
    its name's lane claim falls to the door's own content ladder: bytes
    that decode as UTF-8 become text the way any no-suffix upload does —
    the answer the lane refusal must never preempt.
    """
    service = _unwired_service(threads)
    thread = threads.create_thread(title="image name, text bytes")

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


def test_an_image_named_empty_payload_stays_empty_on_the_unwired_door(
    threads: InMemoryThreadStore,
) -> None:
    """The same ladder, its blank boundary: empty is empty deployment-free."""
    service = _unwired_service(threads)
    thread = threads.create_thread(title="image name, no bytes")

    accepted = service.accept(
        thread_id=thread.id, filename="chart.png", payload=b""
    )
    failed = service.prepare(accepted.attachment_id)

    assert failed.status == "failed", failed
    assert failed.failure_reason == ATTACHMENT_EMPTY_TEXT, failed
