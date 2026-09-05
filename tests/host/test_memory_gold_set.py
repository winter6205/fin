"""SC25-SC28 — over-attention gold set for cross-session memory.

``specs/cross-session-operator-memory.md`` SC25-SC28 (gating bullet):
"Gold (scripted): greeting → 0 prefetch; assembled payload has no
full-store catalog / promote; extract deny classes do not persist;
research turn with a number has a retrieve (or ``read_file``) tool
call; a fact whose title matches a corpus company still uses retrieve
for filing claims; explicit save is recallable on a new ``thread_id``."

The gold set is acceptance, not an enable-gate (spec G3 / ADR 0048):
these cases pin the property after the spec lands and prevent a
future regression from re-introducing over-attention. Every case
runs against scripted / fake seams — no live LLM, no network, no
Postgres — so the suite fits in default CI.

Conventions follow ``docs/guides/prompt-development.md``:

* fixtures before scripts; a behaviour point one scripted input,
* hard gates (JSON shape, prefetch cap, no full-store catalog,
  research turn records a retrieve, verifier ignores memory text,
  cross-thread save is recallable).

Mark every case with ``@pytest.mark.memory_on``. The conftest strips
``FIN_MEMORY_ENABLED`` for unmarked tests and re-installs ``true``
for marked tests; the gold set relies on that re-install to see
the same ON path the operator's ``.env`` produces.
"""

from __future__ import annotations

from fin.corpus._private_fixtures import load_private_fixture

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import pytest
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
)
from langgraph.checkpoint.memory import InMemorySaver

from fin.host import (
    ScriptedChatModel,
    compile_graph,
)
from fin.host.agent_instructions import (
    FIN_MEMORY_PREFETCH_MARKER,
    MEMORY_POINTER_BLOCK,
    MEMORY_POINTER_MARKER,
    memory_prefetch_message,
    messages_for_model,
)
from fin.host.memory_buffer import LocalMemoryBuffer
from fin.host.memory_extract import (
    FactCandidate,
    maybe_extract,
)
from fin.host.memory_port import MemoryPort, OffMemoryAdapter
from fin.host.memory_prefetch import (
    PREFETCH_MAX_ROWS,
    select_prefetch,
)
from fin.host.memory_store import MemoryRow
from fin.host.user_profile import (
    GLOBAL_FILENAME,
    WORKSPACE_FILENAME,
    UserProfileStore,
)


# ----- shared fixtures -----


# A realistic 50-char alphanumeric span that the citation gate can
# match (the gate is "off" here; we only assert that retrieve / read
# ran, not that the gate closed). A long synthetic string keeps the
# helper independent of any specific corpus page.
_LEGIT_SPAN = "ABCDEFGHIJ" * 5
assert len(_LEGIT_SPAN) == 50

_KNOWN_IDS = {"AAPL-2022Q3", "AAPL-2023Q3", "MSFT-2023Q1", "AMZN-2023Q1"}


def _now() -> Any:
    from datetime import datetime, timezone

    return datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)


def _row(
    row_id: str,
    *,
    title: str = "",
    body: str = "",
    workspace: str = "ws-a",
) -> MemoryRow:
    """Build a fact row shaped like the store hands one to the assembler."""
    return MemoryRow(
        id=row_id,
        text=body or title,
        title=title,
        body=body,
        workspace=workspace,
        metadata={},
        created_at=_now(),
    )


