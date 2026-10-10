"""The visual lane decided inside the wired graph (ADR 0089, spec T5).

``read_visual_detail`` is the run-side half of the visual clause: the
current run reads the matching persisted observation, the token names
attachment / page / crop provenance, and a sufficient stored observation is
reused without a second call. The lane is already pinned at its own seam by
``tests/host/test_attachment_visual_read.py``; what is only decidable here
is what a RUN does with it:

* the tool is bound, and the operator is told about it, exactly when this
  process could actually inspect a page;
* a granted read writes provenance rows to ``visual_claims`` and prose to
  the ToolMessage, and the two never swap places;
* a generated observation cannot become a source quote even when it is the
  only thing the model has — the run refuses rather than fabricating a span;
* the claims belong to the run that proved the raster, so a follow-up send
  starts with none;
* a page another thread uploaded is invisible, and nothing is drawn for it.

Hermetic: the real in-memory attachment store and thread-bound reader, a
fake vision port, a fake renderer, ``ScriptedChatModel``, ``InMemorySaver``.
No network, no live model, no Postgres, no PDF library.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from fin.corpus.parsers._pages import PageRaster, Pypdfium2PageRenderer
from fin.host import ScriptedChatModel, compile_graph
from fin.host.agent_instructions import (
    VISUAL_LANE_INSTRUCTIONS_MARKER,
    messages_for_model,
)
from fin.host.attachment_budgets import (
    AttachmentBudgets,
    VisionCallLedger,
)
from fin.host.attachment_index import build_postings
from fin.host.attachment_source_read import AttachmentSourceReader
from fin.host.attachment_store import (
    InMemoryAttachmentStore,
    ParsedAttachmentUnit,
    StoredPageRegion,
)
from fin.host.attachment_vision import VisionCallResult, VisionUsage
from fin.host.state import reset_input_state
from fin.host.thread_store import InMemoryThreadStore
from fin.host.visual_evidence import (
    VISUAL_CLAIM_FIELDS,
    VISUAL_EVIDENCE_KIND,
    VISUAL_LANE_UNWIRED,
)
from fin.retrieve.fake_port import FakeRetrievePort

# A research-track ask: the lane is a tool, and a clarify ask never reaches
# the tool node at all.
GROUNDED = "What was AAPL net sales in 2023 Q3?"
ATTACHMENT_ID = "att-graph-visual-1"
OBJECT_KEY = f"threads/holder/{ATTACHMENT_ID}"
PAGE_BYTES = b"%PDF-1.4 fake"
RASTER_SHA = "a" * 64
MODEL_ID = "g4-fake-vision-1"
OBSERVATION = "The bar reaches 12.3 billion USD on the right axis."
USAGE = VisionUsage(input_tokens=90, output_tokens=30, total_tokens=120)

DETAIL_ARGS = {
    "attachment_id": ATTACHMENT_ID,
    "page": 1,
    "chart": "Revenue by segment",
    "series": "Services",
    "category": "Q3 2024",
}

PAGE_CAP = 2
CROP_CAP = 4


class FakeVisionPort:
    """``VisionPort`` witness: scripted answer, every call kept."""

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


class FakeRasterizer:
    """Draws any page to one stable raster, recording that it was asked to."""

    def __init__(self) -> None:
        self.drawn: list[int] = []

    def __call__(self, payload: bytes, page_no: int) -> PageRaster:
        self.drawn.append(page_no)
        return PageRaster(
            png=b"\x89PNG fixed raster", width_px=8, height_px=8, sha256=RASTER_SHA
        )


class _Objects:
    """The byte store face: the original PDF bytes under one key."""

    def __init__(self) -> None:
        self.reads = 0

    def get(self, key: str) -> bytes:
        self.reads += 1
        return PAGE_BYTES


# The committed mixed-chart gold case, whose page 3 carries the 528 x 328 pt
# chart the manifest records at x=42, top y=100 on a 612 x 792 pt media box.
# The crop cases cut THIS document, so a claim about a region is a claim
# about the fixture's own pixels rather than about a drawn stand-in.
GOLD_MIXED_PDF = (
    Path(__file__).resolve().parents[2]
    / "data"
    / "corpus"
    / "g4_attachment_gold"
    / "g4-mixed-chart-001.pdf"
)


class GoldPageRasterizer:
    """Draws the committed gold PDF through the production renderer."""

    def __init__(self) -> None:
        self.drawn: list[int] = []
        self._bytes = GOLD_MIXED_PDF.read_bytes()

    def whole_page_png(self, page_no: int) -> bytes:
        """The page's own full raster, so a test can tell it from a crop."""
        with Pypdfium2PageRenderer(self._bytes) as renderer:
            return renderer.render_page(page_no=page_no).png

    def __call__(self, payload: bytes, page_no: int) -> PageRaster:
        self.drawn.append(page_no)
        with Pypdfium2PageRenderer(self._bytes) as renderer:
            return renderer.render_page(page_no=page_no)


