"""Zero-read proof at the visual scope boundary (spec SC-3, decisions 4/5/7, ADR 0089).

The claim this suite exists to pin is a NEGATIVE one with a price attached: a
refused visual ask must cost NOTHING beyond the authority metadata reads that
identify the target. No page raster is drawn, no stored object byte is read, no
provider call leaves the process, and the run's visual-call ledger stays at
zero. That is why every case here counts calls instead of only reading the
typed code — a refusal that spends a vision call is a different product from
the refusal the spec promises.

The shape of the proof:

* one lane, wired exactly as the deployment wires it
  (:func:`~fin.host.graph._make_visual_faces_factory` over a real
  ``InMemoryAttachmentStore``, real ``AttachmentBudgets``, and the graph's own
  :func:`~fin.host.graph._make_visual_ledger_factory`), shared by the permitted
  control and every refusal, so a zero count means "the lane was never reached"
  and not "the lane was never built";
* the permitted ask first, to prove those witnesses actually move;
* then one case per distinct refusal cause, each asserting the exact typed
  code, that the per-run faces factory was never invoked, that the vision port,
  the page renderer, and the byte-store face were never called, that the ledger
  charged nothing, and that the run claimed nothing;
* readiness kept distinct from permission (an authorized-but-not-ready row
  answers ``unavailable`` and still draws nothing) and an unconsultable
  authority kept distinct from a denial (``dependency_failed``, again with zero
  spend) — decision 7's vocabulary at the most expensive boundary;
* decision 4's version rule at the visual leg: the version the gate authorizes
  and the products the read binds are the CURRENT ones the trusted store
  answers, so a re-preparation moves the read onto the new version or is
  refused, and a superseded page region is never drawn.

Boundary honesty (spec decision 10): the vision provider here is a recording
fake and the object bytes are fixture bytes, so this module proves the
host-side zero-spend rule, not the live chain. The real store / renderer /
provider round trip is the opt-in ``-m mcp`` live visual lane
(``tests/host/test_live_graph_visual_lane.py``); nothing in this file
substitutes for it.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any, Final, Literal, Mapping

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from fin.corpus.parsers._pages import PageRaster
from fin.host import ScriptedChatModel, compile_graph
from fin.host.attachment_budgets import AttachmentBudgets, VisionCallLedger
from fin.host.attachment_index import build_postings
from fin.host.attachment_source_read import AttachmentSourceReader
from fin.host.attachment_store import (
    AttachmentRecord,
    AttachmentStoreError,
    InMemoryAttachmentStore,
    ParsedAttachmentUnit,
    StoredPageRegion,
)
from fin.host.attachment_vision import VisionCallResult, VisionUsage
from fin.host.graph import (
    _make_read_visual_detail_tool,
    _make_visual_faces_factory,
    _make_visual_ledger_factory,
)
from fin.host.source_scope import (
    SCOPE_ATTACHMENT_NOT_PERMITTED,
    SCOPE_ATTACHMENT_NOT_READY,
    SCOPE_ATTACHMENT_UNKNOWN,
    SCOPE_ATTACHMENT_VERSION_STALE,
    SCOPE_DEPENDENCY_FAILED,
    SCOPE_SOURCE_REF_UNREADABLE,
    SCOPE_THREAD_UNBOUND,
    ScopeRestriction,
)
from fin.host.state import reset_input_state
from fin.host.thread_store import InMemoryThreadStore
from fin.retrieve.fake_port import FakeRetrievePort

REPO_ROOT = Path(__file__).resolve().parents[2]
# The committed, versioned G4 gold case whose page 3 carries a real chart
# region: a claim about a region is then a claim about the fixture's own
# pixels, not about a drawn stand-in.
GOLD_MIXED_PDF = (
    REPO_ROOT / "data" / "corpus" / "g4_attachment_gold" / "g4-mixed-chart-001.pdf"
)
# The manifest's own box for that chart, on the page's 612 x 792 pt media box.
GOLD_CHART_REGION: Final = StoredPageRegion(
    page=3,
    kind="figure",
    left_pt=42.0,
    top_pt=100.0,
    right_pt=570.0,
    bottom_pt=428.0,
)
GOLD_CROP_NORMALIZED: Final = (42.0 / 612.0, 100.0 / 792.0, 570.0 / 612.0, 428.0 / 792.0)

ATTACHMENT_ID = "att-scope-visual"
OBJECT_KEY = f"threads/holder/{ATTACHMENT_ID}"
PAGE_BYTES = b"%PDF-1.4 fake"
RASTER_SHA = "a" * 64
MODEL_ID = "g4-fake-vision-1"
OBSERVATION = "The bar reaches 12.3 billion USD on the right axis."
USAGE = VisionUsage(input_tokens=90, output_tokens=30, total_tokens=120)
PDF_PAGES = 3

DETAIL_ARGS = {
    "chart": "Revenue by segment",
    "series": "Services",
    "category": "Q3 2024",
}

# A research-track ask: the visual lane is a tool on the grounded track, so a
# clarify-routed question would never reach the tool node at all.
GROUNDED_ASK = "What was AAPL net sales in 2023 Q3?"

# The read order one named attachment goes through on a visual ask, used by
# :class:`AuthorityStore` to place a race in a chosen window:
#
# 0. scope resolution (the eligibility consult)
# 1. the boundary's own resolve of the model-named id
#    (:func:`~fin.host.graph._named_attachment_decision`)
# 2. the gate's authority / version re-check
# 3. the gate's readiness check
AuthorityShift = Literal["advance", "drop", "raise"]


# ---------------------------------------------------------------------------
# the faces, made observable
# ---------------------------------------------------------------------------


class RecordingVisionPort:
    """The vision port as a witness: every attempt that left the process."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def inspect(
        self, *, image: bytes, target: Any, media_type: str, question: str | None = None
    ) -> VisionCallResult:
        self.calls.append({"image": image, "target": target})
        return VisionCallResult(
            text=OBSERVATION,
            target=target,
            model_id=MODEL_ID,
            media_type=media_type,
            usage=USAGE,
            cost=None,
        )


