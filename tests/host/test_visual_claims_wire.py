"""The visual-claim channel on the wire, in the store, and in the DTOs.

ADR 0089 says a generated visual observation is NOT a source quote, so a
run's claim rows travel their own channel: the SSE ``visual_claims``
frame, the ``fin_run.visual_claims`` column, the ``TurnRow`` history field,
and ``AskResponse.visual_claims``. What this file pins:

* one claim row -> exactly one ``visual_claims`` frame, ordered AFTER the
  answer frames and BEFORE the terminal ``citations`` frame (the SPA closes
  its reader on ``citations``, so anything after it is unreachable);
* the frame's row key set IS :data:`VISUAL_CLAIM_FIELDS`, in that order -
  the shape SSOT the evidence token mints;
* a run with no claim (or a channel that is not a visual row) leaves NO
  frame and persists ``[]``, never ``None``;
* a persisted claim row survives a store read and reaches GET history, and
  the Postgres JSONB codec round-trips it without a second shape;
* the quote channel is untouched: a claims run leaves ``citations`` (frame,
  row, and the CSV / JSON export) exactly as a claims-free run wrote it,
  with no claim provenance mixed in;
* ``/ask`` answers ``visual_claims``, defaulting to ``[]``;
* a REFUSED turn keeps publishing: the quote gate decides the answer, not
  the claim channel, so a run whose prose was refused still says which
  pixels were read;
* claims belong to one run: a follow-up send on the same thread neither
  replays the previous run's rows nor rewrites them;
* the export door's typed statuses stay honest (422 for a stored citation
  row that names no kind, 400 for an unknown kind / format, 404 for
  nothing to export).

The run-side producer (the visual read) fills ``state["visual_claims"]``
and is another file's contract; this file feeds the HTTP and persistence
surface rows of that exact shape and pins what it does with them.

Hermetic by construction: in-memory thread store, ``ScriptedChatModel`` +
``InMemorySaver``, no network. The Postgres codec is pinned twice - against
a stubbed driver connection for the SQL, and against a live JSONB column
when ``FIN_CHECKPOINT_URL`` is set; without it the live case skips, which is
blocked evidence rather than a pass.
"""

from __future__ import annotations

import csv
import json
import os
import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from fin.corpus.extracted import ExtractedPage, load_extracted_tree
from fin.host import ScriptedChatModel, compile_graph
from fin.host.citation_gate import COMPANY_KIND
from fin.host.thread_store import InMemoryThreadStore, RunRecord
from fin.host.visual_evidence import (
    VISUAL_CLAIM_FIELDS,
    VISUAL_EVIDENCE_KIND,
    VisualEvidence,
)
from fin.host.workspace_http import (
    _visual_claims_wire_payload,
)
from fin.retrieve import FakeRetrievePort


AAPL_2022Q3 = "AAPL-2022Q3"
AAPL_2022Q3_VERSION = (
    "a7a0d8261a0923404fc45446afd71b9965bd2b4064772df0287e761bb68ba9f2"
)
KNOWN_IDS = {AAPL_2022Q3, "AAPL-2023Q1", "MSFT-2023Q1", "AMZN-2023Q1"}

ATTACHMENT_ID = "att-visual-1"
CLAIM_VERSION = "b" * 64
CLAIM_PAGE_SHA256 = "c" * 64
# One sentinel per provenance field the quote channel must never learn:
# a leak is visible in an assertion instead of in the operator's turn.
CLAIM_DETAIL = "claim-detail-sentinel: gross margin row, right column"
CLAIM_FILENAME = "visual-claim-sentinel.pdf"

_QUESTION = "what was AAPL revenue in 2022 Q3?"

# The durable door the repo already uses for live-Postgres cases: unset
# means the live column cannot be read here, which is blocked evidence.
POSTGRES_URL_ENV = "FIN_CHECKPOINT_URL"


_READ_MANIFEST: dict[str, tuple[ExtractedPage, ...]] | None = None


def _read_manifest() -> dict[str, tuple[ExtractedPage, ...]]:
    """The committed extracted tree, loaded once per session.

    ``tests/host`` is not a package, so this mirrors the reader other
    workspace files build rather than importing their private helper.
    """
    global _READ_MANIFEST
    if _READ_MANIFEST is None:
        _READ_MANIFEST = load_extracted_tree(
            Path("data/corpus/manifest.json"), Path("data/corpus/extracted")
        )
    return _READ_MANIFEST


@pytest.fixture(scope="session")
def aapl_page1_text() -> str:
    return _read_manifest()[AAPL_2022Q3][0].text


@pytest.fixture
def http_app() -> FastAPI:
    from fin.host.http import app

    yield app
    app.dependency_overrides.clear()


@pytest.fixture
def client(http_app: FastAPI) -> TestClient:
    return TestClient(http_app)