class _GoldObjects:
    """The byte store face that hands the real gold document to the lane."""

    def __init__(self) -> None:
        self.reads = 0
        self._bytes = GOLD_MIXED_PDF.read_bytes()

    def get(self, key: str) -> bytes:
        self.reads += 1
        return self._bytes


def _crop_bounds(crop: Any) -> tuple[float, float, float, float] | None:
    """The four bounds of a crop, so a test compares numbers not a dataclass."""
    if crop is None:
        return None
    return (crop.left, crop.top, crop.right, crop.bottom)


class _Service:
    """The wired attachment seam, shaped like the deployed preparation service."""

    def __init__(
        self,
        store: InMemoryAttachmentStore,
        *,
        vision: Any | None,
        objects: Any | None = None,
    ) -> None:
        self.store = store
        self.vision = vision
        self.budgets = _budgets()
        self.objects = objects if objects is not None else _Objects()


def _budgets() -> AttachmentBudgets:
    return AttachmentBudgets(
        max_upload_bytes=4 * 1024 * 1024,
        max_pdf_pages=PAGE_CAP,
        max_vision_calls_per_run=PAGE_CAP + CROP_CAP + 1,
        max_vision_crop_calls_per_run=CROP_CAP,
    )


def _store_with_ready_pdf() -> tuple[InMemoryThreadStore, InMemoryAttachmentStore, str]:
    """One thread holding one READY two-page attachment, written through the
    store's own doors so ``ready`` means what the product says it means."""
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
    units = tuple(
        ParsedAttachmentUnit(unit=page, text=f"page {page} body")
        for page in range(1, PAGE_CAP + 1)
    )
    store.record_units(attachment_id=ATTACHMENT_ID, version=1, units=units)
    store.put_index(attachment_id=ATTACHMENT_ID, version=1, postings=build_postings(units))
    store.mark_ready(attachment_id=ATTACHMENT_ID, version=1, unit_count=PAGE_CAP)
    return threads, store, thread_id


def _visual_call(args: dict[str, Any], *, call_id: str = "c-visual") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {"name": "read_visual_detail", "args": args, "id": call_id, "type": "tool_call"}
        ],
    )


def _visual_script(args: dict[str, Any], answer: str) -> list[AIMessage]:
    return [_visual_call(args), AIMessage(content=answer)]


def _compile(
    store: InMemoryAttachmentStore,
    service: _Service | None,
    script: list[AIMessage],
    monkeypatch: pytest.MonkeyPatch,
    rasterizer: FakeRasterizer | None = None,
):
    model = ScriptedChatModel(script=script)
    # The page is always drawn through a fake: the fixture's bytes are not a
    # real PDF, so a run that reached the real renderer would refuse for the
    # wrong reason.
    monkeypatch.setattr(
        "fin.host.graph._visual_page_raster", rasterizer or FakeRasterizer()
    )
    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        adapter=FakeRetrievePort(hits_by_query={}, known_document_ids=set()),
        known_document_ids=set(),
        attachment_service=service,
        attachment_source_reader_factory=(
            None
            if store is None
            else lambda bound: AttachmentSourceReader(store, bound)
        ),
    )
    graph.test_model = model
    return graph


def _run(graph: Any, thread_id: str) -> dict[str, Any]:
    return graph.invoke(
        reset_input_state(GROUNDED),
        config={"configurable": {"thread_id": thread_id}},
    )


def _tool_payload(state: dict[str, Any]) -> dict[str, Any]:
    messages = [
        m
        for m in state["messages"]
        if isinstance(m, ToolMessage) and getattr(m, "name", None) == "read_visual_detail"
    ]
    assert len(messages) == 1, f"expected one visual ToolMessage: {messages}"
    return json.loads(str(messages[0].content))


