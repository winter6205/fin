"""The ``sql`` 档's wire shape: window filters, named failures, row sibling.

Issue #322 T7 (specs/search-mode-business-sql-contract.md §4/§7/§8.0): fin
no longer refuses ``sql`` at the port — the request reaches the iknow wire
WITH its expiry window, and what comes back is consumed as its own evidence
kind, never as a chunk. What is pinned here, in spec order:

* the POSITIVE send: a ``sql`` request with a valid window puts
  ``filters.search_mode=sql`` beside ``filters.sql_window_start`` /
  ``sql_window_end`` on the JSON-RPC payload (§8.0's sibling keys are the
  producer's contract, mirrored here as the consumer's send);
* the window gate: a missing or malformed window is named
  ``invalid_business_query`` BEFORE the transport — the same exit the
  producer refuses with before a connection exists (§7.1), never a
  defaulted window;
* the structured named failures: each token the producer declares in the
  JSON ``error`` field surfaces under ITS OWN name (real_db_not_wired
  means NOT PROVISIONED, never refused-at-door, and never collapses into
  a transport-down ``retrieve_unavailable``);
* the zero-row exit: a query that ran and matched no row is the RECORDED
  job fact ``sql_no_matching_rows`` — an empty answer named as itself,
  distinct from 「语料没东西」 (§7);
* the row sibling: ``database_rows`` rides the port envelope beside
  ``chunks`` as the E1 row OBJECT ``{schema, rows, result_count}`` (the
  bare-array producer shape is obsolete) and an envelope without it
  stays byte-identical (document 档 untouched, §8.0);
* the sibling's contract check: a PRESENT row object is put through the
  E1 structural validator at ingress, and a §3/§6 violation becomes the
  named structured failure carrying the exact code — never the interim
  silent absent-sibling reading (spec §4.4/§6);
* the closed set still closed: ``sql`` resolves to its token now that the
  routing-time table has no row, and an out-of-set token is still
  ``invalid_search_mode``.

Fake transport only (the documented ``_call_mcp`` seam); no live MCP
server, no live ledger — the real-boundary legs are §9's opt-in suites.
"""

from __future__ import annotations

import json

import pytest

from fin.retrieve.mcp_adapter import MCPRetrieveAdapter
from fin.retrieve.port import (
    DATABASE_ROWS_KEY,
    InvalidBusinessQueryError,
    InvalidSearchModeError,
    RETRIEVE_OUTCOME_SQL_NO_MATCHING_ROWS,
    SQL_WINDOW_END_FILTER_KEY,
    SQL_WINDOW_START_FILTER_KEY,
    resolve_search_mode,
    resolve_sql_window,
)
from fin.retrieve.tiers import (
    REAL_DB_NOT_WIRED,
    RETRIEVE_TIERS,
    TIER_UNAVAILABLE_CODE,
)


class _CapturingAdapter(MCPRetrieveAdapter):
    """Records the JSON-RPC payload a send would put on the wire.

    Skips the parent ``__init__`` exactly like the sibling suite's
    double, so no subprocess is allocated; the scripted response can
    carry any envelope the test needs.
    """

    def __init__(self, scripted_response: dict | None = None) -> None:
        self._command = None
        self._locator_map: dict[str, object] = {}
        self._known_doc_ids = None
        self._timeout = 5.0
        self._scripted_response = (
            scripted_response
            if scripted_response is not None
            else {"result": {"chunks": []}}
        )
        self.payloads: list[dict] = []

    async def _call_mcp(self, payload: dict) -> dict:  # type: ignore[override]
        self.payloads.append(payload)
        return self._scripted_response


def _filters(adapter: _CapturingAdapter, index: int = 0) -> dict:
    return adapter.payloads[index]["params"]["arguments"].get("filters", {})


def _is_error_envelope(body: dict) -> dict:
    """One ``tools/call`` refusal the way the real server sends it:
    ``isError`` with the structured JSON body in the first text block."""
    return {
        "result": {
            "isError": True,
            "content": [{"type": "text", "text": json.dumps(body)}],
        }
    }


# ----- the SSOT flip: sql is no longer refused at routing -----


def test_the_routing_time_table_refuses_no_档_anymore() -> None:
    """One row retired, all three seams with it: ``sql`` resolves to its
    token like every other member of the closed set, and the refusal
    table is empty rather than deleted — the mechanism stays so a future
    plan slot is one table row, exactly as 综述 opening was."""
    assert TIER_UNAVAILABLE_CODE == {}
    for token in RETRIEVE_TIERS:
        assert resolve_search_mode(token) == token
    assert resolve_search_mode("sql") == "sql"


