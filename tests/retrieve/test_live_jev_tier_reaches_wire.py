"""One run in which the REAL judge chose the 档 and the REAL server answered it.

Two pins already exist for the 档 hop, and each proves only half of it.
``tests/host/test_assigned_mode_reaches_port.py`` proves the SHAPE against a
capturing fake that answers itself; ``tests/retrieve/test_live_host_graph_hop.
py`` proves a 档 reached the real iknow wire, but there the 档 came from the
deterministic rules tier (an utterance spelling ``graph档``). What neither
shows is a single execution whose 档 came out of the live TypeSafe Jev judge
and still reached the live server. That is the link this case has to be able
to name.

The utterance deliberately does NOT name a 档, so the rules tier abstains at
that tier; the track itself is still decided by the ``filing_ask`` rule in
``fin.host.track_decision``, which keeps exactly one non-deterministic step in
the run — the 档 cascade's judge call. Everything else is the real stack:
``compile_graph`` runs its normal scripted model, the cascade's judge is the
client the ``jev_live`` gate builds, and the only retrieve adapter in the run
is the one the graph was compiled with, talking to a real iknow over PG + ES +
Neo4j.

The judge is not deterministic, so the assertion is on the OPENED set rather
than on one preferred 档: every tier in ``_OPENED_FILING_TIERS`` (derived from
the availability SSOT, not spelled) satisfies the case, and
pinning which one the judge likes would be the flaky landmine the rest of this
file avoids. What must hold on every draw: the tier really was the judge's, the
audited token is byte-identical to the one the server received, and the server
answered with the chunk THIS run pushed.

Storage isolation follows ``test_live_host_graph_hop.py``: per-run document /
version ids, per-run fin-owned ES indices and one per-run fin-owned PG schema,
all dropped at teardown; ``public`` is never touched.

Named command (run deliberately, twice, to see the judge bind on its own):

    uv run pytest -q -m "mcp and jev_live" \
        tests/retrieve/test_live_jev_tier_reaches_wire.py
"""

from __future__ import annotations

import json
import random
import string
import uuid
import warnings
from typing import Any, Iterator

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

import fin.host.search_mode_decision as smd
from fin.corpus.chunking import split_unit
from fin.corpus.parsers._common import build_unit
from fin.corpus.push import envelope_of_unit
from fin.host.graph import compile_graph
from fin.host.jev_choice import JEV_CHOICE_EXIT_TRANSPORT, JevChoiceClient
from fin.host.named_failures import LASTING_RETRIEVE_TIERS, TIER_UNAVAILABLE_CODE
from fin.host.scripted_model import ScriptedChatModel
from fin.host.state import reset_input_state
from fin.retrieve.port import SEARCH_MODE_FILTER_KEY

from live_iknow_support import (
    IKNOW_ROOT,
    await_es_searchable,
    drop_es_indices,
    live_iknow,
)


pytestmark = [pytest.mark.mcp, pytest.mark.jev_live]

_RUN = uuid.uuid4().hex[:10]
_DOCUMENT_ID = f"JEVTIER-{_RUN.upper()}"
_VERSION = uuid.uuid4().hex + uuid.uuid4().hex
_CHUNK_INDEX = f"fin_jev_tier_chunks_{_RUN}"
_ENTITY_INDEX = f"fin_jev_tier_entities_{_RUN}"
_PG_SCHEMA = f"fin_jev_tier_pg_{_RUN}"

# Letters-only token (the live cluster's analyzers keep a pure-letters run as
# one term), so a hit on it can only have come from what THIS run pushed.
_TOKEN = "pterodactyl" + "".join(random.choices(string.ascii_lowercase, k=16))

# A grounded filing ask with no 档 spelling: the judge owns this tier.
_GROUNDED_ASK = "what was AAPL net sales in 2023 Q3?"

