"""T3 live acceptance: the run-side visual lane through the real graph.

The hermetic modules already pin the contract of this lane in isolation
(``tests/host/test_graph_visual_lane.py``,
``tests/host/test_attachment_visual_read.py``,
``tests/host/test_visual_claims_wire.py``) and T4 already proved the storage
door against real bytes. What only a live run can decide is whether the whole
chain holds together with the dependencies the product actually uses: the
wired graph, the real vision provider, real SeaweedFS object bytes, and a real
Postgres attachment store.

What is real here
-----------------
* ``compile_graph`` — the shipped graph, with ``read_visual_detail`` bound
  exactly as the deployment binds it, and ``_visual_page_raster`` the
  production pypdfium2 renderer drawing the stored original bytes.
* :class:`~fin.host.attachment.attachment_vision.ChatModelVisionAdapter` — the one
  production vision port, reading ``FIN_MODEL`` / ``FIN_BASE_URL`` /
  ``FIN_API_KEY`` as the deployment does. Every provider call in this module is
  a real request to a real model; there is no fake port anywhere in it.
* :class:`~fin.host.attachment.attachment_store.PostgresAttachmentStore` and
  :class:`~fin.host.thread_store.PostgresThreadStore` on the live
  ``FIN_CHECKPOINT_URL`` — including the T2 ``record_regions`` /
  ``regions`` SQL and its DDL, which no run had executed
  before this module.
* :func:`~fin.corpus.object_store.create_object_store` over the configured
  SeaweedFS/S3 endpoint, so the page the crop is cut from is bytes that came
  back out of the bucket rather than from a fixture read in-process.
* the committed, versioned G4 gold cases in ``data/corpus/g4_attachment_gold/``
  (dataset ``fin-g4-attachment-gold-v1``), whose page 3 chart is the plotted
  target every leg below asks about.

Not real, and said so rather than hidden: the run's *plan* is a
``ScriptedChatModel``, because the object under test is the lane the run
takes, not the planner that chooses to take it. Every face the visual lane
itself reaches — provider, bytes, store, renderer — is real.

One leg is a refusal leg, and it is the reason the witnesses exist: a visual
ask the current source scope refuses must refuse before the bucket is opened,
before the renderer runs, and before the provider is asked, so that leg counts
stored-object reads, renderer draws, provider calls, and the run's ledger
charge through wrappers that only observe the real faces they forward to.

Opt-in
------
``pytestmark = pytest.mark.mcp`` keeps this out of the default hermetic run.
Every real service is a named prerequisite, so a missing one skips here with
the reason attached; the validation record logs a skip as blocked evidence,
never as a pass.

    FIN_CHECKPOINT_URL=… FIN_OBJECT_STORE_URL=… FIN_OBJECT_STORE_BUCKET=… \\
    FIN_API_KEY=… uv run --no-sync pytest -q -m mcp \\
        tests/host/test_live_graph_visual_lane.py
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from fin.corpus.parsers._pages import Pypdfium2PageRenderer
from fin.host import ScriptedChatModel, compile_graph
from fin.host.attachment.attachment_budgets import AttachmentBudgets, VisionCallLedger
from fin.host.attachment.attachment_index import build_postings
from fin.host.attachment.attachment_source_read import AttachmentSourceReader
from fin.host.attachment.attachment_store import (
    ParsedAttachmentUnit,
    PostgresAttachmentStore,
    StoredPageRegion,
)
from fin.host.attachment.attachment_vision import VisionCallResult
from fin.host.grounded.citation_gate import QUOTABLE_EVIDENCE_KINDS
from fin.host.evidence.evidence_contract import EVIDENCE_SCHEMA_VERSION
from fin.host.source_scope import SCOPE_ATTACHMENT_NOT_PERMITTED, ScopeRestriction
from fin.host.state import reset_input_state
from fin.host.thread_store import PostgresThreadStore
from fin.host.evidence.visual_evidence import (
    VISUAL_BUDGET_EXCEEDED,
    VISUAL_CLAIM_FIELDS,
    VISUAL_EVIDENCE_KIND,
)
from fin.retrieve.fake_port import FakeRetrievePort

REPO_ROOT = Path(__file__).resolve().parents[2]
GOLD_DIR = REPO_ROOT / "data" / "corpus" / "g4_attachment_gold"
GOLD_MANIFEST = GOLD_DIR / "manifest.json"
# The one gold case whose page 3 carries a real chart region. The manifest
# names its provenance; :func:`dataset_version` below binds the run to the
# committed set it came from.
MIXED_PDF = GOLD_DIR / "g4-mixed-chart-001.pdf"

pytestmark = pytest.mark.mcp

# The mixed gold case's page 3 chart, in the PDF points the manifest records
# on a 612 x 792 pt media box. This box is the page's own figure region, so a
# claim about it is a claim about the fixture's real pixels.
GOLD_CHART_REGION = StoredPageRegion(
    page=3, kind="figure", left_pt=42.0, top_pt=100.0, right_pt=570.0, bottom_pt=428.0
)
CHART_ASPECT_PT = (612.0, 792.0)

# The real plotted value behind the fixture (manifest: mixed page 3 draws
# AAPL 2023 Q3 net sales). Read out of the committed filings, never from fin.
EXPECTED_VALUE_IN_ANSWER = "81,797"

ANSWER_SCRIPT = "The chart plots that value; the figure is on the page's upper half."

# A research-track question. The visual lane is a TOOL on the grounded track,
# and a clarify ask never reaches the tool node at all, so a live leg that
# wanted the tool to fire has to ask a question the router really routes to
# research. This is the real AAPL ask the corpus answers; it carries no chart
# vocabulary, so nothing but the tool call draws the figure.
GROUNDED_ASK = "What was AAPL net sales in 2023 Q3?"


# ----- prerequisites ---------------------------------------------------


# The provider settings the real deployment reads. The suite-wide hermetic
# strip in ``tests/conftest.py`` deletes ``FIN_API_KEY`` / ``FIN_MODEL`` /
# ``FIN_BASE_URL`` from ``os.environ`` at import, BEFORE any test module is
# imported. This module reads the process snapshot captured before that
# strip. A value is never printed.
_VISION_ENV_KEYS = ("FIN_API_KEY", "FIN_MODEL", "FIN_BASE_URL")


def _missing_prerequisites() -> list[str]:
    """The real services this module needs, named but never printed.

    Only presence is reported — a value is never echoed. The provider
    credential is the process variable ``FIN_API_KEY``.
    """
    missing: list[str] = []
    if not (os.environ.get("FIN_CHECKPOINT_URL") or "").strip():
        missing.append("FIN_CHECKPOINT_URL")
    endpoint = (
        os.environ.get("FIN_OBJECT_STORE_URL")
        or os.environ.get("FIN_OBJECT_STORE_ENDPOINT")
        or ""
    ).strip()
    if not endpoint:
        missing.append("FIN_OBJECT_STORE_URL")
    if not (os.environ.get("FIN_OBJECT_STORE_BUCKET") or "").strip():
        missing.append("FIN_OBJECT_STORE_BUCKET")
    if not _vision_settings().get("FIN_API_KEY"):
        missing.append("FIN_API_KEY")
    return missing


def _vision_settings() -> dict[str, str]:
    """Provider settings captured before the hermetic strip.

    Values are never echoed: only presence reaches an assertion.
    """
    from tests.conftest import _PROCESS_SNAPSHOT

    return {key: _PROCESS_SNAPSHOT[key] for key in _VISION_ENV_KEYS if key in _PROCESS_SNAPSHOT}


@pytest.fixture(autouse=True)
def _restore_operator_vision_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Put the operator's real provider settings back after the hermetic strip.

    ``tests/conftest.py`` deletes these three keys before every test so the
    default run can never reach a real model. This module is the opt-in
    suite whose whole subject IS the real model, so it restores exactly the
    settings the operator supplied for it. It only ever re-sets a value the
    operator supplied; with none supplied the module skips as blocked rather
    than inventing one.
    """
    for key, value in _vision_settings().items():
        if value:
            monkeypatch.setenv(key, value)