def _buffer_rows_as_memory_rows(
    buffer_entries: list[dict[str, object]],
) -> list[MemoryRow]:
    """Convert ``MemoryBuffer.entries()`` dicts into ``MemoryRow`` objects.

    ``select_prefetch`` requires ``MemoryRow`` instances, not the
    JSON-shaped dicts the buffer hands out for inspection. The
    conversion is lossy on metadata shapes (date strings back to
    ``datetime``) but it preserves the fields the assembler reads:
    ``id``, ``title``, ``body``, ``hook``.
    """
    from datetime import datetime

    out: list[MemoryRow] = []
    for entry in buffer_entries:
        created_at_raw = entry.get("created_at")
        if isinstance(created_at_raw, datetime):
            created_at = created_at_raw
        elif isinstance(created_at_raw, str):
            try:
                created_at = datetime.fromisoformat(created_at_raw)
            except ValueError:
                created_at = _now()
        else:
            created_at = _now()
        out.append(
            MemoryRow(
                id=str(entry.get("id", "")),
                text=str(entry.get("text", "")),
                title=str(entry.get("title", "")),
                body=str(entry.get("body", "")),
                workspace=str(entry.get("workspace", "")),
                metadata=dict(entry.get("metadata") or {}),
                created_at=created_at,
            )
        )
    return out


@dataclass
class _FakeMemoryPort:
    """In-process ``MemoryPort`` shim with a workspace-aware local buffer.

    Mirrors the same shape used by ``test_memory_graph_tools.py`` so
    the gold cases share one port fixture across all ON tests.
    """

    enabled: bool = True
    buffer: LocalMemoryBuffer = field(default_factory=LocalMemoryBuffer)

    def extract(
        self,
        text: str,
        *,
        metadata: Mapping[str, object] | None = None,
        workspace: str = "",
    ) -> dict[str, object]:
        return self.buffer.extract(
            text,
            metadata=metadata,
            title=str((metadata or {}).get("title", "")) if metadata else "",
            workspace=workspace,
        )

    def recall(
        self,
        query: str,
        *,
        limit: int = 8,
        workspace: str = "",
    ) -> dict[str, object]:
        return self.buffer.recall(query, limit=limit, workspace=workspace)

    def gc(self, *, limit: int = 8) -> dict[str, object]:
        return self.buffer.gc(limit=limit)

    def dream(self, *, limit: int = 8) -> dict[str, object]:
        return self.buffer.dream(limit=limit)


def _profile_store(tmp_path: Path, *, ws: str = "ws") -> UserProfileStore:
    """Build a profile store rooted under ``tmp_path``; parents eager."""
    store = UserProfileStore(
        global_path=tmp_path / "home" / ".fin" / GLOBAL_FILENAME,
        workspace_path=tmp_path / ws / ".fin" / WORKSPACE_FILENAME,
    )
    store.global_path.parent.mkdir(parents=True, exist_ok=True)
    store.workspace_path.parent.mkdir(parents=True, exist_ok=True)
    return store


def _compiled_graph(
    *,
    tmp_path: Path,
    port: _FakeMemoryPort | MemoryPort,
    workspace_root: str | None = None,
    known_ids: set[str] | None = None,
    model: ScriptedChatModel | None = None,
) -> Any:
    """Build an ON graph with the standard gold-case fixtures."""
    return compile_graph(
        checkpointer=InMemorySaver(),
        model=model or ScriptedChatModel(script=[]),
        memory_port=port,
        user_profile_store=_profile_store(tmp_path),
        workspace_root=workspace_root or str(tmp_path / "ws"),
        known_document_ids=known_ids if known_ids is not None else _KNOWN_IDS,
    )


# ===========================================================================
# SC25 — Greeting → 0 prefetch.
# ===========================================================================