def test_an_out_of_set_token_is_still_invalid_search_mode() -> None:
    """The closed set did NOT widen into prose acceptance: a retired
    letter or a look-alike word still names ``invalid_search_mode`` at
    the seam (the rename's pin survives the tier opening)."""
    for raw in ("A", "D", "summary", "SQL ", ""):
        with pytest.raises(InvalidSearchModeError) as excinfo:
            resolve_search_mode(raw)
        assert excinfo.value.code == "invalid_search_mode", raw


# ----- the window gate (port side, before transport) -----


def test_a_valid_sql_window_resolves_to_its_pair() -> None:
    window = resolve_sql_window("sql", "2026-01-01", "2026-03-31")
    assert window == ("2026-01-01", "2026-03-31")


@pytest.mark.parametrize(
    ("start", "end"),
    [
        pytest.param(None, None, id="both-missing"),
        pytest.param("2026-01-01", None, id="end-missing"),
        pytest.param(None, "2026-03-31", id="start-missing"),
        pytest.param("2026-3-1", "2026-03-31", id="unpadded"),
        pytest.param("20260101", "20260331", id="undashed"),
        pytest.param("2026-13-01", "2026-13-31", id="not-a-real-date"),
        pytest.param("2026-03-31", "2026-01-01", id="end-before-start"),
    ],
)
def test_a_window_the_template_cannot_bind_is_named_before_transport(
    start: str | None, end: str | None
) -> None:
    """Same exit name the producer uses before a connection exists
    (iknow refuses ``invalid_business_query`` in its executor, never
    with a defaulted window), so an operator's malformed ask is named
    without ever spending a round trip."""
    with pytest.raises(InvalidBusinessQueryError) as excinfo:
        resolve_sql_window("sql", start, end)
    assert excinfo.value.code == "invalid_business_query"


@pytest.mark.parametrize("mode", ["lexical", "graph", "global", None])
def test_a_document_档_carries_no_window(mode: str | None) -> None:
    """The window belongs to the ``sql`` 档 alone: a document send
    resolves to ``None`` and never learns the filter keys — the document
    wire shape stays byte-identical (§8.0's blast-radius rule)."""
    assert resolve_sql_window(mode, "2026-01-01", "2026-03-31") is None
    assert resolve_sql_window(mode, None, None) is None


def test_a_window_on_a_document_send_does_not_reach_the_wire() -> None:
    adapter = _CapturingAdapter()
    adapter.retrieve("revenue", search_mode="graph")
    assert adapter.payloads, "the graph send still ran"
    assert SQL_WINDOW_START_FILTER_KEY not in _filters(adapter)
    assert SQL_WINDOW_END_FILTER_KEY not in _filters(adapter)


def test_sql_send_reaches_the_wire_with_its_window() -> None:
    """The positive hop: 真数据库 is no longer refused at the door, at
    the port, or in the cascade — it rides the SAME single retrieve
    entry point with its expiry window as bound routing filters."""
    adapter = _CapturingAdapter()
    adapter.retrieve(
        "contracts expiring",
        search_mode="sql",
        sql_window_start="2026-01-01",
        sql_window_end="2026-03-31",
    )

    assert len(adapter.payloads) == 1
    assert _filters(adapter) == {
        "search_mode": "sql",
        SQL_WINDOW_START_FILTER_KEY: "2026-01-01",
        SQL_WINDOW_END_FILTER_KEY: "2026-03-31",
    }


def test_sql_without_a_window_never_starts_a_round_trip() -> None:
    adapter = _CapturingAdapter()
    with pytest.raises(InvalidBusinessQueryError):
        adapter.retrieve("contracts", search_mode="sql")
    assert adapter.payloads == [], (
        "a windowless sql request must not spend the wire it cannot bind"
    )


# ----- structured named failures (spec §7.1) -----


