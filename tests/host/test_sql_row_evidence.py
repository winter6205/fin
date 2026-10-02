"""The sql 档 consumer: wire rows become database-row evidence, exits stay named.

the tracked change T7 host half. The retrieve tool today builds evidence ONLY
from chunks; a ``sql`` answer carries its rows in the ``database_rows``
sibling (spec §8.0), so without this leg a ledger row is text the model
sees but a fact the citation gate cannot verify. Pinned here, against
fake ports only (no live MCP, no live ledger — §9's opt-in suites own
the real-boundary legs):

* one wire row becomes one ``EvidenceItem`` of the ``database-row``
  kind carrying the d2c3aa0 ``DatabaseRow`` — empty document slots, the
  row's own identity, no bridging;
* the row's SHAPE is decided by the E1 structural validator (no inline
  key-presence copy at this seam): a non-conforming row carries its
  exact §6 code out of :func:`database_row_shape_refusal` and never
  becomes evidence;
* the row evidence passes the gate's row verification lane end-to-end:
  an answer quoting the row's display line and a returned figure cites
  the record (ADR 0091), and a figure no returned field supports is
  refused;
* the run-scoped outcome record carries the zero-row job fact, so the
  terminal refusal names ``sql_no_matching_rows`` — never
  ``corpus_not_ready`` — and its operator copy names the ledger, not
  documents (spec §7);
* a structured not-provisioned / denied / timeout exit stays a typed
  EXIT envelope through the tool seam, distinct from a transport-down
  ``retrieve_unavailable``;
* a cascade-assigned ``sql`` turn reaches the port with the token (the
  silent-``graph`` downgrade the handoff names as trap 1 is impossible
  through this hop), and the wire for such a turn is refused
  ``invalid_business_query`` before transport when no window was given
  — named, never defaulted.
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from fin.corpus.extracted import Locator
from fin.host.citation_gate import (
    DATABASE_ROW_DISPLAY,
    DATABASE_ROW_KIND,
    EvidenceItem,
    verify_citations,
)
from fin.host.evidence_admission import (
    database_row_shape_refusal,
    evidence_from_database_rows,
)
from fin.host.graph import _make_retrieve_tool, compile_graph
from fin.host.named_failures import (
    COMMUNITY_SUMMARY_INDEX_FAILURE,
    CORPUS_NOT_READY,
    EMPTY_FAST_GRAPH_FAILURE,
    MAPPING_DROP,
    RETRIEVE_OUTCOME_SQL_NO_MATCHING_ROWS,
    SQL_NO_MATCHING_ROWS,
    TIER_NOT_OPEN,
    retrieve_blocking_failure,
)
from fin.host.run_payload import ORDINARY_PERSON_COPY
from fin.host.scripted_model import ScriptedChatModel
from fin.host.state import reset_input_state
from fin.retrieve.fake_port import FakeRetrievePort
from fin.retrieve.port import DATABASE_ROWS_KEY, Chunk

pytestmark = [pytest.mark.soft_track]

_ROW_WIRE = {
    # An INGRESS row: the envelope names the format, the row does not
    # (spec 4.4 "schema placement at ingress"). Carrying a per-row
    # ``schema`` here is what the real producer does NOT do, and the
    # previous fixture's copy of it is why the hermetic suite never
    # caught the producer mismatch.
    "source": "ledger_ro.contract_expiry",
    "record_key": {"contract_id": "C-2291"},
    "queried_at": "2026-03-01T09:14:22Z",
    "fields": {
        "counterparty": "Northwind Logistics",
        "expiry_date": "2026-03-14",
        "notice_days": 30,
        "notice_by": "2026-02-12",
    },
    "summary": (
        "C-2291 Northwind Logistics, expires 2026-03-14, notice by "
        "2026-02-12"
    ),
}


def _hit() -> Chunk:
    return Chunk(
        text="Apple net sales for fiscal 2023 third quarter grew.",
        source_ref=Locator(
            document_id="AAPL-2022Q3", version="a" * 64, unit=1
        ),
        score=1.0,
    )


# ----- the row builder (pure seam) -----


def test_one_wire_row_becomes_one_row_evidence_item() -> None:
    items = evidence_from_database_rows([_ROW_WIRE])

    assert len(items) == 1
    item = items[0]
    assert item.kind == DATABASE_ROW_KIND
    assert item.display == DATABASE_ROW_DISPLAY
    # The row's identity lives in its own carrier, not in the document
    # slots: empty document id and version, no ordinal to bridge on.
    assert item.locator == Locator(document_id="", version="", unit=1)
    assert item.attachment_id == ""
    assert item.position == 0 and item.next_position is None
    row = item.database_row[0]
    assert row.source == "ledger_ro.contract_expiry"
    assert dict(row.record_key) == {"contract_id": "C-2291"}
    assert row.queried_at == "2026-03-01T09:14:22Z"
    # ``unit_text`` is the display line the model was shown, so the row
    # is quotable the way a passage is — and it came from the wire.
    assert item.unit_text == _ROW_WIRE["summary"]


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        pytest.param("not-a-mapping", "malformed_evidence_contract", id="string"),
        pytest.param(None, "malformed_evidence_contract", id="none"),
        pytest.param(
            {"record_key": {"contract_id": "X"}},
            "malformed_evidence_contract",
            id="no-source",
        ),
        pytest.param(
            {"source": "s", "record_key": {}},
            "malformed_evidence_contract",
            id="no-key",
        ),
        pytest.param(
            {"source": "s", "record_key": {"k": "v"}, "summary": ""},
            "malformed_evidence_contract",
            id="no-display-line",
        ),
    ],
)
def test_an_unreadable_row_is_a_named_shape_failure_and_never_evidence(
    raw: Any, code: str
) -> None:
    """The interim silent drop is replaced by the validator's named
    refusal: the shape decision is the E1 validator's, reached through
    :func:`database_row_shape_refusal` (no inline key-presence copy),
    and the exact §6 code is assertable — not just ``no item``."""
    assert database_row_shape_refusal(raw) == code
    assert evidence_from_database_rows([raw]) == []


def test_a_conforming_row_carries_no_shape_refusal() -> None:
    assert database_row_shape_refusal(_ROW_WIRE) is None


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param({**_ROW_WIRE, "source": {"nested": 1}}, id="dict-source"),
        pytest.param({**_ROW_WIRE, "record_key": ["k"]}, id="list-record-key"),
    ],
)
def test_an_unhashable_row_value_is_a_named_refusal_not_a_crash(raw: Any) -> None:
    """The seam's own frozenset seeding must never hash a producer value:
    an unseedable identity simply stays absent and the validator's shape
    check names the refusal (the never-raise contract, review E session)."""
    assert database_row_shape_refusal(raw) == "malformed_evidence_contract"


def test_an_empty_display_line_is_guarded_from_the_gate_lane() -> None:
    """The shape validator owns keys, types, and bounds — an empty
    ``summary`` is shape-legal; the carrier guard stays because the
    gate's ``_validate_evidence`` raises on an empty ``unit_text``, and
    a row the answer cannot quote must not refuse the whole run."""
    raw = {**_ROW_WIRE, "summary": ""}
    assert database_row_shape_refusal(raw) is None
    assert evidence_from_database_rows([raw]) == []


def test_no_rows_builds_no_evidence() -> None:
    assert evidence_from_database_rows([]) == []
    assert evidence_from_database_rows(None) == []


# ----- the gate's row lane, fed by the built evidence -----


def test_the_built_row_evidence_passes_the_row_verification_lane() -> None:
    """End-to-end through the d2c3aa0 carrier: the row this seam built
    cites by its ``record_key`` and returned ``fields`` — no 32-code-
    point span anywhere (ADR 0091)."""
    items = evidence_from_database_rows([_ROW_WIRE])
    result = verify_citations(
        answer=(
            f"{_ROW_WIRE['summary']}. The notice period is 30 days."
        ),
        evidence=items,
        repair_count=0,
        known_document_ids=set(),
    )
    from fin.host.citation_gate import DoneResult

    assert isinstance(result, DoneResult), result
    assert len(result.citations) == 1
    citation = result.citations[0]
    assert citation.kind == DATABASE_ROW_KIND
    assert citation.database_source == "ledger_ro.contract_expiry"
    assert dict(citation.database_record_key) == {"contract_id": "C-2291"}


def test_an_unsupported_figure_refuses_on_the_row_reason() -> None:
    """A number no returned field supports is the ADR 0091 refusal,
    not a locator bounce: nothing here needs re-citing."""
    from fin.host.citation_gate import RefuseResult

    items = evidence_from_database_rows([_ROW_WIRE])
    result = verify_citations(
        answer=(
            f"{_ROW_WIRE['summary']}. The total amount is 999999 USD."
        ),
        evidence=items,
        repair_count=0,
        known_document_ids=set(),
    )
    assert isinstance(result, RefuseResult), result
    assert result.reason == "unsupported_database_claim"


def test_a_supported_figure_beside_a_fabricated_one_still_refuses() -> None:
    """The refusal is a SUBSET check (spec §8: "a number the model
    states that no returned fields supports is unsupported"), not an
    existence check: one true figure must not launder a fabricated one
    beside it. Under the old ``any(row supports any token)`` reading
    this answer published with a citation, which is exactly the
    laundering the gate's own comment forbids."""
    from fin.host.citation_gate import RefuseResult

    items = evidence_from_database_rows([_ROW_WIRE])
    result = verify_citations(
        answer=(
            f"{_ROW_WIRE['summary']}. The notice period is 30 days "
            "and the penalty is 5,000,000 USD."
        ),
        evidence=items,
        repair_count=0,
        known_document_ids=set(),
    )
    assert isinstance(result, RefuseResult), result
    assert result.reason == "unsupported_database_claim"


# ----- the run seam: rows through the retrieve tool -----


def _run_tool(port: FakeRetrievePort) -> dict[str, Any]:
    """One tool invocation of the retrieve node over the given port.

    The tool is driven the way the graph drives it — a full model
    ToolCall (the tool carries an ``InjectedToolCallId``, so a bare
    input dict is refused by design) with the run's state and config as
    the injected args — so the assertion covers the REAL hop, not a
    hand-built call.
    """
    tool = _make_retrieve_tool(port)
    state = {
        "search_mode_decision": {"search_mode": "sql"},
        "evidence": [],
    }
    return tool.invoke(
        {
            "type": "tool_call",
            "name": "retrieve",
            "id": "c-sql",
            "args": {
                "query": "contracts",
                "state": state,
                "config": {"configurable": {"thread_id": "sql-run"}},
            },
        }
    )


def test_rows_retrieve_becomes_row_evidence_and_rides_the_envelope() -> None:
    port = FakeRetrievePort(database_rows_by_query={"contracts": [_ROW_WIRE]})
    command = _run_tool(port)

    update = command.update
    evidence = update["evidence"]
    assert [item.kind for item in evidence] == [DATABASE_ROW_KIND]
    assert evidence[0].database_row[0].source == "ledger_ro.contract_expiry"

    import json

    envelope = json.loads(update["messages"][0].content)
    assert envelope["chunks"] == []
    # The E1 row object rides to the model intact (spec
    # enterprise-evidence-contract §4.4): the graph reads the row list
    # out of ``rows`` for evidence and passes the envelope's ``schema`` /
    # ``schema_version`` pair and ``result_count`` through untouched.
    assert envelope[DATABASE_ROWS_KEY] == {
        "schema": "fin.evidence.retrieve_rows",
        "schema_version": 1,
        "rows": [_ROW_WIRE],
        "result_count": 1,
    }, "the model can only quote the display line it was shown"


def test_a_zero_row_sql_run_records_the_named_empty() -> None:
    """The the tracked change empty outcome: the run's own record carries the job
    fact, so the verifier's refusal is re-labelled
    ``sql_no_matching_rows`` — NOT ``corpus_not_ready``, whose advice
    (ingest documents) is factually wrong after a database miss."""
    port = FakeRetrievePort()  # unscripted sql answers the named empty
    command = _run_tool(port)
    outcomes = command.update["retrieve_outcomes"]
    assert retrieve_blocking_failure(outcomes) == SQL_NO_MATCHING_ROWS


def test_the_rewrite_retry_still_counts_a_rows_object_as_a_result() -> None:
    """Spec §8.0's rule survives the E1 row-object shape: an empty-chunks
    envelope whose sibling OBJECT carries rows is not a miss — rewriting
    one would re-query the ledger and OVERWRITE the rows — while the
    recorded zero-row object (empty ``rows``) still reads as empty."""
    from fin.host.rewrite_retry import _is_empty_hit

    rows_answer = {
        "chunks": [],
        DATABASE_ROWS_KEY: {
            "schema": "fin.evidence.retrieve_rows",
            "schema_version": 1,
            "rows": [_ROW_WIRE],
            "result_count": 1,
        },
    }
    assert _is_empty_hit(rows_answer) is False
    zero_rows_answer = {
        "chunks": [],
        DATABASE_ROWS_KEY: {
            "schema": "fin.evidence.retrieve_rows",
            "schema_version": 1,
            "rows": [],
            "result_count": 0,
        },
    }
    assert _is_empty_hit(zero_rows_answer) is True


def test_the_zero_row_copy_names_the_ledger_not_documents() -> None:
    copy = ORDINARY_PERSON_COPY[SQL_NO_MATCHING_ROWS]
    lowered = copy.lower()
    assert "database" in lowered or "ledger" in lowered
    for forbidden in ("ingest", "corpus", "document"):
        assert forbidden not in lowered, copy


# ----- named exits through the tool seam -----


@pytest.mark.parametrize(
    "code",
    [
        "real_db_not_wired",
        "sql_query_denied",
        "sql_query_timeout",
        "sql_authorization_unverified",
        "invalid_business_query",
    ],
)
def test_a_structured_sql_failure_surfaces_verbatim_not_as_a_miss(
    code: str,
) -> None:
    """The tool's typed-EXIT branch passes the port's error dict
    through: the run never gets an evidence list, and the operator's
    fact keeps its own name (retrying a denied read is a different
    instruction than retrying an outage)."""
    import json

    port = FakeRetrievePort(sql_named_failure=code)
    command = _run_tool(port)
    envelope = json.loads(command.update["messages"][0].content)

    assert envelope["error"] == code
    assert "chunks" not in envelope
    assert command.update["evidence"] == []


def test_an_assigned_sql_turn_reaches_the_port_with_the_token() -> None:
    """Handoff trap 1, pinned at the hop: naming 真数据库 assigns through
    to the retrieve path — the audit carries ``sql`` and the port
    receives ``sql``, never ``None`` dressed as iknow's default 知识问答."""
    port = FakeRetrievePort(hits_by_query={"revenue": [_hit()]})
    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=ScriptedChatModel(
            script=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "retrieve",
                            "args": {"query": "revenue"},
                            "id": "c-hop-sql",
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(content="Per the ledger: one contract."),
            ]
        ),
        adapter=port,
    )
    out = graph.invoke(
        {**reset_input_state(
            # The wording carries a research signal on purpose: an
            # underspecified ask would route the turn to ``clarify``
            # (track rules), and the 档 cascade — the seam under test —
            # only runs on a ``grounded`` turn.
            "sql档 what contracts expire for AAPL in 2026 Q1?"
        ),
         "evidence": []},
        config={"configurable": {"thread_id": "hop-sql"}},
    )

    assert out["search_mode_decision"]["search_mode"] == "sql"
    assert out["search_mode_decision"]["failure_code"] is None
    modes = [call["search_mode"] for call in port.calls]
    assert modes and set(modes) == {"sql"}, modes


# ----- the five retrieve-side classes stay five names -----


def test_sql_exits_do_not_collapse_into_the_existing_codes() -> None:
    """Spec §7's distinctness requirement, extended to the new family:
    empty-corpus / mapping drop / 档未开 / summary-index / zero-rows are
    five different public tokens; adding ``sql_no_matching_rows`` must
    not alias one of them, and its outcome key is distinct from the
    community-summary key it rides beside."""
    codes = {
        CORPUS_NOT_READY,
        MAPPING_DROP,
        TIER_NOT_OPEN,
        COMMUNITY_SUMMARY_INDEX_FAILURE,
        SQL_NO_MATCHING_ROWS,
        EMPTY_FAST_GRAPH_FAILURE,
    }
    assert len(codes) == 6
    assert SQL_NO_MATCHING_ROWS == "sql_no_matching_rows"
    assert RETRIEVE_OUTCOME_SQL_NO_MATCHING_ROWS == SQL_NO_MATCHING_ROWS