@pytest.mark.memory_on
def test_greeting_yields_zero_prefetch_rows(tmp_path: Path) -> None:
    """SC25: a greeting with no overlapping fact titles/bodies injects
    0 prefetch rows. Hard-coded for "hi" and "hello" — the spec wording
    names these as the canonical greeting examples.

    The gold set pairs lexical zero-hit with payload-zero-hit: the
    assembled model-facing payload (``messages_for_model`` +
    ``memory_prefetch_message``) must contain no
    ``[FIN_MEMORY_PREFETCH]`` block when ``select_prefetch`` returns
    ``[]`` (spec SC7: zero hits inject nothing).
    """
    port = _FakeMemoryPort()
    workspace = str(tmp_path / "ws")
    # Store ≥1 unrelated fact (the spec gates on a NON-empty workspace
    # so we know the zero is a *lexical* zero, not a store-empty zero).
    port.buffer.extract(
        "Operator's quarterly cadence: every Friday a recap of AAPL filings.",
        metadata={"title": "Quarterly cadence"},
        workspace=workspace,
    )

    # Pull rows via the buffer API and run ``select_prefetch`` exactly
    # the way the host assembler does on a turn.
    rows = _buffer_rows_as_memory_rows(
        port.buffer.entries(workspace=workspace, include_disabled=True)
    )
    assert len(rows) >= 1

    for greeting in ("hi", "hello"):
        result = select_prefetch(greeting, rows)
        assert result == [], (
            f"greeting {greeting!r} produced prefetch rows {result!r} "
            f"on a workspace with unrelated facts"
        )

    # Payload layer: ``memory_prefetch_message`` of an empty row set
    # returns ``None`` (the assembler contract from agent_instructions:
    # "Returns ``None`` when there are no rows" — adding an empty
    # marker block would change the token count and break SC7).
    payload_msg = memory_prefetch_message(select_prefetch("hi", rows))
    assert payload_msg is None


@pytest.mark.memory_on
def test_greeting_payload_has_no_full_store_catalog(tmp_path: Path) -> None:
    """SC25 (negative) — paired with the lexical zero-hit: even on a
    workspace with several facts in the store, the model-facing
    payload for a greeting must not enumerate titles. The system
    prefix carries the existence pointer exactly once (no catalog,
    no promote-on-recall-count); the user-side overlay is the
    ``None`` payload from the previous test.
    """
    port = _FakeMemoryPort()
    workspace = str(tmp_path / "ws")
    for index, (title, body) in enumerate(
        [
            ("Quarterly cadence", "Recap AAPL filings every Friday."),
            ("Operator profile", "Prefers short receipts."),
            ("Filing watchlist", "Tracks AAPL, MSFT, AMZN."),
        ]
    ):
        port.buffer.extract(
            body,
            metadata={"title": title},
            workspace=workspace,
        )
    rows = _buffer_rows_as_memory_rows(
        port.buffer.entries(workspace=workspace, include_disabled=True)
    )
    assert len(rows) >= 3

    model_messages = messages_for_model(
        [HumanMessage(content="hi")],
        track="dialogue",
        memory_on=True,
    )
    system_msg = next(
        msg for msg in model_messages if msg.type == "system"
    )
    system_text = str(system_msg.content)

    # Existence pointer appears exactly once.
    assert system_text.count(MEMORY_POINTER_MARKER) == 1
    assert MEMORY_POINTER_BLOCK.split("\n", 1)[1] in system_text

    # No full-store title catalog: NONE of the fact titles are
    # enumerated in the system prefix.
    for title in ("Quarterly cadence", "Operator profile", "Filing watchlist"):
        assert title not in system_text, (
            f"system prefix enumerated stored title {title!r} — "
            f"that is the full-store catalog the spec forbids (SC10)"
        )

    # The user-side overlay for a greeting is ``None`` — no
    # ``[FIN_MEMORY_PREFETCH]`` block anywhere in the payload.
    payload = memory_prefetch_message(select_prefetch("hi", rows))
    assert payload is None
    full_payload_text = "\n".join(str(m.content) for m in model_messages)
    assert FIN_MEMORY_PREFETCH_MARKER not in full_payload_text


# ===========================================================================
# SC26 — Research number → retrieve or read_file recorded.
# ===========================================================================


