"""A source-scope refusal is enforced at every source and evidence boundary.

Expected values come from ``specs/question-source-scope.md`` (decisions 2, 3,
4, 5, 7) and ADR 0088 — not from the implementation:

* enforcement happens BEFORE source data crosses a boundary: a refused read
  never reaches the read leg, a refused retrieval never reaches the search
  port (asserted on the fake's recorded calls, which is the "before, not
  after" proof);
* a known locator cannot bypass the current scope (SC-3);
* an in-force restriction is the NARROWER of authority, restriction, and what
  the call names, and is never silently broadened by dropping an
  out-of-scope id (SC-2);
* the company branch keeps its eligibility under a restriction that only NAMES
  attachments (ADR 0088:10) and loses it under one the operator declared
  EXCLUSIVE ("use only these sources"), and that refusal lands before any
  corpus data crosses a boundary (decision 5);
* capability is derived from the proven expressibility set, so an operation no
  wired adapter is proven to express fails closed instead of passing
  (decision 5);
* the outcomes stay distinct — denied, unavailable, dependency_failed, and a
  successful empty are never collapsed onto one code, and an empty search is
  not a scope error (SC-4);
* the multi-issuer fan-out branch is a company-source boundary too. It calls
  the retrieve port directly instead of through a tool factory, so an
  operator-declared exclusive scope has to stop EVERY branch before the port
  is reached — and the denial has to survive the join's status fold rather than
  be rendered as a successful miss (SC-3, SC-4);
* an unwired graph (no ``attachment_service``) keeps the pre-scope behavior
  byte for byte ONLY while nothing is in force: a restriction in force over an
  absent authority is a dependency failure, never a licence to skip the gate.

Hermetic: in-memory stores, recording fakes, ``ScriptedChatModel`` only where
a compile is needed — no network, no live backend.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal, Mapping

import pytest

from fin.corpus.catalog import record_filings
from fin.corpus.extracted import Locator
from fin.host import ScriptedChatModel, compile_graph
from fin.host.attachment.attachment_index import build_postings
from fin.host.attachment.attachment_retrieval import AttachmentChunk, AttachmentLocator
from fin.host.attachment.attachment_source_read import AttachmentSourceReader
from fin.host.attachment.attachment_store import (
    AttachmentRecord,
    AttachmentStoreError,
    InMemoryAttachmentStore,
    ParsedAttachmentUnit,
)
from fin.host.graph import (
    _LocatorToolSpec,
    _collect_statement_claims,
    _company_scope_refusal,
    _make_compare_facts_tool,
    _make_get_xbrl_fact_tool,
    _make_list_files_tool,
    _make_locator_tool,
    _make_read_source_tool,
    _make_read_visual_detail_tool,
    _make_redline_tool,
    _make_retrieve_tool,
    _make_statement_tool,
    _make_table_extract_tool,
)
from fin.host.source_scope import (
    SCOPE_ATTACHMENT_NOT_PERMITTED,
    SCOPE_ATTACHMENT_NOT_READY,
    SCOPE_ATTACHMENT_UNKNOWN,
    SCOPE_ATTACHMENT_VERSION_STALE,
    SCOPE_COMPANY_NOT_PERMITTED,
    SCOPE_DEPENDENCY_FAILED,
    SCOPE_OPERATION_UNSUPPORTED,
    SCOPE_PERMITTED,
    SCOPE_SELECTION_EMPTY,
    SCOPE_SOURCE_REF_UNREADABLE,
    ScopeGate,
    ScopeRestriction,
)
from fin.host.state import reset_input_state
from fin.host.thread_store import InMemoryThreadStore
from fin.host.tools.issuer_fanout import (
    ISSUER_FANOUT_SCHEMA,
    ISSUER_FANOUT_TOOL,
    MAX_ISSUERS,
    issuer_query,
)
from fin.retrieve import Chunk
from fin.retrieve.fake_port import FakeRetrievePort
from fin.retrieve.source_read import ATTACHMENT_KIND
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

QUERY = "What is the purchase price in the attached term sheet?"
ATTACHMENT_TEXT = "The term sheet lists a purchase price of 12,000 thousand dollars."
COMPANY_TEXT = "Apple net sales for the fiscal 2023 fourth quarter were 89,493 million."


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------


class _Service:
    """The wired attachment seam, shaped like the deployed service.

    Only ``store`` matters to the boundaries under test; the visual faces are
    added by the visual lane's own suite.
    """

    def __init__(self, store: InMemoryAttachmentStore) -> None:
        self.store = store


class _FlakyStore:
    """A store whose ``get`` raises from the Nth call on.

    Lets a case reach the gate's ``dependency_failed``: the scope resolves
    from the first (healthy) read, then the boundary's own re-resolve faults.
    """

    def __init__(self, inner: InMemoryAttachmentStore, *, healthy_gets: int) -> None:
        self._inner = inner
        self._healthy = healthy_gets
        self._gets = 0

    def get(self, attachment_id: str) -> AttachmentRecord | None:
        index = self._gets
        self._gets += 1
        if index >= self._healthy:
            raise AttachmentStoreError("attachment backend is down")
        return self._inner.get(attachment_id)

    def list_for_thread(self, thread_id: str) -> list[AttachmentRecord]:
        return self._inner.list_for_thread(thread_id)


class _ListFaultStore:
    """A store whose ``list_for_thread`` faults — the enumeration is down.

    Distinct from :class:`_FlakyStore` (which faults ``get``): this models the
    authority the DEFAULT selection resolves through being unconsultable, so
    the scope's ``resolution_fault`` is set and an empty selection from it must
    not be read as a successful empty.
    """

    def __init__(self, inner: InMemoryAttachmentStore) -> None:
        self._inner = inner

    def get(self, attachment_id: str) -> AttachmentRecord | None:
        return self._inner.get(attachment_id)

    def list_for_thread(self, thread_id: str) -> list[AttachmentRecord]:
        raise AttachmentStoreError("attachment enumeration is down")


class _MovingStore:
    """A store whose answer for an id CHANGES after the Nth read.

    Decision 4 re-checks current authority at the boundary, so what was true
    when the scope resolved is not what a boundary may rely on. ``"advance"``
    re-issues the record at a newer version (a re-preparation landing in that
    window); ``"drop"`` stops answering it at all (a tombstone in the same
    window). Either way the boundary must refuse the stale locator, never read
    the row the earlier answer promised.
    """

    def __init__(
        self,
        inner: InMemoryAttachmentStore,
        *,
        moves_after_gets: int,
        mode: Literal["advance", "drop"],
    ) -> None:
        self._inner = inner
        self._moves_after_gets = moves_after_gets
        self._mode = mode
        self._gets = 0

    def get(self, attachment_id: str) -> AttachmentRecord | None:
        index = self._gets
        self._gets += 1
        record = self._inner.get(attachment_id)
        if record is None or index < self._moves_after_gets:
            return record
        if self._mode == "drop":
            return None
        return replace(record, version=record.version + 1)

    def list_for_thread(self, thread_id: str) -> list[AttachmentRecord]:
        return self._inner.list_for_thread(thread_id)


class _RecordingSearchPort:
    """Records every search call and answers from a thread-keyed table.

    Every call's kwargs are kept so a test can prove the host never named an
    out-of-scope id to the port.
    """

    def __init__(self, by_thread: dict[str, list[AttachmentChunk]] | None = None) -> None:
        self.by_thread = by_thread or {}
        self.calls: list[dict[str, Any]] = []

    def search(
        self,
        *,
        thread_id: str,
        query: str,
        attachment_ids: list[str] | None = None,
        limit: int | None = None,
    ) -> list[AttachmentChunk]:
        self.calls.append(
            {
                "thread_id": thread_id,
                "query": query,
                "attachment_ids": None
                if attachment_ids is None
                else list(attachment_ids),
                "limit": limit,
            }
        )
        chunks = list(self.by_thread.get(thread_id, []))
        if attachment_ids is not None:
            allowed = set(attachment_ids)
            chunks = [c for c in chunks if c.source_ref.attachment_id in allowed]
        return chunks[: (8 if limit is None else limit)]


class _RecordingReader:
    """A read leg that records its calls and returns a canned answer."""

    def __init__(self, answer: dict[str, Any] | None = None) -> None:
        self.answer = answer or {}
        self.calls: list[tuple[dict[str, Any], int]] = []

    def read_slice(
        self, source_ref: Mapping[str, Any], position: int = 0
    ) -> dict[str, Any]:
        self.calls.append((dict(source_ref), position))
        return dict(self.answer)


class _LeakingSearchPort:
    """A port that ignores its id filter and returns every hit it holds.

    Models a stale or misbehaving index: the host asked it for the effective
    selection, but it answers with an id outside it. That is exactly the
    post-fetch case the admission re-gate must catch — a leaked hit may reach
    the model as display text, but it can never become evidence.
    """

    def __init__(self, chunks: list[AttachmentChunk]) -> None:
        self.chunks = chunks
        self.calls: list[dict[str, Any]] = []

    def search(
        self,
        *,
        thread_id: str,
        query: str,
        attachment_ids: list[str] | None = None,
        limit: int | None = None,
    ) -> list[AttachmentChunk]:
        self.calls.append(
            {
                "attachment_ids": None
                if attachment_ids is None
                else list(attachment_ids),
            }
        )
        return list(self.chunks)


class _CountingCompanyPort:
    """Wraps the company port and counts the calls that actually reach it.

    The corpus boundary's "before, not after" proof (decision 5) is that a
    refused retrieval never reaches the adapter at all, so the refusal is
    counted on the port itself rather than only on the envelope shape.
    """

    def __init__(self, inner: FakeRetrievePort) -> None:
        self._inner = inner
        self.queries: list[str] = []

    @property
    def call_count(self) -> int:
        return len(self.queries)

    def retrieve(self, query: str, **filters: Any) -> dict[str, Any]:
        self.queries.append(query)
        return self._inner.retrieve(query, **filters)


def _store_with_ready(
    attachment_id: str,
    *,
    text: str = ATTACHMENT_TEXT,
    version: int = 1,
    filename: str = "memo.txt",
) -> tuple[InMemoryThreadStore, InMemoryAttachmentStore, str]:
    """A fresh thread owning one READY attachment under ``attachment_id``."""
    threads = InMemoryThreadStore()
    store = InMemoryAttachmentStore(threads)
    thread_id = threads.create_thread(title="Thread").id
    store.create(
        thread_id=thread_id,
        attachment_id=attachment_id,
        filename=filename,
        size_bytes=1_024,
        sha256="ab" * 32,
        object_key=f"threads/{thread_id}/{attachment_id}",
    )
    units = (ParsedAttachmentUnit(unit=1, text=text),)
    store.record_units(attachment_id=attachment_id, version=version, units=units)
    store.put_index(
        attachment_id=attachment_id, version=version, postings=build_postings(units)
    )
    store.mark_ready(attachment_id=attachment_id, version=version, unit_count=1)
    return threads, store, thread_id


def _seed_threaded(
    attachment_specs: list[tuple[str, str]],
) -> tuple[InMemoryThreadStore, InMemoryAttachmentStore, str]:
    """One thread owning the given ``(attachment_id, text)`` ready rows."""
    threads = InMemoryThreadStore()
    store = InMemoryAttachmentStore(threads)
    thread_id = threads.create_thread(title="Thread").id
    for attachment_id, text in attachment_specs:
        store.create(
            thread_id=thread_id,
            attachment_id=attachment_id,
            filename="memo.txt",
            size_bytes=1_024,
            sha256="ab" * 32,
            object_key=f"threads/{thread_id}/{attachment_id}",
        )
        units = (ParsedAttachmentUnit(unit=1, text=text),)
        store.record_units(attachment_id=attachment_id, version=1, units=units)
        store.put_index(
            attachment_id=attachment_id, version=1, postings=build_postings(units)
        )
        store.mark_ready(attachment_id=attachment_id, version=1, unit_count=1)
    return threads, store, thread_id


def _attachment_ref(attachment_id: str, version: object = 1) -> dict[str, Any]:
    return {
        "kind": ATTACHMENT_KIND,
        "attachment_id": attachment_id,
        "version": version,
        "unit": 1,
    }


def _invoke_read_source(
    tool: Any,
    *,
    source_ref: Mapping[str, Any],
    thread_id: str,
) -> Any:
    return tool.invoke(
        {
            "name": "read_source",
            "args": {"source_ref": dict(source_ref), "position": 0},
            "id": "c-read-source",
            "type": "tool_call",
        },
        config={"configurable": {"thread_id": thread_id}},
    )


def _invoke_retrieve(
    tool: Any,
    *,
    thread_id: str,
    args: dict[str, Any],
) -> dict[str, Any]:
    command = tool.invoke(
        {
            "name": "retrieve",
            "args": dict(args),
            "id": "c-retrieve",
            "type": "tool_call",
        },
        config={"configurable": {"thread_id": thread_id}},
    )
    messages = command.update["messages"]
    assert len(messages) == 1 and isinstance(messages[0], ToolMessage)
    return json.loads(str(messages[0].content))


def _read_source_payload(command: Any) -> dict[str, Any]:
    messages = command.update["messages"]
    assert len(messages) == 1 and isinstance(messages[0], ToolMessage)
    return json.loads(str(messages[0].content))


def _company_port() -> FakeRetrievePort:
    return FakeRetrievePort(
        hits_by_query={
            QUERY: [
                Chunk(
                    text=COMPANY_TEXT,
                    source_ref=Locator(
                        document_id="AAPL-2023Q3", version="d" * 64, unit=3
                    ),
                    score=1.5,
                )
            ]
        },
        known_document_ids={"AAPL-2023Q3"},
    )


def _attachment_chunk(attachment_id: str, *, version: int = 1, unit: int = 1) -> AttachmentChunk:
    return AttachmentChunk(
        text=ATTACHMENT_TEXT,
        source_ref=AttachmentLocator(
            attachment_id=attachment_id, version=version, unit=unit
        ),
        score=0.75,
    )


def _exclusive(attachment_ids: tuple[str, ...] | None) -> ScopeRestriction:
    """The operator's EXCLUSIVE narrowing: this selection and nothing else.

    The third fact of :class:`ScopeRestriction` — declaring the named set the
    WHOLE approved selection puts the company corpus outside it, which is what
    a "use only the attached …" ask means.
    """
    return ScopeRestriction(attachment_ids, "explicit", company_excluded=True)


def _naming_only(attachment_ids: tuple[str, ...]) -> ScopeRestriction:
    """A narrowing that only names attachments — the corpus stays eligible."""
    return ScopeRestriction(attachment_ids, "explicit")


def _company_ref(unit: int = 3) -> dict[str, Any]:
    """One company locator: the corpus triple ``read_source`` routes on.

    Exactly ``{document_id, version, unit}`` — the shape the company leg
    accepts and a retrieve chunk serializes, so a company read is driven with
    real locator input, not a stand-in.
    """
    return {"document_id": "AAPL-2023Q3", "version": "d" * 64, "unit": unit}


def _catalog_row() -> dict[str, Any]:
    """One catalog row the corpus enumeration WOULD answer with."""
    return {
        "document_id": "INJECTED-2023Q1",
        "title": "Injected filing",
        "doc_type": "narrative",
        "company": "INJECTED",
        "period": "2023 Q1",
        "version": "a" * 64,
        "page_count": 2,
    }


def _seed_facts(root: Path) -> Path:
    """A fact table holding one ``us-gaap:Revenues`` row for the company doc."""
    from fin.corpus.facts import Fact, write_facts

    facts_root = root / "facts"
    write_facts(
        document_id="AAPL-2023Q3",
        facts=[
            Fact(
                concept="us-gaap:Revenues",
                value=383285000000,
                period="2023-Q3-YTD",
                unit="USD",
                scale="thousands",
                source_locator=Locator(
                    document_id="AAPL-2023Q3", version="d" * 64, unit=4
                ),
            )
        ],
        facts_root=facts_root,
    )
    return facts_root


def _seed_extracted(root: Path) -> dict[str, Any]:
    """A frozen two-document tree plus a table unit the analysis tools read.

    The bodies and the table are real content, so a naming-only restriction
    answers with a rendered redline / extracted table rather than only proving
    "not refused" — the ADR 0088:10 no-regression bar.
    """
    extracted = root / "extracted"
    docs = {
        "ACME-2022Q3": (
            "Revenue increased steadily.\n\nOperating margin was 10%.",
            "a" * 64,
        ),
        "ACME-2023Q3": (
            "Revenue increased rapidly.\n\nOperating margin was 12%.\n\n"
            "New disclosure paragraph.",
            "b" * 64,
        ),
        "ACME-TABLE-2023Q3": (
            "CONSOLIDATED RESULTS\n\n"
            "| Metric | 2023 | 2022 |\n"
            "| --- | ---: | ---: |\n"
            "| Revenue | 100 | 90 |\n",
            "c" * 64,
        ),
    }
    for document_id, (text, version) in docs.items():
        doc_dir = extracted / document_id
        doc_dir.mkdir(parents=True)
        page = {
            "document_id": document_id,
            "version": version,
            "page": 1,
            "text": text,
        }
        (doc_dir / "page-001.json").write_text(json.dumps(page))
        (doc_dir / "passages-001.json").write_text(
            json.dumps(
                {
                    "passages": [
                        {"text": text, "char_start": 0, "char_end": len(text)}
                    ]
                }
            )
        )
    (extracted / "_index.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "documents": {
                    document_id: {"page_count": 1, "version": version}
                    for document_id, (_, version) in docs.items()
                },
            }
        )
    )
    manifest = root / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "documents": [
                    {
                        "document_id": document_id,
                        "sha256": version,
                        "company": "ACME",
                        "period": "2023 Q3",
                    }
                    for document_id, (_, version) in docs.items()
                ]
            }
        )
    )
    return {
        "manifest": manifest,
        "extracted": extracted,
        "before_version": "a" * 64,
        "after_version": "b" * 64,
        "table_version": "c" * 64,
    }


def _seed_statement_facts(root: Path) -> Path:
    """A fact sidecar set giving one issuer three fiscal years of revenue."""
    from fin.corpus.facts import Fact, write_facts

    facts_root = root / "statements"
    for document_id, period, revenue in (
        ("ACME-2021FY", "2021-FY", 100),
        ("ACME-2022FY", "2022-FY", 120),
        ("ACME-2023FY", "2023-FY", 150),
    ):
        write_facts(
            document_id=document_id,
            facts=[
                Fact(
                    concept="us-gaap:Revenues",
                    value=revenue,
                    period=period,
                    unit="USD",
                    scale="millions",
                    source_locator=Locator(
                        document_id=document_id, version="d" * 64, unit=4
                    ),
                )
            ],
            facts_root=facts_root,
        )
    return facts_root


def _new_boundary_tool(operation: str, root: Path, *, restriction: ScopeRestriction | None):
    """Build one of the three newly gated company tools with no store wired.

    Returns the tool and a real company call for it, so the same probe answers
    both the exclusive-refusal shape (nothing in force needed) and the
    nothing-in-force shape (the real implementation runs against these paths).
    """
    if operation == "redline":
        fixture = _seed_extracted(root)
        tool = _make_redline_tool(
            fixture["manifest"],
            fixture["extracted"],
            scope_restriction=restriction,
        )
        return tool, {
            "before_document_id": "ACME-2022Q3",
            "before_version": fixture["before_version"],
            "after_document_id": "ACME-2023Q3",
            "after_version": fixture["after_version"],
        }
    if operation == "extract_tables":
        fixture = _seed_extracted(root)
        tool = _make_table_extract_tool(
            fixture["manifest"],
            fixture["extracted"],
            scope_restriction=restriction,
        )
        return tool, {
            "document_id": "ACME-TABLE-2023Q3",
            "version": fixture["table_version"],
            "unit": 1,
        }
    tool = _make_statement_tool(
        _seed_statement_facts(root),
        scope_restriction=restriction,
    )
    return tool, {
        "issuer": "ACME",
        "periods": ["2021-FY", "2022-FY", "2023-FY"],
        "concepts": ["us-gaap:Revenues"],
    }


def _impl_must_not_run(
    monkeypatch: pytest.MonkeyPatch,
    module_name: str,
    attribute: str,
    message: str,
) -> None:
    """Patch a tool implementation to raise, proving the gate denies before it runs.

    ``raising=True`` is load-bearing: a renamed or removed symbol must fail
    loudly here instead of letting the real implementation slip past the gate.
    """
    import importlib

    def _boom(**_kwargs: Any) -> dict[str, Any]:
        raise AssertionError(message)

    impl_module = importlib.import_module(module_name)
    monkeypatch.setattr(impl_module, attribute, _boom, raising=True)


# ---------------------------------------------------------------------------
# fan-out harness: the ``research_issuers`` branch, driven through the graph
# ---------------------------------------------------------------------------


FANOUT_QUERY = "services revenue"
FANOUT_ROSTER = ("AAPL", "MSFT", "NVDA")
# One company slot and one period slot, so the rules tier lands on
# ``research`` and the turn really reaches the fan-out instead of clarifying.
FANOUT_QUESTION = (
    "Compare services revenue across AAPL, MSFT and NVDA for Q3 2023."
)


def _fanout_document_id(issuer: str) -> str:
    return f"{issuer}-2023Q3"


def _fanout_version(issuer: str) -> str:
    return (issuer[0].lower() * 64)[:64]


FANOUT_DOCUMENT_IDS = frozenset(
    _fanout_document_id(issuer) for issuer in FANOUT_ROSTER
)

# One passage per issuer: each is long enough to be an extractive span and
# shares no run with a sibling's, so a citation can only come from the issuer
# it belongs to and a hit can never be mistaken for another branch's miss.
_FANOUT_PASSAGES = {
    "AAPL": "Apple grew services and wearables faster than hardware.",
    "MSFT": "Microsoft cloud bookings expanded on enterprise renewals.",
    "NVDA": "Accelerated computing demand outran the available supply.",
}


def _fanout_chunk(issuer: str) -> Chunk:
    return Chunk(
        text=_FANOUT_PASSAGES[issuer],
        source_ref=Locator(
            document_id=_fanout_document_id(issuer),
            version=_fanout_version(issuer),
            unit=1,
        ),
        score=1.0,
    )


def _fanout_port() -> _CountingCompanyPort:
    """The corpus port the branches call, counting every call that lands.

    Keyed by the branch query the worker itself builds, so a permitted
    fan-out answers with real passages rather than a miss the fake invented.
    """
    return _CountingCompanyPort(
        FakeRetrievePort(
            hits_by_query={
                issuer_query(issuer, FANOUT_QUERY): [_fanout_chunk(issuer)]
                for issuer in FANOUT_ROSTER
            }
        )
    )


def _fanout_script() -> list[AIMessage]:
    """One ``research_issuers`` call, then prose quoting every branch's hit."""
    return [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": ISSUER_FANOUT_TOOL,
                    "args": {"issuers": list(FANOUT_ROSTER), "query": FANOUT_QUERY},
                    "id": "c-fanout",
                    "type": "tool_call",
                }
            ],
        ),
        AIMessage(
            content=" ".join(_FANOUT_PASSAGES[issuer] for issuer in FANOUT_ROSTER)
        ),
    ]