# ----- the lane is offered only when it can answer -----------------------


def test_the_page_lane_is_bound_only_when_every_face_is_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _threads, store, _thread_id = _store_with_ready_pdf()
    wired = _compile(store, _Service(store, vision=FakeVisionPort()), [], monkeypatch)
    assert "read_visual_detail" in _bound_names(wired)
    assert "read_visual_detail" in _bound_names_of(wired.test_model)

    # A text-only deployment has no vision port: the model is not offered a
    # question this process could only refuse.
    blind = _compile(store, _Service(store, vision=None), [], monkeypatch)
    assert "read_visual_detail" not in _bound_names(blind)

    no_service = _compile(None, None, [], monkeypatch)
    assert "read_visual_detail" not in _bound_names(no_service)


def _bound_names(graph: Any) -> list[str]:
    return [tool.name for tool in graph._bound_tools]


def _bound_names_of(model: Any) -> list[str]:
    return [getattr(tool, "name", str(tool)) for tool in model.bound_tools]


def test_the_operator_is_told_about_the_lane_only_when_it_is_bound() -> None:
    prompted = messages_for_model(
        [HumanMessage(content=GROUNDED)], "research", visual_lane_on=True
    )
    assert VISUAL_LANE_INSTRUCTIONS_MARKER in str(prompted[0].content)

    silent = messages_for_model([HumanMessage(content=GROUNDED)], "research")
    assert isinstance(silent[0], SystemMessage)
    assert VISUAL_LANE_INSTRUCTIONS_MARKER not in str(silent[0].content)
    # The unwired deployment keeps the bytes it had before the lane existed:
    # a prompt-golden elsewhere stays valid because OFF adds nothing at all.
    assert silent == messages_for_model(
        [HumanMessage(content=GROUNDED)], "research", visual_lane_on=False
    )


# ----- a granted read earns provenance, not a quote ----------------------


def test_a_visual_answer_persists_provenance_and_shows_the_model_the_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _threads, store, thread_id = _store_with_ready_pdf()
    rasterizer = FakeRasterizer()
    graph = _compile(
        store,
        _Service(store, vision=FakeVisionPort()),
        _visual_script(DETAIL_ARGS, "The page plots that."),
        monkeypatch,
        rasterizer,
    )

    state = _run(graph, thread_id)

    rows = state["visual_claims"]
    assert len(rows) == 1, rows
    row = rows[0]
    assert set(row) == set(VISUAL_CLAIM_FIELDS)
    assert row["kind"] == VISUAL_EVIDENCE_KIND
    assert row["attachment_id"] == ATTACHMENT_ID
    assert row["filename"] == "Segment Revenue.PDF"
    assert row["page"] == 1
    assert row["unit"] == 1
    assert row["observation_version"] == 1
    assert row["page_image_sha256"] == RASTER_SHA
    assert row["provider"] == MODEL_ID
    assert row["reused"] is False
    assert row["outbound_calls"] == 1
    # Provenance, never prose: nothing the provider generated rides the row.
    assert OBSERVATION not in json.dumps(row)
    assert rasterizer.drawn == [1]

    payload = _tool_payload(state)
    assert payload["observations"] == [
        {
            "page": 1,
            "observation_version": 1,
            "text": OBSERVATION,
            "truncated": False,
        }
    ]