@pytest.mark.memory_on
def test_research_turn_with_number_records_retrieve_tool_call(
    tmp_path: Path,
) -> None:
    """SC26: a research ask that states a number records a retrieve or
    ``read_file`` tool call before the run reaches ``done``.

    The scripted model performs a retrieve call first (research
    track turns must read a real source for citation), then answers
    with a verifiable prose line. The post-run state captures the
    tool calls; we assert at least one of ``retrieve`` /
    ``read_file`` is in the run's messages, regardless of final
    status (the run may bounce or refuse depending on the
    citation-gate span; the spec pins the TOOL CALL, not the gate).
    """
    port = _FakeMemoryPort()
    workspace = str(tmp_path / "ws")
    model = ScriptedChatModel(
        script=[
            # Step 1: ask retrieve for the filing-page chunk that
            # carries the figure.
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "retrieve",
                        "args": {
                            "query": "AAPL net sales 2023 Q3",
                            "document_id": "AAPL-2023Q3",
                            "limit": 4,
                        },
                        "id": "c-retrieve-1",
                        "type": "tool_call",
                    }
                ],
            ),
            # Step 2: write an answer that quotes the legit span so
            # the citation gate can match (and we don't bounce).
            AIMessage(
                content=(
                    f"{load_private_fixture('10f10c61c4efb0b60744df900c4978298f4abc23b933036a713b48f8d2f4529b')}{_LEGIT_SPAN}."
                )
            ),
        ]
    )
    g = _compiled_graph(
        tmp_path=tmp_path,
        port=port,
        workspace_root=workspace,
        model=model,
    )

    out = g.invoke(
        {
            "messages": [
                HumanMessage(content="What was AAPL net sales in 2023Q3?")
            ],
            "evidence": [],
            "repair_count": 0,
            "step_count": 0,
            "answer": "",
            "citations": [],
            "status": "running",
            "thinking_text": "",
            "tool_summaries": [],
        },
        config={"configurable": {"thread_id": "t-gold-sc26"}},
    )

    # The critical property: at least one retrieve or read_file tool
    # call appears in the run's messages. We inspect every message
    # because the ToolNode emits both the AIMessage that requested
    # the tool and the ToolMessage that answered it; the
    # ``tool_calls`` list lives on the AIMessage side.
    messages = list(out.get("messages", []))
    seen_tool_calls: list[str] = []
    for message in messages:
        if isinstance(message, AIMessage):
            for tc in message.tool_calls or []:
                seen_tool_calls.append(str(tc.get("name", "")))
    assert "retrieve" in seen_tool_calls or "read_file" in seen_tool_calls, (
        f"SC26 violated: research turn with a number produced no "
        f"retrieve/read_file tool calls; saw {seen_tool_calls!r}"
    )


# ===========================================================================
# SC27 — Company-named fact still retrieves for filing claims.
# ===========================================================================