@pytest.fixture
def fresh_store(http_app: FastAPI) -> InMemoryThreadStore:
    """In-memory thread store + a no-op scripted graph bound to the app.

    The read-side cases (GET history, export) need the store alone; the
    graph binding is there because ``GET /api/threads/{id}`` probes the
    lock seam, and an unbound dependency would boot the durable composer.
    Cases that drive a run install their own graph through
    ``_install_claim_graph``, which overwrites this one.
    """
    from fin.host.http import get_graph, get_thread_store

    store = InMemoryThreadStore()
    http_app.dependency_overrides[get_thread_store] = lambda: store
    http_app.dependency_overrides[get_graph] = lambda: compile_graph(
        checkpointer=InMemorySaver(),
        model=ScriptedChatModel(script=[AIMessage(content="never reached")]),
        known_document_ids=KNOWN_IDS,
        adapter=FakeRetrievePort(pages_by_doc=_read_manifest()),
    )
    return store


# ----- the claim channel's producer, injected at the seam this file owns --


def _claim_row(**overrides: Any) -> dict[str, Any]:
    """One claim row minted by the SSOT token, not hand-assembled here.

    WHY the token builds it: the wire shape is only a real contract if the
    consumer can be fed the producer's own output, so a field added to
    :class:`VisualEvidence` fails this file instead of drifting quietly.
    """
    row = VisualEvidence(
        attachment_id=ATTACHMENT_ID,
        version=CLAIM_VERSION,
        version_int=7,
        unit=3,
        page=3,
        observation_version=2,
        scope=None,
        provider="stub-vision",
        model_version="stub-vision-1",
        page_image_sha256=CLAIM_PAGE_SHA256,
        detail=CLAIM_DETAIL,
        filename=CLAIM_FILENAME,
        reused=False,
        outbound_calls=1,
    ).as_claim_row()
    row.update(overrides)
    return row


class _ClaimCarryingGraph:
    """The real scripted graph with the visual-claim channel stamped on.

    WHY a proxy instead of driving the visual read: filling
    ``state["visual_claims"]`` for real needs a page raster and a live
    vision provider, and that lane is another file's contract. This file
    owns what the HTTP surface and the store do with the rows they are
    handed, so the proxy writes at the seam the route actually reads and
    the citations still come from the real graph - the leak test below is
    therefore about the two channels, not about two fixtures.

    ``claims`` stays assignable because the two-send case needs the same
    graph, store, and checkpoint to hand a different channel per run.
    """

    def __init__(self, inner: Any, claims: Any) -> None:
        self._inner = inner
        self.claims = claims

    def stream(self, input: Any, config: dict | None = None, **kwargs: Any):
        for mode, payload in self._inner.stream(
            input, config=config, **kwargs
        ):
            if mode == "values" and isinstance(payload, dict):
                payload = {**payload, "visual_claims": self.claims}
            yield mode, payload

    def invoke(self, input: Any, config: dict | None = None, **kwargs: Any):
        result = self._inner.invoke(input, config=config, **kwargs)
        if isinstance(result, dict):
            return {**result, "visual_claims": self.claims}
        return result

    def __getattr__(self, name: str) -> Any:
        # Lock probes, checkpoint reads, and finalization stay the real
        # graph's own behaviour: the proxy adds one channel and nothing
        # else, so the surrounding run semantics cannot drift.
        return getattr(self._inner, name)


def _install_claim_graph(
    http_app: FastAPI,
    *,
    claims: Any,
    answer: str | None = None,
    script: list[AIMessage] | None = None,
) -> InMemoryThreadStore:
    """Bind a scripted grounded graph (read -> cite) plus the claim rows.

    ``script`` overrides the grounded default when the case needs a turn
    the citation gate refuses; ``answer`` is the prose of that default.

    Returns the store the routes write through.
    """
    from fin.host.http import get_graph, get_thread_store

    if script is None:
        assert answer is not None, "a scripted default needs its answer"
        script = _read_then_answer_script(answer)
    model = ScriptedChatModel(script=script)
    inner = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
        adapter=FakeRetrievePort(pages_by_doc=_read_manifest()),
    )
    store = InMemoryThreadStore()
    bound = _ClaimCarryingGraph(inner, claims)
    http_app.dependency_overrides[get_thread_store] = lambda: store
    # WHY one instance, not a constructor lambda: a fresh proxy per
    # resolution would give each request its own channel, so a case that
    # rebinds the channel between two sends would be mutating an object
    # the route never sees.
    http_app.dependency_overrides[get_graph] = lambda: bound
    return store


def _rebind_claim_channel(http_app: FastAPI, claims: Any) -> None:
    """Swap the rows the bound graph hands out for the NEXT send.

    WHY swap rather than install a second graph: a follow-up send must
    travel the same store, thread, and checkpoint, or the case would be
    measuring a different conversation.
    """
    from fin.host.http import get_graph

    bound = http_app.dependency_overrides[get_graph]()
    if not isinstance(bound, _ClaimCarryingGraph):
        raise AssertionError("the case bound a graph without a claim channel")
    bound.claims = claims


def _read_source_call() -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "read_source",
                "args": {
                    "source_ref": {
                        "document_id": AAPL_2022Q3,
                        "version": AAPL_2022Q3_VERSION,
                        "unit": 1,
                    },
                    "position": 0,
                },
                "id": "c-read",
                "type": "tool_call",
            }
        ],
    )


