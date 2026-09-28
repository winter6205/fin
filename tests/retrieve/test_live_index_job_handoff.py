"""Opt-in real-boundary acceptance for T4's fin→iknow index-job handoff.

The default ``uv run pytest -q`` never runs this file — it is marked
``mcp`` and ``tests/retrieve/conftest.py`` deselects that mark unless
``-m mcp`` names it.  The opt-in command::

    uv run pytest -q -m mcp tests/retrieve/test_live_index_job_handoff.py

The client under test is :class:`fin.retrieve.mcp_index_job_port.MCPIndexJobPort`
(:meth:`submit_index_job` / :meth:`poll_index_job` and their typed
``IndexJob*`` failures) driven over real stdio JSON-RPC against a real
compiled iknow-memory process on real Postgres — independent of any fin
worker or HTTP surface.  The wire contract is iknow-memory
``specs/mcp-tool-surface.md`` §Index-job tool contract:

1. a submit of the ``(source_ref, content_identity)`` pair read back from
   a real push answers the persisted row (field-for-field against PG),
   with a durable ``job_id`` and a status inside the frozen vocabulary;
2. a duplicate submit of the same pair JOINS — same ``job_id``,
   ``reused=True``, no second row;
3. a submit pairing an existing ``source_ref`` with a wrong identity is
   refused by name (``index_job_identity_mismatch``) and persists nothing;
4. a poll of an unknown ``job_id`` raises the typed
   ``IndexJobUnknownJobError`` rather than answering an empty record;
5. a poll answers the job row and ``community_index_state`` as two
   SIBLING fields, each read from its own table — job status and corpus
   C status are never collapsed into one (any combination is legal);
6. empty and over-ceiling locators are named
   ``index_job_invalid_locator`` — by fin before the round trip and by
   the service on the wire — and the oversized value is never echoed.

Like the content-identity driver, everything expensive (切分, the push)
happens inside module-scoped fixtures, never while pytest merely
COLLECTS this file.
"""

from __future__ import annotations

import itertools
import json
import uuid
from typing import Any, Iterator

import psycopg
import pytest

from fin.corpus.chunking import split_unit
from fin.corpus.parsers._common import build_unit
from fin.corpus.push import PushEnvelope, envelope_of_unit
from fin.retrieve.index_job_port import (
    INDEX_JOB_STATUSES,
    LOCATOR_CEILING_CODEPOINTS,
    IndexJobIdentityMismatchError,
    IndexJobInvalidLocatorError,
    IndexJobUnknownJobError,
)
from fin.retrieve.mcp_index_job_port import MCPIndexJobPort

from live_iknow_support import (
    IKNOW_PG_URL,
    IKNOW_ROOT,
    drop_es_indices,
    live_iknow,
)


pytestmark = pytest.mark.mcp

_RUN = uuid.uuid4().hex[:10]
_DOCUMENT_ID = f"IJH-{_RUN.upper()}"
_VERSION = uuid.uuid4().hex + uuid.uuid4().hex
_CHUNK_INDEX = f"fin_ijh_chunks_{_RUN}"
_ENTITY_INDEX = f"fin_ijh_entities_{_RUN}"
_PG_SCHEMA = f"fin_ijh_pg_{_RUN}"

# One short single-sentence 资料: split_unit keeps it one slice, so the
# push is the cheapest real write that still produces a stored
# content_identity for the handoff.
_BODY = "索引作业交接验收：本资料仅用于驱动一次真实的 submit 与 poll。"

_REQUEST_IDS = itertools.count(500)

# A locator just over the shared ceiling; its character is unique to this
# module so an echo is detectable in any response body.
_OVER_CEILING = "u" * (LOCATOR_CEILING_CODEPOINTS + 1)
_AT_CEILING = "a" * LOCATOR_CEILING_CODEPOINTS


def _envelope() -> PushEnvelope:
    unit = build_unit(
        fmt="txt",
        source_sha256=_VERSION,
        document_id=_DOCUMENT_ID,
        version=_VERSION,
        unit=1,
        text=_BODY,
    )
    envelope = envelope_of_unit(
        split_unit(unit, doc_type="narrative"),
        valid_from="2020-01-01T00:00:00.000Z",
        valid_until="9999-12-31T23:59:59.999Z",
    )
    assert len(envelope.chunks) == 1, (
        "the fixture 资料 must stay one-slice so the push's written count "
        f"is fixed at the shape asserted below: {envelope!r}"
    )
    return envelope