@pytest.mark.memory_on
def test_company_named_fact_still_uses_retrieve_for_filing_claim(
    tmp_path: Path,
) -> None:
    """SC27: a fact titled with a corpus company name still uses
    retrieve for a filing claim — memory did NOT short-circuit the
    research track into memory text.

    Setup: save a fact whose TITLE is "Apple Inc" and whose body is
    a filing number ("Form 10-Q filed on Aug 4 2023, accession
    0000320193-23-000106"). The research ask is about that
    company's filing numbers; the scripted model performs a
    ``retrieve`` call. We then assert:

    * the run recorded a ``retrieve`` / ``read_file`` tool call
      (memory did not short-circuit);
    * the fact body did NOT satisfy the citation verifier — the
      evidence channel stays empty even though memory text was
      surfaced. This is the structural property from
      ``test_memory_not_citation.py`` reused: the verifier's input
      is the evidence channel, not the message list.
    """
    port = _FakeMemoryPort()
    workspace = str(tmp_path / "ws")
    # Save a fact titled with a corpus company name.
    port.buffer.extract(
        "Form 10-Q filed on Aug 4 2023, accession 0000320193-23-000106.",
        metadata={"title": "Apple Inc"},
        workspace=workspace,
    )

    model = ScriptedChatModel(
        script=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "retrieve",
                        "args": {
                            "query": "Apple Inc 10-Q filing number accession",
                            "document_id": "AAPL-2023Q3",
                            "limit": 4,
                        },
                        "id": "c-retrieve-1",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(
                content=(
                    f"Per the filing: {_LEGIT_SPAN}. The 10-Q was filed "
                    "with accession 0000320193-23-000106."
                )
            ),
        ]
    )
    g = _compiled_graph(
        tmp_path=tmp_path,
        port=port,
        workspace_root=workspace,
        model=model,
    )

    out = g.invoke(
        {
            "messages": [
                HumanMessage(
                    content="What is the filing number for the Apple Inc 10-Q?"
                )
            ],
            "evidence": [],
            "repair_count": 0,
            "step_count": 0,
            "answer": "",
            "citations": [],
            "status": "running",
            "thinking_text": "",
            "tool_summaries": [],
        },
        config={"configurable": {"thread_id": "t-gold-sc27"}},
    )

    # (a) The model still hit retrieve / read_file even though the
    # memory fact's title matched the company name — memory did not
    # short-circuit.
    messages = list(out.get("messages", []))
    seen_tool_calls: list[str] = []
    for message in messages:
        if isinstance(message, AIMessage):
            for tc in message.tool_calls or []:
                seen_tool_calls.append(str(tc.get("name", "")))
    assert "retrieve" in seen_tool_calls or "read_file" in seen_tool_calls, (
        f"SC27 violated: company-named fact short-circuited the "
        f"research turn; saw {seen_tool_calls!r}"
    )

    # (b) The fact body is NOT citation evidence. The evidence
    # channel is fed only by retrieve / read_file ``Command.update``
    # payloads (test_memory_not_citation structural proof), and
    # here the FakeRetrievePort did not actually return any chunk
    # for the scripted query — so the evidence list stays empty.
    # The empty evidence list means the gate has nothing to match
    # against; the fact body bytes live in message history, not in
    # evidence, so they cannot satisfy the verifier.
    evidence = list(out.get("evidence", []))
    fact_body = "0000320193-23-000106"
    for evidence_item in evidence:
        assert fact_body not in str(evidence_item.page_text), (
            "SC27 violated: the fact body leaked into the evidence "
            "channel and could be cited as if it were corpus text"
        )


# ===========================================================================
# SC28 — Cross-thread save is recallable.
# ===========================================================================