# The 档 a filing-figure ask may legally land on — the OPENED ones, derived
# from the availability SSOT instead of spelled out: ``global`` and ``sql`` are
# the lasting tiers with a routing-time named failure, everything else is
# open. The derivation is also the early warning — when ``global`` opens this
# set widens here and in ``tests/host/test_live_jev_search_mode.py`` at once,
# and the pairing there explains why that widening has to be re-examined
# rather than waved through: the per-档 glosses exist to keep a filing-only
# figure off a corpus-wide synthesis, and a newly opened 档 does not change
# that.
_OPENED_FILING_TIERS = frozenset(
    tier for tier in LASTING_RETRIEVE_TIERS if tier not in TIER_UNAVAILABLE_CODE
)


def _fixture_text() -> str:
    return (
        f"{_TOKEN} appears once in this live judged-tier unit. "
        "Apple reported net sales growth in the third fiscal quarter. "
        + "Revenue discussion continues across the slice. " * 20
    )


def _script() -> list[AIMessage]:
    """The scripted LLM: one retrieve call, then the answer.

    The retrieve query is the rare token, because that is the only text the
    pushed corpus contains — so the host's own tool has to ask the real server
    for it under whatever 档 the judge assigned.
    """
    return [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "retrieve",
                    "args": {"query": _TOKEN, "limit": 8},
                    "id": "c-live-jev-tier",
                    "type": "tool_call",
                }
            ],
        ),
        AIMessage(content="Net sales grew in the third fiscal quarter."),
    ]