def _read_then_answer_script(answer: str) -> list[AIMessage]:
    """One grounded read, then an extractive answer the gate admits."""
    return [_read_source_call(), AIMessage(content=answer)]


# ----- helpers -----


def _new_thread(client: TestClient) -> str:
    return client.post("/api/threads", json={"first_message": "q"}).json()["id"]


def _parse_sse_events(body: str) -> list[tuple[str, Any]]:
    events: list[tuple[str, Any]] = []
    current: dict[str, str] = {}
    for line in body.splitlines():
        if not line:
            if "event" in current and "data" in current:
                try:
                    parsed = json.loads(current["data"])
                except json.JSONDecodeError:
                    parsed = {"raw": current["data"]}
                events.append((current["event"], parsed))
            current = {}
            continue
        if line.startswith("event:"):
            current["event"] = line[len("event:"):].strip()
        elif line.startswith("data:"):
            current["data"] = line[len("data:"):].strip()
    if "event" in current and "data" in current:
        try:
            parsed = json.loads(current["data"])
        except json.JSONDecodeError:
            parsed = {"raw": current["data"]}
        events.append((current["event"], parsed))
    return events


def _send(
    client: TestClient, thread_id: str, question: str = _QUESTION
) -> list[tuple[str, Any]]:
    """One send driven to the end of its wire, parsed into frames."""
    response = client.post(
        f"/api/threads/{thread_id}/messages", json={"question": question}
    )
    assert response.status_code == 200, response.text
    return _parse_sse_events(response.text)


# ----- 1. the frame: one per claims run, in the fixed position -----


def test_one_claim_row_emits_exactly_one_frame_before_the_terminal_citations(
    client: TestClient, http_app: FastAPI, aapl_page1_text: str
) -> None:
    """Normal path: a run carrying one claim row shows it on the wire.

    The frame is its own event name, sits after the answer frames, and
    lands BEFORE ``citations`` - which is terminal, so a frame after it
    would never be read.
    """
    answer = f"Per the filing: {aapl_page1_text[:80]}"
    _install_claim_graph(http_app, claims=[_claim_row()], answer=answer)
    thread_id = _new_thread(client)

    events = _send(client, thread_id)
    names = [name for name, _ in events]

    assert names.count("visual_claims") == 1, names
    assert names[-1] == "citations", names
    claims_index = names.index("visual_claims")
    last_answer = max(index for index, name in enumerate(names) if name == "answer")
    assert claims_index > last_answer, names
    assert claims_index < len(names) - 1, names
    # The terminal trio keeps its fixed order.
    assert names[claims_index - 2 : claims_index] == ["status", "answer"], names
    assert names[claims_index + 1] == "citations", names

    frame = events[claims_index][1]
    assert list(frame) == ["visual_claims"]
    rows = frame["visual_claims"]
    assert len(rows) == 1
    row = rows[0]
    # The key set IS the SSOT tuple, in its order - not a superset.
    assert list(row) == list(VISUAL_CLAIM_FIELDS)
    assert row["kind"] == VISUAL_EVIDENCE_KIND
    assert row["attachment_id"] == ATTACHMENT_ID
    assert row["page_image_sha256"] == CLAIM_PAGE_SHA256
    assert row["scope"] is None


def test_two_claim_rows_travel_in_order_and_a_crop_scope_survives(
    client: TestClient, http_app: FastAPI, aapl_page1_text: str
) -> None:
    """Cardinality + the region field: two reads stay two rows in first-
    touch order, and a crop scope (four normalized bounds) keeps its shape
    instead of collapsing to the whole page."""
    answer = f"Per the filing: {aapl_page1_text[:80]}"
    whole = _claim_row()
    crop = _claim_row(
        attachment_id="att-visual-2",
        scope=[0.1, 0.2, 0.35, 0.6],
        reused=True,
        outbound_calls=0,
    )
    _install_claim_graph(http_app, claims=[whole, crop], answer=answer)
    thread_id = _new_thread(client)

    events = _send(client, thread_id)
    names = [name for name, _ in events]
    rows = events[names.index("visual_claims")][1]["visual_claims"]

    assert [row["attachment_id"] for row in rows] == [
        ATTACHMENT_ID,
        "att-visual-2",
    ]
    assert rows[0]["scope"] is None
    assert rows[1]["scope"] == [0.1, 0.2, 0.35, 0.6]
    assert rows[1]["reused"] is True
    assert rows[1]["outbound_calls"] == 0
    assert all(list(row) == list(VISUAL_CLAIM_FIELDS) for row in rows)