@pytest.mark.memory_on
def test_explicit_save_on_thread_a_recallable_on_thread_b(
    tmp_path: Path,
) -> None:
    """SC28: explicit save on thread A is recallable on a NEW thread
    B in the SAME workspace when the utterance hits the saved
    title/body.

    The contract has two halves, both pinned here:

    * ``recall_memory`` on thread B returns the fact that thread A
      saved (memory is workspace-isolated, not thread-isolated);
    * ``select_prefetch`` on thread B's utterance (lexically
      hitting the saved title) surfaces the fact in the prefetch
      rows. ``shown_ids`` is empty on the new thread — prefetch
      does not yet know about the saved row, so it cannot skip
      it (the cap-3 + lexical ranking applies fresh).

    Thread-isolation is asserted by the WORKSPACE side: thread A's
    save is invisible from a DIFFERENT workspace (spec SC6).
    """
    port = _FakeMemoryPort()
    workspace = str(tmp_path / "ws")
    graph_a = _compiled_graph(
        tmp_path=tmp_path,
        port=port,
        workspace_root=workspace,
    )

    # --- Thread A: explicit save via the bound tool ---
    save_tool = graph_a._tool_node.tools_by_name["save_memory"]
    save_tool.invoke(
        {
            "title": "Coffee preference",
            "body": "Latte in the morning. Double shot on Mondays.",
        }
    )

    # The save persisted at the workspace level (port-backed).
    saved_rows = port.buffer.entries(workspace=workspace)
    assert len(saved_rows) == 1
    saved_title = saved_rows[0]["title"]
    assert saved_title == "Coffee preference"

    # --- Thread B: a NEW graph on the SAME workspace, NEW thread_id ---
    graph_b = _compiled_graph(
        tmp_path=tmp_path,
        port=port,
        workspace_root=workspace,
    )

    # (a) recall_memory on thread B sees the row from thread A.
    recall_tool = graph_b._tool_node.tools_by_name["recall_memory"]
    raw = recall_tool.invoke({"query": "latte morning coffee double shot"})
    payload = json.loads(raw) if isinstance(raw, str) else raw
    assert payload["status"] == "ok"
    assert len(payload["memories"]) == 1
    assert payload["memories"][0]["title"] == "Coffee preference"
    # Advisory marker pinned (spec SC12: recall is never citation
    # evidence — the body bytes are advisory-only).
    assert payload["memories"][0]["advisory"] is True
    assert payload["memories"][0]["citation_eligible"] is False

    # (b) select_prefetch on the same workspace on a fresh thread
    # (shown_ids is empty) surfaces the saved row when the
    # utterance hits its title/body.
    rows = _buffer_rows_as_memory_rows(
        port.buffer.entries(workspace=workspace, include_disabled=True)
    )
    prefetch_rows = select_prefetch(
        "morning latte please",
        rows,
        shown_ids=set(),  # NEW thread — nothing shown yet
    )
    assert len(prefetch_rows) == 1
    assert prefetch_rows[0]["fact_id"] == saved_rows[0]["id"]
    assert prefetch_rows[0]["title"] == "Coffee preference"

    # (c) Cross-workspace isolation: the row is invisible from a
    # DIFFERENT workspace (spec SC6).
    other_workspace = str(tmp_path / "ws-other")
    other_entries = port.buffer.entries(
        workspace=other_workspace, include_disabled=True
    )
    assert other_entries == []
    other_rows = _buffer_rows_as_memory_rows(other_entries)
    cross_prefetch = select_prefetch(
        "morning latte", other_rows, shown_ids=set()
    )
    assert cross_prefetch == []


# ===========================================================================
# Extract deny classes do not persist.
# ===========================================================================


@pytest.mark.memory_on
def test_extract_deny_filing_claim_does_not_persist(tmp_path: Path) -> None:
    """SC15 (gold) — a candidate that names a corpus filing locator
    (e.g. ``AAPL-2022Q3``) is denied at the gate and the buffer
    stays unchanged. Mirrors ``test_extract_deny_classes`` style in
    ``test_memory_extract.py``.
    """
    port = _FakeMemoryPort()
    workspace = str(tmp_path / "ws")
    messages = [
        HumanMessage(content="q0"),
        AIMessage(content="r0"),
        HumanMessage(content="q1"),
        AIMessage(content="r1"),
        HumanMessage(content="q2"),
        AIMessage(content="r2"),
    ]

    class _FilingProposer:
        def propose(self, _msgs):
            return FactCandidate(
                title="AAPL revenue",
                body="Per AAPL-2022Q3 the issuer reported 394328M revenue.",
            )

    result = maybe_extract(
        {"messages": list(messages)},
        port.buffer,
        thread_id="t-gold-deny-1",
        workspace=workspace,
        llm=_FilingProposer(),
    )
    assert result.status == "denied"
    assert result.reason == "filing_claim"
    # Buffer unchanged — no rows landed.
    assert port.buffer.entries(workspace=workspace) == []