@pytest.fixture(scope="module")
def dataset_version() -> str:
    """The versioned identity of the gold set every case below is cut from."""
    manifest = json.loads(GOLD_MANIFEST.read_text())
    assert manifest["dataset_id"] == "fin-g4-attachment-gold-v1"
    return f"{manifest['dataset_id']}@{manifest['schema_version']}"


@pytest.fixture(scope="module")
def postgres_url() -> str:
    missing = _missing_prerequisites()
    if missing:
        pytest.skip(
            "real prerequisites are not configured: " + ", ".join(missing) + "; "
            "this module is the real-boundary evidence and cannot run against "
            "an in-memory stand-in"
        )
    return os.environ["FIN_CHECKPOINT_URL"].strip()


@pytest.fixture(scope="module")
def deployed_bounds() -> dict[str, str]:
    """The deployed bounds exactly as ``settings.example.yaml`` ships them.

    Read through the product's settings helper rather than restated here, so
    this module can never bless a number the deployment does not carry.
    """
    from fin.host.attachment.attachment_budgets import _ENV_BOUNDS as _PRODUCT_ENV_BOUNDS
    from fin.host.operator_settings import shipped_attachment_bounds

    values = shipped_attachment_bounds(repo_root=REPO_ROOT)
    # Read the key set from the product's own table, so a fifth bound could
    # not be silently untested here.
    missing = [key for key, _ in _PRODUCT_ENV_BOUNDS if key not in values]
    assert not missing, f"settings.example.yaml lost {missing}"
    return {key: values[key] for key, _ in _PRODUCT_ENV_BOUNDS}


@dataclass(frozen=True)
class _Live:
    """One live thread's real stores, its thread id, and its cleanup keys."""

    threads: PostgresThreadStore
    store: PostgresAttachmentStore
    thread_id: str
    attachment_ids: list[str]
    object_keys: list[str]


@pytest.fixture
def live(postgres_url: str) -> Iterator[_Live]:
    """A real thread with one real uploaded attachment row, cleaned up after.

    The rows are written through the store's own doors and the bytes through
    the real object store, so the row and the blob agree the way an upload
    makes them agree. Cleanup removes the bytes and the rows in a
    ``finally``, so an assertion failure can leave neither an owed byte nor a
    live thread behind.
    """
    from fin.corpus.object_store import create_object_store

    objects = create_object_store()
    threads = PostgresThreadStore(postgres_url)
    # Constructing the attachment store applies its DDL, so the T2 region
    # table comes into existence here or the first region write fails.
    store = PostgresAttachmentStore(postgres_url)
    thread_id = threads.create_thread(title="T3 live visual lane").id
    attachment_id = f"att-t3-{uuid.uuid4().hex[:12]}"
    object_key = f"threads/{thread_id}/{attachment_id}"
    attachment_ids: list[str] = []
    object_keys: list[str] = []
    try:
        yield _Live(threads, store, thread_id, attachment_ids, object_keys)
    finally:
        for key in object_keys:
            try:
                objects.delete(key)
            except Exception:  # noqa: BLE001 - cleanup must not mask a failure
                pass
        # Delete the rows first: every attachment row and its derived
        # layers cascade with the thread, and a still-live row would leave a
        # tombstone for bytes the ``finally`` above already removed.
        threads.delete_thread(thread_id)


def _upload(
    live: _Live, attachment_id: str, filename: str, source: Path
) -> tuple[str, bytes]:
    """Write the real bytes to the real bucket and the real row to real SQL.

    Returns ``(object_key, bytes)``. The row is marked ``ready`` with the
    physical page count the document really has, because the lane's page
    admission is checked against it.
    """
    from fin.corpus.object_store import create_object_store

    payload = source.read_bytes()
    object_key = f"threads/{live.thread_id}/{attachment_id}"
    create_object_store().put(object_key, payload)
    live.object_keys.append(object_key)

    live.store.create(
        thread_id=live.thread_id,
        attachment_id=attachment_id,
        filename=filename,
        size_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        object_key=object_key,
    )
    live.attachment_ids.append(attachment_id)
    return object_key, payload


def _mark_ready_pdf(
    live: _Live, attachment_id: str, *, pages: int, region: StoredPageRegion | None
) -> None:
    """The derived layers preparation would have written, through real doors.

    Units and the index are written with the same builders the preparation
    worker uses, and the page regions through T2's ``record_regions`` — whose
    SQL and DDL no run had executed before this module.
    """
    units = tuple(
        ParsedAttachmentUnit(unit=page, text=f"page {page} body text")
        for page in range(1, pages + 1)
    )
    live.store.record_units(attachment_id=attachment_id, version=1, units=units)
    live.store.put_index(
        attachment_id=attachment_id, version=1, postings=build_postings(units)
    )
    if region is not None:
        live.store.record_regions(
            attachment_id=attachment_id, version=1, regions=(region,)
        )
    live.store.mark_ready(attachment_id=attachment_id, version=1, unit_count=pages)


# ----- the wired run -----------------------------------------------------


class _LiveService:
    """The real attachment service, holding the real faces.

    ``vision`` is the production adapter — not a fake, and not a stub that
    answers a fixture. ``objects`` is the configured object store, so the
    page the crop is cut from is read back out of the bucket on every read.
    """

    def __init__(
        self, store: PostgresAttachmentStore, budgets: AttachmentBudgets
    ) -> None:
        from fin.corpus.object_store import create_object_store
        from fin.host.attachment.attachment_vision import ChatModelVisionAdapter

        self.store = store
        self.vision = ChatModelVisionAdapter()
        self.budgets = budgets
        self.objects = create_object_store()


