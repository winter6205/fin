"""fin the tracked change task 2: cross-repo search-mode driver on real PG + ES + Neo4j.

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
subprocess whose A tier lexically hits **real Elasticsearch** and whose
parent-window seam resolves through **real PG (pgvector)**.

Focus assertions — what this fin-side driver PROVES (spec
``search-mode-retrieve.md`` SC2/SC3/SC7 and the named-exit contract of
``fin.retrieve.port``):

* **A** — a real-ES lexical hit on a child resolves through the ledger
  back-pointer and cites the PARENT body, never a child-only slice.
* **B on the wire** — an explicit B carries ``filters.search_mode``
  while a missing mode does not send it.
* **B equivalence** — the missing mode and explicit B answer the same
  fused ranking (iknow's own default is B).
* **B fusion** — the fused answer carries NO degradation reason, so
  BOTH core arms — vector and the ES/BM25 lexical arm — participated.
* **B order** — the adapter faithfully preserves iknow's descending
  fused-score order.
* **C / D / invalid** — the named exits (``tier_not_open``,
  ``real_db_not_wired``, ``invalid_search_mode``) are unchanged when
  the full stack is live: the 档 check still runs before the transport.

What this driver does NOT prove: graph-priority REORDERING is iknow's
internal contract, validated against real Neo4j + GDS in the tracked change
task 3 (``iknow-memory/tests/integration/neo4j-ppr-arm.test.ts``).
Here entity extraction needs an LLM while the harness runs fake
embeddings against an empty ``ANTHROPIC_AUTH_TOKEN``, so the
entity-less corpus gives the graph arm no seeds and no reorder is
observable — shape equality and descending scores would hold for ANY
consistent ranking. The assertions above therefore pin the
fin-observable wire/fusion/order contract instead of impersonating
graph-priority coverage.

Isolation on the shared cluster: per-run unique document/version ids
and per-run fin-owned ES indices (deleted at teardown). The PG rows
stay namespaced by the unique document id — shared-authoritative
storage is not this driver's to drop.
"""

from __future__ import annotations

import json
import random
import string
import time
import urllib.request
import uuid
from typing import Any, Iterator

import pytest

from fin.corpus.chunking import split_unit
from fin.corpus.parsers._common import build_unit
from fin.corpus.push import envelope_of_unit
from fin.host.named_failures import REAL_DB_NOT_WIRED, TIER_NOT_OPEN
from fin.retrieve.port import InvalidSearchModeError, SEARCH_MODE_FILTER_KEY

from live_iknow_support import IKNOW_ES_URL, IKNOW_ROOT, live_iknow


pytestmark = pytest.mark.mcp

_RUN = uuid.uuid4().hex[:10]
_DOCUMENT_ID = f"T2-{_RUN.upper()}"
_VERSION = uuid.uuid4().hex + uuid.uuid4().hex
_CHUNK_INDEX = f"fin_task2_chunks_{_RUN}"
_ENTITY_INDEX = f"fin_task2_entities_{_RUN}"


def _rare_token(prefix: str) -> str:
    """Letters-only token: ik_max_word splits letter/digit boundaries
    into subtokens while the standard query analyzer keeps the whole
    run — only a pure-letters token is one term under both (verified
    against the live cluster's ``_analyze``)."""

    suffix = "".join(random.choices(string.ascii_lowercase, k=16))
    return f"{prefix}{suffix}"


_A_TOKEN = _rare_token("pterodactyl")
_B_TOKEN = _rare_token("quokka")

_ES_VISIBLE_TIMEOUT_SECONDS = 10.0


def _fixture_text() -> str:
    """One unit whose narrative 切分 yields ≥2 children, each carrying
    one tier's rare token so A and B hit different children of the
    same parent."""

    filler_a = "Revenue discussion continues across the first child slice. " * 20
    filler_b = "Cost of revenue discussion continues across the second child slice. " * 20
    return (
        f"{_A_TOKEN} opens the first child slice of this live parent "
        "window fixture for the A tier lexical check. " + filler_a
        + f"{_B_TOKEN} marks the second child slice reserved for the B "
        "tier fused ranking check. " + filler_b
    )


def _retrieve_requests(live: Any) -> list[dict]:
    return [
        request
        for request in live.session.requests
        if request.get("method") == "tools/call"
        and request.get("params", {}).get("name") == "retrieve"
    ]