@pytest.mark.parametrize(
    "code",
    [
        pytest.param(REAL_DB_NOT_WIRED, id="not-provisioned"),
        pytest.param("sql_query_denied", id="denied"),
        pytest.param("sql_query_timeout", id="timeout"),
        pytest.param("sql_authorization_unverified", id="untrusted"),
        pytest.param("invalid_business_query", id="unbound-request"),
    ],
)
def test_a_structured_named_failure_keeps_its_own_name(
    code: str,
) -> None:
    """Each declared token surfaces as the named EXIT — never
    ``retrieve_unavailable`` (a dead-server reading the operator would
    retry in vain) and never 「档未开」-at-door (§7: after A2
    ``real_db_not_wired`` means NOT PROVISIONED only)."""
    adapter = _CapturingAdapter(_is_error_envelope({"error": code}))
    assert adapter.retrieve(
        "q",
        search_mode="sql",
        sql_window_start="2026-01-01",
        sql_window_end="2026-03-31",
    ) == {"error": code}


def test_not_provisioned_is_distinct_from_transport_down() -> None:
    """The same seam, two facts: a structured ``real_db_not_wired``
    names the missing ledger; an untokened error (plain prose) stays the
    outage it is, so the two exits never merge in either direction."""
    named = _CapturingAdapter(
        _is_error_envelope({"error": REAL_DB_NOT_WIRED})
    )
    assert named.retrieve(
        "q",
        search_mode="sql",
        sql_window_start="2026-01-01",
        sql_window_end="2026-03-31",
    ) == {"error": REAL_DB_NOT_WIRED}

    prose = _CapturingAdapter(
        {"result": {"isError": True, "content": [{"type": "text", "text": "boom"}]}}
    )
    assert prose.retrieve(
        "q",
        search_mode="sql",
        sql_window_start="2026-01-01",
        sql_window_end="2026-03-31",
    ) == {"error": "retrieve_unavailable"}


def test_zero_rows_is_a_recorded_empty_named_fact() -> None:
    """A query that ran and matched no row is the RECORDED job fact
    (the community-summary precedent: empty chunks plus the flag), NOT
    an error dict and never the bare empty a host would read as
    「语料没东西」 — the fix is a wider window, not an ingest (§7)."""
    adapter = _CapturingAdapter(
        _is_error_envelope({"error": "sql_no_matching_rows"})
    )
    out = adapter.retrieve(
        "q",
        search_mode="sql",
        sql_window_start="2026-04-01",
        sql_window_end="2026-06-30",
    )
    assert out == {
        "chunks": [],
        DATABASE_ROWS_KEY: {
            "schema": "fin.evidence.retrieve_rows",
            "schema_version": 1,
            "rows": [],
            "result_count": 0,
        },
        RETRIEVE_OUTCOME_SQL_NO_MATCHING_ROWS: True,
    }


def test_the_community_summary_flag_path_is_unchanged() -> None:
    """Opening sql moved nothing on the global 综述 runtime exit: the
    same structured read still answers empty-plus-flag, byte-equal."""
    adapter = _CapturingAdapter(
        _is_error_envelope({"error": "community_summary_index_failure"})
    )
    out = adapter.retrieve("q", search_mode="global")
    assert out == {
        "chunks": [],
        "community_summary_unready": True,
    }


# ----- the row sibling on the port envelope (spec §8.0) -----


_ROW = {
    # An INGRESS row as the REAL producer emits it (spec 4.4 "schema
    # placement at ingress"): exactly the five value keys, and NO
    # per-row ``schema``. The envelope below is what names the format of
    # the list. WHY this fixture used to be wrong: it carried a
    # per-row ``schema`` token, so it agreed with the validator by
    # construction and could never surface the producer/validator
    # disagreement this lane exists to catch.
    "source": "ledger_ro.contract_expiry",
    "record_key": {"contract_id": "C-2291"},
    # Whole seconds, ``Z`` form. A producer emitting ``.500Z`` is refused
    # below, not normalized: the value is an instant the ledger reported.
    "queried_at": "2026-03-01T09:14:22Z",
    "fields": {
        "counterparty": "Northwind Logistics",
        "expiry_date": "2026-03-14",
        "notice_days": 30,
        "notice_by": "2026-02-12",
    },
    "summary": "C-2291 Northwind Logistics, expires 2026-03-14, notice by 2026-02-12",
}

# The E1-frozen row-object schema token (iknow envelope contract, spec
# enterprise-evidence-contract §4.4). Spelled as the contract's literal,
# not imported, so the expectation stays independent of the consumer.
# The token carries NO version segment (spec 2.1): the version is the
# integer ``schema_version`` sibling below, not part of the name.
_ROWS_SCHEMA = "fin.evidence.retrieve_rows"
_SCHEMA_VERSION = 1