def _record(label: str, confidence: float, *, tier: str) -> None:
    """Surface the observed judge answer in the run's warnings summary.

    A pass with no trace of what the real judge said is indistinguishable from
    a stubbed run, so the evidence travels with the report. Label and
    confidence only — nothing credential-shaped is ever recorded.
    """
    warnings.warn(
        f"jev_live evidence via=live_graph tier={tier} label={label} "
        f"confidence={confidence:.3f}",
        stacklevel=2,
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
    """One real iknow subprocess on the PG + ES + Neo4j stack, booted with a
    per-run fin-owned schema (see ``live_iknow_support``)."""

    entry = IKNOW_ROOT / "dist" / "index.js"
    if not entry.is_file():
        raise AssertionError(
            f"{entry} is missing; run `npm run build` in {IKNOW_ROOT} — the "
            "live judged-tier acceptance case has no fake to fall back to"
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
def corpus(live: Any) -> Any:
    """The one unit this run retrieves over, pushed through the product push
    port onto the real stack, so a hit on ``_TOKEN`` can only be the server's
    own answer."""

    unit = build_unit(
        fmt="txt",
        source_sha256=_VERSION,
        document_id=_DOCUMENT_ID,
        version=_VERSION,
        unit=1,
        text=_fixture_text() + "\n",
    )
    envelope = envelope_of_unit(
        split_unit(unit, doc_type="narrative"),
        valid_from="2020-01-01T00:00:00.000Z",
        valid_until="9999-12-31T23:59:59.999Z",
    )
    pushed = live.push.push([envelope.to_wire()])
    assert pushed["written"] == len(envelope.chunks), pushed
    assert pushed["duplicates"] == 0, pushed
    await_es_searchable(list(pushed["block_ids"]), chunk_index=_CHUNK_INDEX)
    return envelope


def test_the_judged_档_reaches_the_real_server_which_answers(
    live: Any, corpus: Any, jev_live_client: JevChoiceClient
) -> None:
    before = len(_retrieve_requests(live))
    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=ScriptedChatModel(script=_script()),
        adapter=live.retrieve,
        jev_client=jev_live_client,
    )
    out = graph.invoke(
        {**reset_input_state(_GROUNDED_ASK), "evidence": []},
        config={"configurable": {"thread_id": f"jev-tier-{_RUN}"}},
    )

    audit = out["search_mode_decision"]
    signals = audit["signals"]
    label = signals.get("mode_jev_label")
    confidence = signals.get("mode_jev_confidence")

    if signals.get("mode_jev_exit") == JEV_CHOICE_EXIT_TRANSPORT:
        pytest.fail(
            "jev_live_unreachable: the 档 cascade's judge request did not come "
            f"back, so no 档 could have been judged; mode_jev_exit="
            f"{signals.get('mode_jev_exit')!r}, signals={signals}",
            pytrace=False,
        )
    # 「bound」 is the tier, the OPENED set AND the confidence floor. Drop the
    # floor and a judge answer at 0.0 would satisfy this leg, so the file
    # would certify a binding it never proved.
    decided_an_opened_tier = (
        audit["decision_tier"] == "jev"
        and audit["search_mode"] in _OPENED_FILING_TIERS
        and isinstance(confidence, (int, float))
        and confidence >= smd._MIN_JEV_CONFIDENCE
    )
    if not decided_an_opened_tier:
        pytest.fail(
            "jev_live_tier_not_bound: the 档 tier must have been the live "
            f"judge, must have bound one of {sorted(_OPENED_FILING_TIERS)} at "
            f"or above the floor {smd._MIN_JEV_CONFIDENCE} for "
            f"{_GROUNDED_ASK!r}; observed decision_tier="
            f"{audit['decision_tier']!r} search_mode={audit['search_mode']!r} "
            f"label={label!r} confidence={confidence!r} exit="
            f"{signals.get('mode_jev_exit')!r} failure_code="
            f"{audit.get('failure_code')!r}",
            pytrace=False,
        )
    # The evidence always travels with the report once the leg above holds:
    # an unguarded record is what let a run with no judge signal pass silently.
    _record(str(label), float(confidence), tier=str(audit["decision_tier"]))
    # The judged token is what the cascade carries, not a substitute it
    # invented: the audit's answer and the judge's own label are separate
    # artifacts produced on different sides of the seam.
    assert audit["search_mode"] == label, (audit, label)

    # The wire call was emitted BY THE GRAPH, not by this test — and a run
    # that never put anything on the server proves nothing about the hop.
    requests = _retrieve_requests(live)
    if len(requests) <= before:
        pytest.fail(
            "jev_live_no_retrieve_call: the host run sent zero retrieve "
            "requests to the real server, so the judged 档 never travelled and "
            f"the hop is unproven however the audit reads; audit={audit}",
            pytrace=False,
        )
    host_arguments = [
        request["params"]["arguments"] for request in requests[before:]
    ]
    sent_modes = [
        arguments.get("filters", {}).get(SEARCH_MODE_FILTER_KEY)
        for arguments in host_arguments
    ]
    assert all(mode == audit["search_mode"] for mode in sent_modes), (
        f"the 档 this run audited ({audit['search_mode']!r}) is not the token "
        f"the real server received ({sent_modes!r}): the hop from audit to "
        "transport leaked"
    )

    # No separate reader check here, on purpose: the transport's
    # ``filters[SEARCH_MODE_FILTER_KEY]`` is BUILT from
    # ``assigned_retrieve_mode(state)`` (``fin.host.graph``'s tool node), so
    # comparing that reader with the recorded request is one value read twice
    # — a broken reader or a leaked hop moves both sides together and nothing
    # could fail it. The assert above is the audit→wire hop; the reader's own
    # contract is pinned hermetically in
    # ``tests/host/test_assigned_mode_reaches_port.py``.

    # The real server ANSWERED this run's 档-bearing call with the chunk this
    # run pushed: ``{"hits": []}``, a transport error, or another corpus
    # cannot satisfy any of the three asserts below.
    passages = [item.page_text for item in out["evidence"]]
    assert passages, out
    assert any(_TOKEN in passage for passage in passages), passages
    assert {
        item.locator.document_id for item in out["evidence"]
    } == {_DOCUMENT_ID}, [item.locator for item in out["evidence"]]
    envelopes = [
        json.loads(message.content)
        for message in out["messages"]
        if getattr(message, "name", None) == "retrieve"
    ]
    assert envelopes, out["messages"]
    for envelope in envelopes:
        assert "error" not in envelope, envelope
        assert envelope["chunks"], envelope