class RecordingRasterizer:
    """The page renderer as a witness: every page it was asked to draw."""

    def __init__(self) -> None:
        self.drawn: list[int] = []

    def __call__(self, payload: bytes, page_no: int) -> PageRaster:
        self.drawn.append(page_no)
        return PageRaster(
            png=b"\x89PNG fixed raster", width_px=8, height_px=8, sha256=RASTER_SHA
        )


class RecordingObjects:
    """The byte store face as a witness: every stored-object read."""

    def __init__(self, payload: bytes = PAGE_BYTES) -> None:
        self.reads: list[str] = []
        self._payload = payload

    def get(self, key: str) -> bytes:
        self.reads.append(key)
        return self._payload


class CountingFacesFactory:
    """Wraps the run's real faces factory and records whether it was invoked.

    The factory is the single door through which the reader, the payload face,
    the renderer, and the ledger enter the lane, so "this was never called" is
    the strongest available statement that a refusal reached no page at all.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.builds: list[str] = []

    def __call__(self, thread_id: str, ledger: Any) -> Any:
        self.builds.append(thread_id)
        return self._inner(thread_id, ledger)


class AuthorityStore:
    """The trusted store as the boundary sees it: metadata reads counted.

    Wraps a real :class:`~fin.host.attachment_store.InMemoryAttachmentStore` so
    the authority facts come from the store's own answers, then changes that
    answer from the ``shift_after``-th read on: ``advance`` re-issues the row at
    a newer version (a re-preparation landing mid-decision), ``drop`` stops
    answering it (a tombstone in the same window), ``raise`` makes it
    unconsultable. Nothing here can draw a page or spend a call — it is the
    authority face only.
    """

    def __init__(
        self,
        inner: InMemoryAttachmentStore,
        *,
        shift_after: int | None = None,
        mode: AuthorityShift = "advance",
        list_raises: bool = False,
    ) -> None:
        self._inner = inner
        self._shift_after = shift_after
        self._mode = mode
        self._list_raises = list_raises
        self.gets: list[str] = []
        self.lists: list[str] = []

    def get(self, attachment_id: str) -> AttachmentRecord | None:
        index = len(self.gets)
        self.gets.append(attachment_id)
        shifted = self._shift_after is not None and index > self._shift_after
        if shifted and self._mode == "raise":
            raise AttachmentStoreError("attachment authority could not be consulted")
        record = self._inner.get(attachment_id)
        if record is None:
            return None
        if not shifted:
            return record
        if self._mode == "drop":
            return None
        return replace(record, version=record.version + 1)

    def list_for_thread(self, thread_id: str) -> list[AttachmentRecord]:
        self.lists.append(thread_id)
        if self._list_raises:
            raise AttachmentStoreError("attachment enumeration could not be consulted")
        return self._inner.list_for_thread(thread_id)

    def __getattr__(self, name: str) -> Any:  # the remaining store doors, unchanged
        return getattr(self._inner, name)


class Service:
    """The wired attachment seam, shaped like the deployed preparation service."""

    def __init__(
        self,
        store: Any,
        *,
        vision: RecordingVisionPort,
        objects: RecordingObjects,
    ) -> None:
        self.store = store
        self.vision = vision
        # A self-consistent deployed-shaped ceiling: every admissible page, the
        # crop headroom, and the one reserved full-page retry.
        self.budgets = AttachmentBudgets(
            max_upload_bytes=4 * 1024 * 1024,
            max_pdf_pages=PDF_PAGES + 1,
            max_vision_calls_per_run=2 * (PDF_PAGES + 1) + 1,
            max_vision_crop_calls_per_run=PDF_PAGES + 1,
        )
        self.objects = objects


class MintedLedgers:
    """The ledgers a compiled graph mints for its own runs, kept for reading.

    A graph entry builds the ceiling itself, so a run through
    :func:`~fin.host.compile_graph` cannot be handed one. Subclassing the
    product's own ledger and recording construction observes the REAL object —
    its counters are the ones the run charged — without substituting it.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import fin.host.attachment_budgets as budgets_module

        minted: list[VisionCallLedger] = []
        real = budgets_module.VisionCallLedger

        class _Collecting(real):  # type: ignore[misc,valid-type]
            def __init__(self, budgets: AttachmentBudgets) -> None:
                super().__init__(budgets)
                minted.append(self)

        monkeypatch.setattr(budgets_module, "VisionCallLedger", _Collecting)
        self.minted = minted

    def last(self) -> VisionCallLedger:
        assert self.minted, "the graph minted no visual ledger for this run"
        return self.minted[-1]