# The egress counterpart: the snapshot object travels ALONE inside a
# citation, so it is the one shape that keeps its own ``schema`` and
# gains the integer version beside it (spec 4.4 egress).
_EGRESS_SNAPSHOT = {
    "schema": "fin.evidence.database_row_snapshot",
    "schema_version": 1,
    "source": "ledger_ro.contract_expiry",
    "record_key": {"contract_id": "C-2291"},
    "queried_at": "2026-03-01T09:14:22Z",
    "fields": {"notice_days": "30", "expiry_date": "2026-03-14"},
    "summary": "C-2291 Northwind Logistics, expires 2026-03-14, notice by 2026-02-12",
}


def _rows_object(
    rows: list[object], result_count: int | None = None, **overrides: object
) -> dict:
    """One E1 row object: ``{schema, schema_version, rows, result_count}``.
    ``result_count`` defaults to the row count; a test can pin a
    producer's own number separately (fin passes it through and does NOT
    re-derive it — consistency validation is the E3 validator's
    boundary, not this consumer's). ``**overrides`` lets a test drop or
    replace an envelope key without hand-writing the whole object."""
    envelope: dict = {
        "schema": _ROWS_SCHEMA,
        "schema_version": _SCHEMA_VERSION,
        "rows": rows,
        "result_count": len(rows) if result_count is None else result_count,
    }
    envelope.update(overrides)
    return envelope


def _wire_rows_envelope(rows: list[object], result_count: int | None = None) -> dict:
    """One MCP ``tools/call`` success whose business envelope carries
    the row sibling beside ``chunks`` as the E1 row OBJECT (the canonical
    content-block form the real server answers with). The bare-array
    producer shape is obsolete."""
    inner = {"chunks": [], DATABASE_ROWS_KEY: _rows_object(rows, result_count)}
    return {
        "result": {
            "content": [{"type": "text", "text": json.dumps(inner)}]
        }
    }


def _wire_sibling_envelope(sibling: object) -> dict:
    """One MCP ``tools/call`` success carrying a scripted sibling value
    verbatim — the shape the wire sent, however the validator reads it."""
    inner = {"chunks": [], DATABASE_ROWS_KEY: sibling}
    return {
        "result": {
            "content": [{"type": "text", "text": json.dumps(inner)}]
        }
    }


def test_rows_come_back_as_their_own_sibling_key_not_chunks() -> None:
    adapter = _CapturingAdapter(_wire_rows_envelope([_ROW]))
    out = adapter.retrieve(
        "contracts expiring",
        search_mode="sql",
        sql_window_start="2026-01-01",
        sql_window_end="2026-03-31",
    )

    assert out["chunks"] == [], "a row never enters chunks[]"
    assert out[DATABASE_ROWS_KEY]["rows"] == [_ROW]


def test_the_consumer_passes_schema_and_result_count_through() -> None:
    """``schema`` / ``result_count`` are the producer's own fields: the
    consumer reads ``rows`` and hands the sibling on with them intact
    (the snapshot carrier reads them; re-typing or re-deriving them here
    would put fin's number where the wire's number was — E3's
    consistency check judges the WIRE, not a consumer rewrite)."""
    adapter = _CapturingAdapter(_wire_rows_envelope([_ROW], result_count=1))
    out = adapter.retrieve(
        "q",
        search_mode="sql",
        sql_window_start="2026-01-01",
        sql_window_end="2026-03-31",
    )
    assert out[DATABASE_ROWS_KEY]["schema"] == _ROWS_SCHEMA
    assert out[DATABASE_ROWS_KEY]["result_count"] == 1


def test_a_row_that_is_not_an_object_is_the_named_contract_failure() -> None:
    """A present row object whose row is not a record is a wire
    violation, named by its §6 code — not a silent half-answer with the
    readable rows surviving and the rest dropped off the ledger (SC22's
    drop-and-count posture held only while ingress did not validate;
    the E3 validator home owns the shape now)."""
    adapter = _CapturingAdapter(_wire_rows_envelope([_ROW, "not-a-row"]))
    out = adapter.retrieve(
        "q",
        search_mode="sql",
        sql_window_start="2026-01-01",
        sql_window_end="2026-03-31",
    )
    assert out == {"error": "malformed_evidence_contract"}


