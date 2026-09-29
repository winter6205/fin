"""fin #276 task 2: cross-repo search-mode driver on real PG + ES + Neo4j.

The default ``uv run pytest -q`` never runs this file — it is marked
``mcp`` and ``tests/retrieve/conftest.py`` deselects that mark unless
``-m mcp`` names it, so the default suite stays hermetic. The opt-in
command::

    uv run pytest -q -m mcp tests/retrieve/test_live_search_modes_pg_es.py

Every earlier live suite here backs iknow with the in-process memory
store; this one is the task-2 driver the memory harness deliberately
could not cover (see the vector-only note in ``live_iknow_support``):
the product :class:`~fin.retrieve.mcp_adapter.MCPRetrieveAdapter` and
:class:`~fin.retrieve.mcp_push_port.MCPPushPort` over one real iknow
subprocess whose ``lexical`` 档 lexically hits **real Elasticsearch** and
whose parent-window seam resolves through **real PG (pgvector)**.

Focus assertions — what this fin-side driver PROVES (spec
``search-mode-retrieve.md`` SC2/SC3/SC7 and the named-exit contract of
``fin.retrieve.port``):

* **lexical** — a real-ES lexical hit on a child resolves through the
  ledger back-pointer and cites the PARENT body, never a child-only slice.
* **graph on the wire** — an explicit ``graph`` carries
  ``filters.search_mode=graph`` while a missing mode does not send it.
* **graph equivalence** — the missing mode and explicit ``graph`` answer
  the same fused ranking (iknow's own default is ``graph``).
* **graph fusion** — the fused answer carries NO degradation reason, so
  BOTH core arms — vector and the ES/BM25 lexical arm — participated.
* **graph order** — the adapter faithfully preserves iknow's descending
  fused-score order.
* **global is a token the SERVER knows** — fin's port has not opened 综述
  yet, so the ordinary call refuses 「档未开」 before the wire; the raw
  round trip still proves iknow accepts ``global`` and answers with its
  own not-ready fact (``community_summary_index_failure``), never with a
  refused enum value.
* **sql / leftover letters / invalid** — the named exits
  (``real_db_not_wired``, ``invalid_search_mode`` for every retired letter
  ``A``/``B``/``C``/``D`` and for ``local``) are unchanged when the full
  stack is live: the 档 check still runs before the transport.

What this driver does NOT prove: graph-priority REORDERING is iknow's
internal contract, validated against real Neo4j + GDS in iknow #242
task 3 (``iknow-memory/tests/integration/neo4j-ppr-arm.test.ts``).
Here entity extraction needs an LLM while the harness runs fake
embeddings against an empty ``ANTHROPIC_AUTH_TOKEN``, so the
entity-less corpus gives the graph arm no seeds and no reorder is
observable — shape equality and descending scores would hold for ANY
consistent ranking. The assertions above therefore pin the
fin-observable wire/fusion/order contract instead of impersonating
graph-priority coverage.

Isolation on the shared cluster: per-run unique document/version ids, per-run
fin-owned ES indices and one per-run fin-owned PG schema (all deleted at
teardown). The schema is what makes the boot legal at all: the embedding
generation ledger iknow guards against its configured embedder is per-schema,
and ``public`` on this cluster belongs to another suite's 1536-dim generation,
which no fake-embedding driver may realign.
"""

from __future__ import annotations

import asyncio
import json
import random
import string
import uuid
from typing import Any, Iterator

import pytest

from fin.corpus.chunking import split_unit
from fin.corpus.parsers._common import build_unit
from fin.corpus.push import envelope_of_unit
from fin.host.named_failures import (
    COMMUNITY_SUMMARY_INDEX_FAILURE,
    REAL_DB_NOT_WIRED,
)
from fin.retrieve.port import (
    InvalidSearchModeError,
    RETRIEVE_OUTCOME_COMMUNITY_SUMMARY_UNREADY,
    SEARCH_MODE_FILTER_KEY,
)

from live_iknow_support import (
    IKNOW_ROOT,
    await_es_searchable,
    drop_es_indices,
    live_iknow,
)


pytestmark = pytest.mark.mcp

_RUN = uuid.uuid4().hex[:10]
_DOCUMENT_ID = f"T2-{_RUN.upper()}"
_VERSION = uuid.uuid4().hex + uuid.uuid4().hex
_CHUNK_INDEX = f"fin_task2_chunks_{_RUN}"
_ENTITY_INDEX = f"fin_task2_entities_{_RUN}"
_PG_SCHEMA = f"fin_task2_pg_{_RUN}"