def _assert_ledger_spent_nothing(ledger: VisionCallLedger) -> None:
    """The run's own ceiling, untouched: the graph-level form of zero spend."""
    assert (
        ledger.calls_spent,
        ledger.full_page_calls_spent,
        ledger.crop_calls_spent,
        ledger.retries_spent,
    ) == (0, 0, 0, 0), "a refused run must leave its minted ceiling untouched"


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------


def _prepare(
    store: InMemoryAttachmentStore,
    attachment_id: str,
    *,
    version: int,
    pages: int = PDF_PAGES,
) -> None:
    """The derived products one preparation pass would publish for a version."""
    units = tuple(
        ParsedAttachmentUnit(unit=page, text=f"page {page} of version {version}")
        for page in range(1, pages + 1)
    )
    store.record_units(attachment_id=attachment_id, version=version, units=units)
    store.put_index(
        attachment_id=attachment_id, version=version, postings=build_postings(units)
    )
    store.mark_ready(attachment_id=attachment_id, version=version, unit_count=pages)


def _fresh_store(
    *, ready: bool = True, pages: int = PDF_PAGES
) -> tuple[InMemoryThreadStore, InMemoryAttachmentStore, str]:
    """One thread holding one PDF row, ready (or still preparing) at version 1."""
    threads = InMemoryThreadStore()
    store = InMemoryAttachmentStore(threads)
    thread_id = threads.create_thread(title="Chart thread").id
    store.create(
        thread_id=thread_id,
        attachment_id=ATTACHMENT_ID,
        filename="Segment Revenue.PDF",
        size_bytes=2_048,
        sha256="ab" * 32,
        object_key=OBJECT_KEY,
    )
    if ready:
        _prepare(store, ATTACHMENT_ID, version=1, pages=pages)
    return threads, store, thread_id


def _lane(
    monkeypatch: pytest.MonkeyPatch,
    store: InMemoryAttachmentStore,
    thread_id: str,
    *,
    restriction: ScopeRestriction | None = None,
    authority: AuthorityStore | None = None,
    gold: bool = False,
    threads: InMemoryThreadStore | None = None,
) -> dict[str, Any]:
    """One visual lane wired the way the deployment wires it, with witnesses.

    The faces factory and the run's ledger are the production ones. The vision
    port and the byte-store face are recording witnesses; the renderer is the
    production function for ``gold`` (the committed fixture really is drawn)
    and the recording stand-in otherwise, because the stub bytes are not a PDF
    and a real renderer would then refuse for a reason unrelated to the rule
    under test.
    """
    authority_store: Any = authority if authority is not None else store
    service = Service(
        authority_store,
        vision=RecordingVisionPort(),
        objects=RecordingObjects(
            GOLD_MIXED_PDF.read_bytes() if gold else PAGE_BYTES
        ),
    )
    rasterizer: RecordingRasterizer | None = None
    if not gold:
        rasterizer = RecordingRasterizer()
        monkeypatch.setattr(
            "fin.host.graph._visual_page_raster", rasterizer, raising=True
        )

    real_factory = _make_visual_faces_factory(
        service, lambda bound: AttachmentSourceReader(authority_store, bound)
    )
    assert real_factory is not None, "the page lane must be wired for these cases"
    ledger_factory = _make_visual_ledger_factory(service.budgets)
    assert ledger_factory is not None, "a deployed page lane mints its own ceiling"
    factory = CountingFacesFactory(real_factory)

    return {
        "threads": threads,
        "store": store,
        "service": service,
        "vision": service.vision,
        "objects": service.objects,
        "rasterizer": rasterizer,
        "thread_id": thread_id,
        "factory": factory,
        "tool": _make_read_visual_detail_tool(
            factory,
            attachment_service=service,
            scope_restriction=restriction,
        ),
        "ledger": ledger_factory(),
    }