@pytest.mark.parametrize(
    ("sibling", "code"),
    [
        pytest.param(
            # NEGATIVE-LIST: this stays ``.v2`` — a token the registry
            # has not frozen is exactly the unknown-token refusal, and
            # reusing the current token here would make the case
            # indistinguishable from the schema_version legs below.
            _rows_object([], schema="fin.evidence.retrieve_rows.v2"),
            "unsupported_evidence_contract",
            id="unknown-envelope-schema",
        ),
        pytest.param(
            _rows_object([_ROW], result_count=2),
            "malformed_evidence_contract",
            id="result-count-mismatch",
        ),
        pytest.param(
            _rows_object("not-a-list", result_count=0),
            "malformed_evidence_contract",
            id="rows-not-a-list",
        ),
        pytest.param(
            _rows_object([{**_ROW, "queried_at": "2026-03-01T09:14:22+08:00"}]),
            "malformed_evidence_contract",
            id="offset-timestamp",
        ),
        pytest.param(
            _rows_object([_ROW, {"extra": 1, **_ROW}]),
            "malformed_evidence_contract",
            id="unknown-row-field",
        ),
        pytest.param(
            _rows_object([_ROW] * 21),
            "database_row_overflow",
            id="more-than-20-rows",
        ),
    ],
)
def test_a_shape_violation_on_a_present_sibling_is_named_on_the_wire(
    sibling: object, code: str
) -> None:
    """§3/§6 violations at ingress surface as the named structured
    failure carrying the exact code — never the interim silent
    absent-sibling reading, and never prose."""
    adapter = _CapturingAdapter(_wire_sibling_envelope(sibling))
    out = adapter.retrieve(
        "q",
        search_mode="sql",
        sql_window_start="2026-01-01",
        sql_window_end="2026-03-31",
    )
    assert out == {"error": code}


# ----- the ingress row shape, as the real producer emits it -----
#
# The legs below are the reproduction of the producer/validator
# incompatibility this pass exists for. They would ALL fail against the
# pre-change code: the row carried a per-row ``schema`` there, so a
# fixture built from the real producer output had no conforming shape to
# assert, and the millisecond ``queried_at`` was never exercised at all.


def _sql_call(sibling: object) -> dict:
    """One sql-mode ``retrieve`` answer, asserted for its named exit."""
    adapter = _CapturingAdapter(_wire_sibling_envelope(sibling))
    return adapter.retrieve(
        "contracts expiring",
        search_mode="sql",
        sql_window_start="2026-01-01",
        sql_window_end="2026-03-31",
    )


def test_the_real_producer_row_shape_is_accepted() -> None:
    """The positive leg of the reproduction: an envelope whose rows carry
    EXACTLY the five value keys, with a whole-second ``queried_at``, a
    real-shaped ``record_key`` object and a real-shaped ``fields`` object,
    is accepted and handed on intact. Before the change there was no
    such conforming producer shape to assert."""
    out = _sql_call(_rows_object([_ROW]))
    assert DATABASE_ROWS_KEY in out, out
    assert out[DATABASE_ROWS_KEY]["rows"] == [_ROW]
    assert out[DATABASE_ROWS_KEY]["schema"] == _ROWS_SCHEMA
    assert out[DATABASE_ROWS_KEY]["schema_version"] == 1


def test_a_row_carrying_its_own_schema_is_refused() -> None:
    """The envelope already names the format; a per-row marker duplicates
    that declaration on every element, permits a list whose rows disagree
    about their own format, and adds no information (spec 4.4). It is an
    unknown key at the row, so §2.4 ``malformed_evidence_contract`` — not
    a tolerated extra, and not silently stripped."""
    out = _sql_call(
        _rows_object([{**_ROW, "schema": "fin.evidence.database_row_snapshot"}])
    )
    assert out == {"error": "malformed_evidence_contract"}


@pytest.mark.parametrize(
    "dropped",
    ["source", "record_key", "queried_at", "fields", "summary"],
)
def test_a_row_missing_any_required_key_is_refused(dropped: str) -> None:
    row = {key: value for key, value in _ROW.items() if key != dropped}
    out = _sql_call(_rows_object([row]))
    assert out == {"error": "malformed_evidence_contract"}