def _rare_token(prefix: str) -> str:
    """Letters-only token: ik_max_word splits letter/digit boundaries
    into subtokens while the standard query analyzer keeps the whole
    run — only a pure-letters token is one term under both (verified
    against the live cluster's ``_analyze``)."""

    suffix = "".join(random.choices(string.ascii_lowercase, k=16))
    return f"{prefix}{suffix}"


_LEXICAL_TOKEN = _rare_token("pterodactyl")
_GRAPH_TOKEN = _rare_token("quokka")


def _fixture_text() -> str:
    """One unit whose narrative 切分 yields ≥2 children, each carrying
    one 档's rare token so ``lexical`` and graph hit different children of
    the same parent."""

    filler_a = "Revenue discussion continues across the first child slice. " * 20
    filler_b = "Cost of revenue discussion continues across the second child slice. " * 20
    return (
        f"{_LEXICAL_TOKEN} opens the first child slice of this live parent "
        "window fixture for the lexical 档 check. " + filler_a
        + f"{_GRAPH_TOKEN} marks the second child slice reserved for the graph "
        "档 fused ranking check. " + filler_b
    )


def _retrieve_requests(live: Any) -> list[dict]:
    return [
        request
        for request in live.session.requests
        if request.get("method") == "tools/call"
        and request.get("params", {}).get("name") == "retrieve"
    ]