def _call_tool(
    live: Any, name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    """One raw ``tools/call`` on the session, parsed from its text block.

    Raw-wire arms (locator refusals, the poll's sibling-field check) need
    the UNTRANSLATED body: the typed port refuses oversized locators
    locally and raises on refusals, so only the wire shows what the
    service itself names and echoes.
    """
    response = live.session.call(
        {
            "jsonrpc": "2.0",
            "id": next(_REQUEST_IDS),
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        20.0,
    )
    if "error" in response:
        raise AssertionError(f"{name} returned a JSON-RPC error: {response!r}")
    blocks = response["result"]["content"]
    return json.loads(blocks[0]["text"])


def _pg_rows(sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    """Direct read of the run's ephemeral schema — the store's own verdict,
    same shape as the content-identity driver's helper."""
    with psycopg.connect(IKNOW_PG_URL, autocommit=True) as connection:
        return list(connection.execute(sql, params).fetchall())


@pytest.fixture(scope="module")
def live() -> Iterator[Any]:
    """One real iknow subprocess on real PG + ES + Neo4j for the whole module."""
    entry = IKNOW_ROOT / "dist" / "index.js"
    if not entry.is_file():
        pytest.skip(
            f"{entry} is missing; run `npm run build` in {IKNOW_ROOT} "
            "to exercise the live index-job handoff"
        )
    monkeypatch = pytest.MonkeyPatch()
    try:
        with live_iknow(
            monkeypatch,
            storage="pg_es",
            es_chunk_index=_CHUNK_INDEX,
            es_entity_index=_ENTITY_INDEX,
            pg_schema=_PG_SCHEMA,
        ) as session:
            yield session
    finally:
        monkeypatch.undo()
        drop_es_indices(_CHUNK_INDEX, _ENTITY_INDEX)


@pytest.fixture(scope="module")
def envelope() -> PushEnvelope:
    return _envelope()


@pytest.fixture(scope="module")
def pushed(live: Any, envelope: PushEnvelope) -> dict[str, Any]:
    """The one real push whose response supplies the handed-off pair."""
    outcome = live.push.push([envelope.to_wire()])
    assert outcome["written"] == 1, outcome
    assert outcome["duplicates"] == 0, outcome
    identities = outcome["content_identities"]
    assert set(identities) == {envelope.source_ref}, identities
    return outcome


@pytest.fixture(scope="module")
def pair(pushed: dict[str, Any], envelope: PushEnvelope) -> tuple[str, str]:
    """The ``(source_ref, content_identity)`` pair exactly as fin read it
    off the push response — the no-recompute rule starts here."""
    return envelope.source_ref, pushed["content_identities"][envelope.source_ref]


@pytest.fixture(scope="module")
def job_port(live: Any) -> MCPIndexJobPort:
    """The client under test, riding the SAME session the push used."""
    return MCPIndexJobPort(session=live.session, timeout_seconds=20.0)


@pytest.fixture(scope="module")
def first_submit(job_port: MCPIndexJobPort, pair: tuple[str, str]) -> Any:
    """One fresh submit of the pair, shared by the submit/duplicate/poll arms."""
    return job_port.submit_index_job(*pair)


def test_submit_answers_the_persisted_row_with_a_durable_job_id(
    first_submit: Any, pair: tuple[str, str]
) -> None:
    """The success answer IS the persisted row: every field PG holds matches.

    ``job_id`` durability is checked where durability lives — the row in
    the run's own ``index_jobs`` table — so a claim of a durable id is
    evidence, not an echo of the handler's own string.
    """
    source_ref, identity = pair
    record = first_submit.record
    assert first_submit.reused is False, (
        f"a first submit of a never-submitted pair claimed a join: {first_submit!r}"
    )
    assert record.source_ref == source_ref
    assert record.content_identity == identity
    assert record.job_id
    assert record.status in INDEX_JOB_STATUSES, (
        f"the answered status {record.status!r} is outside the frozen "
        f"vocabulary {sorted(INDEX_JOB_STATUSES)}"
    )
    rows = _pg_rows(
        f"SELECT job_id, source_ref, content_identity, status, "
        f"started_at IS NULL, finished_at IS NULL, failure_code "
        f"FROM {_PG_SCHEMA}.index_jobs WHERE job_id = %s",
        (record.job_id,),
    )
    assert len(rows) == 1, (
        f"the answered job_id {record.job_id!r} names "
        f"{len(rows)} rows in the ledger; a submit's answer must be a "
        "persisted fact"
    )
    row_job_id, row_ref, row_identity, row_status, started_null, _, row_failure = rows[0]
    assert (row_job_id, row_ref, row_identity) == (
        record.job_id,
        record.source_ref,
        record.content_identity,
    )
    assert row_status == record.status
    # queued semantics on both sides: a null started_at in the row and None
    # on the record must be the SAME nullness, whatever the job's state.
    assert started_null is (record.started_at is None), (
        f"row says started_at null={started_null}, the adapter answered "
        f"{record.started_at!r}"
    )
    assert row_failure == record.failure_code


def test_duplicate_submit_joins_the_same_durable_job(
    job_port: MCPIndexJobPort, first_submit: Any, pair: tuple[str, str]
) -> None:
    """Same pair → same job_id, reused=True, and still exactly one row."""
    source_ref, _ = pair
    again = job_port.submit_index_job(*pair)
    assert again.reused is True, again
    assert again.record.job_id == first_submit.record.job_id, (
        "a duplicate submit answered a different job_id — the pair is "
        "supposed to join, not fork a second queue entry"
    )
    rows = _pg_rows(
        f"SELECT count(*) FROM {_PG_SCHEMA}.index_jobs WHERE source_ref = %s",
        (source_ref,),
    )
    assert rows[0][0] == 1, (
        f"a duplicate submit of one pair left {rows[0][0]} rows for its "
        "source_ref; the join must persist nothing new"
    )


def test_submit_with_a_wrong_identity_is_refused_by_name(
    job_port: MCPIndexJobPort, first_submit: Any, pair: tuple[str, str]
) -> None:
    """An existing source paired with a non-current identity: named refusal,
    nothing persisted — the failure mode fin's no-recompute rule exists to stop."""
    source_ref, _ = pair
    wrong_identity = "0" * 64
    assert wrong_identity != pair[1]
    with pytest.raises(IndexJobIdentityMismatchError) as refused:
        job_port.submit_index_job(source_ref, wrong_identity)
    assert refused.value.code == "index_job_identity_mismatch"
    rows = _pg_rows(
        f"SELECT count(*) FROM {_PG_SCHEMA}.index_jobs WHERE source_ref = %s",
        (source_ref,),
    )
    assert rows[0][0] == 1, (
        "the refused submit still left its mark: the ledger now holds "
        f"{rows[0][0]} rows for {source_ref!r} — a named refusal must "
        "persist nothing"
    )


def test_poll_of_an_unknown_job_id_raises_the_typed_unknown_job(
    job_port: MCPIndexJobPort,
) -> None:
    """The poll-side twin of a named not-found — raised, never an empty record."""
    unknown_job_id = uuid.uuid4().hex
    with pytest.raises(IndexJobUnknownJobError) as missed:
        job_port.poll_index_job(unknown_job_id)
    assert missed.value.code == "index_job_unknown_job"


def test_poll_answers_job_row_and_community_state_as_separate_fields(
    live: Any,
    job_port: MCPIndexJobPort,
    first_submit: Any,
) -> None:
    """Per-source status and corpus C status are two fields, each sourced from
    its own table — never collapsed, in either direction.

    The raw arm pins the SHAPE first: a poll answers a
    ``community_index_state`` key beside the job's own ``status`` even
    before the C singleton has ever been written (there it is ``null``,
    visibly NOT a restatement of the job status). The typed arm then
    seeds the singleton with a value no job-status derivation could invent
    and checks the port passes the row through verbatim, in a field
    separate from the record it reads from ``index_jobs``.
    """
    job_id = first_submit.record.job_id

    wire = _call_tool(live, "poll_index_job", {"job_id": job_id})
    assert "community_index_state" in wire, (
        f"a poll answered without the separate C field: {sorted(wire)!r}"
    )
    assert "status" in wire and wire["status"] in INDEX_JOB_STATUSES, wire

    # Seed the corpus-wide singleton (the community-summary CLI's own row
    # shape) so the pass-through check compares against a value PG holds,
    # not against whatever the job's status happens to be.
    c_failure = "community_summary_index_failure"
    with psycopg.connect(IKNOW_PG_URL, autocommit=True) as connection:
        connection.execute(
            f"INSERT INTO {_PG_SCHEMA}.community_index_state (id, status, failure_code) "
            "VALUES (1, 'failed', %s) "
            "ON CONFLICT (id) DO UPDATE SET status = EXCLUDED.status, "
            "failure_code = EXCLUDED.failure_code",
            (c_failure,),
        )
    stored_c = _pg_rows(
        f"SELECT status, failure_code FROM {_PG_SCHEMA}.community_index_state WHERE id = 1"
    )[0]
    stored_job = _pg_rows(
        f"SELECT status, failure_code FROM {_PG_SCHEMA}.index_jobs WHERE job_id = %s",
        (job_id,),
    )[0]

    poll = job_port.poll_index_job(job_id)
    # The record is the job row verbatim...
    assert poll.record.job_id == job_id
    assert (poll.record.status, poll.record.failure_code) == tuple(stored_job), (
        f"the polled record {poll.record.status!r}/"
        f"{poll.record.failure_code!r} diverged from the job row "
        f"{stored_job!r}"
    )
    # ...and the C sub-object is the singleton row verbatim, in its OWN field.
    assert poll.community_index_state.status == stored_c[0]
    assert poll.community_index_state.failure_code == stored_c[1]
    assert poll.community_index_state.failure_code == c_failure
    # Separation, not coincidence: C names the corpus-wide state while the
    # record names this source's job; collapsing them would answer one
    # status twice and lose the failure_code pairing.
    assert poll.record.status == wire["status"], (
        "the polled record disagrees with the raw wire status the same "
        "poll answered — the two fields were read through one another"
    )


def test_invalid_locator_inputs_are_named_without_echoing_the_overflow(
    live: Any,
    job_port: MCPIndexJobPort,
    pair: tuple[str, str],
) -> None:
    """Empty / whitespace / over-ceiling locators: typed by fin on the client
    (never sent) and named by the service on the wire (never echoed). The
    exactly-at-ceiling control shows the boundary is inclusive, not a
    blanket length refusal."""
    source_ref, identity = pair

    empty_calls = (
        lambda: job_port.submit_index_job("", identity),
        lambda: job_port.submit_index_job(source_ref, ""),
        lambda: job_port.submit_index_job(source_ref, "   "),
        lambda: job_port.poll_index_job(""),
        lambda: job_port.poll_index_job("   "),
        lambda: job_port.submit_index_job(_OVER_CEILING, identity),
        lambda: job_port.submit_index_job(source_ref, _OVER_CEILING),
        lambda: job_port.poll_index_job(_OVER_CEILING),
    )
    for call in empty_calls:
        with pytest.raises(IndexJobInvalidLocatorError) as refused:
            call()
        assert refused.value.code == "index_job_invalid_locator"
        assert "u" * 200 not in str(refused.value), (
            "the client's refusal echoed the oversized locator it was "
            "supposed to withhold"
        )

    # The service's own verdict, at the same boundary: over-ceiling submits
    # are named and the oversized value appears nowhere in the answer.
    refused_wire = _call_tool(
        live,
        "submit_index_job",
        {"source_ref": _OVER_CEILING, "content_identity": identity},
    )
    assert refused_wire.get("error") == "index_job_invalid_locator", refused_wire
    assert _OVER_CEILING not in json.dumps(refused_wire, ensure_ascii=False), (
        "the service echoed the oversized locator back — it is the overflow"
    )
    refused_poll = _call_tool(
        live, "poll_index_job", {"job_id": _OVER_CEILING}
    )
    assert refused_poll.get("error") == "index_job_invalid_locator", refused_poll
    assert _OVER_CEILING not in json.dumps(refused_poll, ensure_ascii=False)

    # Boundary control: exactly at the ceiling the locator check passes, so
    # the named failure must move one check deeper (unknown source),
    # proving 1024 is accepted and 1025 refused.
    at_ceiling = _call_tool(
        live,
        "submit_index_job",
        {"source_ref": _AT_CEILING, "content_identity": "0" * 64},
    )
    assert at_ceiling.get("error") == "index_job_unknown_source", (
        "a locator at exactly the ceiling must clear the locator check and "
        f"fail as an unknown source, not be refused as oversized: {at_ceiling!r}"
    )