def _ask(
    lane: dict[str, Any],
    *,
    attachment_id: str = ATTACHMENT_ID,
    thread_id: str | None = None,
    page: int = 1,
    unbound: bool = False,
) -> dict[str, Any]:
    """One visual ask through the tool, plus every witness read back from it."""
    configurable: dict[str, Any] = {"vision_call_ledger": lane["ledger"]}
    if not unbound:
        configurable["thread_id"] = (
            lane["thread_id"] if thread_id is None else thread_id
        )
    command = lane["tool"].invoke(
        {
            "name": "read_visual_detail",
            "args": {"attachment_id": attachment_id, "page": page, **DETAIL_ARGS},
            "id": "c-visual",
            "type": "tool_call",
        },
        config={"configurable": configurable},
    )
    messages: list[Any] = command.update["messages"]
    assert len(messages) == 1, f"one ToolMessage per ask: {messages}"
    return {
        "payload": json.loads(str(messages[0].content)),
        "claims": command.update["visual_claims"],
        "lane": lane,
    }


def _assert_zero_spend(answer: dict[str, Any]) -> None:
    """What a refusal must never touch: the lane, a page, bytes, the provider,
    the run's ceiling, and the claim channel."""
    lane = answer["lane"]
    factory: CountingFacesFactory = lane["factory"]
    assert factory.builds == [], "a refusal must not build the run's visual faces"
    assert lane["vision"].calls == [], "a refusal must not spend a vision call"
    if lane["rasterizer"] is not None:
        assert lane["rasterizer"].drawn == [], "a refusal must not draw a page"
    assert lane["objects"].reads == [], "a refusal must not read a stored object byte"
    _assert_ledger_spent_nothing(lane["ledger"])
    assert answer["claims"] == [], "a refusal claims nothing"


def _narrowing() -> ScopeRestriction:
    """The operator's narrowing to this one attachment — the ask is in scope."""
    return ScopeRestriction((ATTACHMENT_ID,), "explicit")


# ---------------------------------------------------------------------------
# the control: the witnesses move, so every zero below is a fact
# ---------------------------------------------------------------------------


def test_a_permitted_in_scope_visual_read_reaches_the_page_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A granted ask builds the lane, draws the page, reads the bytes, charges.

    Every zero-spend assertion in this module is worth reading only because
    THIS case proves each witness moves when the ask is permitted: an inert
    harness would show zero everywhere and pin nothing.
    """
    _threads, store, thread_id = _fresh_store()
    lane = _lane(monkeypatch, store, thread_id, restriction=_narrowing())

    answer = _ask(lane)

    assert "observations" in answer["payload"], answer["payload"]
    assert lane["factory"].builds == [thread_id]
    assert lane["rasterizer"].drawn == [1]
    assert len(lane["vision"].calls) == 1
    assert lane["objects"].reads, "a granted read re-reads the stored original bytes"
    assert lane["ledger"].calls_spent == 1
    assert len(answer["claims"]) == 1


# ---------------------------------------------------------------------------
# one case per distinct refusal cause, each with zero spend
# ---------------------------------------------------------------------------


def test_an_attachment_outside_the_effective_selection_draws_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A narrowing to one attachment refuses a visual ask about another."""
    _threads, store, thread_id = _fresh_store()
    lane = _lane(
        monkeypatch,
        store,
        thread_id,
        restriction=ScopeRestriction(("att-not-this-one",), "explicit"),
    )

    answer = _ask(lane)

    assert answer["payload"] == {"error": SCOPE_ATTACHMENT_NOT_PERMITTED}
    _assert_zero_spend(answer)


def test_a_foreign_threads_attachment_draws_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR 0088's trusted-thread rule at the visual boundary under a default scope.

    Nothing is in force: the effective selection is the asking thread's own
    attachments, and an id another thread holds is outside it.
    """
    threads, store, _thread_id = _fresh_store()
    other = threads.create_thread(title="Other thread").id
    lane = _lane(monkeypatch, store, other, threads=threads, restriction=None)

    answer = _ask(lane)

    assert answer["payload"] == {"error": SCOPE_ATTACHMENT_NOT_PERMITTED}
    _assert_zero_spend(answer)


def test_an_id_no_authority_stands_behind_after_resolution_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tombstone landing between the boundary's resolve and its re-check.

    The id was authorized when the decision resolved it and is gone when the
    gate re-checks: the answer names the identity unproven (``unknown``) rather
    than dressing the disappearance up as a version mismatch — and it draws
    nothing.
    """
    _threads, store, thread_id = _fresh_store()
    ghosting = AuthorityStore(store, shift_after=1, mode="drop")
    lane = _lane(
        monkeypatch, store, thread_id, restriction=_narrowing(), authority=ghosting
    )

    answer = _ask(lane)

    assert answer["payload"] == {"error": SCOPE_ATTACHMENT_UNKNOWN}, ghosting.gets
    assert len(ghosting.gets) >= 2, ghosting.gets
    _assert_zero_spend(answer)