@pytest.fixture(scope="module")
def live() -> Iterator[Any]:
    """One iknow subprocess on the real PG + ES + Neo4j stack for the
    whole module (schema bootstrap and graph wiring make a per-test
    respawn needlessly slow). The module-scoped fixture cannot take the
    function-scoped ``monkeypatch``, so a private ``MonkeyPatch`` is
    created and undone around the harness."""

    entry = IKNOW_ROOT / "dist" / "index.js"
    if not entry.is_file():
        pytest.skip(
            f"{entry} is missing; run `npm run build` in {IKNOW_ROOT} "
            "to exercise the live PG+ES+Neo4j search-mode stack"
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
def parent_envelope(live: Any) -> Any:
    """The one multi-child unit this module retrieves against, pushed
    through the product push port onto the real stack."""

    unit = build_unit(
        fmt="txt",
        source_sha256=_VERSION,
        document_id=_DOCUMENT_ID,
        version=_VERSION,
        unit=1,
        text=_fixture_text() + "\n",
    )
    split = split_unit(unit, doc_type="narrative")
    envelope = envelope_of_unit(
        split,
        valid_from="2020-01-01T00:00:00.000Z",
        valid_until="9999-12-31T23:59:59.999Z",
    )
    # The lexical-tier claim is only meaningful over a real parent/child tree.
    assert len(envelope.chunks) >= 2, (
        "the fixture unit 切分 collapsed to a single child; the lexical "
        f"child→parent assertion would be vacuous: {envelope!r}"
    )
    assert all(chunk.content != envelope.body for chunk in envelope.chunks)

    pushed = live.push.push([envelope.to_wire()])
    assert pushed["written"] == len(envelope.chunks), pushed
    assert pushed["duplicates"] == 0, pushed

    await_es_searchable(list(pushed["block_ids"]), chunk_index=_CHUNK_INDEX)
    return envelope


def test_lexical_tier_real_es_hit_cites_the_parent_body(
    live: Any, parent_envelope: Any
) -> None:
    """Spec SC2: the ``lexical`` 档 hits an in-window child in REAL ES and
    answers the parent 抽图窗 body as the citable chunk text — a
    child-only slice wearing the answer is exactly what ADR-0060
    forbids."""

    answer = live.retrieve.retrieve(
        _LEXICAL_TOKEN, limit=5, search_mode="lexical"
    )

    assert "error" not in answer, answer
    chunks = answer["chunks"]
    assert chunks, (
        f"lexical 档 returned no chunk for the rare token {_LEXICAL_TOKEN!r} "
        "that a pushed child carries and the ES visibility poll confirmed: "
        f"{answer!r}"
    )
    for chunk in chunks:
        assert chunk.text == parent_envelope.body, (
            "the lexical 档 cited something other than the parent body "
            "(child→parent collapse is the lexical-tier contract); the "
            "fixture guarantees no child equals the body, so a child-only "
            "slice cannot pass this equality"
        )
        assert chunk.source_ref.document_id == _DOCUMENT_ID
        assert chunk.source_ref.version == _VERSION
        assert chunk.source_ref.unit == 1


def test_graph_tier_sends_search_mode_on_the_wire_and_matches_the_default(
    live: Any, parent_envelope: Any
) -> None:
    """Spec SC2 + the port's「missing → do not send」rule: the explicit
    ``graph`` request carries ``filters.search_mode=graph`` on the wire,
    the missing mode sends no such key, and BOTH answer the same fused
    vector∥BM25 ranking that contains the pushed child, with no
    degradation reason on either. The fused score order is preserved
    by the adapter. Graph-priority REORDERING is NOT claimed here —
    the entity-less corpus gives the graph arm no seeds, so equality
    and ordering hold for any consistent ranking (see the module
    docstring for where the graph contract is actually validated)."""

    before = len(_retrieve_requests(live))
    default_answer = live.retrieve.retrieve(_GRAPH_TOKEN, limit=8)
    explicit_answer = live.retrieve.retrieve(
        _GRAPH_TOKEN, limit=8, search_mode="graph"
    )

    assert "error" not in default_answer, default_answer
    assert "error" not in explicit_answer, explicit_answer
    # graph is contractually vector∥BM25 fused. A healthy graph arm on
    # this real stack answers with NO degradation at all (observed: the
    # ``degraded_reasons`` field is absent, and even the graph arm of
    # this entity-less corpus emits no emptiness reason), so the
    # strongest honest check is an empty reason list. It covers both
    # core arms symmetrically: a ``vector:*`` reason (BM25-only
    # fallback) fails just as hard as a ``keyword_bm25:*`` one
    # (vector-only fallback) — neither degraded shape is the graph
    # contract and none may pass as one.
    for answer in (default_answer, explicit_answer):
        reasons = list(answer.get("degraded_reasons") or [])
        assert reasons == [], (
            "the graph 档's fused ranking must include BOTH core arms "
            "(vector and ES/BM25 lexical); any degradation reason means "
            f"one arm answered alone: {answer!r}"
        )

    requests = _retrieve_requests(live)
    assert len(requests) == before + 2
    default_args = requests[before]["params"]["arguments"]
    explicit_args = requests[before + 1]["params"]["arguments"]
    assert SEARCH_MODE_FILTER_KEY not in default_args.get("filters", {}), (
        "a missing search_mode must not ride the wire (iknow's own "
        f"default 档 answers): {default_args!r}"
    )
    assert explicit_args["filters"][SEARCH_MODE_FILTER_KEY] == "graph", (
        "explicit graph must send filters.search_mode=graph: "
        f"{explicit_args!r}"
    )

    def shape(answer: dict) -> list[tuple[str, str, int, float]]:
        return [
            (
                chunk.text,
                chunk.source_ref.document_id,
                chunk.source_ref.unit,
                chunk.score,
            )
            for chunk in answer["chunks"]
        ]

    assert shape(default_answer) == shape(explicit_answer), (
        "missing mode and explicit graph diverged — iknow's own default "
        "mode is graph, so both requests must answer identically"
    )
    texts = [chunk.text for chunk in explicit_answer["chunks"]]
    assert any(_GRAPH_TOKEN in text for text in texts), (
        f"档 graph's fused ranking omitted the child carrying "
        f"{_GRAPH_TOKEN!r}: {explicit_answer!r}"
    )
    scores = [chunk.score for chunk in explicit_answer["chunks"]]
    assert scores == sorted(scores, reverse=True), (
        "the adapter's chunks must stay in descending fused-score order"
    )


def test_a_global_send_reaches_the_server_and_surfaces_the_runtime_index_failure(
    live: Any, parent_envelope: Any
) -> None:
    """Both halves of the 综述 runtime fact, on one real round trip.

    ``global`` is now an OPEN 档: fin forwards it to the wire, and a
    not-ready community summary index comes back as a RUNTIME fact, not a
    routing-time ``tier_not_open``. The raw transport pins the server half
    first — the server takes ``global`` as a legal ``filters.search_mode``
    and answers with its own ``community_summary_index_failure`` (a server
    ``invalid_search_mode`` here would mean fin renamed a token the sibling
    never carried). The adapter half then proves fin translates that
    refusal into the recorded outcome flag rather than collapsing it to
    ``retrieve_unavailable``.
    """

    before = len(_retrieve_requests(live))
    response = asyncio.run(
        live.retrieve._call_mcp(
            {
                "jsonrpc": "2.0",
                # The same request id the adapter's own payload uses; the
                # session answers one request at a time, so the value is
                # transport-local.
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "retrieve",
                    "arguments": {
                        "query": _GRAPH_TOKEN,
                        "limit": 8,
                        "filters": {SEARCH_MODE_FILTER_KEY: "global"},
                    },
                },
            }
        )
    )
    requests = _retrieve_requests(live)
    assert len(requests) == before + 1, (
        "the raw round trip did not reach the server, so it proves nothing "
        "about what the server accepts"
    )
    assert (
        requests[before]["params"]["arguments"]["filters"][
            SEARCH_MODE_FILTER_KEY
        ]
        == "global"
    )

    observed = json.dumps(response, default=str)
    assert "invalid_search_mode" not in observed, (
        "the server refused the ``global`` token itself, so fin's closed "
        f"set does not match what iknow accepts: {observed}"
    )
    # Pin the wire shape the way the adapter actually reads it: decode
    # the first text block and match its structured ``error`` FIELD.
    # Substring containment over the serialized response would pass on a
    # prose mention too, so it could not detect producer drift.
    result = response.get("result")
    assert isinstance(result, dict) and result.get("isError") is True, (
        f"expected an isError result envelope: {observed}"
    )
    first_text = next(
        (
            block["text"]
            for block in result.get("content", [])
            if isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        ),
        None,
    )
    assert first_text is not None, (
        f"the refusal carried no text block: {observed}"
    )
    body = json.loads(first_text)
    assert isinstance(body, dict) and body.get("error") == (
        COMMUNITY_SUMMARY_INDEX_FAILURE
    ), (
        "the 综述 arm is not ready on this stack, and that is the fact the "
        f"server should name in the error field: {observed}"
    )

    # fin's own seam: 综述 is open, so the adapter forwards global to the
    # wire (a second request) and turns the server's typed refusal into the
    # recorded outcome flag — the runtime emit — NOT ``tier_not_open`` and
    # NOT ``retrieve_unavailable``.
    assert live.retrieve.retrieve(_GRAPH_TOKEN, search_mode="global") == {
        "chunks": [],
        RETRIEVE_OUTCOME_COMMUNITY_SUMMARY_UNREADY: True,
    }
    assert len(_retrieve_requests(live)) == before + 2, (
        "an open 档 must spend its wire call; the server's not-ready fact "
        "reaches fin through the transport, not a pre-wire refusal"
    )