def test_the_claim_rows_are_not_handed_to_the_model_to_be_copied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tool result carries prose; the channel carries provenance.

    A row inside the ToolMessage would let the model restate — or edit — the
    identity the host minted, which is the one thing the split exists to stop.
    """
    _threads, store, thread_id = _store_with_ready_pdf()
    graph = _compile(
        store,
        _Service(store, vision=FakeVisionPort()),
        _visual_script(DETAIL_ARGS, "The page plots that."),
        monkeypatch,
    )

    payload = _tool_payload(_run(graph, thread_id))

    assert list(payload) == ["observations"]
    assert sorted(payload["observations"][0]) == [
        "observation_version",
        "page",
        "text",
        "truncated",
    ]


def test_a_generated_observation_never_becomes_a_source_quote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    threads, store, thread_id = _store_with_ready_pdf()
    graph = _compile(
        store,
        _Service(store, vision=FakeVisionPort()),
        _visual_script(DETAIL_ARGS, OBSERVATION),
        monkeypatch,
    )

    state = _run(graph, thread_id)

    # The model repeated exactly what the page showed it — and still cannot
    # finish, because no verifier has a read text behind that sentence.
    assert state["status"] != "done", state["status"]
    assert not state["citations"], state["citations"]
    assert not state["evidence"], state["evidence"]
    # The claim stands beside the refusal: what was looked at is a fact about
    # this run even when nothing may be quoted from it.
    assert len(state["visual_claims"]) == 1


def test_a_visual_only_run_contributes_nothing_to_the_quote_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _threads, store, thread_id = _store_with_ready_pdf()
    graph = _compile(
        store,
        _Service(store, vision=FakeVisionPort()),
        _visual_script(DETAIL_ARGS, "The page plots that."),
        monkeypatch,
    )

    state = _run(graph, thread_id)

    kinds = {getattr(item, "kind", None) for item in state["evidence"]}
    assert VISUAL_EVIDENCE_KIND not in kinds


# ----- the run owns its calls -------------------------------------------


def test_a_second_ask_on_the_same_page_costs_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Asking twice inside one run: the second read answers from the stored row.

    Deliberately one run, not two sends — a citation-refused run bounces and
    re-enters the tool node, so two sends would prove reuse only by accident.
    """
    _threads, store, thread_id = _store_with_ready_pdf()
    vision = FakeVisionPort()
    rasterizer = FakeRasterizer()
    graph = _compile(
        store,
        _Service(store, vision=vision),
        [
            _visual_call(DETAIL_ARGS, call_id="c-visual-1"),
            _visual_call(DETAIL_ARGS, call_id="c-visual-2"),
            AIMessage(content="The page plots that."),
        ],
        monkeypatch,
        rasterizer,
    )

    rows = _run(graph, thread_id)["visual_claims"]

    assert len(rows) == 2, rows
    assert rows[0]["reused"] is False
    assert rows[0]["observation_version"] == 1
    assert rows[1]["reused"] is True
    assert rows[1]["outbound_calls"] == 0
    assert rows[1]["observation_version"] == 1
    assert len(vision.calls) == 1, "a covering observation must not be re-inspected"
    assert rasterizer.drawn == [1, 1], "each read re-proves the hash of its page"


def test_a_follow_up_send_starts_with_no_claims_of_its_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _threads, store, thread_id = _store_with_ready_pdf()
    graph = _compile(
        store,
        _Service(store, vision=FakeVisionPort()),
        _visual_script(DETAIL_ARGS, "The page plots that."),
        monkeypatch,
    )
    assert len(_run(graph, thread_id)["visual_claims"]) == 1

    graph.test_model.script = [AIMessage(content="No page this time.")]
    state = _run(graph, thread_id)

    assert state["visual_claims"] == []


# ----- the region a page lane located reaches the run's claim row --------


def test_a_persisted_reliable_region_reaches_the_run_as_a_crop_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run's claim names the region, not the page, when a box was cut.

    The seam tests already pin the cut; what only the wired run can show is
    that the crop scope survives the whole outward path — tool body, claim
    row, and persisted run state — with the page it names intact beside it.
    """
    _threads, store, thread_id = _store_with_ready_pdf()
    # The 528 x 328 pt chart of the committed gold case, on a 612 x 792 pt
    # media box, in the PDF points the store keeps.
    store.record_regions(
        attachment_id=ATTACHMENT_ID,
        version=1,
        regions=(
            StoredPageRegion(
                page=1, kind="figure", left_pt=42.0, top_pt=100.0,
                right_pt=570.0, bottom_pt=428.0,
            ),
        ),
    )
    vision = FakeVisionPort()
    rasterizer = GoldPageRasterizer()
    graph = _compile(
        store,
        _Service(store, vision=vision, objects=_GoldObjects()),
        _visual_script(DETAIL_ARGS, "The chart plots that."),
        monkeypatch,
        rasterizer,
    )

    state = _run(graph, thread_id)

    rows = state["visual_claims"]
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["scope"] == pytest.approx([42 / 612, 100 / 792, 570 / 612, 428 / 792])
    assert row["page"] == 1
    assert row["attachment_id"] == ATTACHMENT_ID
    # The provider was sent the chart's own pixels, not the whole page's.
    assert _crop_bounds(vision.calls[0]["target"].crop) == pytest.approx(
        (42 / 612, 100 / 792, 570 / 612, 428 / 792)
    )
    assert vision.calls[0]["image"] != rasterizer.whole_page_png(1)
    # And the model-facing body still carries only the prose, never the row.
    assert _tool_payload(state)["observations"][0]["text"] == OBSERVATION


def test_a_page_with_no_reliable_box_reaches_the_run_as_a_whole_page_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The permitted fallback reports the scope it actually used.

    ``scope=None`` in the row is the honest answer for a whole-page
    inspection, and the claim gate still treats the row as a typed visual
    claim rather than a quote either way.
    """
    _threads, store, thread_id = _store_with_ready_pdf()
    vision = FakeVisionPort()
    graph = _compile(
        store,
        _Service(store, vision=vision, objects=_GoldObjects()),
        _visual_script(DETAIL_ARGS, "The page plots that."),
        monkeypatch,
        GoldPageRasterizer(),
    )

    rows = _run(graph, thread_id)["visual_claims"]

    assert len(rows) == 1, rows
    assert rows[0]["scope"] is None
    assert vision.calls[0]["target"].crop is None