def test_answer_and_citation_frames_keep_running_when_a_claim_lands(
    client: TestClient, http_app: FastAPI, aapl_page1_text: str
) -> None:
    """Regression: the new frame is additive - the status / answer /
    citations payloads a claims-free run produced are byte-identical here,
    so the SPA's existing reader sees nothing else change."""
    answer = f"Per the filing: {aapl_page1_text[:80]}"
    claim = _claim_row()
    _install_claim_graph(http_app, claims=[claim], answer=answer)
    claims_thread = _new_thread(client)
    claims_events = _send(client, claims_thread)

    _install_claim_graph(http_app, claims=[], answer=answer)
    plain_thread = _new_thread(client)
    plain_events = _send(client, plain_thread)

    def by_name(events: list[tuple[str, Any]], name: str) -> Any:
        return [data for event_name, data in events if event_name == name]

    # ``run_id`` is minted per run and ``thinking_ms`` is wall-clock, so
    # the two status frames can only be compared on what the run's shape
    # actually decides.
    def statuses(events: list[tuple[str, Any]]) -> Any:
        return [
            {
                key: value
                for key, value in data.items()
                if key not in {"run_id", "thinking_ms"}
            }
            for data in by_name(events, "status")
        ]

    assert statuses(claims_events) == statuses(plain_events)
    assert by_name(claims_events, "answer") == by_name(plain_events, "answer")
    assert by_name(claims_events, "citations") == by_name(
        plain_events, "citations"
    )
    # The claims run's own frame: exactly one, carrying exactly the row the
    # channel was handed, and the claims-free run still sends none.
    assert by_name(claims_events, "visual_claims") == [{"visual_claims": [claim]}]
    assert by_name(plain_events, "visual_claims") == []


# ----- 2. no claims: no frame, persisted ``[]`` -----


def test_run_with_no_claim_rows_emits_no_frame_and_persists_an_empty_list(
    client: TestClient, http_app: FastAPI, aapl_page1_text: str, fresh_store
) -> None:
    """The empty case: absence is expressed as NO frame plus ``[]`` in the
    row, so neither the wire nor a reload makes a consumer branch on a
    missing field."""
    del fresh_store  # the graph installer binds the store it writes
    answer = f"Per the filing: {aapl_page1_text[:80]}"
    store = _install_claim_graph(http_app, claims=[], answer=answer)
    thread_id = _new_thread(client)

    events = _send(client, thread_id)
    names = [name for name, _ in events]

    assert "visual_claims" not in names, names
    assert names[-3:] == ["status", "answer", "citations"], names
    runs = store.list_runs(thread_id)
    assert len(runs) == 1
    assert runs[0].visual_claims == []