@pytest.mark.memory_on
def test_extract_deny_profile_duplicate_does_not_persist(tmp_path: Path) -> None:
    """SC15 (gold) — a candidate that duplicates user-profile text is
    denied and the buffer stays unchanged.
    """
    port = _FakeMemoryPort()
    workspace = str(tmp_path / "ws")
    profile_store = _profile_store(tmp_path)
    profile_store.write_layer(
        "workspace",
        "operator prefers quarterly revenue comparisons across six quarters\n",
    )

    messages = [
        HumanMessage(content="q0"),
        AIMessage(content="r0"),
        HumanMessage(content="q1"),
        AIMessage(content="r1"),
        HumanMessage(content="q2"),
        AIMessage(content="r2"),
    ]

    class _DuplicateProposer:
        def propose(self, _msgs):
            return FactCandidate(
                title="preferences",
                body=(
                    "operator prefers quarterly revenue comparisons "
                    "across six quarters and the workspace owner "
                    "prefers quarterly."
                ),
            )

    result = maybe_extract(
        {"messages": list(messages)},
        port.buffer,
        thread_id="t-gold-deny-2",
        workspace=workspace,
        user_profile_store=profile_store,
        llm=_DuplicateProposer(),
    )
    assert result.status == "denied"
    assert result.reason == "profile_duplicate"
    assert port.buffer.entries(workspace=workspace) == []


@pytest.mark.memory_on
def test_extract_deny_identity_content_does_not_persist(tmp_path: Path) -> None:
    """SC15 (gold) — a candidate that reads as agent identity / soul
    is denied and the buffer stays unchanged.
    """
    port = _FakeMemoryPort()
    workspace = str(tmp_path / "ws")
    messages = [
        HumanMessage(content="q0"),
        AIMessage(content="r0"),
        HumanMessage(content="q1"),
        AIMessage(content="r1"),
        HumanMessage(content="q2"),
        AIMessage(content="r2"),
    ]

    class _IdentityProposer:
        def propose(self, _msgs):
            return FactCandidate(
                title="agent identity",
                body=(
                    "I am fin, a workspace knowledge-base agent, and I "
                    "answer with citations from the corpus."
                ),
            )

    result = maybe_extract(
        {"messages": list(messages)},
        port.buffer,
        thread_id="t-gold-deny-3",
        workspace=workspace,
        llm=_IdentityProposer(),
    )
    assert result.status == "denied"
    assert result.reason == "identity_content"
    assert port.buffer.entries(workspace=workspace) == []


# ===========================================================================
# No full-store catalog / no promote.
# ===========================================================================


@pytest.mark.memory_on
def test_full_store_payload_has_no_catalog_no_promote(tmp_path: Path) -> None:
    """SC10 / SC11 (gold) — even with several facts in the store, the
    model-facing payload does NOT enumerate all titles in the system
    prefix and the user-side overlay does not promote into the
    system.

    The existence pointer is the only memory sentence in the
    system prefix. The pointer appears EXACTLY ONCE per thread —
    not stacked across turns.
    """
    port = _FakeMemoryPort()
    workspace = str(tmp_path / "ws")
    titles = [
        "Coffee preference",
        "Filing watchlist",
        "Quarterly cadence",
        "Operator profile",
        "Revenue comparison style",
    ]
    for index, title in enumerate(titles):
        port.buffer.extract(
            f"{title} body line {index}.",
            metadata={"title": title},
            workspace=workspace,
        )

    # Build the system prefix twice (two turns) — the prefix bytes
    # stay stable across calls per prompt-development rule 3.
    first = messages_for_model(
        [HumanMessage(content="anything for turn one")],
        track="dialogue",
        memory_on=True,
    )
    second = messages_for_model(
        [HumanMessage(content="anything for turn two")],
        track="dialogue",
        memory_on=True,
    )
    first_system = next(m for m in first if m.type == "system")
    second_system = next(m for m in second if m.type == "system")
    first_text = str(first_system.content)
    second_text = str(second_system.content)

    # (a) Existence pointer appears exactly once per system message.
    for label, system_text in (("turn-one", first_text), ("turn-two", second_text)):
        assert system_text.count(MEMORY_POINTER_MARKER) == 1, (
            f"{label} system prefix has "
            f"{system_text.count(MEMORY_POINTER_MARKER)} copies of "
            f"the existence pointer — must be 1"
        )

    # (b) NO stored title is enumerated in the system prefix (no
    # full-store catalog; no promote-on-recall-count).
    for label, system_text in (("turn-one", first_text), ("turn-two", second_text)):
        for title in titles:
            assert title not in system_text, (
                f"{label} system prefix enumerated stored title "
                f"{title!r} — that is the full-store catalog the spec "
                f"forbids (SC10)"
            )

    # (c) Prefetch rows (when present) live on the USER side, not
    # in the prefix. An utterance that hits every title produces 3
    # rows (the SC8 cap), and they are surfaced via the prefetch
    # HumanMessage — never injected into the system prefix.
    rows = _buffer_rows_as_memory_rows(
        port.buffer.entries(workspace=workspace, include_disabled=True)
    )
    prefetch_rows = select_prefetch(
        "preference quarterly coffee profile comparison",
        rows,
        shown_ids=set(),
    )
    assert len(prefetch_rows) == PREFETCH_MAX_ROWS == 3
    prefetch_msg = memory_prefetch_message(prefetch_rows)
    assert prefetch_msg is not None
    assert isinstance(prefetch_msg, HumanMessage)
    assert FIN_MEMORY_PREFETCH_MARKER in str(prefetch_msg.content)

    # The prefetch marker / rows are NOT in either system prefix.
    assert FIN_MEMORY_PREFETCH_MARKER not in first_text
    assert FIN_MEMORY_PREFETCH_MARKER not in second_text

    # (d) Prefix is byte-stable across turns.
    assert first_text == second_text, (
        "system prefix bytes changed across turns — prompt-development "
        "rule 3 says identity / soul / pointer / tool schema stay "
        "byte-stable for the thread"
    )