class _RecordingVision:
    """The production adapter, wrapped so calls and bytes stay observable.

    Everything the wrapper does is observation: it forwards the real
    :class:`VisionPort` call unchanged and records what left the process. A
    test that counted calls or hashed an image needs that record, and the
    alternative — asserting only on the run's own ``outbound_calls`` — would
    be the run agreeing with itself.
    """

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.calls: list[dict[str, Any]] = []

    def inspect(
        self, *, image: bytes, target: Any, media_type: str, question: str | None = None
    ) -> VisionCallResult:
        self.calls.append(
            {
                "image_sha256": hashlib.sha256(image).hexdigest(),
                "image_bytes": len(image),
                "crop": target.crop,
                "page": target.page,
            }
        )
        return self.inner.inspect(
            image=image, target=target, media_type=media_type, question=question
        )

    @property
    def model_id(self) -> str:
        """The model that will answer, read the way the port itself reads it.

        Resolved through the port's own public resolver rather than by
        reaching into the adapter's private config, so the identity asserted
        here is the one the deployment configures.
        """
        from fin.host.attachment.attachment_vision import resolve_vision_model_config

        return resolve_vision_model_config().model_id


class _CountingObjects:
    """The configured bucket, wrapped so a read of stored bytes stays observable.

    The wrapper only records the key it was asked for and forwards the real
    read: the bytes returned are the ones SeaweedFS holds, so a leg that
    asserts "no object was read" is asserting about the real door rather than a
    substitute for it.
    """

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.reads: list[str] = []

    def get(self, key: str) -> bytes:
        self.reads.append(key)
        return self.inner.get(key)

    def __getattr__(self, name: str) -> Any:  # every other door, unchanged
        return getattr(self.inner, name)


class _RecordingRenderer:
    """The production page renderer, wrapped so a draw is countable.

    The draw itself stays the real pypdfium2 render of the stored original
    bytes; this only remembers which pages were asked for, so a refusal that
    drew nothing means nothing was rastered, not that the raster was stubbed.
    """

    def __init__(self) -> None:
        import fin.host.graph as graph_module

        self._real = graph_module._visual_page_raster
        self.drawn: list[int] = []

    def __call__(self, payload: bytes, page: int) -> Any:
        self.drawn.append(page)
        return self._real(payload, page)


def _budgets(deployed: dict[str, str]) -> AttachmentBudgets:
    from fin.host.attachment.attachment_budgets import from_environ

    return from_environ(environ=deployed)


def _recording_service(
    store: PostgresAttachmentStore, deployed: dict[str, str]
) -> tuple[_LiveService, _RecordingVision]:
    """A real service whose vision port is the real adapter, made observable.

    Every leg that counts calls or hashes an image needs the same pair, and
    the two must be built together: the wrapper only observes the real adapter
    it is handed, so a leg that made them separately could end up running
    against an unrecorded port and prove nothing.
    """
    service = _LiveService(store, _budgets(deployed))
    vision = _RecordingVision(service.vision)
    service.vision = vision
    return service, vision


def _witnessed_service(
    store: PostgresAttachmentStore,
    deployed: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[_LiveService, _RecordingVision, _CountingObjects, _RecordingRenderer]:
    """Every expensive face of the lane, wrapped so "never reached" is countable.

    A refusal that must cost nothing has four separate prices — a stored object
    read, a page drawn, a provider called, a ceiling charged — and a run can
    only be shown to have paid none of them if each door reports its own
    traffic. The byte face and the renderer are therefore wrapped here, next to
    the port, so one leg counts all of them on one wiring.
    """
    service, vision = _recording_service(store, deployed)
    objects = _CountingObjects(service.objects)
    service.objects = objects
    renderer = _RecordingRenderer()
    monkeypatch.setattr(
        "fin.host.graph._visual_page_raster", renderer, raising=True
    )
    return service, vision, objects, renderer


def _compile(
    store: PostgresAttachmentStore,
    service: _LiveService,
    script: list[AIMessage],
    *,
    scope_restriction: ScopeRestriction | None = None,
):
    """The shipped graph, wired the way a scoped run is wired.

    ``scope_restriction`` is handed through exactly as Runtime hands it, so a
    refusal leg below is refused by the graph a deployment compiles rather
    than by a gate built for the test. The default of ``None`` keeps every
    other leg in this module running the unrestricted graph it always ran.
    """
    model = ScriptedChatModel(script=script)
    return compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        adapter=FakeRetrievePort(hits_by_query={}, known_document_ids=set()),
        known_document_ids=set(),
        attachment_service=service,
        attachment_source_reader_factory=(
            lambda bound: AttachmentSourceReader(store, bound)
        ),
        scope_restriction=scope_restriction,
    )


def _visual_call(args: dict[str, Any], call_id: str = "c-visual") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {"name": "read_visual_detail", "args": args, "id": call_id, "type": "tool_call"}
        ],
    )