@pytest.mark.parametrize(
    ("mode", "code"),
    [
        pytest.param("sql", REAL_DB_NOT_WIRED, id="tier-sql"),
    ],
)
def test_unopened_tier_keeps_its_named_exit_with_the_full_stack_live(
    live: Any, mode: str, code: str
) -> None:
    """An unopened 档 exit is routing-time, not transport-time: even with
    real ES/PG/Neo4j behind iknow (where a graph-shaped answer WOULD
    succeed), the port still refuses 「真库未接」 by name and the request
    never reaches the wire — no silent default answer dressed as 真数据库.
    (``global`` no longer appears here: it is an open 档 now, so it spends
    its wire call and surfaces the runtime index fact instead.)"""

    before = len(_retrieve_requests(live))
    assert live.retrieve.retrieve(_GRAPH_TOKEN, search_mode=mode) == {
        "error": code
    }
    assert len(_retrieve_requests(live)) == before


@pytest.mark.parametrize("leftover", ["A", "B", "C", "D", "b", "c", "local"])
def test_invalid_mode_raises_before_the_wire_with_the_full_stack_live(
    live: Any,
    leftover: str,
) -> None:
    """An out-of-set token is the caller's error: it raises
    ``InvalidSearchModeError`` (code ``invalid_search_mode``) before
    the round trip even though iknow itself is fully wired and could
    answer graph-shaped hits — guessing around it would be the silent
    degradation the spec forbids. The retired letters are in this list on
    purpose: they are the tokens a stale client is most likely to still
    send, and the port is exactly where they stop."""

    before = len(_retrieve_requests(live))
    with pytest.raises(InvalidSearchModeError) as excinfo:
        live.retrieve.retrieve(_GRAPH_TOKEN, search_mode=leftover)
    assert excinfo.value.code == "invalid_search_mode"
    assert len(_retrieve_requests(live)) == before