@pytest.mark.parametrize(
    ("row", "code"),
    [
        pytest.param(
            {**_ROW, "fields": {f"col_{n}": n for n in range(17)}},
            "database_row_overflow",
            id="seventeen-fields",
        ),
        pytest.param(
            {**_ROW, "summary": "y" * 1001},
            "database_row_overflow",
            id="summary-1001-codepoints",
        ),
        pytest.param(
            {**_ROW, "record_key": "C-2291"},
            "malformed_evidence_contract",
            id="record-key-not-an-object",
        ),
        pytest.param(
            {**_ROW, "record_key": {f"k{n}": n for n in range(9)}},
            "database_row_overflow",
            id="nine-record-key-pairs",
        ),
        pytest.param(
            {**_ROW, "fields": {"note": "z" * 513}},
            "database_row_overflow",
            id="field-value-513-chars",
        ),
        pytest.param(
            {**_ROW, "source": "s" * 129},
            "database_row_overflow",
            id="source-129-chars",
        ),
    ],
)
def test_a_row_exceeding_any_single_bound_is_refused(row: dict, code: str) -> None:
    """Every §3 bound on the row is a named overflow, and the non-object
    ``record_key`` is the shape refusal. Each bound is exercised on its
    own row so no bound can be passed by another failing first — a test
    that overflows two bounds at once proves only that one of them
    holds."""
    out = _sql_call(_rows_object([row]))
    assert out == {"error": code}


def test_a_millisecond_queried_at_is_refused_and_never_normalized() -> None:
    """The producer's obligation, pinned as a guard. A row carrying
    ``2026-03-01T09:14:22.500Z`` is refused as
    ``malformed_evidence_contract``: fin does NOT truncate the fraction
    to whole seconds to make it fit. WHY it must stay a refusal: the
    value is the instant the ledger was read, and a rewritten one would
    put fin's guess where the producer's record was — the stored
    ``queried_at`` would then disagree with the database's own record
    undetectably (spec 2.5). The fix belongs in the producer."""
    out = _sql_call(_rows_object([{**_ROW, "queried_at": "2026-03-01T09:14:22.500Z"}]))
    assert out == {"error": "malformed_evidence_contract"}


@pytest.mark.parametrize("schema_version", [0, 2, -1, 99])
def test_a_wrong_integer_schema_version_is_unsupported(schema_version: object) -> None:
    """A recognized token carrying an unrecognized version INTEGER is
    the §2.3 / §6 unsupported exit — never a silent default to 1, and
    never the nearest older parser. That pair (name known, version not
    spoken) is the whole refusal this field exists to name."""
    out = _sql_call(_rows_object([_ROW], schema_version=schema_version))
    assert out == {"error": "unsupported_evidence_contract"}


@pytest.mark.parametrize(
    "schema_version",
    ["1", True, None, [], {}],
    ids=["string", "bool", "null", "list", "object"],
)
def test_a_non_integer_schema_version_is_malformed(schema_version: object) -> None:
    """A non-integer version is a wrong TYPE, which §6's class table
    puts under malformed, not unsupported. The two are not collapsed:
    unsupported means "a version we do not speak", and a string ``"1"``
    is not a version at all — it is a payload whose version field has
    the wrong type. Collapsing them would let a producer that shipped a
    string be answered "unsupported version", which invites it to bump
    the number rather than fix the type.

    PREMISE CHANGED (ADR 0093 decision 8): the float leg ``1.0`` used to
    sit here asserting malformed. The version rule is over the decoded
    VALUE, so ``1.0`` is version 1 and is ACCEPTED — a JSON number
    spelled ``1.0`` decodes to the float ``1.0`` here and to the integer
    ``1`` in a JavaScript decoder, and the two ends of the wire must not
    answer differently about the same bytes. The accept leg below now
    covers the retrieve envelope's own copy of that case."""
    out = _sql_call(_rows_object([_ROW], schema_version=schema_version))
    assert out == {"error": "malformed_evidence_contract"}


def test_a_float_schema_version_that_is_the_current_value_is_accepted() -> None:
    """The accept half of the cross-language case (ADR 0093 decision 8).

    ``1.0`` and ``1`` are the same number once decoded, so the retrieve
    envelope speaks the current version under both spellings. This is the
    leg that makes the two ends of the wire agree: the SAME JSON text
    reaches a JavaScript decoder as the integer ``1``, and a boundary
    that refused it here while accepting it there would answer one
    request two ways. WHY this lives beside the refusal legs rather than
    only in the shared case set: the retrieve ingress is a distinct
    payload root from the egress snapshot, and the shared set is driven
    at the egress one."""
    out = _sql_call(_rows_object([_ROW], schema_version=1.0))
    assert "error" not in out, out
    assert out[DATABASE_ROWS_KEY]["schema_version"] == 1