class _MintedLedgers:
    """Collects the real ledgers the graph mints, for the run that spent them.

    The graph overwrites ``configurable["vision_call_ledger"]`` on every
    entry, so a test cannot hand one in — and handing one in would test a
    configuration the deployment never builds. This keeps a handle on the
    ledger the graph itself mints, without substituting it: the object
    collected is the shipped :class:`VisionCallLedger`, reached through its
    own constructor, so its counters are the product's own counters.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import fin.host.attachment.attachment_budgets as budgets_module

        minted: list[VisionCallLedger] = []
        real = budgets_module.VisionCallLedger

        class _Collecting(real):  # type: ignore[misc, valid-type]
            def __init__(self, budgets: AttachmentBudgets) -> None:
                super().__init__(budgets)
                minted.append(self)

        monkeypatch.setattr(budgets_module, "VisionCallLedger", _Collecting)
        self.minted = minted

    def last(self) -> VisionCallLedger:
        assert self.minted, "the graph minted no visual ledger for this run"
        return self.minted[-1]

    def pre_spend(self, graph: Any, calls: int) -> VisionCallLedger:
        """Spend ``calls`` crop attempts on the ledger the NEXT run will use.

        The graph mints its ledger inside ``_apply_recursion_limit`` on every
        entry, so the only way to hand a run an already-spent ledger is to
        mint it the way the graph mints it and spend it through the real
        charge door before the run. Nothing here substitutes a stand-in: the
        exhausted object is the product's own, at the product's own bounds.
        """
        merged = graph._apply_recursion_limit(  # noqa: SLF001 - the entry door
            {"configurable": {"thread_id": "pre-spend"}}
        )
        ledger = merged["configurable"]["vision_call_ledger"]
        for _ in range(calls):
            ledger.charge_crop_attempt()
        return ledger


@pytest.fixture
def ledgers(monkeypatch: pytest.MonkeyPatch) -> _MintedLedgers:
    return _MintedLedgers(monkeypatch)


def _run(graph: Any, thread_id: str, minted: _MintedLedgers) -> dict[str, Any]:
    """Drive one run; the ledger it spent is readable from ``minted``."""
    return graph.invoke(
        reset_input_state(GROUNDED_ASK),
        config={"configurable": {"thread_id": thread_id}},
    )


def _turn(
    live: _Live,
    deployed_bounds: dict[str, str],
    ledgers: _MintedLedgers,
    attachment_id: str,
    *,
    scope: list[float] | None = None,
) -> tuple[dict[str, Any], _RecordingVision]:
    """One turn of a real conversation: fresh service, fresh graph, fresh ledger.

    Every multi-turn leg needs exactly this, and the freshness is the point:
    a second turn must not be able to reuse anything in process, only what the
    first turn wrote to real Postgres. Sharing one service or graph across two
    turns would turn a reuse proof into a memoisation proof, so this builds
    the whole chain per turn on purpose.
    """
    service, vision = _recording_service(live.store, deployed_bounds)
    graph = _compile(
        live.store,
        service,
        [
            _visual_call(_detail_args(attachment_id, 3, scope=scope)),
            AIMessage(content=ANSWER_SCRIPT),
        ],
    )
    return _run(graph, live.thread_id, ledgers), vision


def _tool_payload(state: dict[str, Any]) -> dict[str, Any]:
    messages = [
        m
        for m in state["messages"]
        if isinstance(m, ToolMessage) and getattr(m, "name", None) == "read_visual_detail"
    ]
    assert len(messages) == 1, f"expected one visual ToolMessage: {messages}"
    return json.loads(str(messages[0].content))


# The normalized bounds of the manifest's own chart box, which is what a
# model passes back once a claim has told it where the figure is.
CHART_SCOPE = [
    42.0 / CHART_ASPECT_PT[0],
    100.0 / CHART_ASPECT_PT[1],
    570.0 / CHART_ASPECT_PT[0],
    428.0 / CHART_ASPECT_PT[1],
]


def _detail_args(
    attachment_id: str, page: int, *, scope: list[float] | None = None
) -> dict[str, Any]:
    args: dict[str, Any] = {
        "attachment_id": attachment_id,
        "page": page,
        "chart": "Net sales by quarter",
        "series": "Net sales",
        "category": "2023 Q3",
    }
    if scope is not None:
        args["scope"] = list(scope)
    return args


def _re_rendered_page_png(payload: bytes, page: int) -> tuple[bytes, str]:
    """The page's own raster, drawn again from the stored original bytes.

    This is the independent side of the hash proof: the run re-renders the
    page inside the lane, and this function renders it again, independently,
    from the bytes the bucket returned. Two draws of the same original must
    hash alike; that is what makes the stored ``page_image_sha256`` a fact
    about pixels rather than about a caller's memory of them.
    """
    with Pypdfium2PageRenderer(payload) as renderer:
        raster = renderer.render_page(page_no=page)
    return raster.png, raster.sha256


@dataclass(frozen=True)
class _CropRun:
    """One live crop run's durable artifacts, read once for both of its legs.

    The setup is the expensive half of this leg — a real upload, a real
    prepared row, a real provider call — and both assertions below are about
    what that one run LEFT BEHIND, not about how it got there. Running it
    once and reading it twice keeps the two legs honest about each other: a
    second run would mint a second observation version, so "the stored row is
    still the first run's" could not be asserted at all.
    """

    live: _Live
    attachment_id: str
    payload: bytes
    page_sha: str
    vision: _RecordingVision
    state: dict[str, Any]
    ledger: VisionCallLedger
    tool_payload: dict[str, Any]


@pytest.fixture
def crop_run(
    live: _Live,
    deployed_bounds: dict[str, str],
    ledgers: _MintedLedgers,
) -> _CropRun:
    """Wire and drive the real crop run the two legs below then read.

    The gold page 3 chart is a real 528 x 328 pt figure on a real 612 x 792 pt
    page, and this run is handed the production adapter, the real bucket and
    the real Postgres. Nothing here asserts: a failure inside the setup
    surfaces against the fixture, and the two tests keep their own failure
    names.
    """
    attachment_id = "att-t3-crop"
    _object_key, payload = _upload(
        live, attachment_id, "g4-mixed-chart-001.pdf", MIXED_PDF
    )
    _mark_ready_pdf(live, attachment_id, pages=3, region=GOLD_CHART_REGION)

    service, vision = _recording_service(live.store, deployed_bounds)
    graph = _compile(
        live.store,
        service,
        [_visual_call(_detail_args(attachment_id, 3)), AIMessage(content=ANSWER_SCRIPT)],
    )
    state = _run(graph, live.thread_id, ledgers)
    _png, page_sha = _re_rendered_page_png(payload, 3)
    return _CropRun(
        live=live,
        attachment_id=attachment_id,
        payload=payload,
        page_sha=page_sha,
        vision=vision,
        state=state,
        ledger=ledgers.last(),
        tool_payload=_tool_payload(state),
    )


def test_a_real_crop_inspection_calls_the_provider_once_with_the_charts_own_pixels(
    crop_run: _CropRun,
    dataset_version: str,
) -> None:
    """Insufficient evidence ⇒ one real inspection, cut to the real box.

    ``dataset_version`` is the run's binding to the committed set the bytes
    came from: the fixture this leg uploads is verified against the manifest
    of that version, so a fixture swapped underneath the test would fail
    here rather than quietly assert against different pixels.
    """
    vision = crop_run.vision
    state = crop_run.state
    call = vision.calls[0] if len(vision.calls) == 1 else None

    # -- the provider was really called, exactly once, on the chart's pixels
    assert len(vision.calls) == 1, (vision.calls, crop_run.tool_payload)
    assert call["page"] == 3
    assert call["crop"] is not None, "a reliable figure box must be cut, not the page"
    assert (call["crop"].left, call["crop"].top, call["crop"].right, call["crop"].bottom) == pytest.approx(
        (
            42.0 / CHART_ASPECT_PT[0],
            100.0 / CHART_ASPECT_PT[1],
            570.0 / CHART_ASPECT_PT[0],
            428.0 / CHART_ASPECT_PT[1],
        )
    ), "the crop must be the manifest's own box, normalized on the page's media box"

    # The bytes the provider saw are the chart's own pixels, not the page's.
    # The comparison that means something is against the WHOLE-PAGE raster,
    # not against the stored PDF: a 200 dpi PNG of any region is larger than a
    # compressed PDF of the whole document, so only the raster tells a crop
    # from the page it was cut from.
    page_png, _page_sha = _re_rendered_page_png(crop_run.payload, 3)
    assert call["image_bytes"] < len(page_png), (
        "the provider must receive the chart's crop, not the whole page"
    )
    assert call["image_sha256"] != hashlib.sha256(page_png).hexdigest()

    # -- the real provider answered with the real plotted value
    payload_json = crop_run.tool_payload
    assert "error" not in payload_json, payload_json
    body = payload_json["observations"][0]["text"]
    assert EXPECTED_VALUE_IN_ANSWER in body, (
        f"the real provider must report the plotted value; got: {body[:400]!r}"
    )
    # The bytes really are this dataset's bytes, and this dataset's version.
    manifest = json.loads(GOLD_MANIFEST.read_text())
    case = next(
        item for item in manifest["cases"] if item["path"].endswith("g4-mixed-chart-001.pdf")
    )
    assert hashlib.sha256(crop_run.payload).hexdigest() == case["sha256"]
    assert dataset_version == f"{manifest['dataset_id']}@{manifest['schema_version']}"

    # -- the run's ledger was charged, and only for the crop
    assert crop_run.ledger.calls_spent == 1
    assert crop_run.ledger.crop_calls_spent == 1
    assert crop_run.ledger.full_page_calls_spent == 0
    assert crop_run.ledger.retries_spent == 0

    # -- the answer is prose; the provenance is not in the tool body
    assert list(payload_json) == ["observations"]
    assert sorted(payload_json["observations"][0]) == [
        "observation_version",
        "page",
        "text",
        "truncated",
    ]


def test_that_inspection_persists_one_real_observation_and_claims_it(
    crop_run: _CropRun,
    postgres_url: str,
    live_sql_rows: Callable[..., list[Any]],
) -> None:
    """The durable artifacts of that run: the claim row, the real SQL row.

    Reads only what the run left behind, so a regression in the WRITE half
    fails here by name instead of somewhere inside the run's own traceback.
    """
    attachment_id = crop_run.attachment_id
    vision = crop_run.vision
    page_sha = crop_run.page_sha

    # -- the claim row carries accurate provenance, on its own channel
    rows = crop_run.state["visual_claims"]
    assert len(rows) == 1, rows
    row = rows[0]
    assert set(row) == set(VISUAL_CLAIM_FIELDS)
    assert row["kind"] == VISUAL_EVIDENCE_KIND
    assert row["attachment_id"] == attachment_id
    assert row["filename"] == "g4-mixed-chart-001.pdf"
    assert row["page"] == 3 and row["unit"] == 3
    assert row["observation_version"] == 1
    assert row["reused"] is False
    assert row["outbound_calls"] == 1
    assert row["provider"] == vision.model_id
    assert row["scope"] == pytest.approx(
        [
            42.0 / CHART_ASPECT_PT[0],
            100.0 / CHART_ASPECT_PT[1],
            570.0 / CHART_ASPECT_PT[0],
            428.0 / CHART_ASPECT_PT[1],
        ]
    )
    # The page hash is the hash of an independent re-render of the stored
    # original bytes, not a value the run asserted about itself.
    assert row["page_image_sha256"] == page_sha

    # -- a real observation row landed in real Postgres
    stored = crop_run.live.store.observations(attachment_id, 1, 3, 3)
    assert len(stored) == 1, stored
    persisted = stored[0]
    assert persisted.observation_version == 1
    assert persisted.page_image_sha256 == page_sha
    assert persisted.provider == vision.model_id
    assert EXPECTED_VALUE_IN_ANSWER in persisted.answer_text
    assert persisted.scope is not None
    # -- and it is in the table, read over a second SQL connection
    sql = live_sql_rows(
        postgres_url,
        "SELECT version, page, observation_version, provider, page_image_sha256, "
        "scope FROM fin_thread_attachment_observation "
        "WHERE attachment_id = %s AND page = 3",
        (attachment_id,),
    )
    assert len(sql) == 1, sql
    assert int(sql[0][0]) == 1 and int(sql[0][1]) == 3 and int(sql[0][2]) == 1
    assert str(sql[0][3]) == vision.model_id
    assert str(sql[0][4]) == page_sha

    # -- T2's region SQL is now executed against real Postgres too
    region_rows = live_sql_rows(
        postgres_url,
        "SELECT page, kind, left_pt, top_pt, right_pt, bottom_pt "
        "FROM fin_thread_attachment_region WHERE attachment_id = %s "
        "ORDER BY page, ordinal",
        (attachment_id,),
    )
    assert len(region_rows) == 1, region_rows
    assert int(region_rows[0][0]) == 3 and str(region_rows[0][1]) == "figure"
    assert float(region_rows[0][2]) == 42.0 and float(region_rows[0][5]) == 428.0


def test_a_later_turn_reuses_the_stored_evidence_with_no_second_call(
    live: _Live,
    deployed_bounds: dict[str, str],
    ledgers: _MintedLedgers,
    live_sql_rows: Callable[..., list[Any]],
) -> None:
    """Sufficient evidence ⇒ reuse: no call, no re-upload, no new version.

    The second turn is a SEPARATE run — its own service, its own graph, its
    own ledger, the product's own per-entry mint — so reuse cannot come from
    in-process memoisation. It has to come from the row the first run wrote
    and read back through the real store, and the page it re-proves is drawn
    again from bytes that came back out of the bucket.

    The second ask names the same region the first one was cut to. That is
    not a convenience: a crop answer grants coverage of exactly its own
    bounds, so a whole-page ask is a WIDER question the narrower observation
    does not answer, and the lane must inspect again. Asking inside the
    granted bounds is what "sufficient evidence" means here, and the
    test above this one is where the wider ask's behaviour belongs.
    """
    attachment_id = "att-t3-reuse"
    _object_key, _payload = _upload(
        live, attachment_id, "g4-mixed-chart-001.pdf", MIXED_PDF
    )
    _mark_ready_pdf(live, attachment_id, pages=3, region=GOLD_CHART_REGION)

    first_state, first_vision = _turn(
        live, deployed_bounds, ledgers, attachment_id, scope=CHART_SCOPE
    )
    assert len(first_vision.calls) == 1
    assert first_state["visual_claims"][0]["observation_version"] == 1
    assert first_state["visual_claims"][0]["reused"] is False

    # Turn two: a fresh service, a fresh graph, a fresh ledger. Same thread,
    # same stored bytes, same persisted observation.
    second_state, second_vision = _turn(
        live, deployed_bounds, ledgers, attachment_id, scope=CHART_SCOPE
    )
    second_ledger = ledgers.last()

    # -- no provider call, and nothing charged, on the second turn
    assert second_vision.calls == [], "a sufficient observation must not be re-inspected"
    assert second_ledger.calls_spent == 0
    assert second_ledger.crop_calls_spent == 0

    rows = second_state["visual_claims"]
    assert len(rows) == 1, rows
    assert rows[0]["reused"] is True
    assert rows[0]["outbound_calls"] == 0
    assert rows[0]["observation_version"] == 1, "reuse must not mint a new version"
    assert rows[0]["scope"] == pytest.approx(first_state["visual_claims"][0]["scope"])

    # -- the re-proved raster hash is the hash of the stored original bytes
    _png, page_sha = _re_rendered_page_png(MIXED_PDF.read_bytes(), 3)
    assert rows[0]["page_image_sha256"] == page_sha

    # -- still exactly one observation, on real Postgres
    stored = live.store.observations(attachment_id, 1, 3, 3)
    assert len(stored) == 1, "reuse must not persist a second version"
    assert stored[0].page_image_sha256 == page_sha
    sql = live_sql_rows(
        str(os.environ["FIN_CHECKPOINT_URL"]).strip(),
        "SELECT count(*) FROM fin_thread_attachment_observation WHERE attachment_id = %s",
        (attachment_id,),
    )
    assert int(sql[0][0]) == 1

    # -- and the second turn's answer is the same stored prose
    assert EXPECTED_VALUE_IN_ANSWER in _tool_payload(second_state)["observations"][0]["text"]


def test_a_wider_ask_than_the_stored_crop_is_not_reuse(
    live: _Live,
    deployed_bounds: dict[str, str],
    ledgers: _MintedLedgers,
) -> None:
    """A crop answer does not answer a whole-page question.

    The first turn is cut to the chart's own box, so its observation grants
    exactly those bounds. A later ask that names NO region is wider than that,
    and reading the narrow observation as answering it would be a claim about
    a page nobody was shown — so the lane inspects again and persists a
    second version.

    The second inspection is still cut to the same reliable box: which region
    is INSPECTED is a decision about the page's own persisted regions, not
    about the width of the ask. Only a page with no qualifying box falls back
    to the whole page, and the claim reports whichever scope was actually
    used.
    """
    attachment_id = "att-t3-wider"
    _upload(live, attachment_id, "g4-mixed-chart-001.pdf", MIXED_PDF)
    _mark_ready_pdf(live, attachment_id, pages=3, region=GOLD_CHART_REGION)

    first_state, _first_vision = _turn(
        live, deployed_bounds, ledgers, attachment_id, scope=CHART_SCOPE
    )
    assert first_state["visual_claims"][0]["scope"] == pytest.approx(CHART_SCOPE)

    # The same target, asked with no scope at all: the whole page.
    second_state, second_vision = _turn(live, deployed_bounds, ledgers, attachment_id)

    assert len(second_vision.calls) == 1, (
        "a crop observation cannot answer a whole-page question"
    )
    row = second_state["visual_claims"][0]
    assert row["reused"] is False
    assert row["observation_version"] == 2
    # The inspected scope is reported truthfully: the same reliable box, cut
    # again — not widened to the page the ask named.
    assert row["scope"] == pytest.approx(CHART_SCOPE)
    assert second_vision.calls[0]["crop"] is not None
    assert len(live.store.observations(attachment_id, 1, 3, 3)) == 2


def test_the_claim_reaches_run_state_and_the_wire_without_becoming_a_quote(
    live: _Live,
    postgres_url: str,
    deployed_bounds: dict[str, str],
    ledgers: _MintedLedgers,
    live_sql_rows: Callable[..., list[Any]],
) -> None:
    """Persisted run state and the outward channel carry it; citations do not.

    The store write and the wire normalisation are the two outward legs; both
    are driven here from the same real run state the live lane produced, and
    the quote channel is checked to be untouched.
    """
    from fin.host.thread_store import RunRecord
    from fin.host.workspace_http import _visual_claims_wire_payload

    attachment_id = "att-t3-wire"
    _object_key, _payload = _upload(live, attachment_id, "g4-mixed-chart-001.pdf", MIXED_PDF)
    _mark_ready_pdf(live, attachment_id, pages=3, region=GOLD_CHART_REGION)

    service, vision = _recording_service(live.store, deployed_bounds)
    graph = _compile(
        live.store,
        service,
        [_visual_call(_detail_args(attachment_id, 3)), AIMessage(content=ANSWER_SCRIPT)],
    )
    state = _run(graph, live.thread_id, ledgers)
    claim_rows = state["visual_claims"]
    assert len(claim_rows) == 1

    # -- the outward channel: the same row shape, projected as a frame body
    frames, refusals = _visual_claims_wire_payload(claim_rows)
    assert refusals == []
    assert len(frames) == 1
    # The token and its ``schema_version`` sibling travel together (spec
    # 2.1 rule 1, ADR 0093 decision 12), so the row key set is
    # VISUAL_CLAIM_FIELDS plus BOTH. This lane is ``mcp``-marked and is
    # skipped without a vision credential, so an assertion here is only
    # ever exercised where the real provider runs — which is exactly why
    # it must state the full shape rather than the pre-versioning one.
    assert list(frames[0]) == list(VISUAL_CLAIM_FIELDS) + ["schema", "schema_version"]
    assert frames[0]["schema"] == "fin.evidence.visual_claim"
    assert frames[0]["schema_version"] == EVIDENCE_SCHEMA_VERSION

    # -- persisted run state, on real Postgres
    run_id = f"run-t3-{uuid.uuid4().hex[:12]}"
    live.threads.append_run(
        RunRecord(
            id=run_id,
            thread_id=live.thread_id,
            question=GROUNDED_ASK,
            status=state.get("status", "refused"),
            status_reason=str(state.get("status_reason") or "visual_only_run"),
            answer=ANSWER_SCRIPT,
            citations=[],
            thinking_text="",
            thinking_duration_ms=0,
            tool_summaries=[],
            assistant_message=ANSWER_SCRIPT,
            track_decision={},
            comparisons=[],
            analysis_results=[],
            visual_claims=claim_rows,
        )
    )
    runs = live.threads.list_runs(live.thread_id)
    persisted = [run for run in runs if run.id == run_id]
    assert len(persisted) == 1
    assert len(persisted[0].visual_claims) == 1
    assert persisted[0].visual_claims[0]["page_image_sha256"] == (
        claim_rows[0]["page_image_sha256"]
    )

    sql = live_sql_rows(
        postgres_url,
        "SELECT visual_claims::text, citations::text FROM fin_run WHERE id = %s",
        (run_id,),
    )
    assert len(sql) == 1, sql
    assert json.loads(sql[0][0])[0]["kind"] == VISUAL_EVIDENCE_KIND

    # -- the quote channel is untouched, and the gate would refuse the row
    assert json.loads(sql[0][1]) in ([], None)
    assert state["citations"] == []
    assert state["evidence"] == []
    assert not any(
        getattr(item, "kind", None) == VISUAL_EVIDENCE_KIND for item in state["evidence"]
    )
    assert VISUAL_EVIDENCE_KIND not in QUOTABLE_EVIDENCE_KINDS


# ----- failure legs that cross a real boundary --------------------------


def test_a_stored_raster_the_bytes_no_longer_produce_is_not_reused(
    live: _Live,
    deployed_bounds: dict[str, str],
    ledgers: _MintedLedgers,
    live_sql_rows: Callable[..., list[Any]],
    live_sql_write: Callable[..., int],
) -> None:
    """A hash mismatch live: the stored row is refused, not trusted.

    The row this run reads is tampered with *in real Postgres* — a real
    stored hash for pixels the bucket no longer produces. A build that reused
    it would be citing a page nobody has seen; this run must inspect again,
    charge again, and persist a fresh version.
    """
    attachment_id = "att-t3-hash"
    _object_key, _payload = _upload(live, attachment_id, "g4-mixed-chart-001.pdf", MIXED_PDF)
    _mark_ready_pdf(live, attachment_id, pages=3, region=GOLD_CHART_REGION)

    _first_state, first_vision = _turn(live, deployed_bounds, ledgers, attachment_id)
    assert len(first_vision.calls) == 1

    # Tamper the real row so its stored hash is one no re-render can produce.
    real_url = str(os.environ["FIN_CHECKPOINT_URL"]).strip()
    TAMPERED_SHA = "f" * 64
    assert live_sql_write(
        real_url,
        "UPDATE fin_thread_attachment_observation SET page_image_sha256 = %s "
        "WHERE attachment_id = %s AND page = 3",
        (TAMPERED_SHA, attachment_id),
    ) == 1
    assert live_sql_rows(
        real_url,
        "SELECT page_image_sha256 FROM fin_thread_attachment_observation "
        "WHERE attachment_id = %s AND page = 3",
        (attachment_id,),
    )[0][0] == TAMPERED_SHA, "the tamper must actually be what the run reads"

    state, second_vision = _turn(live, deployed_bounds, ledgers, attachment_id)
    ledger2 = ledgers.last()

    assert len(second_vision.calls) == 1, "an unreproducible hash must not be reused"
    assert ledger2.calls_spent == 1
    row = state["visual_claims"][0]
    assert row["reused"] is False
    assert row["observation_version"] == 2, "a fresh inspection mints the next version"
    assert row["page_image_sha256"] != TAMPERED_SHA, (
        "the fresh version must carry the hash of the bytes, not the "
        "tampered row's"
    )
    # The fresh version is the hash a real re-render of the real bytes gives.
    _png, page_sha = _re_rendered_page_png(MIXED_PDF.read_bytes(), 3)
    assert row["page_image_sha256"] == page_sha


def test_a_real_provider_failure_refuses_the_ask_and_charges_the_attempt(
    live: _Live,
    deployed_bounds: dict[str, str],
    ledgers: _MintedLedgers,
) -> None:
    """Provider failure live: a real 401 from the real provider, typed out.

    The credential is deliberately wrong here, so the boundary crossed is the
    provider's own — a real HTTP round trip that answers, not a stubbed
    exception. The lane must refuse with ``vision_call_failed`` and the
    ledger must still show the attempt was spent.
    """
    from fin.host.attachment.attachment_vision import (
        ChatModelVisionAdapter,
        VisionCallFailedError,
        VisionPortError,
    )
    from fin.host.evidence.visual_evidence import VISUAL_PROVIDER_FAILED

    attachment_id = "att-t3-provider"
    _object_key, _payload = _upload(live, attachment_id, "g4-mixed-chart-001.pdf", MIXED_PDF)
    _mark_ready_pdf(live, attachment_id, pages=3, region=GOLD_CHART_REGION)

    budgets = _budgets(deployed_bounds)
    service = _LiveService(live.store, budgets)
    # A real adapter pointed at the real provider with a credential it will
    # reject: the request leaves the process and comes back refused.
    service.vision = ChatModelVisionAdapter(env={**os.environ, "FIN_API_KEY": "invalid-key"})
    graph = _compile(
        live.store,
        service,
        [_visual_call(_detail_args(attachment_id, 3)), AIMessage(content=ANSWER_SCRIPT)],
    )
    state = _run(graph, live.thread_id, ledgers)
    ledger = ledgers.last()

    assert _tool_payload(state) == {"error": VISUAL_PROVIDER_FAILED}
    assert state["visual_claims"] == []
    assert ledger.calls_spent >= 1, "a failed attempt is still spent"
    # And nothing was persisted for a call that never answered.
    assert live.store.observations(attachment_id, 1, 3, 3) == ()
    assert issubclass(VisionCallFailedError, VisionPortError)


def test_a_real_exhausted_budget_refuses_before_the_provider_is_asked(
    live: _Live,
    deployed_bounds: dict[str, str],
    ledgers: _MintedLedgers,
) -> None:
    """Budget exhaustion live: the run really spends its deployed allowance.

    The graph mints a fresh ledger per entry, so an exhausted ledger cannot be
    handed in — the only honest way to exhaust the ceiling is for the run to
    really spend it. This run asks for one more distinct plotted value than
    the deployed crop allowance covers, so the product's own charge door
    refuses the last ask before its request leaves the process. The
    ``ScriptedChatModel`` is only the plan; every call up to the ceiling is a
    real provider request against a real crop of the real gold page.
    """
    attachment_id = "att-t3-budget"
    _object_key, _payload = _upload(live, attachment_id, "g4-mixed-chart-001.pdf", MIXED_PDF)
    _mark_ready_pdf(live, attachment_id, pages=3, region=GOLD_CHART_REGION)

    ceiling = _budgets(deployed_bounds).max_vision_crop_calls_per_run
    service, vision = _recording_service(live.store, deployed_bounds)
    # One ask per chargeable crop, plus one over: the last one meets a run
    # with no crop headroom left.
    asks = [
        _visual_call(_detail_args(attachment_id, 3), call_id=f"c-visual-{index}")
        for index in range(ceiling + 1)
    ]
    for index, ask in enumerate(asks):
        ask.tool_calls[0]["args"]["category"] = f"2023 Q3 ask {index}"
    graph = _compile(
        live.store, service, [*asks, AIMessage(content=ANSWER_SCRIPT)]
    )

    state = _run(graph, live.thread_id, ledgers)
    ledger = ledgers.last()

    assert len(vision.calls) == ceiling, (
        "exactly the deployed allowance may reach the provider; the ask past "
        "it must be refused at the charge door, not after a request"
    )
    assert ledger.crop_calls_spent == ceiling
    assert ledger.calls_spent == ceiling

    payloads = [
        json.loads(
            str(
                m.content
            )
        )
        for m in state["messages"]
        if isinstance(m, ToolMessage)
        and getattr(m, "name", None) == "read_visual_detail"
    ]
    assert len(payloads) == ceiling + 1
    # Every ask inside the allowance answered, in order, and the last one was
    # refused at the charge door. A refusal inside the allowance would mean
    # the ceiling is not the only thing bounding the run, so the codes are
    # named rather than assumed.
    for index, item in enumerate(payloads[:ceiling]):
        assert "observations" in item, (
            f"ask {index} answered inside the allowance, so this was not a "
            f"budget refusal: {item}"
        )
    assert payloads[ceiling] == {"error": VISUAL_BUDGET_EXCEEDED}

    # The over-budget ask contributed nothing: no claim row, and the ceiling
    # of observations — not one more.
    assert len(state["visual_claims"]) == ceiling
    assert len(live.store.observations(attachment_id, 1, 3, 3)) == ceiling


def _assert_the_lane_was_never_reached(
    state: dict[str, Any],
    *,
    vision: _RecordingVision,
    objects: _CountingObjects,
    renderer: _RecordingRenderer,
    ledger: VisionCallLedger,
    reason: str,
) -> None:
    """Every expensive door reported nothing, for one refused ask.

    Four separate witnesses, because a refusal that reads the page anyway is a
    different product from the refusal the scope gate promises: the bucket was
    not opened, no page was rastered, no request left the process, the run's own
    ceiling was not charged, and nothing was claimed.
    """
    assert state["visual_claims"] == [], f"{reason}: a refusal claims nothing"
    assert objects.reads == [], f"{reason}: a refusal must not read stored bytes"
    assert renderer.drawn == [], f"{reason}: a refusal must not draw a page"
    assert vision.calls == [], f"{reason}: a refusal must not call the provider"
    assert (
        ledger.calls_spent,
        ledger.full_page_calls_spent,
        ledger.crop_calls_spent,
        ledger.retries_spent,
    ) == (0, 0, 0, 0), f"{reason}: a refusal must not spend the run's ceiling"


def test_another_thread_and_a_tombstoned_row_are_invisible_live(
    live: _Live,
    deployed_bounds: dict[str, str],
    ledgers: _MintedLedgers,
    live_sql_rows: Callable[..., list[Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cross-thread and tombstone refusals, against real rows and real bytes.

    Both are live because both are rows the real store holds: another
    thread's attachment, and one this thread has just voided. Neither may be
    rendered, and neither may be claimed.

    The refusal each one gets is the scope gate's own code,
    ``scope_attachment_not_permitted``, not the lane's narrower
    ``attachment_unavailable``: an id this thread does not currently hold is
    outside the effective selection, so the ask is answered before the lane is
    entered at all. The hermetic graph-level twins of both legs — a run asking
    for another thread's row, and a run asking after ``delete_for_thread`` voided
    its own — pin the same code and the same zero spend, so the codes below are
    transferred from proven behavior rather than guessed.
    """
    from fin.corpus.object_store import create_object_store

    attachment_id = "att-t3-scope"
    object_key, _payload = _upload(live, attachment_id, "g4-mixed-chart-001.pdf", MIXED_PDF)
    _mark_ready_pdf(live, attachment_id, pages=3, region=GOLD_CHART_REGION)

    other = live.threads.create_thread(title="T3 other thread").id
    try:
        service, vision, objects, renderer = _witnessed_service(
            live.store, deployed_bounds, monkeypatch
        )
        # One graph per case: ``ScriptedChatModel`` consumes its script, so a
        # second run on the same graph would have no plan left to emit and
        # would prove nothing about the lane.
        foreign = _run(
            _compile(
                live.store,
                service,
                [
                    _visual_call(_detail_args(attachment_id, 3)),
                    AIMessage(content=ANSWER_SCRIPT),
                ],
            ),
            other,
            ledgers,
        )
        assert _tool_payload(foreign) == {"error": SCOPE_ATTACHMENT_NOT_PERMITTED}
        _assert_the_lane_was_never_reached(
            foreign,
            vision=vision,
            objects=objects,
            renderer=renderer,
            ledger=ledgers.last(),
            reason="another thread's attachment",
        )

        # Void this thread's own attachment: the bytes stay owed, the row goes.
        keys = live.store.delete_for_thread(live.thread_id)
        assert object_key in keys
        assert live.store.get(attachment_id) is None

        voided = _run(
            _compile(
                live.store,
                service,
                [
                    _visual_call(_detail_args(attachment_id, 3)),
                    AIMessage(content=ANSWER_SCRIPT),
                ],
            ),
            live.thread_id,
            ledgers,
        )
        assert _tool_payload(voided) == {"error": SCOPE_ATTACHMENT_NOT_PERMITTED}
        _assert_the_lane_was_never_reached(
            voided,
            vision=vision,
            objects=objects,
            renderer=renderer,
            ledger=ledgers.last(),
            reason="a voided row",
        )

        # The tombstone really exists in real Postgres and really owns the key.
        url = str(os.environ["FIN_CHECKPOINT_URL"]).strip()
        rows = live_sql_rows(
            url,
            "SELECT thread_id, object_key FROM fin_thread_attachment_tombstone "
            "WHERE attachment_id = %s",
            (attachment_id,),
        )
        assert len(rows) == 1, rows
        assert str(rows[0][0]) == live.thread_id
        assert str(rows[0][1]) == object_key

        # Pay the debt for real, so the live run leaves no owed bytes.
        create_object_store().delete(object_key)
        if object_key in live.object_keys:
            live.object_keys.remove(object_key)
        live.store.confirm_cleanup(attachment_id=attachment_id)
    finally:
        live.threads.delete_thread(other)


