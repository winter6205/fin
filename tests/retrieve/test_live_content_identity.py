"""The content_identity fin reads back is the value iknow actually stored.

The default ``uv run pytest -q`` never runs this file — it is marked
``mcp`` and ``tests/retrieve/conftest.py`` deselects that mark unless
``-m mcp`` names it.  The opt-in command::

    uv run pytest -q -m mcp tests/retrieve/test_live_content_identity.py

Spec ``corpus-serve-ingest`` Decision 2 puts the authority for
``content_identity`` on iknow-memory: the service computes it over the
whole pushed body, stores it in ``source_documents.content_identity``
(unique jointly with ``source_ref``), and answers with that STORED value,
which :meth:`fin.retrieve.mcp_push_port.MCPPushPort.push` exposes as
``content_identities`` keyed by ``source_ref``.  fin is forbidden from
treating its own hash as authoritative, so the claim under test is about a
value only the producing side can supply — the contract fake could merely
echo itself, and an in-memory store has no ``source_documents`` row to
check the echo against.  This driver therefore spawns the real stdio
process and verifies it against real PG:

1. one push of three 资料 answers one 64-hex identity per ``source_ref``,
   equal to an independently derived oracle value over the WHOLE body —
   for the multi-child 资料 it must not equal that value over any child;
2. the row PG holds for that ``source_ref`` carries exactly the identity
   the adapter exposed — byte for byte;
3. re-pushing an unchanged body keeps the identity, the chunk ids, and the
   row counts (the §B1 idempotency baseline on the identity, not just on
   the ids);
4. ``submit_index_job`` accepts that ``(source_ref, content_identity)``
   pair and refuses a wrong one by name, which is the consumer the handoff
   exists for.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import re
import uuid
from typing import Any, Iterator

import psycopg
import pytest

from fin.corpus.chunking import split_unit
from fin.corpus.parsers._common import build_unit
from fin.corpus.push import PushEnvelope, envelope_of_unit

from live_iknow_support import (
    IKNOW_PG_URL,
    IKNOW_ROOT,
    drop_es_indices,
    live_iknow,
)


pytestmark = pytest.mark.mcp

_RUN = uuid.uuid4().hex[:10]
_DOCUMENT_ID = f"CID-{_RUN.upper()}"
_VERSION = uuid.uuid4().hex + uuid.uuid4().hex
_CHUNK_INDEX = f"fin_cid_chunks_{_RUN}"
_ENTITY_INDEX = f"fin_cid_entities_{_RUN}"
_PG_SCHEMA = f"fin_cid_pg_{_RUN}"

# A content identity is a sha256 hex digest; anything else is not one.
_IDENTITY_RE = re.compile(r"^[0-9a-f]{64}$")

# iknow's DEFAULT_CONTENT_IDENTITY_VERSIONS. The document-level identity is
# keyed on these service-owned constants even though the envelope metadata
# carries fin's own ``parse-2`` / ``params-1`` / ``norm-1`` rule versions —
# only the per-slice ids honour caller overrides.
_IDENTITY_VERSIONS = ("normalization-v1", "parse-v1", "parameters-v1")

_SENTENCE = "现金流保持稳健，资本开支按计划推进，管理层维持全年指引不变。"

_BODIES = (
    "第三季度营业收入创历史新高，毛利率小幅回升。",
    "研发投入集中于端侧模型，费用率同比持平。",
    _SENTENCE * 60,  # long enough for 切分 to yield two children
)

_REQUEST_IDS = itertools.count(100)


def _oracle_content_identity(body: str) -> str:
    """The expected identity of one pushed body, derived independently.

    Source of the expectation: the producing side's published formula —
    iknow-memory ``computeContentIdentity`` (``src/tools/ingest.ts``) with
    ``DEFAULT_CONTENT_IDENTITY_VERSIONS``, recorded in fin's spec
    ``corpus-serve-ingest`` Decision 2 as
    ``sha256(JSON([body, "normalization-v1", "parse-v1", "parameters-v1"]))``.
    It is written from that spec/code fact, NOT read out of the push
    response, so it can contradict the adapter: an adapter that echoed the
    wrong field, or hashed the slice instead of the body, fails here rather
    than passing by restating its own output.
    """
    payload = json.dumps(
        [body, *_IDENTITY_VERSIONS],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _envelopes() -> tuple[PushEnvelope, ...]:
    """Three 资料 pushed in one batch.

    The first two are single-chunk, the third 切分 into several children.
    A multi-child 资料 is what makes the body-level claim testable: for a
    one-slice 资料 the body and its only slice are the same text, so an
    answer that hashed the slice would be indistinguishable from one that
    hashed the body.

    Unit-granular ``source_ref`` values come from one per-run document /
    version, so no other live driver on this cluster can address them.
    """
    envelopes: list[PushEnvelope] = []
    for unit_number, text in enumerate(_BODIES, start=1):
        unit = build_unit(
            fmt="txt",
            source_sha256=_VERSION,
            document_id=_DOCUMENT_ID,
            version=_VERSION,
            unit=unit_number,
            text=text,
        )
        envelope = envelope_of_unit(
            split_unit(unit, doc_type="narrative"),
            valid_from="2020-01-01T00:00:00.000Z",
            valid_until="9999-12-31T23:59:59.999Z",
        )
        envelopes.append(envelope)

    assert len(envelopes[0].chunks) == 1 and len(envelopes[1].chunks) == 1, (
        "the two short 资料 must stay one-slice so the batch shape matches "
        f"the ticket's per-资料 identity claim: {envelopes[:2]!r}"
    )
    multi = envelopes[2]
    assert len(multi.chunks) >= 2 and all(
        chunk.content != multi.body for chunk in multi.chunks
    ), (
        "the third 资料 must 切分 into children that are each smaller than "
        "its body, or the body-vs-slice identity check is vacuous: "
        f"{multi!r}"
    )
    return tuple(envelopes)


_ENVELOPES = _envelopes()
_BODIES_BY_SOURCE_REF = {env.source_ref: env.body for env in _ENVELOPES}
_SLICES_BY_SOURCE_REF = {
    env.source_ref: tuple(chunk.content for chunk in env.chunks)
    for env in _ENVELOPES
}
_CHUNK_COUNT = sum(len(env.chunks) for env in _ENVELOPES)


def _wire() -> list[dict[str, Any]]:
    return [env.to_wire() for env in _ENVELOPES]


def _pg_rows(sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    """Direct read of the run's ephemeral schema — the store's own verdict.

    The schema name is this module's generated constant (the same shape
    ``live_iknow_support`` validates before its DDL), never caller text, so
    interpolating it into the qualified name is safe and keeps the query
    pointed at THIS run's tables rather than the cluster's ``public`` ones.
    """
    with psycopg.connect(IKNOW_PG_URL, autocommit=True) as connection:
        return list(connection.execute(sql, params).fetchall())


def _call_tool(
    live: Any, name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    """One raw ``tools/call`` on the session, parsed from its text block.

    The index-job pair has no fin port yet, so the handoff is exercised on
    the transport the ports use — same process, same connection, same
    store the push wrote to.
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