def test_a_version_that_moves_mid_decision_is_refused_as_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A re-preparation between the boundary's resolve and the gate's re-check.

    The version the gate was handed is no longer what the trusted store holds,
    so the ask is refused instead of drawn from page products that describe
    superseded bytes. This is the only way the visual path can present a stale
    version at all: the ask itself names no version.
    """
    _threads, store, thread_id = _fresh_store()
    moving = AuthorityStore(store, shift_after=1, mode="advance")
    lane = _lane(
        monkeypatch, store, thread_id, restriction=_narrowing(), authority=moving
    )

    answer = _ask(lane)

    # The read order the shift relies on is documented at :data:`AuthorityShift`:
    # the boundary resolved version 1 and the gate's re-check found version 2.
    assert answer["payload"] == {"error": SCOPE_ATTACHMENT_VERSION_STALE}, moving.gets
    assert len(moving.gets) >= 3, moving.gets
    _assert_zero_spend(answer)


def test_an_unbound_thread_refuses_the_visual_ask_with_its_own_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run with no host-bound thread gets the thread cause, not the lane's.

    The refusal must be the gate's ``scope_thread_unbound``: the lane's own
    ``visual_lane_unwired`` would read as a deployment gap and hide that the
    ask never had a thread to be authorized against.
    """
    _threads, store, thread_id = _fresh_store()
    lane = _lane(monkeypatch, store, thread_id, restriction=_narrowing())

    answer = _ask(lane, unbound=True)

    assert answer["payload"] == {"error": SCOPE_THREAD_UNBOUND}
    _assert_zero_spend(answer)


def test_a_blank_attachment_id_is_an_unreadable_ref_not_a_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A model-supplied id that names no attachment cannot reach a page.

    The host builds the source ref from the model's string, so an empty value
    is refused as unreadable rather than resolving to some attachment, or to
    the whole selection.
    """
    _threads, store, thread_id = _fresh_store()
    lane = _lane(monkeypatch, store, thread_id, restriction=_narrowing())

    answer = _ask(lane, attachment_id="")

    assert answer["payload"] == {"error": SCOPE_SOURCE_REF_UNREADABLE}
    _assert_zero_spend(answer)


def test_an_authorized_but_not_ready_attachment_is_unavailable_and_draws_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Readiness is not permission: ``unavailable`` for a row still preparing.

    The row is this thread's and current, so the ask is no denial; the
    preparation lifecycle has simply not produced usable products yet. Zero
    page, zero bytes, zero provider, zero ledger: answering "cannot yet" costs
    nothing.
    """
    _threads, store, thread_id = _fresh_store(ready=False)
    lane = _lane(monkeypatch, store, thread_id, restriction=_narrowing())

    answer = _ask(lane)

    assert answer["payload"] == {"error": SCOPE_ATTACHMENT_NOT_READY}
    _assert_zero_spend(answer)


def test_a_resolver_that_raises_is_a_dependency_failure_not_a_denial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An authority that cannot be consulted says so, and spends nothing.

    The resolution consult itself raises: a transient outage must never be
    consumed as a permission answer, and it must never fall through to a page.
    """
    _threads, store, thread_id = _fresh_store()
    down = AuthorityStore(store, shift_after=-1, mode="raise")
    lane = _lane(
        monkeypatch, store, thread_id, restriction=_narrowing(), authority=down
    )

    answer = _ask(lane)

    assert answer["payload"] == {"error": SCOPE_DEPENDENCY_FAILED}
    _assert_zero_spend(answer)


def test_a_readiness_check_that_raises_is_a_dependency_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The distinct read the gate makes last: a faulting readiness probe.

    Identity and version answered; the readiness consult did not. The ask is
    refused as a dependency failure rather than being reported as not-ready or,
    worse, passed as permitted.
    """
    _threads, store, thread_id = _fresh_store()
    flaky = AuthorityStore(store, shift_after=2, mode="raise")
    lane = _lane(
        monkeypatch, store, thread_id, restriction=_narrowing(), authority=flaky
    )

    answer = _ask(lane)

    assert answer["payload"] == {"error": SCOPE_DEPENDENCY_FAILED}
    _assert_zero_spend(answer)