def _drive_fanout(
    *,
    restriction: ScopeRestriction | None,
    attachment_service: Any = None,
    thread_id: str = "t-fanout-scope",
) -> tuple[_CountingCompanyPort, dict[str, Any]]:
    """Run one real fan-out through ``compile_graph`` and report the port.

    The shipped factory with its ``Send`` topology is what is under test: the
    ruling has to come from the branch node the router actually dispatches, not
    from a hand-built call. The counting port comes back so a case can assert
    the CALL COUNT, which is the "before, not after" proof.
    """
    port = _fanout_port()
    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=ScriptedChatModel(script=_fanout_script()),
        adapter=port,  # type: ignore[arg-type]
        known_document_ids=set(FANOUT_DOCUMENT_IDS),
        attachment_service=attachment_service,
        scope_restriction=restriction,
    )
    state = graph.invoke(
        reset_input_state(FANOUT_QUESTION),
        config={"configurable": {"thread_id": thread_id}},
    )
    return port, dict(state)


def _fanout_rows(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The joined envelope's per-issuer rows, in roster order."""
    return list(state["issuer_fanout"]["issuers"])


def _fanout_tool_payload(state: Mapping[str, Any]) -> dict[str, Any]:
    """The one ``research_issuers`` answer the join put on the history."""
    messages = [
        message
        for message in state["messages"]
        if isinstance(message, ToolMessage)
        and getattr(message, "name", "") == ISSUER_FANOUT_TOOL
    ]
    assert len(messages) == 1, "one fan-out call, one joined answer"
    return json.loads(str(messages[0].content))


def _fanout_hit_row(issuer: str) -> dict[str, Any]:
    """One roster row the scopeless fan-out has always answered with."""
    return {
        "issuer": issuer,
        "status": "hit",
        "chunk_count": 1,
        "error": "",
        "locators": [
            {
                "document_id": _fanout_document_id(issuer),
                "version": _fanout_version(issuer),
                "unit": 1,
            }
        ],
    }


def _gated_company_probe(operation: str, root: Path) -> dict[str, Any]:
    """Run one company-source boundary under an exclusive scope, no store.

    Every wired company read answers the same refusal; a boundary that lost
    its seam would answer real content or an impl error instead. The
    attachment service is deliberately absent throughout: the branch ruling
    comes from what the operator declared, never from a store being present.
    """
    restriction = _exclusive(("att-a",))
    if operation == "list_files":
        catalog = root / "catalog.json"
        record_filings([_catalog_row()], catalog_path=catalog)
        return _make_list_files_tool(
            catalog, scope_restriction=restriction
        ).invoke({})
    if operation == "get_xbrl_fact":
        command = _make_get_xbrl_fact_tool(
            _seed_facts(root), scope_restriction=restriction
        ).invoke(
            {
                "name": "get_xbrl_fact",
                "args": {"concept": "us-gaap:Revenues", "period": "2023-Q3-YTD"},
                "id": "c-guard",
                "type": "tool_call",
            }
        )
        return json.loads(str(command.update["messages"][0].content))
    if operation == "compare_facts":
        return _make_compare_facts_tool(
            _seed_facts(root), scope_restriction=restriction
        ).invoke({"concept": "us-gaap:Revenues", "periods": ["2023-Q3-YTD"]})
    if operation == "read_source":
        _threads, _store, thread_id = _seed_threaded([("att-a", ATTACHMENT_TEXT)])
        tool = _make_read_source_tool(
            _RecordingReader(), None, scope_restriction=restriction
        )
        return _read_source_payload(
            _invoke_read_source(
                tool, source_ref=_company_ref(), thread_id=thread_id
            )
        )
    if operation == "retrieve":
        _threads, _store, thread_id = _seed_threaded([("att-a", ATTACHMENT_TEXT)])
        tool = _make_retrieve_tool(_company_port(), scope_restriction=restriction)
        return _invoke_retrieve(tool, thread_id=thread_id, args={"query": QUERY})
    if operation == "research_issuers":
        # The one company boundary reached OUTSIDE a tool factory: the branch
        # calls the retrieve port itself, so it reports its refusal on the
        # finding row's ``error`` (the join folds an unknown status into
        # ``empty``). The carrier is lifted out of the joined row here, and the
        # no-call proof rides with it.
        port, state = _drive_fanout(restriction=restriction)
        assert port.queries == []
        codes = [row["error"] for row in _fanout_rows(state)]
        assert codes == [SCOPE_COMPANY_NOT_PERMITTED] * len(FANOUT_ROSTER)
        return {"error": codes[0]}
    tool, args = _new_boundary_tool(operation, root, restriction=restriction)
    return tool.invoke(args)


# ---------------------------------------------------------------------------
# read_source: a known locator cannot bypass the scope (SC-3)
# ---------------------------------------------------------------------------


def test_known_out_of_scope_locator_is_refused_before_the_read_leg_runs() -> None:
    _threads, store, thread_id = _store_with_ready("att-in-scope")
    leg = _RecordingReader()
    tool = _make_read_source_tool(
        None,
        lambda bound: leg,
        attachment_service=_Service(store),
    )

    command = _invoke_read_source(
        tool, source_ref=_attachment_ref("att-outsider"), thread_id=thread_id
    )

    assert _read_source_payload(command) == {
        "error": SCOPE_ATTACHMENT_NOT_PERMITTED
    }
    # The "before, not after" proof: the read leg was never invoked, so no
    # out-of-scope text was ever fetched and then filtered.
    assert leg.calls == []
    assert command.update["evidence"] == []


def test_in_scope_locator_still_reads_normally() -> None:
    _threads, store, thread_id = _store_with_ready("att-1")
    tool = _make_read_source_tool(
        None,
        lambda bound: AttachmentSourceReader(store, bound),
        attachment_service=_Service(store),
    )

    command = _invoke_read_source(
        tool, source_ref=_attachment_ref("att-1", version="1"), thread_id=thread_id
    )

    payload = _read_source_payload(command)
    assert payload["text"] == ATTACHMENT_TEXT
    # The granted slice earns exactly one attachment-typed evidence item.
    evidence = command.update["evidence"]
    assert [item.attachment_id for item in evidence] == ["att-1"]


def test_a_restriction_narrows_the_readable_attachment_branch() -> None:
    _threads, store, thread_id = _seed_threaded(
        [("att-a", ATTACHMENT_TEXT), ("att-b", "another body")]
    )
    tool = _make_read_source_tool(
        None,
        lambda bound: AttachmentSourceReader(store, bound),
        attachment_service=_Service(store),
        scope_restriction=ScopeRestriction(("att-a",), "explicit"),
    )

    refused = _read_source_payload(
        _invoke_read_source(
            tool, source_ref=_attachment_ref("att-b"), thread_id=thread_id
        )
    )
    permitted = _read_source_payload(
        _invoke_read_source(
            tool, source_ref=_attachment_ref("att-a"), thread_id=thread_id
        )
    )

    assert refused == {"error": SCOPE_ATTACHMENT_NOT_PERMITTED}
    assert permitted["text"] == ATTACHMENT_TEXT


# ---------------------------------------------------------------------------
# retrieve attachment branch: refuse, never silently drop and search broad
# ---------------------------------------------------------------------------


def test_out_of_scope_retrieval_id_is_refused_and_the_search_never_runs() -> None:
    _threads, store, thread_id = _seed_threaded(
        [("att-a", ATTACHMENT_TEXT), ("att-b", "another body")]
    )
    retriever = _RecordingSearchPort(
        {thread_id: [_attachment_chunk("att-a"), _attachment_chunk("att-b")]}
    )
    tool = _make_retrieve_tool(
        _company_port(),
        attachment_retriever=retriever,
        attachment_service=_Service(store),
        scope_restriction=ScopeRestriction(("att-a",), "explicit"),
    )

    envelope = _invoke_retrieve(
        tool, thread_id=thread_id, args={"query": QUERY, "attachment_ids": ["att-b"]}
    )

    # Refused with the typed code, never dropped so the call searched broadly.
    assert envelope["attachment_error"] == {
        "error": SCOPE_ATTACHMENT_NOT_PERMITTED
    }
    # The search port was never asked — and so names no out-of-scope id.
    assert retriever.calls == []
    # The company branch is never narrowed by the attachment refusal.
    assert [c["source_ref"]["document_id"] for c in envelope["chunks"]] == [
        "AAPL-2023Q3"
    ]


def test_company_branch_still_works_with_a_restriction_in_force() -> None:
    """ADR 0088:10 no-regression: naming attachments narrows ATTACHMENTS ONLY."""
    _threads, store, thread_id = _seed_threaded(
        [("att-a", ATTACHMENT_TEXT), ("att-b", "another body")]
    )
    retriever = _RecordingSearchPort(
        {thread_id: [_attachment_chunk("att-a"), _attachment_chunk("att-b")]}
    )
    company = _CountingCompanyPort(_company_port())
    tool = _make_retrieve_tool(
        company,  # type: ignore[arg-type]
        attachment_retriever=retriever,
        attachment_service=_Service(store),
        scope_restriction=_naming_only(("att-a",)),
    )

    envelope = _invoke_retrieve(tool, thread_id=thread_id, args={"query": QUERY})

    # The corpus really was searched — the branch stayed eligible rather than
    # being refused or quietly skipped.
    assert company.call_count == 1
    assert [c["source_ref"]["document_id"] for c in envelope["chunks"]] == [
        "AAPL-2023Q3"
    ]
    # The attachment branch narrows to the restriction even though the call
    # named no id: the search never saw the restricted-out id.
    assert retriever.calls[0]["attachment_ids"] == ["att-a"]
    assert [
        c["source_ref"]["attachment_id"] for c in envelope["attachment_chunks"]
    ] == ["att-a"]


def test_a_successful_empty_search_is_not_a_scope_error() -> None:
    _threads, store, thread_id = _store_with_ready("att-1")
    retriever = _RecordingSearchPort({thread_id: []})
    tool = _make_retrieve_tool(
        _company_port(),
        attachment_retriever=retriever,
        attachment_service=_Service(store),
    )

    envelope = _invoke_retrieve(
        tool, thread_id=thread_id, args={"query": QUERY, "attachment_ids": ["att-1"]}
    )

    assert envelope["attachment_chunks"] == []
    assert "attachment_error" not in envelope


# ---------------------------------------------------------------------------
# unwired graph: no attachment service, pre-scope behavior byte for byte
# ---------------------------------------------------------------------------


def test_an_unwired_graph_passes_the_named_ids_through_unchanged() -> None:
    retriever = _RecordingSearchPort({})
    tool = _make_retrieve_tool(
        _company_port(), attachment_retriever=retriever
    )

    envelope = _invoke_retrieve(
        tool,
        thread_id="any-thread",
        args={"query": QUERY, "attachment_ids": ["att-outsider"]},
    )

    # No gate: the id the model named reaches the port verbatim and no scope
    # error is invented.
    assert retriever.calls[0]["attachment_ids"] == ["att-outsider"]
    assert "attachment_error" not in envelope


def test_an_unwired_graph_reads_an_out_of_scope_locator_on_the_leg() -> None:
    leg = _RecordingReader()
    tool = _make_read_source_tool(None, lambda bound: leg)

    _invoke_read_source(
        tool, source_ref=_attachment_ref("att-outsider"), thread_id="any-thread"
    )

    # With no authority seam, the leg is consulted exactly as before.
    assert len(leg.calls) == 1


# ---------------------------------------------------------------------------
# distinct outcomes: one code per cause, no two collapsed (SC-4)
# ---------------------------------------------------------------------------


def test_every_reachable_outcome_maps_to_its_own_code() -> None:
    _threads, store, thread_id = _store_with_ready("att-1")
    # A not-ready row: created but never published ready (version 0).
    store.create(
        thread_id=thread_id,
        attachment_id="att-pending",
        filename="memo.txt",
        size_bytes=1_024,
        sha256="cd" * 32,
        object_key=f"threads/{thread_id}/att-pending",
    )

    def read(payload_ref: Mapping[str, Any]) -> dict[str, Any]:
        tool = _make_read_source_tool(
            None,
            lambda bound: AttachmentSourceReader(store, bound),
            attachment_service=_Service(store),
        )
        return _read_source_payload(
            _invoke_read_source(tool, source_ref=payload_ref, thread_id=thread_id)
        )

    # permitted: no error. denied/version_stale: in scope, wrong version.
    permitted = read(_attachment_ref("att-1", version="1"))
    stale = read(_attachment_ref("att-1", version="0"))
    # unavailable: authorized but not ready (readiness is not permission).
    not_ready = read(_attachment_ref("att-pending", version="0"))
    # denied/not_permitted: outside the effective selection.
    not_permitted = read(_attachment_ref("att-ghost"))

    assert "error" not in permitted
    assert stale == {"error": SCOPE_ATTACHMENT_VERSION_STALE}
    assert not_ready == {"error": SCOPE_ATTACHMENT_NOT_READY}
    assert not_permitted == {"error": SCOPE_ATTACHMENT_NOT_PERMITTED}


def test_a_faulting_authority_is_dependency_failed_not_a_denial() -> None:
    _threads, store, thread_id = _store_with_ready("att-1")
    flaky = _FlakyStore(store, healthy_gets=1)
    tool = _make_read_source_tool(
        None,
        lambda bound: _RecordingReader(),
        attachment_service=_Service(flaky),  # type: ignore[arg-type]
        # The restriction resolves from the first (healthy) read; the
        # boundary's own re-resolve is the one that faults.
        scope_restriction=ScopeRestriction(("att-1",), "explicit"),
    )

    payload = _read_source_payload(
        _invoke_read_source(
            tool, source_ref=_attachment_ref("att-1", version="1"), thread_id=thread_id
        )
    )

    assert payload == {"error": SCOPE_DEPENDENCY_FAILED}


def test_each_truthful_outcome_carries_its_own_code() -> None:
    """SC-4: each cause, driven through the REAL tools, gets its own code.

    Not a set self-check: a permitted read, three distinct refusals
    (version-stale, not-ready, not-permitted), a dependency fault, and a
    successful empty are each produced by the boundary and compared — no two
    causes collapse, and the empty success carries no refusal code.
    """
    _threads, store, thread_id = _seed_threaded([("att-a", ATTACHMENT_TEXT)])
    # A not-ready row: created but never published ready.
    store.create(
        thread_id=thread_id,
        attachment_id="att-pending",
        filename="memo.txt",
        size_bytes=1_024,
        sha256="cd" * 32,
        object_key=f"threads/{thread_id}/att-pending",
    )

    def read(
        ref: Mapping[str, Any],
        *,
        service: Any = None,
        restriction: ScopeRestriction | None = None,
    ) -> dict[str, Any]:
        tool = _make_read_source_tool(
            None,
            lambda bound: _RecordingReader(),
            attachment_service=service if service is not None else _Service(store),
            scope_restriction=restriction,
        )
        return _read_source_payload(
            _invoke_read_source(tool, source_ref=ref, thread_id=thread_id)
        )

    permitted = read(_attachment_ref("att-a", version="1"))
    stale = read(_attachment_ref("att-a", version="0"))
    not_ready = read(_attachment_ref("att-pending", version="0"))
    not_permitted = read(_attachment_ref("att-ghost"))
    # The restriction resolves from the first (healthy) read; the boundary's
    # own re-resolve is the one that faults.
    dependency_failed = read(
        _attachment_ref("att-a", version="1"),
        service=_Service(_FlakyStore(store, healthy_gets=1)),
        restriction=ScopeRestriction(("att-a",), "explicit"),
    )

    # A healthy index that answers with nothing: a successful empty.
    retriever = _RecordingSearchPort({thread_id: []})
    empty_tool = _make_retrieve_tool(
        _company_port(),
        attachment_retriever=retriever,
        attachment_service=_Service(store),
    )
    empty = _invoke_retrieve(
        empty_tool, thread_id=thread_id, args={"query": QUERY, "attachment_ids": ["att-a"]}
    )

    assert "error" not in permitted
    refusal_codes = {
        stale["error"],
        not_ready["error"],
        not_permitted["error"],
        dependency_failed["error"],
    }
    assert refusal_codes == {
        SCOPE_ATTACHMENT_VERSION_STALE,
        SCOPE_ATTACHMENT_NOT_READY,
        SCOPE_ATTACHMENT_NOT_PERMITTED,
        SCOPE_DEPENDENCY_FAILED,
    }
    # The empty success is not a refusal, and the refusals are not empty.
    assert "attachment_error" not in empty
    assert empty["attachment_chunks"] == []


# ---------------------------------------------------------------------------
# a faulted enumeration is a dependency failure, never an empty (SC-4)
# ---------------------------------------------------------------------------


def test_a_faulting_enumeration_refuses_retrieve_with_dependency_failed() -> None:
    """A faulting ``list_attachments`` is NOT a successful empty.

    ``_default_ids`` swallows the fault into an empty selection; the scope
    records it as a resolution fault so the retrieve boundary answers
    ``scope_dependency_failed`` instead of searching with ``[]`` and reporting
    a successful empty. A dependency failure and "authority answered, nothing
    matched" are different facts (SC-4).
    """
    _threads, store, thread_id = _store_with_ready("att-1")
    faulting = _ListFaultStore(store)
    retriever = _RecordingSearchPort({thread_id: [_attachment_chunk("att-1")]})
    tool = _make_retrieve_tool(
        _company_port(),
        attachment_retriever=retriever,
        attachment_service=_Service(faulting),  # type: ignore[arg-type]
    )

    envelope = _invoke_retrieve(
        tool, thread_id=thread_id, args={"query": QUERY}
    )

    assert envelope["attachment_error"] == {"error": SCOPE_DEPENDENCY_FAILED}
    # NOT the skip shape the fault would collapse into.
    assert "attachment_chunks" not in envelope
    # And the search port was never asked, so nothing was queried on an
    # unconsulted authority.
    assert retriever.calls == []


# ---------------------------------------------------------------------------
# the attachment-branch admit_evidence re-gate (plan T3)
# ---------------------------------------------------------------------------


def test_a_leaked_post_fetch_hit_is_shown_but_never_admitted_as_evidence() -> None:
    """The admission boundary re-gates each hit, not just the request.

    A stale index answers with an id the host never asked for; it reaches the
    model as display text (the branch did look), but the scope's SECOND check
    refuses to admit it, so it can never ground a citation.
    """
    _threads, store, thread_id = _seed_threaded(
        [("att-a", ATTACHMENT_TEXT), ("att-b", "a body outside the effective set")]
    )
    leaking = _LeakingSearchPort([_attachment_chunk("att-b")])
    tool = _make_retrieve_tool(
        _company_port(),
        attachment_retriever=leaking,
        attachment_service=_Service(store),
        scope_restriction=ScopeRestriction(("att-a",), "explicit"),
    )

    command = tool.invoke(
        {
            "name": "retrieve",
            "args": {"query": QUERY},
            "id": "c-retrieve",
            "type": "tool_call",
        },
        config={"configurable": {"thread_id": thread_id}},
    )
    envelope = json.loads(str(command.update["messages"][0].content))

    # The hit's text is published (display only)...
    assert [c["source_ref"]["attachment_id"] for c in envelope["attachment_chunks"]] == [
        "att-b"
    ]
    # ...but the re-gate admitted NO attachment evidence for the out-of-scope
    # id, so it cannot ground a citation. (The company branch's own hit still
    # earns its evidence — the two branches are independent.)
    attachment_evidence = [
        item for item in command.update["evidence"] if item.kind == ATTACHMENT_KIND
    ]
    assert attachment_evidence == []


# ---------------------------------------------------------------------------
# the visual-read boundary (plan T3 / ADR 0089)
# ---------------------------------------------------------------------------


def _invoke_visual(tool: Any, *, attachment_id: str, thread_id: str) -> Any:
    return tool.invoke(
        {
            "name": "read_visual_detail",
            "args": {
                "attachment_id": attachment_id,
                "page": 1,
                "chart": "Revenue by segment",
                "series": "Services",
                "category": "Q3 2024",
            },
            "id": "c-visual",
            "type": "tool_call",
        },
        config={"configurable": {"thread_id": thread_id}},
    )


def test_read_visual_detail_refuses_an_out_of_scope_id_before_any_vision_call() -> None:
    _threads, store, thread_id = _seed_threaded(
        [("att-a", ATTACHMENT_TEXT), ("att-b", "another body")]
    )
    built: list[str] = []

    def faces_factory(bound: str, _ledger: Any) -> Any:
        # A page must never be drawn for a refused ask; building the lane here
        # would be the "after the fact" filter the spec forbids.
        built.append(bound)
        raise AssertionError("the vision lane must not be built for a refusal")

    tool = _make_read_visual_detail_tool(
        faces_factory,
        attachment_service=_Service(store),
        scope_restriction=ScopeRestriction(("att-a",), "explicit"),
    )

    command = _invoke_visual(tool, attachment_id="att-b", thread_id=thread_id)

    payload = json.loads(str(command.update["messages"][0].content))
    assert payload == {"error": SCOPE_ATTACHMENT_NOT_PERMITTED}
    assert built == []
    assert command.update["visual_claims"] == []


def test_read_visual_detail_refuses_a_not_ready_id_before_any_vision_call() -> None:
    _threads, store, thread_id = _store_with_ready("att-1")
    # The preparation lifecycle leaves it not-ready (no new version minted).
    store.mark_failed(attachment_id="att-1", version=1, reason="parse failed")
    built: list[str] = []

    def faces_factory(bound: str, _ledger: Any) -> Any:
        built.append(bound)
        raise AssertionError("the vision lane must not be built for a refusal")

    tool = _make_read_visual_detail_tool(
        faces_factory, attachment_service=_Service(store)
    )

    command = _invoke_visual(tool, attachment_id="att-1", thread_id=thread_id)

    payload = json.loads(str(command.update["messages"][0].content))
    # Availability, not permission: distinct from a denial.
    assert payload == {"error": SCOPE_ATTACHMENT_NOT_READY}
    assert built == []
    assert command.update["visual_claims"] == []


# ---------------------------------------------------------------------------
# the company boundaries (plan T3): the fact / comparison read seam
# ---------------------------------------------------------------------------


def test_a_locator_tool_gate_refusal_short_circuits_the_impl() -> None:
    """The company seam gates BEFORE the implementation is even imported.

    A refusal from ``gate`` must return without any read. The spec below names
    an implementation attribute that does not exist: if the wrapper had read
    before gating, it would raise ``AttributeError``; it must instead return the
    refusal untouched.

    The gate also decides on the request and answers a narrowing beside the
    refusal, so it is invoked with both and this probe returns the permitted
    shape — the refusal with no corpus narrowing — which is what the wrapper
    forwards unchanged.
    """
    spec = _LocatorToolSpec(
        name="probe_scope",
        description="probe",
        signature="concept: str = ''",
        impl_module="fin.host.tools.compare_facts",
        impl_attr="__no_such_impl_so_a_read_would_raise__",
        factory_kwargs={},
        gate=lambda _config, _request: ({"error": SCOPE_DEPENDENCY_FAILED}, None),
    )

    tool = _make_locator_tool(spec)

    assert tool.invoke({"concept": "us-gaap:Revenues"}) == {
        "error": SCOPE_DEPENDENCY_FAILED
    }


def test_the_company_seam_answers_the_branch_ruling_it_promised() -> None:
    """The company seam decides in BOTH directions, on real authority.

    The seam was once pinned as wired-but-unable-to-refuse. That is false now:
    ``company_allowed`` is a resolved fact, so a restriction that only names
    attachments keeps the corpus readable (ADR 0088:10) and a restriction the
    operator declared exclusive refuses it with the denial code the branch
    ruling owns. A graph with nothing in force keeps its pre-scope answer.
    """
    _threads, store, _thread_id = _seed_threaded([("att-a", ATTACHMENT_TEXT)])

    naming_only = _company_scope_refusal(
        _Service(store), _naming_only(("att-a",)), "compare_facts"
    )
    exclusive = _company_scope_refusal(
        _Service(store), _exclusive(("att-a",)), "compare_facts"
    )
    nothing_in_force = _company_scope_refusal(None, None, "get_xbrl_fact")
    # A restriction in force with no attachment store to consult still leaves
    # the corpus readable: an absent authority must not fake an outage for the
    # branch it never governs.
    unwired_naming_only = _company_scope_refusal(
        None, _naming_only(("att-a",)), "get_xbrl_fact"
    )

    assert naming_only is None
    assert exclusive == {"error": SCOPE_COMPANY_NOT_PERMITTED}
    assert nothing_in_force is None
    assert unwired_naming_only is None


def test_an_operation_no_adapter_is_proven_to_express_fails_closed() -> None:
    """Capability is membership in the PROVEN set, never an assumed grant.

    Same wiring, same restriction — only the operation name changes — so the
    refusal can only come from the operation being outside
    ``SCOPE_EXPRESSIBLE_OPERATIONS``. A new source operation therefore fails
    closed until its adapter's expressibility is proven, instead of silently
    reaching the corpus with the restriction unexpressed.
    """
    _threads, store, _thread_id = _seed_threaded([("att-a", ATTACHMENT_TEXT)])

    unproven = _company_scope_refusal(
        _Service(store), _naming_only(("att-a",)), "unproven_company_operation"
    )
    proven = _company_scope_refusal(
        _Service(store), _naming_only(("att-a",)), "list_files"
    )

    assert unproven == {"error": SCOPE_OPERATION_UNSUPPORTED}
    assert proven is None


def test_get_xbrl_fact_reports_a_company_refusal_without_reading(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When the seam produces a refusal, the fact tool returns it, reading nothing.

    The seam produces a ``denied`` refusal through real authority (see
    :func:`test_get_xbrl_fact_refuses_an_exclusive_restriction_before_the_table`);
    this case drives the OTHER refusal cause — a dependency fault — out of band
    to prove the boundary honors any gate refusal before the implementation
    runs, so a faulted authority can never degrade into a fact read.
    """
    from fin.host import graph as graph_module

    monkeypatch.setattr(
        graph_module,
        "_company_scope_refusal",
        lambda *args, **kwargs: {"error": SCOPE_DEPENDENCY_FAILED},
    )

    _impl_must_not_run(
        monkeypatch,
        "fin.host.tools.get_xbrl_fact",
        "get_xbrl_fact",
        "the fact table must not be read on a refusal",
    )

    tool = _make_get_xbrl_fact_tool(tmp_path, attachment_service=object())
    command = tool.invoke(
        {
            "name": "get_xbrl_fact",
            "args": {"concept": "us-gaap:Revenues", "period": "2023-Q3-YTD"},
            "id": "c-fact",
            "type": "tool_call",
        }
    )

    message = command.update["messages"][0]
    assert json.loads(str(message.content)) == {"error": SCOPE_DEPENDENCY_FAILED}


def test_compare_facts_company_read_is_permitted_with_a_restriction_in_force(
    tmp_path: Path,
) -> None:
    """ADR 0088:10 no-regression: naming attachments never narrows the corpus.

    The comparison reads the company fact table through the wired seam; with an
    attachment restriction in force the read is still permitted, and the grid
    carries the fact row — the assertion a refusal would fail, not merely that
    some code ran.
    """
    facts_root = _seed_facts(tmp_path)
    _threads, store, _thread_id = _seed_threaded([("att-a", ATTACHMENT_TEXT)])

    tool = _make_compare_facts_tool(
        facts_root,
        attachment_service=_Service(store),
        scope_restriction=_naming_only(("att-a",)),
    )

    result = tool.invoke(
        {
            "concept": "us-gaap:Revenues",
            "periods": ["2023-Q3-YTD"],
        }
    )

    # The company read ran and returned its row: no refusal code anywhere.
    assert "error" not in result
    cells = result["comparison"]["rows"][0]["cells"]
    assert cells[0]["status"] == "hit"
    assert cells[0]["value"] == 383_285_000_000.0


# ---------------------------------------------------------------------------
# the company branch under an EXCLUSIVE restriction (decision 5 / SC-3):
# refused at every corpus boundary before any company data crosses
# ---------------------------------------------------------------------------


def test_an_exclusive_restriction_refuses_the_corpus_before_the_port_is_reached() -> None:
    """A declared-exclusive scope never issues the corpus search at all.

    "Use only these attachments" removes the company branch from the approved
    selection, and honoring it means the retrieve adapter is never reached —
    a post-fetch filter would already have crossed the boundary. The refusal
    carries the branch's own code rather than the miss shape a filtered-empty
    would have produced, and the attachment branch runs independently.
    """
    _threads, store, thread_id = _seed_threaded(
        [("att-a", ATTACHMENT_TEXT), ("att-b", "another body")]
    )
    retriever = _RecordingSearchPort(
        {thread_id: [_attachment_chunk("att-a"), _attachment_chunk("att-b")]}
    )
    company = _CountingCompanyPort(_company_port())
    tool = _make_retrieve_tool(
        company,  # type: ignore[arg-type]
        attachment_retriever=retriever,
        attachment_reader_factory=lambda bound: AttachmentSourceReader(store, bound),
        attachment_service=_Service(store),
        scope_restriction=_exclusive(("att-a",)),
    )

    command = tool.invoke(
        {
            "name": "retrieve",
            "args": {"query": QUERY},
            "id": "c-retrieve",
            "type": "tool_call",
        },
        config={"configurable": {"thread_id": thread_id}},
    )
    envelope = json.loads(str(command.update["messages"][0].content))

    assert envelope["error"] == SCOPE_COMPANY_NOT_PERMITTED
    assert "chunks" not in envelope
    # The "before, not after" proof: no corpus call was ever issued.
    assert company.queries == []
    # The attachment branch is independent: it searched, and found.
    assert retriever.calls[0]["attachment_ids"] == ["att-a"]
    assert [
        c["source_ref"]["attachment_id"] for c in envelope["attachment_chunks"]
    ] == ["att-a"]
    # No company text was admitted as evidence either.
    assert [item.kind for item in command.update["evidence"]] == [ATTACHMENT_KIND]


def test_list_files_refuses_an_exclusive_restriction_without_listing_rows(
    tmp_path: Path,
) -> None:
    """Enumeration is a source boundary: an exclusive scope answers before a
    single catalog row is read.

    The same table answers in full for a graph with nothing in force, so the
    refusal below cannot be an artifact of an empty corpus — the catalog is set
    up specifically so it WOULD have answered.
    """
    catalog_path = tmp_path / "catalog.json"
    record_filings([_catalog_row()], catalog_path=catalog_path)
    _threads, store, _thread_id = _seed_threaded([("att-a", ATTACHMENT_TEXT)])

    open_tool = _make_list_files_tool(catalog_path)
    refused = _make_list_files_tool(
        catalog_path,
        attachment_service=_Service(store),
        scope_restriction=_exclusive(("att-a",)),
    ).invoke({})

    assert [r["document_id"] for r in open_tool.invoke({})["filings"]] == [
        "INJECTED-2023Q1"
    ]
    assert refused == {"error": SCOPE_COMPANY_NOT_PERMITTED}
    # NOT the empty catalog a post-filter would have produced.
    assert "filings" not in refused


def test_list_files_lists_the_catalog_under_a_naming_only_restriction(
    tmp_path: Path,
) -> None:
    """ADR 0088:10 on the enumeration boundary: naming attachments leaves the
    corpus enumerable, so the requested comparison can still find its filings."""
    catalog_path = tmp_path / "catalog.json"
    record_filings([_catalog_row()], catalog_path=catalog_path)
    _threads, store, _thread_id = _seed_threaded([("att-a", ATTACHMENT_TEXT)])

    tool = _make_list_files_tool(
        catalog_path,
        attachment_service=_Service(store),
        scope_restriction=_naming_only(("att-a",)),
    )
    filings = tool.invoke({"company": "INJECTED"})

    assert "error" not in filings
    assert [r["document_id"] for r in filings["filings"]] == ["INJECTED-2023Q1"]


def test_get_xbrl_fact_refuses_an_exclusive_restriction_before_the_table(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Real wiring produces the denial: the fact implementation is never run.

    Nothing here patches the seam — the exclusive restriction alone drives it.
    The implementation is patched to raise so an unreached fact table is proven
    by the absence of the read, not by the envelope alone.
    """
    _impl_must_not_run(
        monkeypatch,
        "fin.host.tools.get_xbrl_fact",
        "get_xbrl_fact",
        "the fact table must not be read under an exclusive scope",
    )
    _threads, store, _thread_id = _seed_threaded([("att-a", ATTACHMENT_TEXT)])

    tool = _make_get_xbrl_fact_tool(
        tmp_path / "facts",
        attachment_service=_Service(store),
        scope_restriction=_exclusive(("att-a",)),
    )
    command = tool.invoke(
        {
            "name": "get_xbrl_fact",
            "args": {"concept": "us-gaap:Revenues", "period": "2023-Q3-YTD"},
            "id": "c-fact",
            "type": "tool_call",
        }
    )

    assert json.loads(str(command.update["messages"][0].content)) == {
        "error": SCOPE_COMPANY_NOT_PERMITTED
    }


def test_get_xbrl_fact_reads_the_table_under_a_naming_only_restriction(
    tmp_path: Path,
) -> None:
    """The corpus fact row still answers a naming-only restriction."""
    facts_root = _seed_facts(tmp_path)
    _threads, store, _thread_id = _seed_threaded([("att-a", ATTACHMENT_TEXT)])

    tool = _make_get_xbrl_fact_tool(
        facts_root,
        attachment_service=_Service(store),
        scope_restriction=_naming_only(("att-a",)),
    )
    command = tool.invoke(
        {
            "name": "get_xbrl_fact",
            "args": {"concept": "us-gaap:Revenues", "period": "2023-Q3-YTD"},
            "id": "c-fact",
            "type": "tool_call",
        }
    )
    payload = json.loads(str(command.update["messages"][0].content))

    assert "error" not in payload
    assert payload["value"] == 383_285_000_000


def test_compare_facts_refuses_an_exclusive_restriction_before_the_grid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The comparison boundary honors the branch ruling without touching a row."""
    _impl_must_not_run(
        monkeypatch,
        "fin.host.tools.compare_facts",
        "compare_facts",
        "the comparison grid must not be built under an exclusive scope",
    )
    _threads, store, _thread_id = _seed_threaded([("att-a", ATTACHMENT_TEXT)])

    tool = _make_compare_facts_tool(
        _seed_facts(tmp_path),
        attachment_service=_Service(store),
        scope_restriction=_exclusive(("att-a",)),
    )
    result = tool.invoke(
        {"concept": "us-gaap:Revenues", "periods": ["2023-Q3-YTD"]}
    )

    assert result == {"error": SCOPE_COMPANY_NOT_PERMITTED}


def test_an_exclusive_restriction_refuses_a_company_locator_before_the_read_leg() -> None:
    """A known company locator cannot bypass the branch ruling (SC-3).

    The corpus triple is a real, resolvable locator — the only fact that
    changes is the branch it belongs to — so the refusal below is the scope's
    doing, and the read leg proves it by never being consulted.
    """
    _threads, store, thread_id = _seed_threaded([("att-a", ATTACHMENT_TEXT)])
    leg = _RecordingReader()
    tool = _make_read_source_tool(
        leg,  # type: ignore[arg-type]
        None,
        attachment_service=_Service(store),
        scope_restriction=_exclusive(("att-a",)),
    )

    payload = _read_source_payload(
        _invoke_read_source(tool, source_ref=_company_ref(), thread_id=thread_id)
    )

    assert payload == {"error": SCOPE_COMPANY_NOT_PERMITTED}
    assert leg.calls == []


def test_a_company_locator_stays_readable_under_a_naming_only_restriction() -> None:
    """ADR 0088:10 on the direct-read boundary: naming attachments does not put
    the corpus out of reach, so the same triple still reads its slice."""
    _threads, store, thread_id = _seed_threaded([("att-a", ATTACHMENT_TEXT)])
    company_ref = _company_ref()
    leg = _RecordingReader(
        {
            "source_ref": company_ref,
            "text": COMPANY_TEXT,
            "next_position": len(COMPANY_TEXT),
        }
    )
    tool = _make_read_source_tool(
        leg,  # type: ignore[arg-type]
        None,
        attachment_service=_Service(store),
        scope_restriction=_naming_only(("att-a",)),
    )

    payload = _read_source_payload(
        _invoke_read_source(tool, source_ref=company_ref, thread_id=thread_id)
    )

    assert payload["text"] == COMPANY_TEXT
    assert len(leg.calls) == 1


# ---------------------------------------------------------------------------
# an unwired graph with a restriction IN FORCE: the gate still runs
# ---------------------------------------------------------------------------


def test_an_unwired_graph_enforces_a_restriction_in_force_as_a_dependency_failure() -> None:
    """A missing attachment store cannot bypass an explicit restriction.

    The old gap was structural: with no ``attachment_service`` the boundary
    skipped the gate and released the model's own ids into the pre-scope broad
    path — a saved intent the host then never checked. A restriction in force
    now resolves through an authority whose every source read raises, so both
    call shapes answer the dependency-failure code (SC-4: an unconsultable
    authority, not a denial and not a pass) and the retriever is never asked.
    The corpus branch is untouched by that fault: capability is not one of the
    raising reads, so an absent attachment store fakes no outage for it.
    """
    retriever = _RecordingSearchPort({})
    tool = _make_retrieve_tool(
        _company_port(),
        attachment_retriever=retriever,
        scope_restriction=_naming_only(("att-x",)),
    )

    named = _invoke_retrieve(
        tool,
        thread_id="any-thread",
        args={"query": QUERY, "attachment_ids": ["att-outsider"]},
    )
    broad = _invoke_retrieve(tool, thread_id="any-thread", args={"query": QUERY})

    assert named["attachment_error"] == {"error": SCOPE_DEPENDENCY_FAILED}
    assert broad["attachment_error"] == {"error": SCOPE_DEPENDENCY_FAILED}
    # The ids the model named never reached the port, in either shape.
    assert retriever.calls == []
    assert "attachment_chunks" not in named
    # And the company branch still answered.
    assert [c["source_ref"]["document_id"] for c in named["chunks"]] == [
        "AAPL-2023Q3"
    ]


def test_an_exclusive_restriction_refuses_company_tools_without_any_store(
    tmp_path: Path,
) -> None:
    """An exclusive selection holds even with no attachment authority to ask.

    The branch ruling comes from what the operator declared, not from a store,
    so an unwired graph refuses the corpus boundaries rather than falling back
    to the broad path — the company ``authorize`` seam is unconditional, never
    contingent on a store being present. With nothing in force the same wiring
    enumerates exactly as it did before scope existed.
    """
    catalog_path = tmp_path / "catalog.json"
    record_filings([_catalog_row()], catalog_path=catalog_path)
    company = _CountingCompanyPort(_company_port())

    refused_listing = _make_list_files_tool(
        catalog_path, scope_restriction=_exclusive(("att-a",))
    ).invoke({})
    unwired_search = _make_retrieve_tool(
        company,  # type: ignore[arg-type]
        scope_restriction=_exclusive(("att-a",)),
    )
    refused_retrieve = _invoke_retrieve(
        unwired_search, thread_id="any-thread", args={"query": QUERY}
    )
    # A reached comparison impl answers a grid, never a scope code, so this
    # refusal also proves the locator wrapper gated before the import.
    refused_comparison = _make_compare_facts_tool(
        _seed_facts(tmp_path), scope_restriction=_exclusive(("att-a",))
    ).invoke({"concept": "us-gaap:Revenues", "periods": ["2023-Q3-YTD"]})
    unwired_listing = _make_list_files_tool(catalog_path).invoke({})

    assert refused_listing == {"error": SCOPE_COMPANY_NOT_PERMITTED}
    assert refused_retrieve == {"error": SCOPE_COMPANY_NOT_PERMITTED}
    assert refused_comparison == {"error": SCOPE_COMPANY_NOT_PERMITTED}
    assert company.queries == []
    assert [r["document_id"] for r in unwired_listing["filings"]] == [
        "INJECTED-2023Q1"
    ]


# ---------------------------------------------------------------------------
# input-contract evidence on the scope surface (decisions 3 + 4, SC-2)
# ---------------------------------------------------------------------------


def test_an_empty_selection_is_honored_and_never_broadened() -> None:
    """An empty selection is a NARROWING, not the absence of one.

    A restriction approving no attachment must not be read as "no filter" —
    that is the silent broadening decision 3 forbids. The retrieve branch
    searches the empty approved set and truthfully returns an empty (nothing
    was denied, nothing failed, so no refusal code appears), while the direct
    read of an attachment the thread still holds is refused outright.
    """
    _threads, store, thread_id = _seed_threaded([("att-a", ATTACHMENT_TEXT)])
    restriction = ScopeRestriction((), "explicit")
    retriever = _RecordingSearchPort({thread_id: [_attachment_chunk("att-a")]})
    search_tool = _make_retrieve_tool(
        _company_port(),
        attachment_retriever=retriever,
        attachment_service=_Service(store),
        scope_restriction=restriction,
    )
    read_tool = _make_read_source_tool(
        None,
        lambda bound: AttachmentSourceReader(store, bound),
        attachment_service=_Service(store),
        scope_restriction=restriction,
    )

    envelope = _invoke_retrieve(
        search_tool, thread_id=thread_id, args={"query": QUERY}
    )
    refused = _read_source_payload(
        _invoke_read_source(
            read_tool, source_ref=_attachment_ref("att-a"), thread_id=thread_id
        )
    )

    assert retriever.calls[0]["attachment_ids"] == []
    assert envelope["attachment_chunks"] == []
    assert "attachment_error" not in envelope
    # An attachment-only empty selection leaves the corpus alone.
    assert [c["source_ref"]["document_id"] for c in envelope["chunks"]] == [
        "AAPL-2023Q3"
    ]
    assert refused == {"error": SCOPE_ATTACHMENT_NOT_PERMITTED}


def test_a_locator_that_names_no_resolvable_source_is_refused_as_unreadable() -> None:
    """Invalid input on the read surface: a name or version no authority can
    honor is refused as unreadable, and no read is attempted for any shape.

    Whitespace and a non-string name no attachment carries, and a negative,
    non-canonical, or absent version, are all input that cannot address a
    source — distinct from a denial, which would mean the host could have
    named a permitted source and chose not to.
    """
    _threads, store, thread_id = _store_with_ready("att-1")
    leg = _RecordingReader()
    tool = _make_read_source_tool(
        None, lambda bound: leg, attachment_service=_Service(store)
    )
    invalid = [
        {**_attachment_ref("att-1"), "attachment_id": "   "},
        {**_attachment_ref("att-1"), "attachment_id": 1234},
        _attachment_ref("att-1", version=-1),
        _attachment_ref("att-1", version="01"),
        _attachment_ref("att-1", version="+1"),
        _attachment_ref("att-1", version=None),
    ]

    payloads = [
        _read_source_payload(
            _invoke_read_source(tool, source_ref=ref, thread_id=thread_id)
        )
        for ref in invalid
    ]
    # The well-formed locator on the same wiring still reads, so the refusals
    # above cannot be an always-refuse artifact.
    readable = _read_source_payload(
        _invoke_read_source(
            tool, source_ref=_attachment_ref("att-1", version=1), thread_id=thread_id
        )
    )

    assert payloads == [{"error": SCOPE_SOURCE_REF_UNREADABLE}] * len(invalid)
    assert "error" not in readable
    assert len(leg.calls) == 1


def test_a_retrieve_call_naming_a_blank_id_is_refused_not_dropped() -> None:
    """A blank name among the ids the call names invalidates the whole ask.

    Dropping it and searching the rest would silently broaden a call the host
    cannot resolve (decision 3), and it would be the one shape indistinguishable
    from the operator having asked correctly — so the branch is refused and the
    index is never asked for any of them.
    """
    _threads, store, thread_id = _seed_threaded([("att-a", ATTACHMENT_TEXT)])
    retriever = _RecordingSearchPort({thread_id: [_attachment_chunk("att-a")]})
    tool = _make_retrieve_tool(
        _company_port(),
        attachment_retriever=retriever,
        attachment_service=_Service(store),
    )

    envelope = _invoke_retrieve(
        tool,
        thread_id=thread_id,
        args={"query": QUERY, "attachment_ids": ["att-a", "   "]},
    )

    assert envelope["attachment_error"] == {"error": SCOPE_SOURCE_REF_UNREADABLE}
    assert retriever.calls == []


def test_a_restriction_naming_more_ids_than_the_thread_holds_keeps_the_real_ones() -> None:
    """Overflow within the domain: a restriction naming far more ids than the
    thread holds is honored for what authority supports and narrowed for the
    rest.

    Decision 4 — a saved restriction is not a grant. The unsupported names are
    excluded (never searched, never readable), the supported one survives
    intact, and an attachment the thread holds but the restriction did not name
    stays out.
    """
    ghosts = tuple(f"att-ghost-{index}" for index in range(25))
    restriction = _naming_only(("att-a",) + ghosts)
    _threads, store, thread_id = _seed_threaded(
        [("att-a", ATTACHMENT_TEXT), ("att-b", "another body")]
    )
    retriever = _RecordingSearchPort(
        {thread_id: [_attachment_chunk("att-a"), _attachment_chunk("att-b")]}
    )
    search_tool = _make_retrieve_tool(
        _company_port(),
        attachment_retriever=retriever,
        attachment_service=_Service(store),
        scope_restriction=restriction,
    )
    read_tool = _make_read_source_tool(
        None,
        lambda bound: AttachmentSourceReader(store, bound),
        attachment_service=_Service(store),
        scope_restriction=restriction,
    )

    envelope = _invoke_retrieve(
        search_tool, thread_id=thread_id, args={"query": QUERY}
    )
    ghost = _read_source_payload(
        _invoke_read_source(
            read_tool, source_ref=_attachment_ref("att-ghost-0"), thread_id=thread_id
        )
    )
    held = _read_source_payload(
        _invoke_read_source(
            read_tool, source_ref=_attachment_ref("att-b"), thread_id=thread_id
        )
    )

    assert retriever.calls[0]["attachment_ids"] == ["att-a"]
    assert [
        c["source_ref"]["attachment_id"] for c in envelope["attachment_chunks"]
    ] == ["att-a"]
    assert ghost == {"error": SCOPE_ATTACHMENT_NOT_PERMITTED}
    assert held == {"error": SCOPE_ATTACHMENT_NOT_PERMITTED}


def test_an_authority_version_that_moves_after_resolution_is_refused_as_stale() -> None:
    """A re-preparation landing between resolution and the gate is caught.

    The selection said the id was in scope at version 1; current authority now
    answers version 2. The boundary re-checks rather than trusting the
    resolution it just did (decision 4), so the locator is refused as stale and
    no text is read.
    """
    _threads, store, thread_id = _store_with_ready("att-1")
    advancing = _MovingStore(store, moves_after_gets=1, mode="advance")
    leg = _RecordingReader()
    tool = _make_read_source_tool(
        None,
        lambda bound: leg,
        attachment_service=_Service(advancing),  # type: ignore[arg-type]
    )

    payload = _read_source_payload(
        _invoke_read_source(
            tool, source_ref=_attachment_ref("att-1", version=1), thread_id=thread_id
        )
    )

    assert payload == {"error": SCOPE_ATTACHMENT_VERSION_STALE}
    assert leg.calls == []


def test_an_attachment_that_disappears_after_resolution_is_refused_as_unknown() -> None:
    """A tombstone in the same window is its own answer, not a stale version.

    The id was authorized when the scope resolved and is gone when the boundary
    asks: the refusal names the identity as unproven (``unknown``) rather than
    dressing the disappearance up as a version mismatch or passing it on the
    strength of the resolved selection.
    """
    _threads, store, thread_id = _store_with_ready("att-1")
    dropping = _MovingStore(store, moves_after_gets=1, mode="drop")
    leg = _RecordingReader()
    tool = _make_read_source_tool(
        None,
        lambda bound: leg,
        attachment_service=_Service(dropping),  # type: ignore[arg-type]
    )

    payload = _read_source_payload(
        _invoke_read_source(
            tool, source_ref=_attachment_ref("att-1", version=1), thread_id=thread_id
        )
    )

    assert payload == {"error": SCOPE_ATTACHMENT_UNKNOWN}
    assert leg.calls == []


# ---------------------------------------------------------------------------
# redline, extract_tables, three_year_statement: the seam lands before the body
# ---------------------------------------------------------------------------


def test_redline_refuses_an_exclusive_restriction_before_either_body(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A redline opens two company bodies, so both must stay shut on refusal.

    The implementation is patched to raise; the wrapper answers the branch
    ruling before importing it, so the denial proves the bodies were never
    opened rather than merely that the envelope looks like a refusal.
    """
    _impl_must_not_run(
        monkeypatch,
        "fin.host.tools.redline",
        "redline",
        "a redline must not open a body under an exclusive scope",
    )
    _threads, store, _thread_id = _seed_threaded([("att-a", ATTACHMENT_TEXT)])
    fixture = _seed_extracted(tmp_path)
    tool = _make_redline_tool(
        fixture["manifest"],
        fixture["extracted"],
        attachment_service=_Service(store),
        scope_restriction=_exclusive(("att-a",)),
    )
    result = tool.invoke(
        {
            "before_document_id": "ACME-2022Q3",
            "before_version": fixture["before_version"],
            "after_document_id": "ACME-2023Q3",
            "after_version": fixture["after_version"],
        }
    )

    assert result == {"error": SCOPE_COMPANY_NOT_PERMITTED}


def test_redline_opens_both_bodies_under_a_naming_only_restriction(
    tmp_path: Path,
) -> None:
    """ADR 0088:10: naming attachments leaves the corpus redline readable."""
    _threads, store, _thread_id = _seed_threaded([("att-a", ATTACHMENT_TEXT)])
    fixture = _seed_extracted(tmp_path)
    tool = _make_redline_tool(
        fixture["manifest"],
        fixture["extracted"],
        attachment_service=_Service(store),
        scope_restriction=_naming_only(("att-a",)),
    )
    result = tool.invoke(
        {
            "before_document_id": "ACME-2022Q3",
            "before_version": fixture["before_version"],
            "after_document_id": "ACME-2023Q3",
            "after_version": fixture["after_version"],
        }
    )

    assert result["redline"]["schema"] == "fin.redline.v1"
    assert result["redline"]["changes"][0]["before"] == "Revenue increased steadily."


def test_extract_tables_refuses_an_exclusive_restriction_before_the_unit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Extraction reads one frozen unit; a refusal must open none."""
    _impl_must_not_run(
        monkeypatch,
        "fin.host.tools.table_extract",
        "extract_tables",
        "a table read must not open a unit under an exclusive scope",
    )
    _threads, store, _thread_id = _seed_threaded([("att-a", ATTACHMENT_TEXT)])
    fixture = _seed_extracted(tmp_path)
    tool = _make_table_extract_tool(
        fixture["manifest"],
        fixture["extracted"],
        attachment_service=_Service(store),
        scope_restriction=_exclusive(("att-a",)),
    )
    result = tool.invoke(
        {
            "document_id": "ACME-TABLE-2023Q3",
            "version": fixture["table_version"],
            "unit": 1,
        }
    )

    assert result == {"error": SCOPE_COMPANY_NOT_PERMITTED}


def test_extract_tables_reads_the_unit_under_a_naming_only_restriction(
    tmp_path: Path,
) -> None:
    """The corpus table still answers when the restriction only names attachments."""
    _threads, store, _thread_id = _seed_threaded([("att-a", ATTACHMENT_TEXT)])
    fixture = _seed_extracted(tmp_path)
    tool = _make_table_extract_tool(
        fixture["manifest"],
        fixture["extracted"],
        attachment_service=_Service(store),
        scope_restriction=_naming_only(("att-a",)),
    )
    result = tool.invoke(
        {
            "document_id": "ACME-TABLE-2023Q3",
            "version": fixture["table_version"],
            "unit": 1,
        }
    )

    assert result["table_extract"]["schema"] == "fin.table_extract.v1"
    assert result["table_extract"]["tables"][0]["headers"] == [
        "Metric",
        "2023",
        "2022",
    ]


def test_three_year_statement_refuses_an_exclusive_restriction_before_the_facts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The statement reads the same fact table the other two fact tools do."""
    _impl_must_not_run(
        monkeypatch,
        "fin.host.tools.statements",
        "three_year_statement",
        "a statement must not read a fact row under an exclusive scope",
    )
    _threads, store, _thread_id = _seed_threaded([("att-a", ATTACHMENT_TEXT)])
    tool = _make_statement_tool(
        _seed_statement_facts(tmp_path),
        attachment_service=_Service(store),
        scope_restriction=_exclusive(("att-a",)),
    )
    result = tool.invoke(
        {
            "issuer": "ACME",
            "periods": ["2021-FY", "2022-FY", "2023-FY"],
            "concepts": ["us-gaap:Revenues"],
        }
    )

    assert result == {"error": SCOPE_COMPANY_NOT_PERMITTED}


def test_three_year_statement_renders_the_grid_under_a_naming_only_restriction(
    tmp_path: Path,
) -> None:
    """A naming-only restriction leaves the fact grid renderable."""
    _threads, store, _thread_id = _seed_threaded([("att-a", ATTACHMENT_TEXT)])
    tool = _make_statement_tool(
        _seed_statement_facts(tmp_path),
        attachment_service=_Service(store),
        scope_restriction=_naming_only(("att-a",)),
    )
    result = tool.invoke(
        {
            "issuer": "ACME",
            "periods": ["2021-FY", "2022-FY", "2023-FY"],
            "concepts": ["us-gaap:Revenues"],
        }
    )

    assert result["statement"]["schema"] == "fin.statement.v1"
    assert [cell["value"] for cell in result["statement"]["rows"][0]["cells"]] == [
        100,
        120,
        150,
    ]


# ---------------------------------------------------------------------------
# the seam is unconditional: it holds with no store and is inert with nothing
# in force
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "operation", ["redline", "extract_tables", "three_year_statement"]
)
def test_an_exclusive_restriction_refuses_the_new_boundaries_without_any_store(
    operation: str, tmp_path: Path
) -> None:
    """The branch ruling comes from the declaration, not a store being wired."""
    tool, args = _new_boundary_tool(
        operation, tmp_path, restriction=_exclusive(("att-a",))
    )

    assert tool.invoke(args) == {"error": SCOPE_COMPANY_NOT_PERMITTED}


@pytest.mark.parametrize(
    ("operation", "payload_key"),
    [
        ("redline", "redline"),
        ("extract_tables", "table_extract"),
        ("three_year_statement", "statement"),
    ],
)
def test_the_new_boundaries_answer_unchanged_with_no_restriction_and_no_store(
    operation: str, payload_key: str, tmp_path: Path
) -> None:
    """Nothing in force keeps the pre-scope behavior: the real body is read."""
    tool, args = _new_boundary_tool(operation, tmp_path, restriction=None)
    result = tool.invoke(args)

    assert result != {"error": SCOPE_COMPANY_NOT_PERMITTED}
    assert result[payload_key]["schema"].startswith("fin.")


# ---------------------------------------------------------------------------
# a refusal grounds no numeric evidence (SC-4, decisions 2 + 7)
# ---------------------------------------------------------------------------


def test_a_company_scope_refusal_grounds_no_numeric_claim(tmp_path: Path) -> None:
    """A refused statement read contributes no claim row a citation could rest on.

    The same tool under a naming-only restriction DOES ground numeric claims, so
    the empty result below is the refusal's doing, not a collector that never
    produces anything.
    """
    facts_root = _seed_statement_facts(tmp_path)
    call = {
        "issuer": "ACME",
        "periods": ["2021-FY", "2022-FY", "2023-FY"],
        "concepts": ["us-gaap:Revenues"],
    }
    refusal = _make_statement_tool(
        facts_root, scope_restriction=_exclusive(("att-a",))
    ).invoke(call)
    permitted = _make_statement_tool(
        facts_root, scope_restriction=_naming_only(("att-a",))
    ).invoke(call)

    refused_claims, refused_facts = _collect_statement_claims(
        [ToolMessage(content=json.dumps(refusal), name="three_year_statement",
                    tool_call_id="c-refused")]
    )
    permitted_claims, _permitted_facts = _collect_statement_claims(
        [ToolMessage(content=json.dumps(permitted), name="three_year_statement",
                     tool_call_id="c-permitted")]
    )

    assert refusal == {"error": SCOPE_COMPANY_NOT_PERMITTED}
    assert refused_claims == []
    assert refused_facts == []
    assert ("us-gaap:Revenues", "2023-FY") in permitted_claims


# ---------------------------------------------------------------------------
# the multi-issuer fan-out branch: a company boundary reached outside a
# tool factory (decision 5 / SC-3 / SC-4)
# ---------------------------------------------------------------------------


def test_an_exclusive_restriction_stops_every_fanout_branch_before_the_port() -> None:
    """A declared-exclusive scope fans out NO corpus read around the gate.

    Every other company boundary arrives with the seam through a
    ``_make_*_tool`` factory; this branch calls the module-level retrieve port
    itself, so it is the one corpus read the seam did not arrive with. The
    proof is the port's own call count — a post-fetch filter would already have
    crossed the boundary — and the ruling has to hold for EVERY branch, not
    just the first.
    """
    _threads, store, thread_id = _seed_threaded([("att-a", ATTACHMENT_TEXT)])
    port, state = _drive_fanout(
        restriction=_exclusive(("att-a",)),
        attachment_service=_Service(store),
        thread_id=thread_id,
    )

    assert port.call_count == 0
    assert port.queries == []
    assert [row["error"] for row in _fanout_rows(state)] == [
        SCOPE_COMPANY_NOT_PERMITTED
    ] * len(FANOUT_ROSTER)
    # A refused branch grounds nothing: no evidence reaches the one verifier
    # that runs after the join.
    assert list(state.get("evidence") or []) == []


def test_a_refused_fanout_branch_is_unavailable_never_a_successful_miss() -> None:
    """SC-4: the denial survives the join's status fold.

    The join narrows any status outside ``hit|empty|unavailable`` to ``empty``,
    so a refusal named only by a status would be rendered as "the corpus has
    nothing about this issuer" — the exact collapse SC-4 forbids. That is why
    the code rides on ``error``, and these buckets are the observable
    difference. The model-facing row is asserted too: it is what the synthesis
    reads, and a roster that was refused cannot report a grounded answer.
    """
    _port, state = _drive_fanout(restriction=_exclusive(("att-a",)))
    fanout = state["issuer_fanout"]

    assert [row["status"] for row in _fanout_rows(state)] == [
        "unavailable"
    ] * len(FANOUT_ROSTER)
    assert fanout["unavailable"] == list(FANOUT_ROSTER)
    # NOT the successful-miss shape the fold would have produced.
    assert fanout["empty"] == []
    assert fanout["contributing"] == []
    assert fanout["locator_count"] == 0

    payload = _fanout_tool_payload(state)["fanout"]
    assert [row["error"] for row in payload["issuers"]] == [
        SCOPE_COMPANY_NOT_PERMITTED
    ] * len(FANOUT_ROSTER)
    assert payload["empty"] == []
    assert state["status"] != "done"
    assert tuple(state.get("citations") or ()) == ()


def test_the_fanout_searches_the_corpus_under_a_naming_only_restriction() -> None:
    """ADR 0088:10 on the fan-out hop: naming attachments narrows ATTACHMENTS
    only, so every branch still reaches the port and contributes a hit.

    This is the pair without which both refusal cases above could be satisfied
    by a graph that refused every fan-out outright.
    """
    _threads, store, thread_id = _seed_threaded([("att-a", ATTACHMENT_TEXT)])
    port, state = _drive_fanout(
        restriction=_naming_only(("att-a",)),
        attachment_service=_Service(store),
        thread_id=thread_id,
    )

    assert sorted(port.queries) == sorted(
        issuer_query(issuer, FANOUT_QUERY) for issuer in FANOUT_ROSTER
    )
    fanout = state["issuer_fanout"]
    assert fanout["contributing"] == list(FANOUT_ROSTER)
    assert fanout["unavailable"] == []
    assert [row["error"] for row in _fanout_rows(state)] == [""] * len(FANOUT_ROSTER)
    # The passages became citable evidence, one citation per contributor: the
    # fan-out ANSWERED, it did not merely run.
    assert {citation.document_id for citation in state["citations"]} == set(
        FANOUT_DOCUMENT_IDS
    )


def test_the_fanout_answers_unchanged_with_nothing_in_force() -> None:
    """No restriction in force keeps the pre-scope fan-out byte for byte.

    The whole envelope is compared with the shape the join documents — roster
    rows in ask order, the three buckets, the locator count — so a guard that
    leaked a refusal code, dropped a row, or moved an issuer between buckets on
    the default path fails here.
    """
    port, state = _drive_fanout(restriction=None)

    assert port.call_count == len(FANOUT_ROSTER)
    assert state["issuer_fanout"] == {
        "schema": ISSUER_FANOUT_SCHEMA,
        "query": FANOUT_QUERY,
        "cap": MAX_ISSUERS,
        "requested": len(FANOUT_ROSTER),
        "issuers": [_fanout_hit_row(issuer) for issuer in FANOUT_ROSTER],
        "contributing": list(FANOUT_ROSTER),
        "empty": [],
        "unavailable": [],
        "locator_count": len(FANOUT_ROSTER),
    }
    assert state["status"] == "done"


def test_an_exclusive_restriction_refuses_the_fanout_without_any_store() -> None:
    """The branch seam does not depend on a store — and is not a blanket ban.

    Both directions on identical unwired wiring: what decides is the operator's
    declaration, never the presence of an attachment authority. The graph with
    a naming-only restriction still searches, so the exclusive refusal cannot
    be an always-refuse artifact of a missing store.
    """
    refused_port, refused = _drive_fanout(restriction=_exclusive(("att-a",)))
    searched_port, searched = _drive_fanout(restriction=_naming_only(("att-a",)))

    assert refused_port.call_count == 0
    assert [row["error"] for row in _fanout_rows(refused)] == [
        SCOPE_COMPANY_NOT_PERMITTED
    ] * len(FANOUT_ROSTER)
    assert refused["issuer_fanout"]["empty"] == []
    assert searched_port.call_count == len(FANOUT_ROSTER)
    assert searched["issuer_fanout"]["contributing"] == list(FANOUT_ROSTER)


# ---------------------------------------------------------------------------
# structural guard: no company-source boundary is left without the seam
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "operation",
    [
        "list_files",
        "retrieve",
        "get_xbrl_fact",
        "compare_facts",
        "read_source",
        "redline",
        "extract_tables",
        "three_year_statement",
        "research_issuers",
    ],
)
def test_every_wired_company_source_boundary_carries_the_scope_seam(
    operation: str, tmp_path: Path
) -> None:
    """Every company read the wiring binds refuses an exclusive scope first.

    Three of these boundaries were once ungated, so the check is a closed list
    over the company-source operations: a company tool added WITHOUT the seam
    would answer real content here instead of the branch-ruling code, which is
    how the earlier gap surfaced. The list also carries the fan-out branch, the
    one company boundary the wiring binds outside a tool factory — it reaches
    the port directly, so it is exactly the shape a factory-only list misses.
    """
    assert _gated_company_probe(operation, tmp_path) == {
        "error": SCOPE_COMPANY_NOT_PERMITTED
    }