@pytest.fixture(scope="module")
def live() -> Iterator[Any]:
    """One real iknow subprocess on real PG + ES + Neo4j for the whole module.

    The PG branch is what makes the stored-row comparison checkable at all;
    the schema and indices are fin-owned, per-run unique, dropped at teardown.
    """
    entry = IKNOW_ROOT / "dist" / "index.js"
    if not entry.is_file():
        pytest.skip(
            f"{entry} is missing; run `npm run build` in {IKNOW_ROOT} "
            "to exercise the live content_identity handoff"
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
def pushed(live: Any) -> dict[str, Any]:
    """The one batch push both identity assertions read, and no more."""
    outcome = live.push.push(_wire())
    assert outcome["written"] == _CHUNK_COUNT, outcome
    assert outcome["duplicates"] == 0, outcome
    return outcome


def test_push_answers_one_oracle_identity_per_pushed_source_ref(
    pushed: dict[str, Any],
) -> None:
    """Spec Decision 2: the identities are 64-hex, per 资料, and oracle-equal.

    A batch answers ``content_identities[]`` in send order, so this also
    pins the attribution: a value present but paired with the wrong
    ``source_ref`` fails the per-key equality below.
    """
    identities = pushed["content_identities"]
    assert set(identities) == set(_BODIES_BY_SOURCE_REF), (
        "the identities fin exposes are not attributed to exactly the "
        f"资料 fin pushed: {sorted(identities)!r}"
    )
    for source_ref, identity in identities.items():
        assert _IDENTITY_RE.match(identity), (
            f"content_identity for {source_ref} is not a 64-hex sha256 "
            f"digest: {identity!r}"
        )
        assert identity == _oracle_content_identity(
            _BODIES_BY_SOURCE_REF[source_ref]
        ), (
            f"the identity exposed for {source_ref} is not the value the "
            "producing side's formula gives for that body"
        )
        for slice_text in _SLICES_BY_SOURCE_REF[source_ref]:
            if slice_text == _BODIES_BY_SOURCE_REF[source_ref]:
                continue
            assert identity != _oracle_content_identity(slice_text), (
                f"the identity for {source_ref} is the hash of one of its "
                "children, not of the whole body — the document-level "
                "identity and the per-slice one have been confused"
            )


def test_stored_pg_identity_equals_the_identity_fin_exposes(
    pushed: dict[str, Any],
) -> None:
    """The stored value is the authority fin hands on, byte for byte.

    This is the end-to-end evidence the fake port cannot supply — the row
    PG actually holds must carry the string the adapter handed on, and must
    hold it for the body that was pushed (so the oracle comparison above is
    about the bytes sent, not about a service-side rewrite of them).
    """
    rows = _pg_rows(
        f"SELECT source_ref, content_identity, content "
        f"FROM {_PG_SCHEMA}.source_documents ORDER BY source_ref"
    )
    assert len(rows) == len(_ENVELOPES), (
        f"expected one 资料 row per pushed envelope, got {len(rows)}: {rows!r}"
    )
    stored = {source_ref: identity for source_ref, identity, _ in rows}
    assert stored == dict(pushed["content_identities"]), (
        "the identity the adapter exposed is not what PG stores beside "
        "the same source_ref"
    )
    for source_ref, _, content in rows:
        assert content == _BODIES_BY_SOURCE_REF[source_ref], (
            f"PG holds a body other than the one pushed for {source_ref}; "
            "the identity comparison above would then be about a text this "
            "driver never hashed"
        )


def test_repushing_an_unchanged_body_reuses_the_stored_identity(
    live: Any, pushed: dict[str, Any]
) -> None:
    """An unchanged re-push keeps the identity, the ids, and the row count."""
    before_docs = _pg_rows(
        f"SELECT count(*) FROM {_PG_SCHEMA}.source_documents"
    )[0][0]
    before_chunks = _pg_rows(
        f"SELECT count(*) FROM {_PG_SCHEMA}.chunks"
    )[0][0]

    again = live.push.push(_wire())

    assert again["content_identities"] == pushed["content_identities"], (
        "an unchanged re-push answered a different identity for the same "
        "source_ref; the pair fin hands submit_index_job would not match "
        "the stored row"
    )
    assert again["block_ids"] == pushed["block_ids"], again
    assert again["written"] == 0 and again["duplicates"] == _CHUNK_COUNT, again
    assert _pg_rows(
        f"SELECT count(*) FROM {_PG_SCHEMA}.source_documents"
    )[0][0] == before_docs
    assert _pg_rows(
        f"SELECT count(*) FROM {_PG_SCHEMA}.chunks"
    )[0][0] == before_chunks
    stored = dict(
        _pg_rows(
            f"SELECT source_ref, content_identity "
            f"FROM {_PG_SCHEMA}.source_documents"
        )
    )
    assert stored == dict(pushed["content_identities"])


def test_submit_index_job_takes_the_pushed_pair_and_refuses_a_wrong_identity(
    live: Any, pushed: dict[str, Any]
) -> None:
    """``submit_index_job`` is the consumer that validates the handed-off pair.

    The accepted submit is the point of exposing the stored identity; the
    mismatching control is what makes the acceptance non-vacuous — a tool
    that ignored ``content_identity`` would accept both calls, and the
    wrong pair is the failure mode fin's no-recompute rule exists to stop.
    """
    source_ref = _ENVELOPES[0].source_ref
    identity = pushed["content_identities"][source_ref]

    accepted = _call_tool(
        live,
        "submit_index_job",
        {"source_ref": source_ref, "content_identity": identity},
    )
    assert accepted.get("error") != "index_job_identity_mismatch", accepted
    assert accepted["source_ref"] == source_ref, accepted
    assert accepted["content_identity"] == identity, accepted
    assert accepted["status"] == "in_progress", accepted
    job_id = accepted["job_id"]

    joined = _call_tool(
        live,
        "submit_index_job",
        {"source_ref": source_ref, "content_identity": identity},
    )
    assert joined["job_id"] == job_id and joined["reused"] is True, joined

    refused = _call_tool(
        live,
        "submit_index_job",
        {"source_ref": source_ref, "content_identity": "0" * 64},
    )
    assert refused.get("error") == "index_job_identity_mismatch", refused