def test_a_missing_schema_version_is_malformed_not_unsupported() -> None:
    """Absent and wrong are different failures. A required key that is
    MISSING is §2.4 malformed; only a value that is present and is not
    the supported integer reaches the §2.3 unsupported exit — otherwise
    "no version stated" would masquerade as "a version we do not
    speak", and a producer that simply forgot the field would be told
    it shipped a future format."""
    envelope = _rows_object([_ROW])
    del envelope["schema_version"]
    assert _sql_call(envelope) == {"error": "malformed_evidence_contract"}


# ----- the egress counterpart, through the same wire -----
#
# The snapshot object travels ALONE under a citation's ``database_row``
# key, so it is the one shape that keeps its per-object ``schema`` and
# gains the integer ``schema_version`` beside it. The ingress rule (no
# per-row token) does NOT reach it, and this is the leg that keeps the
# two directions from collapsing into one.


def _database_row_citation(snapshot: dict) -> dict:
    return {
        "kind": "database-row",
        "document_id": "",
        "attachment_id": "",
        "version": snapshot["queried_at"],
        "unit": 1,
        "display": "record",
        "filename": "",
        "quote": snapshot["summary"],
        "database_row": snapshot,
    }


def _citations_call(citations: list[dict]):
    """One E1 citation channel run through the validator home directly.

    The row sibling reaches the validator THROUGH the adapter (the legs
    above), but the citation channel is a different payload root, so
    these egress legs name the same home explicitly rather than routing
    a citation channel through a sql retrieve call that never produces
    one. Imported locally: :mod:`fin.host` imports back into
    :mod:`fin.retrieve.mcp_adapter`, so a top-level import here closes a
    cycle (the same reason the adapter defers its own import)."""
    from fin.host.evidence_contract import (
        seed_context_from_rows,
        validate_evidence,
    )

    return validate_evidence(
        {
            "evidence_schema": "fin.evidence.citation",
            "schema_version": 1,
            "citations": citations,
        },
        seed_context_from_rows(citation_rows=citations),
    )


def test_an_egress_snapshot_with_schema_and_schema_version_is_accepted() -> None:
    result = _citations_call([_database_row_citation(_EGRESS_SNAPSHOT)])
    assert result.accepted, result.refusal


def test_an_egress_snapshot_whose_float_version_is_the_current_value_is_accepted() -> None:
    """The egress half of the cross-language accept case (ADR 0093
    decision 8), stated separately because the egress snapshot is its
    own token and the shared case set is also driven at this boundary.

    ``1.0`` is the current version by value. WHY it matters that the
    REBUILT object is checked as well: the decoder must not answer
    ``malformed`` and it must not pass the value through — a snapshot
    that re-emits the float would leave the next reader to judge it
    again, and the rule is that only the integer is ever minted."""
    snapshot = {**_EGRESS_SNAPSHOT, "schema_version": 1.0}
    result = _citations_call([_database_row_citation(snapshot)])
    assert result.accepted, result.refusal


@pytest.mark.parametrize("schema_version", [0, 2, 99])
def test_an_egress_snapshot_with_a_wrong_schema_version_is_refused(
    schema_version: object,
) -> None:
    snapshot = {**_EGRESS_SNAPSHOT, "schema_version": schema_version}
    result = _citations_call([_database_row_citation(snapshot)])
    assert not result.accepted
    assert result.refusal is not None
    assert result.refusal.code == "unsupported_evidence_contract"


@pytest.mark.parametrize("schema_version", ["1", True, None])
def test_an_egress_snapshot_with_a_non_integer_schema_version_is_refused(
    schema_version: object,
) -> None:
    """PREMISE CHANGED (ADR 0093 decision 8): the float leg ``1.0`` was
    removed from this list. The version rule is over the decoded VALUE,
    so ``1.0`` is version 1 and is accepted, not refused — see the accept
    leg above."""
    snapshot = {**_EGRESS_SNAPSHOT, "schema_version": schema_version}
    result = _citations_call([_database_row_citation(snapshot)])
    assert not result.accepted
    assert result.refusal is not None
    assert result.refusal.code == "malformed_evidence_contract"