def test_another_thread_sees_no_page_and_draws_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    threads, store, _thread_id = _store_with_ready_pdf()
    foreign = threads.create_thread(title="Other thread").id
    vision = FakeVisionPort()
    rasterizer = FakeRasterizer()
    graph = _compile(
        store,
        _Service(store, vision=vision),
        _visual_script(DETAIL_ARGS, "The page plots that."),
        monkeypatch,
        rasterizer,
    )

    state = _run(graph, foreign)

    assert state["visual_claims"] == []
    assert vision.calls == []
    assert rasterizer.drawn == []
    # The scope gate now refuses BEFORE the lane runs (spec SC-3: the
    # model-supplied id is gated before any vision call is spent). The
    # refusal moved from the lane's unified ``attachment_unavailable`` to
    # the gate's own typed code; the security property this test pins —
    # nothing is claimed, no vision call, no page drawn — is unchanged and
    # the code is now the more specific scope denial. A foreign-thread id is
    # outside the effective selection, exactly like an unknown id.
    assert _tool_payload(state) == {"error": "scope_attachment_not_permitted"}


# ----- the two plain-data exits the tool must never guess about ----------


def test_a_run_with_no_ceiling_refuses_before_a_page_is_drawn() -> None:
    from fin.host.graph import _visual_read_result

    _threads, store, thread_id = _store_with_ready_pdf()
    service = _Service(store, vision=FakeVisionPort())
    faces_factory = _visual_faces_service(service)
    assert faces_factory is not None

    payload, rows = _visual_read_result(
        faces_factory,
        ATTACHMENT_ID,
        1,
        DETAIL_ARGS["chart"],
        DETAIL_ARGS["series"],
        DETAIL_ARGS["category"],
        None,
        {"configurable": {"thread_id": thread_id}},
    )

    assert payload == {"error": VISUAL_LANE_UNWIRED}
    assert rows == []
    # The original bytes are the first thing a real inspection would need, so
    # reading zero objects is the witness that nothing was attempted.
    assert service.objects.reads == 0


    assert service.objects.reads == 0


def _visual_faces_service(service: _Service) -> Any:
    from fin.host.graph import _make_visual_faces_factory

    return _make_visual_faces_factory(
        service, lambda bound: AttachmentSourceReader(service.store, bound)
    )


def test_the_ledger_is_minted_per_entry_and_the_caller_config_is_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _threads, store, thread_id = _store_with_ready_pdf()
    graph = _compile(store, _Service(store, vision=FakeVisionPort()), [], monkeypatch)
    config = {"configurable": {"thread_id": thread_id}}

    first = graph._apply_recursion_limit(config)
    second = graph._apply_recursion_limit(config)

    assert isinstance(first["configurable"]["vision_call_ledger"], VisionCallLedger)
    assert first["configurable"]["vision_call_ledger"] is not second[
        "configurable"
    ]["vision_call_ledger"]
    assert config["configurable"] == {"thread_id": thread_id}, (
        "the caller's own config must not be mutated"
    )


def test_a_deployment_with_no_page_lane_mints_no_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _threads, store, thread_id = _store_with_ready_pdf()
    graph = _compile(store, _Service(store, vision=None), [], monkeypatch)
    config = {"configurable": {"thread_id": thread_id}}

    merged = graph._apply_recursion_limit(config)

    assert "vision_call_ledger" not in merged["configurable"]