def test_an_enumeration_that_raises_is_a_dependency_failure_not_an_empty_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With nothing in force, a dead enumeration is not a successful empty.

    The default selection could not be assembled, so the visual ask answers
    ``dependency_failed`` — the honest difference between "authority answered
    and nothing matched" and "authority could not be asked".
    """
    _threads, store, thread_id = _fresh_store()
    blind = AuthorityStore(store, list_raises=True)
    lane = _lane(monkeypatch, store, thread_id, restriction=None, authority=blind)

    answer = _ask(lane)

    assert answer["payload"] == {"error": SCOPE_DEPENDENCY_FAILED}
    assert blind.lists, "the enumeration the gate could not consult was attempted"
    _assert_zero_spend(answer)


# ---------------------------------------------------------------------------
# decision 4 at the visual leg: the binding is the CURRENT version
# ---------------------------------------------------------------------------


def test_a_visual_ask_binds_the_stores_current_version_not_the_first_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ask names no version; the version that travels is the store's answer.

    The row was prepared at version 1 and preparation then published version 2.
    A host that carried the earlier version forward would answer with version
    1's products; the boundary resolves the CURRENT authority instead, so the
    claim names version 2 and the observation is written against version 2.
    """
    _threads, store, thread_id = _fresh_store()
    lane = _lane(monkeypatch, store, thread_id, restriction=_narrowing())

    _prepare(store, ATTACHMENT_ID, version=2)

    answer = _ask(lane)

    assert "observations" in answer["payload"], answer["payload"]
    assert [row["version"] for row in answer["claims"]] == ["2"], answer["claims"]
    assert store.observations(ATTACHMENT_ID, 2, 1, 1), "the read wrote version 2"
    assert store.observations(ATTACHMENT_ID, 1, 1, 1) == ()


def test_a_superseded_page_region_is_never_drawn_at_the_host_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The region the lane crops from is the CURRENT version's own geometry.

    The chart box was recorded for version 1 and version 2 carries no box: a
    host that found the region by id and page alone would crop pixels that
    describe bytes this row no longer is. With the production renderer on the
    committed gold fixture, the granted read reports the whole page instead —
    and the paired control shows the same box IS cropped on the version that
    owns it, so it is the version talking, not the page.
    """
    _threads, store, thread_id = _fresh_store()
    store.record_regions(
        attachment_id=ATTACHMENT_ID, version=1, regions=(GOLD_CHART_REGION,)
    )
    assert store.regions(ATTACHMENT_ID, 1, page=3) != ()
    _prepare(store, ATTACHMENT_ID, version=2)
    assert store.regions(ATTACHMENT_ID, 2, page=3) == ()
    lane = _lane(
        monkeypatch, store, thread_id, restriction=_narrowing(), gold=True
    )

    answer = _ask(lane, page=3)

    assert "observations" in answer["payload"], answer["payload"]
    assert answer["claims"][0]["version"] == "2", answer["claims"]
    assert answer["claims"][0]["scope"] is None, (
        "a superseded version's box must not become this read's crop: "
        f"{answer['claims']}"
    )


def test_the_current_versions_own_region_is_cropped_at_the_host_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control the case above needs in order to mean anything.

    Same fixture, same renderer, same ask — the box recorded for the version
    the row actually holds IS cropped, and the claim names that region. So the
    whole-page answer above was the version rule refusing a stale box, not a
    lane that cannot crop.
    """
    _threads, store, thread_id = _fresh_store()
    _prepare(store, ATTACHMENT_ID, version=2)
    store.record_regions(
        attachment_id=ATTACHMENT_ID, version=2, regions=(GOLD_CHART_REGION,)
    )
    lane = _lane(
        monkeypatch, store, thread_id, restriction=_narrowing(), gold=True
    )

    answer = _ask(lane, page=3)

    assert "observations" in answer["payload"], answer["payload"]
    assert answer["claims"][0]["version"] == "2", answer["claims"]
    assert answer["claims"][0]["scope"] == pytest.approx(GOLD_CROP_NORMALIZED)
    assert lane["ledger"].calls_spent == 1


# ---------------------------------------------------------------------------
# the vocabulary stays closed: no refusal arrives without a named cause
# ---------------------------------------------------------------------------