def test_an_egress_snapshot_missing_its_schema_version_is_refused() -> None:
    snapshot = {key: value for key, value in _EGRESS_SNAPSHOT.items()
                if key != "schema_version"}
    result = _citations_call([_database_row_citation(snapshot)])
    assert not result.accepted
    assert result.refusal is not None
    assert result.refusal.code == "malformed_evidence_contract"


def test_an_egress_snapshot_missing_its_schema_token_is_refused() -> None:
    """The egress object's token is not optional just because the
    envelope carries one — different directions, different objects."""
    snapshot = {key: value for key, value in _EGRESS_SNAPSHOT.items()
                if key != "schema"}
    result = _citations_call([_database_row_citation(snapshot)])
    assert not result.accepted
    assert result.refusal is not None


def test_a_success_envelope_with_zero_rows_is_the_named_empty_not_a_silent_one() -> None:
    """A well-formed row object answering zero rows on the SUCCESS
    channel is the empty fact named as itself — distinct from
    malformed, and no longer the silent empty-chunks envelope a host
    would read as 「语料没东西」."""
    adapter = _CapturingAdapter(_wire_rows_envelope([]))
    out = adapter.retrieve(
        "q",
        search_mode="sql",
        sql_window_start="2026-04-01",
        sql_window_end="2026-06-30",
    )
    assert out == {"error": "sql_no_matching_rows"}


def test_a_bare_array_wire_is_the_obsolete_shape_read_as_no_rows() -> None:
    """The retired producer shape is not silently accepted: it carries
    no readable row object, so the consumer answers as if the sibling
    were absent — the no-rows reading, never a crash and never rows
    laundered from a shape the E1 contract no longer names."""
    adapter = _CapturingAdapter(
        {"result": {"chunks": [], DATABASE_ROWS_KEY: [_ROW]}}
    )
    out = adapter.retrieve(
        "q",
        search_mode="sql",
        sql_window_start="2026-01-01",
        sql_window_end="2026-03-31",
    )
    assert DATABASE_ROWS_KEY not in out


def test_a_document_envelope_stays_byte_identical() -> None:
    """No ``database_rows`` key on the wire means no empty list in the
    port answer: the absent-vs-empty distinction the producer promises
    (key ABSENT when not applicable) survives into the host envelope."""
    adapter = _CapturingAdapter({"result": {"chunks": []}})
    out = adapter.retrieve("q", search_mode="graph")
    assert out == {"chunks": []}
    assert DATABASE_ROWS_KEY not in out


# ----- the fake ports mirror the closed set AND the sql shape -----


def test_the_fake_accepts_sql_and_scripts_rows() -> None:
    from fin.retrieve.fake_port import FakeRetrievePort

    port = FakeRetrievePort(
        database_rows_by_query={"contracts": [_ROW]},
    )
    out = port.retrieve(
        "contracts",
        search_mode="sql",
        sql_window_start="2026-01-01",
        sql_window_end="2026-03-31",
    )
    assert out[DATABASE_ROWS_KEY] == _rows_object([_ROW])
    assert out["chunks"] == []
    last = port.calls[-1]
    assert last["search_mode"] == "sql"
    assert last["sql_window_start"] == "2026-01-01"
    assert last["sql_window_end"] == "2026-03-31"


def test_the_fake_answers_an_unscripted_sql_query_as_the_named_empty() -> None:
    """Zero rows is a NAMED empty fact even in the fake — the silent
    ``{"chunks": []}`` (which the host would read as 语料未就绪) is
    reserved for document 档 misses."""
    from fin.retrieve.fake_port import FakeRetrievePort

    port = FakeRetrievePort()
    out = port.retrieve(
        "nothing here",
        search_mode="sql",
        sql_window_start="2026-01-01",
        sql_window_end="2026-03-31",
    )
    assert out[RETRIEVE_OUTCOME_SQL_NO_MATCHING_ROWS] is True
    assert out[DATABASE_ROWS_KEY] == _rows_object([])


def test_the_fake_scripts_the_not_provisioned_exit() -> None:
    from fin.retrieve.fake_port import FakeRetrievePort

    port = FakeRetrievePort(sql_named_failure=REAL_DB_NOT_WIRED)
    out = port.retrieve(
        "q",
        search_mode="sql",
        sql_window_start="2026-01-01",
        sql_window_end="2026-03-31",
    )
    assert out == {"error": REAL_DB_NOT_WIRED}