def test_a_non_visual_row_on_the_channel_is_dropped_not_published(
    client: TestClient,
    http_app: FastAPI,
    aapl_page1_text: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Invalid input at the seam: a citation-shaped row (or a partial
    provenance row) pushed onto the claim channel is dropped with a
    WARNING and never published, so a claim cannot smuggle a quote or a
    half-known link onto the wire. The row's CONTENT never reaches the
    log - only the fact that a row was dropped."""
    answer = f"Per the filing: {aapl_page1_text[:80]}"
    store = _install_claim_graph(
        http_app,
        claims=[
            {"kind": COMPANY_KIND, "document_id": AAPL_2022Q3},
            {"kind": VISUAL_EVIDENCE_KIND, "attachment_id": ATTACHMENT_ID},
            "not-a-row",
            _claim_row(),
        ],
        answer=answer,
    )
    thread_id = _new_thread(client)

    events = _send(client, thread_id)
    names = [name for name, _ in events]
    rows = events[names.index("visual_claims")][1]["visual_claims"]

    # Only the complete visual row survives, and the run still lands done.
    assert len(rows) == 1
    assert list(rows[0]) == list(VISUAL_CLAIM_FIELDS)
    assert next(data for name, data in events if name == "status")["status"] == "done"

    warnings = [
        record.getMessage()
        for record in caplog.records
        if "visual_claims wire" in record.getMessage()
    ]
    assert len(warnings) == 3, warnings
    assert CLAIM_PAGE_SHA256 not in caplog.text
    assert CLAIM_DETAIL not in caplog.text
    # The drop is run-scoped: the persisted row carries only the claim
    # that made it onto the wire.
    assert len(store.list_runs(thread_id)[0].visual_claims) == 1


# ----- 3. the channel follows the run, whatever the status says -----


def test_a_gate_refused_turn_still_publishes_its_claim_rows(
    client: TestClient, http_app: FastAPI
) -> None:
    """Refuse is the quote gate's verdict on the PROSE, not on the claim
    channel: a run whose answer was refused still says which raster it
    read, so the frame, the persisted row, and GET history keep the claim
    while ``citations`` stays empty."""
    refused = (
        "Per the ZZZZ-9999Q1 filing revenue grew across all segments and "
        "operating margin improved."
    )
    claim = _claim_row()
    store = _install_claim_graph(
        http_app, claims=[claim], script=[AIMessage(content=refused)]
    )
    thread_id = _new_thread(client)

    events = _send(client, thread_id)
    names = [name for name, _ in events]
    status = next(data for name, data in events if name == "status")
    assert status["status"] == "refuse", status

    assert names.count("visual_claims") == 1, names
    assert events[names.index("visual_claims")][1] == {"visual_claims": [claim]}
    # The refusal keeps the fixed order and the empty quote channel.
    assert names[-1] == "citations", names
    assert events[-1][1] == []

    run = store.list_runs(thread_id)[0]
    assert run.status == "refuse"
    assert run.citations == []
    assert run.visual_claims == [claim]

    turns = client.get(f"/api/threads/{thread_id}").json()["turns"]
    assert turns[0]["status"] == "refuse"
    assert turns[0]["visual_claims"] == [claim]
    assert list(turns[0]["visual_claims"][0]) == list(VISUAL_CLAIM_FIELDS)


def test_a_follow_up_send_does_not_replay_the_previous_runs_claims(
    client: TestClient, http_app: FastAPI, aapl_page1_text: str
) -> None:
    """The channel belongs to one run: a claim vouches for the pixels that
    run read, so the next send on the SAME thread emits no frame and
    persists ``[]`` while the first run's row keeps its own row unchanged."""
    answer = f"Per the filing: {aapl_page1_text[:80]}"
    claim = _claim_row()
    store = _install_claim_graph(http_app, claims=[claim], answer=answer)
    thread_id = _new_thread(client)

    first = _send(client, thread_id)
    assert [name for name, _ in first].count("visual_claims") == 1

    _rebind_claim_channel(http_app, [])
    second = _send(client, thread_id)
    assert "visual_claims" not in [name for name, _ in second], second

    runs = store.list_runs(thread_id)
    assert len(runs) == 2
    assert runs[1].visual_claims == []
    assert runs[0].visual_claims == [claim]

    turns = client.get(f"/api/threads/{thread_id}").json()["turns"]
    assert [turn["visual_claims"] for turn in turns] == [[claim], []]


# ----- 4. persistence + history -----


def test_claim_rows_survive_a_store_read_and_reach_history(
    client: TestClient, http_app: FastAPI, aapl_page1_text: str
) -> None:
    """The link back to the original page has to outlive the stream: a
    second store read (the seam a restart goes through) and GET history
    both hand back the SAME rows the frame carried, and mutating what a
    reader got cannot rewrite the store."""
    answer = f"Per the filing: {aapl_page1_text[:80]}"
    claim = _claim_row(scope=[0.0, 0.25, 1.0, 0.75])
    store = _install_claim_graph(http_app, claims=[claim], answer=answer)
    thread_id = _new_thread(client)

    events = _send(client, thread_id)
    names = [name for name, _ in events]
    wire_rows = events[names.index("visual_claims")][1]["visual_claims"]

    persisted = store.list_runs(thread_id)[0].visual_claims
    assert persisted == wire_rows

    # A reload (a fresh read of the same row) is unaffected by a caller
    # that mutates the copy it was handed.
    persisted[0]["scope"].append(9.0)
    persisted[0]["detail"] = "rewritten"
    reread = store.list_runs(thread_id)[0].visual_claims
    assert reread == wire_rows
    assert reread[0]["scope"] == [0.0, 0.25, 1.0, 0.75]

    turns = client.get(f"/api/threads/{thread_id}").json()["turns"]
    assert len(turns) == 1
    assert turns[0]["visual_claims"] == wire_rows
    assert list(turns[0]["visual_claims"][0]) == list(VISUAL_CLAIM_FIELDS)


def test_history_rows_default_to_an_empty_claim_list(client: TestClient, fresh_store) -> None:
    """Input contract on the read side: a pre-migration row (no channel at
    all) still serializes ``visual_claims: []`` rather than a missing key
    or a null, so the SPA's history render needs no absence branch."""
    thread_id = _new_thread(client)
    fresh_store.append_run(
        RunRecord(
            id="run-no-claims",
            thread_id=thread_id,
            question="q",
            status="done",
            answer="a" * 40,
        )
    )

    turns = client.get(f"/api/threads/{thread_id}").json()["turns"]
    assert turns[0]["visual_claims"] == []


# ----- 5. a claim is never a citation -----


def test_a_claim_run_leaves_the_citation_frame_and_export_untouched(
    client: TestClient, http_app: FastAPI, aapl_page1_text: str
) -> None:
    """The separation ADR 0089 asks for: the quote channel keeps its own
    key set and its own text, and none of the claim's provenance - the
    raster hash, the observation version, the detail prose, the file name -
    appears in the citations frame, the persisted citation rows, or the
    citation export."""
    answer = f"Per the filing: {aapl_page1_text[:80]}"
    store = _install_claim_graph(http_app, claims=[_claim_row()], answer=answer)
    thread_id = _new_thread(client)

    events = _send(client, thread_id)
    names = [name for name, _ in events]
    citation_rows = events[names.index("citations")][1]
    assert len(citation_rows) >= 1, "the grounded run must still cite"

    claim_secrets = [
        CLAIM_PAGE_SHA256,
        CLAIM_DETAIL,
        CLAIM_FILENAME,
        ATTACHMENT_ID,
        CLAIM_VERSION,
    ]
    for row in citation_rows:
        assert list(row) == [
            "kind",
            "document_id",
            "attachment_id",
            "version",
            "unit",
            "display",
            "filename",
            "quote",
        ]
        assert row["kind"] == COMPANY_KIND
        for secret in claim_secrets:
            assert secret not in json.dumps(row, default=str)

    run = store.list_runs(thread_id)[0]
    assert run.citations and all(
        cit.get("kind") != VISUAL_EVIDENCE_KIND for cit in run.citations
    )
    assert len(run.visual_claims) == 1

    csv_export = client.get(
        f"/api/threads/{thread_id}/export", params={"kind": "citations"}
    )
    assert csv_export.status_code == 200, csv_export.text
    assert csv_export.text.splitlines()[0] == "kind,source_id,version,unit,quote"
    for secret in claim_secrets:
        assert secret not in csv_export.text

    json_export = client.get(
        f"/api/threads/{thread_id}/export",
        params={"kind": "citations", "format": "json"},
    )
    assert json_export.status_code == 200, json_export.text
    body = json_export.json()
    assert list(body) == ["thread_id", "run_id", "kind", "citations"]
    for secret in claim_secrets:
        assert secret not in json_export.text

    # And the other direction: a claim row is not exportable as a
    # citation, because the export only reads the quote channel.
    claims_only_thread = _new_thread(client)
    store.append_run(
        RunRecord(
            id="run-claims-only",
            thread_id=claims_only_thread,
            question="q",
            status="done",
            answer="a" * 40,
            visual_claims=[_claim_row()],
        )
    )
    refused = client.get(
        f"/api/threads/{claims_only_thread}/export", params={"kind": "citations"}
    )
    assert refused.status_code == 404, refused.text
    assert refused.json() == {"error": "export_empty"}


# ----- 6. the non-streaming door -----


def test_ask_answers_visual_claims_defaulting_to_an_empty_list(
    client: TestClient, http_app: FastAPI, aapl_page1_text: str
) -> None:
    """``/ask`` shape: the field is always present, ``[]`` when the run
    produced no claim, and the same projected rows the stream carried when
    it did - one projector behind both doors, so they cannot drift."""
    answer = f"Per the filing: {aapl_page1_text[:80]}"

    _install_claim_graph(http_app, claims=[], answer=answer)
    empty = client.post("/ask", json={"question": _QUESTION})
    assert empty.status_code == 200, empty.text
    assert "visual_claims" in empty.json()
    assert empty.json()["visual_claims"] == []

    claim = _claim_row()
    _install_claim_graph(http_app, claims=[claim], answer=answer)
    body = client.post("/ask", json={"question": _QUESTION}).json()
    assert [list(row) for row in body["visual_claims"]] == [list(VISUAL_CLAIM_FIELDS)]
    assert body["visual_claims"] == [claim]
    assert body["citations"], "the quote channel still travels on /ask"
    assert all(row.get("kind") != VISUAL_EVIDENCE_KIND for row in body["citations"])


# ----- 7. the projector, pinned directly -----


def test_projector_keeps_the_ssot_fields_in_order_and_drops_the_rest() -> None:
    """The projector is the single normaliser behind the SSE frame, the
    persisted row, and ``/ask``: it projects onto the SSOT tuple in order
    (extra keys off, missing keys out), keeps a legitimate ``scope=None``,
    and turns nothing at all into ``[]``."""
    full = _claim_row(extra_key="smuggled")
    projected = _visual_claims_wire_payload([full])
    assert list(projected[0]) == list(VISUAL_CLAIM_FIELDS)
    assert "extra_key" not in projected[0]

    # Field PRESENCE, not truthiness: a whole-page claim carries
    # ``scope=None`` and falsy counters and is still a real claim.
    sparse = _visual_claims_wire_payload(
        [_claim_row(scope=None, page=0, outbound_calls=0, reused=False)]
    )
    assert sparse[0]["scope"] is None
    assert sparse[0]["outbound_calls"] == 0

    assert _visual_claims_wire_payload(None) == []
    assert _visual_claims_wire_payload(()) == []
    # A row missing ONE provenance field names an unknown link, so it is
    # not a claim at all.
    partial = _claim_row()
    partial.pop("page_image_sha256")
    assert _visual_claims_wire_payload([partial]) == []
    assert _visual_claims_wire_payload([{"kind": COMPANY_KIND, "quote": "x"}]) == []
    assert _visual_claims_wire_payload([None, "x", 7]) == []

    # Input order survives, and the caller's dict is not the wire's dict.
    source = _claim_row()
    emitted = _visual_claims_wire_payload([source])
    emitted[0]["detail"] = "rewritten"
    assert source["detail"] == CLAIM_DETAIL


# ----- 8. the Postgres codec: stubbed driver, then the live column -----


def test_postgres_schema_adds_the_visual_claims_column_idempotently() -> None:
    """A durable boot against a pre-ADR-0089 ``fin_run`` must not raise on
    the first INSERT: the schema declares the column in CREATE TABLE and
    adds it with ``ADD COLUMN IF NOT EXISTS``, defaulting to an empty
    array so old rows read back as ``[]``."""
    from fin.host.thread_store import _SCHEMA_SQL

    assert (
        "ALTER TABLE fin_run ADD COLUMN IF NOT EXISTS visual_claims "
        "JSONB NOT NULL DEFAULT '[]'::jsonb" in _SCHEMA_SQL
    )
    assert "visual_claims     JSONB NOT NULL DEFAULT '[]'::jsonb" in _SCHEMA_SQL


def test_postgres_run_sql_round_trips_visual_claims_jsonb(monkeypatch) -> None:
    """The Postgres INSERT serializes ``visual_claims`` as one JSON array
    of provenance rows and ``_row_to_run`` parses it back - in both driver
    codec shapes (a JSON string, or an already-decoded list) - and a row
    from before the migration yields ``[]`` instead of ``None``."""
    import fin.host.thread_store as ts_mod

    captured_params: list[tuple] = []
    rows_back: list[tuple] = []

    class _StubCursor:
        def __init__(self, value):
            self._value = value

        def fetchone(self):
            return self._value

        def fetchall(self):
            return [self._value] if self._value is not None else []

    class _StubConn:
        def execute(self, sql, params=None):
            if params is not None and sql.startswith("INSERT INTO fin_run"):
                captured_params.append(params)
                # Mirror ``list_runs``' SELECT column order off the write.
                rows_back.append(tuple(params))
            return _StubCursor(rows_back[-1] if rows_back else None)

        def commit(self):
            pass

    class _StubCM:
        def __init__(self, conn):
            self._conn = conn

        def __enter__(self):
            return self._conn

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(
        ts_mod.psycopg, "connect", lambda *a, **kw: _StubCM(_StubConn())
    )

    store = ts_mod.PostgresThreadStore.__new__(ts_mod.PostgresThreadStore)
    store._url = "postgresql://stub"
    claim = _claim_row(scope=[0.1, 0.1, 0.9, 0.9])
    store.append_run(
        RunRecord(
            id="r-pg",
            thread_id="t-pg",
            question="q",
            status="done",
            answer="a",
            visual_claims=[claim],
        )
    )

    params = captured_params[0]
    # The written column is the LAST INSERT parameter, serialized as JSON.
    assert json.loads(params[18]) == [claim]
    assert len(params) == 19

    parsed = store._row_to_run(rows_back[0])
    assert parsed.visual_claims == [claim]
    assert list(parsed.visual_claims[0]) == list(VISUAL_CLAIM_FIELDS)

    # The other driver codec: psycopg hands back a decoded list.
    decoded_row = list(rows_back[0])
    decoded_row[18] = [claim]
    assert store._row_to_run(tuple(decoded_row)).visual_claims == [claim]

    # A pre-migration row (the SELECT did not know the column) is ``[]``.
    short_row = tuple(rows_back[0][:12])
    assert store._row_to_run(short_row).visual_claims == []

    # ``list_runs`` keeps its isolation promise for the new field too.
    runs = store.list_runs("t-pg")
    runs[0].visual_claims[0]["detail"] = "rewritten"
    assert store.list_runs("t-pg")[0].visual_claims[0]["detail"] == CLAIM_DETAIL


@pytest.fixture
def postgres_store_url() -> str:
    """The live column door, matching ``tests/host/test_attachment_store.py``.

    WHY a skip instead of a stub here: the driver-shaped cases above pin
    the SQL against a fake connection, which cannot prove the JSONB column
    itself keeps a provenance row's key ORDER and its nested ``scope`` -
    only a real Postgres can. No URL, no proof: the case is blocked
    evidence, never a green pass.
    """
    url = os.environ.get(POSTGRES_URL_ENV, "").strip()
    if not url:
        pytest.skip(
            "FIN_CHECKPOINT_URL is not set; the visual-claim channel is "
            "untested against a live JSONB column in this environment"
        )
    return url


def test_live_postgres_round_trips_the_claim_channel_without_a_second_shape(
    postgres_store_url: str,
) -> None:
    """The persisted claim rows come back through the real column with the
    SSOT key set and order, the nested ``scope`` intact, and the quote
    column still empty - and the next run without a claim reads ``[]`` on
    the same thread."""
    import psycopg

    from fin.host.thread_store import PostgresThreadStore

    store = PostgresThreadStore(postgres_store_url)
    thread = store.create_thread(title="visual claim wire")
    claim = _claim_row(scope=[0.0, 0.25, 1.0, 0.75])
    cited = {
        "kind": COMPANY_KIND,
        "document_id": AAPL_2022Q3,
        "attachment_id": "",
        "version": AAPL_2022Q3_VERSION,
        "unit": 1,
        "display": "page",
        "filename": "",
        "quote": "Total net sales increased during the third quarter",
    }
    claimed_run_id = f"run-{uuid.uuid4().hex}"
    plain_run_id = f"run-{uuid.uuid4().hex}"
    try:
        store.append_run(
            RunRecord(
                id=claimed_run_id,
                thread_id=thread.id,
                question=_QUESTION,
                status="done",
                answer="a" * 40,
                citations=[cited],
                visual_claims=[claim],
            )
        )
        store.append_run(
            RunRecord(
                id=plain_run_id,
                thread_id=thread.id,
                question=_QUESTION,
                status="refuse",
                answer="",
                citations=[],
            )
        )

        by_id = {run.id: run for run in store.list_runs(thread.id)}
        assert by_id[claimed_run_id].visual_claims == [claim]
        # The key SET is what survives a JSONB round trip; the key ORDER is
        # not. Postgres jsonb normalizes object keys (by length, then
        # lexicographically) and does not preserve insertion order, so a
        # stored claim comes back with the same fields in a different
        # sequence. The declared order is asserted where it is actually
        # guaranteed — on the in-memory rows above — and here the claim is
        # pinned by set, so this leg cannot pass a row carrying a field the
        # contract does not list.
        assert set(by_id[claimed_run_id].visual_claims[0]) == set(
            VISUAL_CLAIM_FIELDS
        )
        assert by_id[claimed_run_id].visual_claims[0]["scope"] == [
            0.0,
            0.25,
            1.0,
            0.75,
        ]
        # The two channels stay two columns: the quote row never picked up
        # a claim, and the claim never picked up a quote.
        assert [cit["kind"] for cit in by_id[claimed_run_id].citations] == [
            COMPANY_KIND
        ]
        assert (
            VISUAL_EVIDENCE_KIND
            not in json.dumps(by_id[claimed_run_id].citations)
        )
        assert by_id[plain_run_id].visual_claims == []
    finally:
        # ``fin_run.thread_id`` cascades, so removing the thread row
        # removes both runs and leaves the operator's table as found.
        with psycopg.connect(postgres_store_url) as conn:
            conn.execute(
                "DELETE FROM fin_thread WHERE id = %s", (thread.id,)
            )
            conn.commit()


# ----- 9. the export door's typed statuses stay honest -----


def _seed_run(store: InMemoryThreadStore, thread_id: str, **run_kwargs) -> None:
    store.append_run(
        RunRecord(
            id=run_kwargs.pop("id", "run-seeded"),
            thread_id=thread_id,
            question="compare AAPL",
            status="done",
            answer="x" * 40,
            **run_kwargs,
        )
    )


def test_export_status_ladder_on_the_route(
    client: TestClient, fresh_store
) -> None:
    """422 for a stored citation row that names no kind (the identity a
    citation owes cannot be guessed), 400 for an unknown kind or format,
    404 for nothing to export - one run, one assertion per typed refusal."""
    malformed_thread = _new_thread(client)
    _seed_run(
        fresh_store,
        malformed_thread,
        id="run-kindless",
        citations=[
            {
                "document_id": AAPL_2022Q3,
                "attachment_id": "",
                "version": AAPL_2022Q3_VERSION,
                "unit": 1,
                "display": "page",
                "filename": "",
                "quote": "Total net sales increased during the quarter",
            }
        ],
        visual_claims=[_claim_row()],
    )
    refused = client.get(
        f"/api/threads/{malformed_thread}/export", params={"kind": "citations"}
    )
    assert refused.status_code == 422, refused.text
    assert refused.json() == {"error": "malformed_citation_row"}

    bad_kind = client.get(
        f"/api/threads/{malformed_thread}/export", params={"kind": "spreadsheet"}
    )
    assert bad_kind.status_code == 400, bad_kind.text
    assert bad_kind.json() == {"error": "unsupported_export"}

    bad_format = client.get(
        f"/api/threads/{malformed_thread}/export",
        params={"kind": "citations", "format": "xlsx"},
    )
    assert bad_format.status_code == 400, bad_format.text
    assert bad_format.json() == {"error": "unsupported_export"}

    empty_thread = _new_thread(client)
    _seed_run(fresh_store, empty_thread, id="run-empty", comparisons=[])
    empty = client.get(
        f"/api/threads/{empty_thread}/export", params={"kind": "citations"}
    )
    assert empty.status_code == 404, empty.text
    assert empty.json() == {"error": "export_empty"}

    unknown_run = client.get(
        f"/api/threads/{malformed_thread}/export",
        params={"kind": "citations", "run_id": "nope"},
    )
    assert unknown_run.status_code == 404, unknown_run.text
    assert unknown_run.json() == {"error": "run_not_found"}

    missing_thread = client.get(
        "/api/threads/no-such-thread/export", params={"kind": "citations"}
    )
    assert missing_thread.status_code == 404, missing_thread.text
    assert missing_thread.json() == {"error": "thread_not_found"}


def test_citation_export_still_renders_the_unit_trail_verbatim(
    client: TestClient, fresh_store
) -> None:
    """Keeps the pre-existing export contract honest on a run that ALSO
    carries a claim: one CSV row per citation, the gate's own column
    order, and no claim row appended to the file."""
    thread_id = _new_thread(client)
    citation = {
        "kind": COMPANY_KIND,
        "document_id": AAPL_2022Q3,
        "attachment_id": "",
        "version": AAPL_2022Q3_VERSION,
        "unit": 1,
        "display": "page",
        "filename": "",
        "quote": "Total net sales increased during the third quarter",
    }
    _seed_run(
        fresh_store,
        thread_id,
        id="run-cited",
        citations=[citation],
        visual_claims=[_claim_row()],
    )

    response = client.get(f"/api/threads/{thread_id}/export", params={"kind": "citations"})
    assert response.status_code == 200, response.text
    rows = list(csv.reader(response.text.splitlines()))
    assert rows == [
        ["kind", "source_id", "version", "unit", "quote"],
        [
            COMPANY_KIND,
            AAPL_2022Q3,
            AAPL_2022Q3_VERSION,
            "1",
            citation["quote"],
        ],
    ]