def test_every_visual_refusal_codes_from_the_gates_own_closed_vocabulary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Six causes, six distinct codes, none collapsed onto another.

    A matrix over the causes the visual boundary can meet, asserting each one
    arrives with its own code and with zero spend: a run that could only report
    "denied" would be indistinguishable from one that keeps an outage apart from
    a refusal, and the model could not tell "re-upload it" from "widen the
    scope" apart.
    """
    cases: list[tuple[str, Mapping[str, Any], str]] = [
        ("outside", {"restriction": ScopeRestriction(("att-x",), "explicit")},
         SCOPE_ATTACHMENT_NOT_PERMITTED),
        ("gone-after-resolution",
         {"restriction": _narrowing(),
          "authority": lambda store: AuthorityStore(store, shift_after=1, mode="drop")},
         SCOPE_ATTACHMENT_UNKNOWN),
        ("moved-mid-decision",
         {"restriction": _narrowing(),
          "authority": lambda store: AuthorityStore(store, shift_after=1, mode="advance")},
         SCOPE_ATTACHMENT_VERSION_STALE),
        ("unbound", {"restriction": _narrowing(), "unbound": True},
         SCOPE_THREAD_UNBOUND),
        ("not-ready",
         {"restriction": _narrowing(), "ready": False},
         SCOPE_ATTACHMENT_NOT_READY),
        ("authority-down",
         {"restriction": _narrowing(),
          "authority": lambda store: AuthorityStore(store, shift_after=-1, mode="raise")},
         SCOPE_DEPENDENCY_FAILED),
    ]
    seen: set[str] = set()

    for label, shape, expected in cases:
        _threads, store, thread_id = _fresh_store(ready=shape.get("ready", True))
        factory = shape.get("authority")
        authority = factory(store) if factory is not None else None
        lane = _lane(
            monkeypatch,
            store,
            thread_id,
            restriction=shape["restriction"],
            authority=authority,
        )

        answer = _ask(lane, unbound=bool(shape.get("unbound")))

        assert answer["payload"] == {"error": expected}, f"{label}: {answer['payload']}"
        _assert_zero_spend(answer)
        seen.add(expected)

    assert seen == {
        SCOPE_ATTACHMENT_NOT_PERMITTED,
        SCOPE_ATTACHMENT_UNKNOWN,
        SCOPE_ATTACHMENT_VERSION_STALE,
        SCOPE_THREAD_UNBOUND,
        SCOPE_ATTACHMENT_NOT_READY,
        SCOPE_DEPENDENCY_FAILED,
    }, "two causes collapsed onto one code at the visual boundary"


# ---------------------------------------------------------------------------
# the shipped graph carries the restriction to the visual tool
# ---------------------------------------------------------------------------


def _compiled_run(
    monkeypatch: pytest.MonkeyPatch,
    store: InMemoryAttachmentStore,
    thread_id: str,
    *,
    restriction: ScopeRestriction | None,
    attachment_id: str,
) -> dict[str, Any]:
    """The shipped graph, with a plan that asks for one page.

    The scope restriction is handed to :func:`~fin.host.compile_graph` the way
    the Runtime hands it to a run, so these cases decide whether the accepted
    selection actually reaches the visual tool through the wiring a run uses —
    not only through a directly built tool. The ledger reported back is the one
    THIS graph entry minted, so the run's ceiling is read, not assumed.
    """
    service = Service(
        store, vision=RecordingVisionPort(), objects=RecordingObjects()
    )
    rasterizer = RecordingRasterizer()
    monkeypatch.setattr(
        "fin.host.graph._visual_page_raster", rasterizer, raising=True
    )
    minted = MintedLedgers(monkeypatch)
    model = ScriptedChatModel(
        script=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "read_visual_detail",
                        "args": {
                            "attachment_id": attachment_id,
                            "page": 1,
                            **DETAIL_ARGS,
                        },
                        "id": "c-visual",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="The page plots that."),
        ]
    )
    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        adapter=FakeRetrievePort(hits_by_query={}, known_document_ids=set()),
        known_document_ids=set(),
        attachment_service=service,
        attachment_source_reader_factory=(
            lambda bound: AttachmentSourceReader(store, bound)
        ),
        scope_restriction=restriction,
    )
    state = graph.invoke(
        reset_input_state(GROUNDED_ASK),
        config={"configurable": {"thread_id": thread_id}},
    )
    messages = [
        m
        for m in state["messages"]
        if isinstance(m, ToolMessage) and getattr(m, "name", None) == "read_visual_detail"
    ]
    assert len(messages) == 1, f"one visual ToolMessage per run: {messages}"
    return {
        "state": state,
        "payload": json.loads(str(messages[0].content)),
        "vision": service.vision,
        "objects": service.objects,
        "rasterizer": rasterizer,
        "ledger": minted.last(),
    }


def _assert_graph_refusal_and_zero_spend(
    run: dict[str, Any], expected_code: str
) -> None:
    """The shipped-graph form of the same promise: a named cause and nothing spent.

    The graph-level witnesses hang on the run itself — its state and the ceiling
    THIS graph entry minted — so the refusal cases state this block once here
    instead of each restating it.
    """
    assert run["payload"] == {"error": expected_code}
    assert run["rasterizer"].drawn == []
    assert run["vision"].calls == []
    assert run["objects"].reads == []
    assert run["state"]["visual_claims"] == []
    _assert_ledger_spent_nothing(run["ledger"])


def test_a_restricted_run_refuses_an_out_of_scope_page_in_the_shipped_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The narrowing reaches the visual tool through the real graph wiring.

    A run whose accepted selection is one attachment asks about another: the
    model sees the gate's typed code inside its own turn, and the run leaves no
    page drawn, no stored byte read, no provider call, and no claim row behind.
    """
    _threads, store, thread_id = _fresh_store()

    run = _compiled_run(
        monkeypatch,
        store,
        thread_id,
        restriction=ScopeRestriction(("att-not-this-one",), "explicit"),
        attachment_id=ATTACHMENT_ID,
    )

    _assert_graph_refusal_and_zero_spend(run, SCOPE_ATTACHMENT_NOT_PERMITTED)