def _await_es_searchable(chunk_ids: list[str]) -> None:
    """Bounded poll until every pushed child is visible in the ES chunk
    index. push's bulk write uses ``refresh: "wait_for"``, so the docs
    should already be searchable when push returned — this checks that
    deterministic hand-off against the live cluster rather than
    sleeping, and a timeout names the real miss for the operator
    instead of letting the tier assertion fail obscurely."""

    deadline = time.monotonic() + _ES_VISIBLE_TIMEOUT_SECONDS
    missing = set(chunk_ids)
    body = json.dumps(
        {"size": len(chunk_ids), "query": {"ids": {"values": list(chunk_ids)}}}
    ).encode("utf-8")
    while missing:
        request = urllib.request.Request(
            f"{IKNOW_ES_URL}/{_CHUNK_INDEX}/_search",
            data=body,
            headers={"content-type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            hits = json.load(response)["hits"]["hits"]
        missing -= {hit["_id"] for hit in hits}
        if not missing:
            return
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"chunks {sorted(missing)} never became searchable in ES "
                f"index {_CHUNK_INDEX} within "
                f"{_ES_VISIBLE_TIMEOUT_SECONDS}s"
            )
        time.sleep(0.25)


def _drop_fin_indices() -> None:
    """Teardown only ever touches the two per-run fin-owned indices."""

    for index in (_CHUNK_INDEX, _ENTITY_INDEX):
        request = urllib.request.Request(
            f"{IKNOW_ES_URL}/{index}", method="DELETE"
        )
        try:
            urllib.request.urlopen(request, timeout=5)
        except OSError:
            # best-effort cleanup; the run's unique names keep a leftover
            # index out of every later run's way.
            pass


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
        ) as session:
            yield session
    finally:
        monkeypatch.undo()
        _drop_fin_indices()


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
    # The A-tier claim is only meaningful over a real parent/child tree.
    assert len(envelope.chunks) >= 2, (
        "the fixture unit 切分 collapsed to a single child; the 档 A "
        f"child→parent assertion would be vacuous: {envelope!r}"
    )
    assert all(chunk.content != envelope.body for chunk in envelope.chunks)

    pushed = live.push.push([envelope.to_wire()])
    assert pushed["written"] == len(envelope.chunks), pushed
    assert pushed["duplicates"] == 0, pushed

    _await_es_searchable(list(pushed["block_ids"]))
    return envelope


def test_a_tier_real_es_lexical_hit_cites_the_parent_body(
    live: Any, parent_envelope: Any
) -> None:
    """Spec SC2: 档 A lexically hits an in-window child in REAL ES and
    answers the parent 抽图窗 body as the citable chunk text — a
    child-only slice wearing the answer is exactly what ADR-0060
    forbids."""

    answer = live.retrieve.retrieve(_A_TOKEN, limit=5, search_mode="A")

    assert "error" not in answer, answer
    chunks = answer["chunks"]
    assert chunks, (
        f"档 A returned no chunk for the rare token {_A_TOKEN!r} that a "
        "pushed child carries and the ES visibility poll confirmed: "
        f"{answer!r}"
    )
    for chunk in chunks:
        assert chunk.text == parent_envelope.body, (
            "档 A cited something other than the parent body (child→"
            "parent collapse is the A-tier contract); the fixture "
            "guarantees no child equals the body, so a child-only "
            "slice cannot pass this equality"
        )
        assert chunk.source_ref.document_id == _DOCUMENT_ID
        assert chunk.source_ref.version == _VERSION
        assert chunk.source_ref.page == 1


