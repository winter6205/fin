"""The host graph's 档 hop, proven against a REAL booted iknow.

``tests/host/test_assigned_mode_reaches_port.py`` pins the hop's SHAPE: a
capturing adapter subclass records what the host PUT in the request and
answers ``{"hits": []}`` itself, so that suite passes even with no server
ever answering. What it cannot show is the other half of the contract —
that a real iknow process receives the 档 the host cascade assigned and
still answers with its own ranking. This case closes that gap.

What it drives is the compiled host graph (``compile_graph``) with its
normal scripted LLM, so the whole hop is on the path: the cascade names a
档 from the utterance → ``classify_node`` writes the run's audit →
``model`` → the single ``retrieve`` tool → the reader in
``fin.host.search_mode_decision`` → ``fin.retrieve.tool`` → the port →
``MCPRetrieveAdapter`` → the REAL stdio server over PG + ES + Neo4j.
Nothing here calls the adapter by hand; the only adapter in the run is the
one the graph was compiled with.

Assertions:

* the server ANSWERED — the run's evidence carries the chunk of the unit
  this run pushed (a rare token nothing else in the cluster contains), so
  an empty or stubbed reply cannot pass;
* the wire call the host itself emitted carries
  ``filters.search_mode=graph`` (SC2 at the transport, not at the seam);
* the run's audit records EXACTLY the 档 that went out, and the reader the
  hop calls returns that same wire token when read back off the finished
  run's state.

Storage isolation follows ``test_live_search_modes_pg_es.py``: per-run
unique document / version ids, per-run fin-owned ES indices and one per-run
fin-owned PG schema, all dropped at teardown — ``public`` is never touched.

Opt-in like every live file: ``tests/retrieve/conftest.py`` deselects the
``mcp`` mark unless the command line names it::

    uv run pytest -q -m mcp tests/retrieve/test_live_host_graph_hop.py
"""

from __future__ import annotations

import json
import random
import string
import uuid
from typing import Any, Iterator

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from fin.corpus.chunking import split_unit
from fin.corpus.parsers._common import build_unit
from fin.corpus.push import envelope_of_unit
from fin.host.graph import compile_graph
from fin.host.scripted_model import ScriptedChatModel
from fin.host.search_mode_decision import assigned_retrieve_mode
from fin.host.state import reset_input_state
from fin.retrieve.port import SEARCH_MODE_FILTER_KEY

from live_iknow_support import (
    IKNOW_ROOT,
    await_es_searchable,
    drop_es_indices,
    live_iknow,
)


pytestmark = pytest.mark.mcp

_RUN = uuid.uuid4().hex[:10]
_DOCUMENT_ID = f"HOSTHOP-{_RUN.upper()}"
_VERSION = uuid.uuid4().hex + uuid.uuid4().hex
_CHUNK_INDEX = f"fin_host_hop_chunks_{_RUN}"
_ENTITY_INDEX = f"fin_host_hop_entities_{_RUN}"
_PG_SCHEMA = f"fin_host_hop_pg_{_RUN}"

# Letters-only token (the live cluster's analyzers keep a pure-letters run
# as one term), so a hit on it can only have come from what THIS run pushed.
_TOKEN = "pterodactyl" + "".join(random.choices(string.ascii_lowercase, k=16))

# An ordinary grounded ask: the track cascade decides it, the 档 comes from
# the ``graph档`` marker the operator spelling carries.
_PLAIN_ASK = "what was AAPL net sales in 2023 Q3?"


def _fixture_text() -> str:
    return (
        f"{_TOKEN} appears once in this live host-hop unit. "
        "Apple reported net sales growth in the third fiscal quarter. "
        + "Revenue discussion continues across the slice. " * 20
    )


def _script() -> list[AIMessage]:
    """The scripted LLM: one retrieve call, then the answer.

    The retrieve query is the rare token, because that is the only text
    the pushed corpus contains — the host's own tool then has to ask the
    real server for it.
    """
    return [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "retrieve",
                    "args": {"query": _TOKEN, "limit": 8},
                    "id": "c-live-hop",
                    "type": "tool_call",
                }
            ],
        ),
        AIMessage(content="Net sales grew in the third fiscal quarter."),
    ]


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
            "live host-hop acceptance case has no fake to fall back to"
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
    """The one unit this run retrieves over: pushed through the product push
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


def test_the_host_run_sends_its_assigned_档_to_the_real_server_which_answers(
    live: Any, corpus: Any
) -> None:
    before = len(_retrieve_requests(live))
    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=ScriptedChatModel(script=_script()),
        adapter=live.retrieve,
    )
    out = graph.invoke(
        {**reset_input_state(f"graph档 {_PLAIN_ASK}"), "evidence": []},
        config={"configurable": {"thread_id": f"host-hop-{_RUN}"}},
    )

    # (b) the run's audit records exactly the 档 that was sent.
    audit = out["search_mode_decision"]
    assert audit["search_mode"] == "graph", audit
    assert audit["decision_tier"] == "rules", audit

    # The wire call was emitted BY THE GRAPH, not by this test.
    requests = _retrieve_requests(live)
    assert len(requests) > before, (
        "the host run never put a retrieve call on the real server, so the "
        "hop is unproven regardless of what the audit says"
    )
    host_arguments = [
        request["params"]["arguments"] for request in requests[before:]
    ]
    assert all(
        arguments["filters"][SEARCH_MODE_FILTER_KEY] == "graph"
        for arguments in host_arguments
    ), host_arguments

    # The reader is pinned against the token the REAL SERVER received, not
    # against the audit it reads from: ``assigned_retrieve_mode(out)`` vs
    # ``audit["search_mode"]`` compares the reader with its own body and no
    # wiring bug could fail it. Two independent artifacts — the state this
    # run finished with, and the request recorded on the transport — must
    # still name the same 档.
    sent_mode = requests[before]["params"]["arguments"]["filters"][
        SEARCH_MODE_FILTER_KEY
    ]
    assert assigned_retrieve_mode(out) == sent_mode, (
        "the reader the hop uses must return the token this run put on the "
        f"wire, not a value the test hand-coded: sent {sent_mode!r}, audit "
        f"{audit!r}"
    )

    # (a) the real server answered: the retrieved passage this run admitted
    # as evidence is the text this run pushed. ``{"hits": []}`` — or any
    # other server's corpus — cannot satisfy this.
    #
    # The run's own END status is deliberately not asserted: the scripted
    # answer is citation-free, so the citation gate is what decides it, and
    # that is a different contract than the 档 hop under test.
    passages = [item.unit_text for item in out["evidence"]]
    assert passages, out
    assert any(_TOKEN in passage for passage in passages), passages
    assert {
        item.locator.document_id for item in out["evidence"]
    } == {_DOCUMENT_ID}, [item.locator for item in out["evidence"]]
    # And the tool envelope the model saw is a real hit set, not one of the
    # port's typed exits: ``{"error": ...}`` (transport down) or
    # ``{"chunks": []}`` (a miss) would both mean the server never answered
    # this run's 档-bearing call.
    envelopes = [
        json.loads(message.content)
        for message in out["messages"]
        if getattr(message, "name", None) == "retrieve"
    ]
    assert envelopes, out["messages"]
    for envelope in envelopes:
        assert "error" not in envelope, envelope
        assert envelope["chunks"], envelope