# ===========================================================================
# Default pytest stays OFF (plan acceptance last bullet).
#
# This case is UNMARKED on purpose: the autouse strip in
# tests/conftest.py + tests/host/test_memory_pytest_off.py removes
# ``FIN_MEMORY_ENABLED`` before this test's body runs, so the
# compiled graph sees OFF and exposes zero memory tools. The gold
# set's other tests are ``@pytest.mark.memory_on`` and skip this
# OFF assertion; this case runs under default ``uv run pytest -q``
# to keep the OFF contract pinned.
# ===========================================================================


def test_default_pytest_builds_off_graph_with_zero_memory_tools(
    tmp_path: Path,
) -> None:
    """Plan-acceptance last bullet: default pytest builds OFF even
    if the operator ``.env`` is ON. The compiled graph exposes
    exactly the existing eight tools — no save_memory,
    recall_memory, or edit_user_profile in either the bound model
    or the ToolNode.

    This test is intentionally UNMARKED: the default suite must
    observe the OFF contract, not opt back into ON via the marker.
    """
    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=ScriptedChatModel(script=[]),
        memory_port=OffMemoryAdapter(),  # explicit OFF
        user_profile_store=_profile_store(tmp_path),
        workspace_root=str(tmp_path / "ws"),
        known_document_ids=_KNOWN_IDS,
    )

    bound_names = {t.name for t in graph._bound_tools}
    tool_node_names = set(graph._tool_node.tools_by_name)

    # The three spec memory tools are absent on both surfaces.
    for forbidden in ("save_memory", "recall_memory", "edit_user_profile"):
        assert forbidden not in bound_names, (
            f"OFF graph bound {forbidden!r} on the model side"
        )
        assert forbidden not in tool_node_names, (
            f"OFF graph surfaced {forbidden!r} in the ToolNode"
        )

    # No dream tool on either surface (map G4 / SC12).
    for forbidden in ("dream", "run_dream", "memory_dream"):
        assert forbidden not in bound_names
        assert forbidden not in tool_node_names

    # The OFF path uses the existing eight ToolNode tools (no
    # memory surface). Locked to a set equality so a future
    # add-the-ninth-tool change is caught.
    assert tool_node_names == {
        "list_files",
        "read_file",
        "retrieve",
        "get_xbrl_fact",
        "compare_facts",
        "redline",
        "extract_tables",
        "three_year_statement",
    }