def test_a_run_asked_for_another_threads_attachment_is_refused_in_the_shipped_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A default-scope run cannot page another thread's attachment, live shape.

    Nothing is in force, so the effective selection is the asking thread's own
    rows; the id belongs to a different thread and the answer is the membership
    refusal. This is the arrangement the live lane reproduces against real
    Postgres rows, pinned here so its expected code is not a guess.
    """
    threads, store, _thread_id = _fresh_store()
    other = threads.create_thread(title="Other thread").id

    run = _compiled_run(
        monkeypatch,
        store,
        other,
        restriction=None,
        attachment_id=ATTACHMENT_ID,
    )

    _assert_graph_refusal_and_zero_spend(run, SCOPE_ATTACHMENT_NOT_PERMITTED)


def test_a_voided_attachment_is_refused_before_any_page_in_the_shipped_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A row this thread has voided is outside its own selection, not inside it.

    The delete goes through the store's own door, so the enumeration no longer
    names the id and the record answers nothing. The run must not fall through
    to the lane's narrower "unavailable" answer: a voided attachment is refused
    as a member of the selection, before a byte is read or a page is drawn.
    """
    _threads, store, thread_id = _fresh_store()
    assert store.delete_for_thread(thread_id) == [OBJECT_KEY]
    assert store.get(ATTACHMENT_ID) is None

    run = _compiled_run(
        monkeypatch,
        store,
        thread_id,
        restriction=None,
        attachment_id=ATTACHMENT_ID,
    )

    _assert_graph_refusal_and_zero_spend(run, SCOPE_ATTACHMENT_NOT_PERMITTED)


def test_a_restricted_run_reads_its_own_attachment_in_the_shipped_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control the case above needs: the same graph does read an in-scope page.

    Without this, the refusal above could be read as the lane being unwired in a
    compiled graph. With it, the only difference between the two runs is the
    accepted selection.
    """
    _threads, store, thread_id = _fresh_store()

    run = _compiled_run(
        monkeypatch,
        store,
        thread_id,
        restriction=_narrowing(),
        attachment_id=ATTACHMENT_ID,
    )

    assert "observations" in run["payload"], run["payload"]
    assert run["rasterizer"].drawn == [1]
    assert len(run["vision"].calls) == 1
    assert run["objects"].reads
    assert len(run["state"]["visual_claims"]) == 1
    # The witness moved at the graph level too: the run's own minted ceiling was
    # charged for the one call it made, so the zeros above are refusals.
    assert run["ledger"].calls_spent == 1


def test_the_visual_gate_is_the_only_door_for_an_excluded_attachment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sensitivity witness, not a behavior claim: remove the refusal, pay the price.

    An attachment excluded by the accepted selection is still this thread's own,
    still ready, and its bytes are still in the store — so nothing but the scope
    refusal stands between that ask and a real page. With the refusal made a
    no-op the same wiring draws, reads, calls, and charges. That is the strongest
    statement available that the zeros in the refusal cases above are the gate
    holding and not a lane that was unable to run.
    """
    import fin.host.graph as graph_module

    monkeypatch.setattr(graph_module, "_visual_scope_refusal", lambda *_a: None)
    _threads, store, thread_id = _fresh_store()

    run = _compiled_run(
        monkeypatch,
        store,
        thread_id,
        restriction=ScopeRestriction(("att-not-this-one",), "explicit"),
        attachment_id=ATTACHMENT_ID,
    )

    assert "observations" in run["payload"], run["payload"]
    assert run["rasterizer"].drawn == [1]
    assert len(run["vision"].calls) == 1
    assert run["objects"].reads
    assert run["ledger"].calls_spent == 1