def test_a_selection_that_excludes_the_target_refuses_before_the_bucket_is_opened_live(
    live: _Live,
    deployed_bounds: dict[str, str],
    ledgers: _MintedLedgers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The scope decision, taken with real bytes on both sides of the door.

    A thread holds two real attachments and the accepted selection names one of
    them. The refused ask is the attachment the selection excludes: the answer is
    the gate's code and NO expensive door opened — no byte came out of the
    bucket, pypdfium2 drew nothing, the model was never asked, and the run's
    minted ceiling is untouched. The permitted ask then runs on the same wiring
    and the same witnesses, and every one of them moves, so the zeros above are
    a refusal and not a lane that could not have run.

    This is the leg the hermetic boundary suites cannot close: they prove the
    host refuses before it spends, against fixture bytes and a recording port.
    Here the selection, the exclusion, the stored bytes, the render, and the
    provider call are all the real ones.
    """
    held_id = "att-t3-scoped-held"
    excluded_id = "att-t3-scoped-excluded"
    _upload(live, held_id, "g4-mixed-chart-001.pdf", MIXED_PDF)
    _mark_ready_pdf(live, held_id, pages=3, region=GOLD_CHART_REGION)
    _upload(live, excluded_id, "g4-mixed-chart-001.pdf", MIXED_PDF)
    _mark_ready_pdf(live, excluded_id, pages=3, region=GOLD_CHART_REGION)

    service, vision, objects, renderer = _witnessed_service(
        live.store, deployed_bounds, monkeypatch
    )
    selection = ScopeRestriction((held_id,), "explicit")

    refused = _run(
        _compile(
            live.store,
            service,
            [
                _visual_call(_detail_args(excluded_id, 3)),
                AIMessage(content=ANSWER_SCRIPT),
            ],
            scope_restriction=selection,
        ),
        live.thread_id,
        ledgers,
    )
    assert _tool_payload(refused) == {"error": SCOPE_ATTACHMENT_NOT_PERMITTED}
    _assert_the_lane_was_never_reached(
        refused,
        vision=vision,
        objects=objects,
        renderer=renderer,
        ledger=ledgers.last(),
        reason="an attachment the accepted selection excludes",
    )
    # The exclusion is a fact about this run's selection, not about the row:
    # the excluded attachment is ready, this thread's own, and its bytes are in
    # the bucket. A refusal that only looked at readiness would have drawn it.
    assert live.store.get(excluded_id) is not None
    assert objects.reads == []

    granted = _run(
        _compile(
            live.store,
            service,
            [
                _visual_call(_detail_args(held_id, 3)),
                AIMessage(content=ANSWER_SCRIPT),
            ],
            scope_restriction=selection,
        ),
        live.thread_id,
        ledgers,
    )
    assert "observations" in _tool_payload(granted), _tool_payload(granted)
    assert objects.reads, "the permitted leg must open the real bucket"
    assert renderer.drawn == [3], "the permitted leg must draw the real page"
    assert len(vision.calls) == 1, "the permitted leg must ask the real provider"
    assert ledgers.last().calls_spent == 1