def test_b_tier_sends_search_mode_on_the_wire_and_matches_the_default(
    live: Any, parent_envelope: Any
) -> None:
    """Spec SC3 + the port's「missing → do not send」rule: the explicit
    B request carries ``filters.search_mode`` on the wire, the missing
    mode sends no such key, and BOTH answer the same fused
    vector∥BM25 ranking that contains the pushed child, with no
    degradation reason on either. The fused score order is preserved
    by the adapter. Graph-priority REORDERING is NOT claimed here —
    the entity-less corpus gives the graph arm no seeds, so equality
    and ordering hold for any consistent ranking (see the module
    docstring for where the graph contract is actually validated)."""

    before = len(_retrieve_requests(live))
    default_answer = live.retrieve.retrieve(_B_TOKEN, limit=8)
    explicit_answer = live.retrieve.retrieve(
        _B_TOKEN, limit=8, search_mode="B"
    )

    assert "error" not in default_answer, default_answer
    assert "error" not in explicit_answer, explicit_answer
    # B is contractually vector∥BM25 fused. A healthy B on this real
    # stack answers with NO degradation at all (observed: the
    # ``degraded_reasons`` field is absent, and even the graph arm of
    # this entity-less corpus emits no emptiness reason), so the
    # strongest honest check is an empty reason list. It covers both
    # core arms symmetrically: a ``vector:*`` reason (BM25-only
    # fallback) fails just as hard as a ``keyword_bm25:*`` one
    # (vector-only fallback) — neither degraded shape is the B
    # contract and none may pass as one.
    for answer in (default_answer, explicit_answer):
        reasons = list(answer.get("degraded_reasons") or [])
        assert reasons == [], (
            "B's fused ranking must include BOTH core arms (vector and "
            "ES/BM25 lexical); any degradation reason means one arm "
            f"answered alone: {answer!r}"
        )

    requests = _retrieve_requests(live)
    assert len(requests) == before + 2
    default_args = requests[before]["params"]["arguments"]
    explicit_args = requests[before + 1]["params"]["arguments"]
    assert SEARCH_MODE_FILTER_KEY not in default_args.get("filters", {}), (
        "a missing search_mode must not ride the wire (iknow's own "
        f"default B answers): {default_args!r}"
    )
    assert explicit_args["filters"][SEARCH_MODE_FILTER_KEY] == "B", (
        f"explicit B must send filters.search_mode: {explicit_args!r}"
    )

    def shape(answer: dict) -> list[tuple[str, str, int, float]]:
        return [
            (
                chunk.text,
                chunk.source_ref.document_id,
                chunk.source_ref.page,
                chunk.score,
            )
            for chunk in answer["chunks"]
        ]

    assert shape(default_answer) == shape(explicit_answer), (
        "missing mode and explicit B diverged — iknow's own default "
        "mode is B, so both requests must answer identically"
    )
    texts = [chunk.text for chunk in explicit_answer["chunks"]]
    assert any(_B_TOKEN in text for text in texts), (
        f"档 B's fused ranking omitted the child carrying "
        f"{_B_TOKEN!r}: {explicit_answer!r}"
    )
    scores = [chunk.score for chunk in explicit_answer["chunks"]]
    assert scores == sorted(scores, reverse=True), (
        "the adapter's chunks must stay in descending fused-score order"
    )


@pytest.mark.parametrize(
    ("mode", "code"),
    [
        pytest.param("C", TIER_NOT_OPEN, id="tier-c"),
        pytest.param("D", REAL_DB_NOT_WIRED, id="tier-d"),
    ],
)
def test_unopened_tier_keeps_its_named_exit_with_the_full_stack_live(
    live: Any, mode: str, code: str
) -> None:
    """C/D exits are routing-time, not transport-time: even with real
    ES/PG/Neo4j behind iknow (where a B-shaped answer WOULD succeed),
    the port still refuses 「档未开」/「真库未接」 by name and the
    request never reaches the wire — no silent default-B dressed as C
    or D."""

    before = len(_retrieve_requests(live))
    assert live.retrieve.retrieve(_B_TOKEN, search_mode=mode) == {
        "error": code
    }
    assert len(_retrieve_requests(live)) == before


def test_invalid_mode_raises_before_the_wire_with_the_full_stack_live(
    live: Any,
) -> None:
    """An out-of-set token is the caller's error: it raises
    ``InvalidSearchModeError`` (code ``invalid_search_mode``) before
    the round trip even though iknow itself is fully wired and could
    answer B-shaped hits — guessing around it would be the silent
    degradation the spec forbids."""

    before = len(_retrieve_requests(live))
    with pytest.raises(InvalidSearchModeError) as excinfo:
        live.retrieve.retrieve(_B_TOKEN, search_mode="c")
    assert excinfo.value.code == "invalid_search_mode"
    assert len(_retrieve_requests(live)) == before
