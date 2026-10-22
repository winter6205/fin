"""The ACCEPTED source-scope snapshot must constrain the company fact tools too.

The document-choice resume commits a versioned selection to the thread
checkpoint, and :func:`fin.host.graph._scope_for_run` resolves it on every
read boundary. Four of the six company-facing sites already resolve their
scope that way: ``read_source``, ``read_visual_detail``, the attachment
retrieval branch, and the company text-admission boundary.

The three fact/catalog tools did not: ``list_files``, ``get_xbrl_fact`` and
``compare_facts`` gated through :func:`fin.host.graph._company_scope_refusal`,
which consumed ONLY the compile-time ``scope_restriction``. So a user who
picked document A got a snapshot that said "restricted" while a fact lookup
for document B was still answered — the widening an accepted selection exists
to prevent.

What is asserted here, all through the REAL shipped graph (``compile_graph``,
``InMemorySaver``), with the host's own catalog and fact tables as fixtures:

* a committed corpus snapshot narrows ``list_files`` to the named document,
  and refuses ``get_xbrl_fact`` / ``compare_facts`` for a document outside it;
* a MALFORMED snapshot answers :data:`SCOPE_SNAPSHOT_UNREADABLE` on all three
  rather than falling back to the whole catalog / whole fact table;
* a thread with NO committed snapshot behaves byte-for-byte as before — the
  compile-time restriction stays the only input, and with nothing in force the
  full catalog and a fact row for either document are still answered.

A snapshot is only ever written by the host (a Runtime-accepted reply), so
every test seeds it through the graph's own state channel rather than through
model output.

Hermetic: temp catalog and fact tables, in-memory checkpoint. No database, no
network, no ``$HOME``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from fin.corpus.catalog import record_filings
from fin.corpus.extracted import Locator
from fin.corpus.facts import Fact, write_facts
from fin.host import ScriptedChatModel, compile_graph
from fin.host.attachment.attachment_index import build_postings
from fin.host.attachment.attachment_store import (
    InMemoryAttachmentStore,
    ParsedAttachmentUnit,
)
from fin.host.source_scope import (
    SCOPE_ATTACHMENT_VERSION_STALE,
    SCOPE_COMPANY_NOT_PERMITTED,
    SCOPE_SNAPSHOT_UNREADABLE,
    ScopeRestriction,
    SourceScopeSnapshot,
)
from fin.host.state import reset_input_state
from fin.host.thread_store import InMemoryThreadStore

# A question the track cascade routes to the grounded leaf: only that leaf
# binds the full tool set, so a clarify/dialogue leaf would run the fact tools
# on a narrow node that does not carry them at all.
QUESTION = "What was Apple revenue in 2023 Q3?"

DOC_A = "AAPL-2023Q3"
DOC_B = "AAPL-2022Q4"
VERSION = "v" * 8
CONCEPT = "us-gaap:Revenues"
PERIOD = "2023-Q3-YTD"
VALUE_A = 383285000000.0
VALUE_B = 32958000000.0


# ---------------------------------------------------------------------------
# harness — the real host tools over the real catalog / fact tables
# ---------------------------------------------------------------------------


def _catalog(tmp_path: Path) -> Path:
    """Two catalog rows, written through the ingest seam ``list_files`` reads.

    The rows are the ones a push produces, so the row set the tool filters is
    the row set the catalog really holds.
    """
    catalog_path = tmp_path / "catalog.json"
    # A NEW physical row carries the T6 display fields: the spec's row
    # contract (qa-chain-corpus-retrieval §"Catalog fields and
    # compatibility projection") requires a non-empty ``title`` and
    # ``doc_type`` on every write.
    record_filings(
        [
            {
                "document_id": DOC_A,
                "title": "AAPL 2023 Q3 10-Q",
                "doc_type": "10-Q",
                "company": "AAPL",
                "period": "2023 Q3",
                "version": VERSION,
                "page_count": 12,
            },
            {
                "document_id": DOC_B,
                "title": "AAPL 2022 Q4 10-Q",
                "doc_type": "10-Q",
                "company": "AAPL",
                "period": "2022 Q4",
                "version": VERSION,
                "page_count": 9,
            },
        ],
        catalog_path=catalog_path,
    )
    return catalog_path


def _facts(tmp_path: Path) -> Path:
    """One fact row per document, written through ``fin.corpus.facts``."""
    facts_root = tmp_path / "facts"
    for document_id, value in ((DOC_A, VALUE_A), (DOC_B, VALUE_B)):
        write_facts(
            document_id=document_id,
            facts=[
                Fact(
                    concept=CONCEPT,
                    value=value,
                    period=PERIOD,
                    unit="USD",
                    scale="thousands",
                    source_locator=Locator(
                        document_id=document_id, version=VERSION, unit=4
                    ),
                )
            ],
            facts_root=facts_root,
        )
    return facts_root


class _Service:
    """The wired attachment seam, shaped like the deployed service.

    Built from ``store`` alone: the minimal real seam, with no turn-time
    preparation involved (the runs below name no incoming attachment).
    """

    def __init__(self, store: InMemoryAttachmentStore) -> None:
        self.store = store


def _attachment_world() -> tuple[InMemoryAttachmentStore, str]:
    """An in-memory attachment store with one READY body owned by a thread.

    The store is what makes the recheck reachable at all: without an authority
    to consult, a selection resolves to a FAULT rather than to a rechecked
    answer, and a faulted selection is not one this task's gates decide.
    """
    threads = InMemoryThreadStore()
    store = InMemoryAttachmentStore(threads)
    thread_id = threads.create_thread(title="Thread").id
    store.create(
        thread_id=thread_id,
        attachment_id="att-real",
        filename="memo.txt",
        size_bytes=1_024,
        sha256="ab" * 32,
        object_key=f"threads/{thread_id}/att-real",
    )
    units = (ParsedAttachmentUnit(unit=1, text="The body nobody selected."),)
    store.record_units(attachment_id="att-real", version=1, units=units)
    store.put_index(
        attachment_id="att-real", version=1, postings=build_postings(units)
    )
    store.mark_ready(attachment_id="att-real", version=1, unit_count=1)
    return store, thread_id


def _compile(
    tmp_path: Path,
    script: list[AIMessage],
    *,
    scope_restriction: ScopeRestriction | None = None,
    store: InMemoryAttachmentStore | None = None,
) -> Any:
    return compile_graph(
        checkpointer=InMemorySaver(),
        model=ScriptedChatModel(script=script),
        catalog_path=_catalog(tmp_path),
        facts_root=_facts(tmp_path),
        scope_restriction=scope_restriction,
        attachment_service=None if store is None else _Service(store),
    )


def _run(graph: Any, thread_id: str) -> dict[str, Any]:
    return graph.invoke(
        reset_input_state(QUESTION),
        config={"configurable": {"thread_id": thread_id}},
    )


def _commit(graph: Any, thread_id: str, raw: Mapping[str, Any]) -> None:
    """Write an accepted snapshot onto the thread's own state channel.

    A plain ``update_state`` with no ``as_node``: seeding a selection must not
    itself model a turn, and the channel is the same one the document-choice
    resume commits to.
    """
    graph._graph.update_state(
        {"configurable": {"thread_id": thread_id}},
        {"source_scope": dict(raw)},
    )


def _snapshot(
    *sources: tuple[str, str, str], mode: str = "restricted"
) -> dict[str, Any]:
    """The wire shape a Runtime-accepted reply commits, built through the
    module's own validator so a test cannot seed a value the writer refused.
    """
    if mode == "unrestricted":
        raw: dict[str, Any] = {
            "schema_version": 1,
            "mode": "unrestricted",
            "sources": [],
            "accepted_input_version": 1,
        }
    else:
        raw = {
            "schema_version": 1,
            "mode": "restricted",
            "sources": [
                {"source_kind": kind, "source_id": sid, "version": ver}
                for kind, sid, ver in sources
            ],
            "accepted_input_version": 1,
        }
    SourceScopeSnapshot.from_state_dict(raw)
    return raw


def _tool_payloads(out: Mapping[str, Any], name: str) -> list[Any]:
    """Every payload this run received under ``name``, in call order."""
    return [
        json.loads(str(m.content))
        for m in out["messages"]
        if isinstance(m, ToolMessage) and getattr(m, "name", None) == name
    ]


def _tool_payload(out: Mapping[str, Any], name: str) -> dict[str, Any]:
    payloads = _tool_payloads(out, name)
    assert len(payloads) == 1, f"expected one {name} result: {payloads}"
    return payloads[0]


def _call(name: str, args: dict[str, Any], call_id: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {"name": name, "args": args, "id": call_id, "type": "tool_call"}
        ],
    )


def _list_call(call_id: str = "c-list") -> AIMessage:
    return _call("list_files", {}, call_id)


def _fact_call(document_id: str | None, call_id: str = "c-fact") -> AIMessage:
    args: dict[str, Any] = {"concept": CONCEPT, "period": PERIOD}
    if document_id is not None:
        args["document_id"] = document_id
    return _call("get_xbrl_fact", args, call_id)


def _compare_call(
    document_ids: list[str] | None, call_id: str = "c-compare"
) -> AIMessage:
    return _call(
        "compare_facts",
        {
            "concept": CONCEPT,
            "periods": [PERIOD],
            "document_ids": document_ids or [],
        },
        call_id,
    )


def _compare_issuer_call(call_id: str = "c-issuer") -> AIMessage:
    return _call(
        "compare_facts",
        {"concept": CONCEPT, "periods": [PERIOD], "issuers": ["AAPL"]},
        call_id,
    )


def _filings(payload: Mapping[str, Any]) -> list[str]:
    return [row["document_id"] for row in payload["filings"]]


# ---------------------------------------------------------------------------
# 1. the committed selection reaches the three company tools
# ---------------------------------------------------------------------------


def test_a_committed_selection_narrows_the_catalog_enumeration(
    tmp_path: Path,
) -> None:
    """``list_files`` enumerates the accepted document, not the whole catalog."""
    graph = _compile(tmp_path, [_list_call(), AIMessage(content="Reviewed.")])
    thread_id = "thread-list"
    _commit(graph, thread_id, _snapshot(("corpus", DOC_A, VERSION)))

    out = _run(graph, thread_id)

    payload = _tool_payload(out, "list_files")
    assert _filings(payload) == [DOC_A]
    assert "error" not in payload


def test_a_committed_selection_refuses_a_fact_outside_it(tmp_path: Path) -> None:
    """``get_xbrl_fact`` for an unnamed document is refused, not answered."""
    graph = _compile(tmp_path, [_fact_call(DOC_B), AIMessage(content="Reviewed.")])
    thread_id = "thread-fact-other"
    _commit(graph, thread_id, _snapshot(("corpus", DOC_A, VERSION)))

    out = _run(graph, thread_id)

    payload = _tool_payload(out, "get_xbrl_fact")
    assert payload == {"error": SCOPE_COMPANY_NOT_PERMITTED}
    assert "source_locator" not in payload
    assert str(VALUE_B) not in json.dumps([str(m.content) for m in out["messages"]])


def test_a_fact_inside_the_selection_is_still_answered(tmp_path: Path) -> None:
    """The selection narrows the branch; it does not disable it."""
    graph = _compile(tmp_path, [_fact_call(DOC_A), AIMessage(content="Reviewed.")])
    thread_id = "thread-fact-named"
    _commit(graph, thread_id, _snapshot(("corpus", DOC_A, VERSION)))

    out = _run(graph, thread_id)

    payload = _tool_payload(out, "get_xbrl_fact")
    assert "error" not in payload
    assert payload["source_locator"]["document_id"] == DOC_A
    assert payload["value"] == VALUE_A


def test_a_comparison_naming_an_unnamed_document_is_refused(
    tmp_path: Path,
) -> None:
    """``compare_facts`` rows must be provably inside the accepted selection."""
    graph = _compile(
        tmp_path,
        [_compare_call([DOC_B]), AIMessage(content="Reviewed.")],
    )
    thread_id = "thread-compare-other"
    _commit(graph, thread_id, _snapshot(("corpus", DOC_A, VERSION)))

    out = _run(graph, thread_id)

    payload = _tool_payload(out, "compare_facts")
    assert payload == {"error": SCOPE_COMPANY_NOT_PERMITTED}
    assert "comparison" not in payload


def test_a_comparison_naming_the_selected_document_is_answered(
    tmp_path: Path,
) -> None:
    graph = _compile(
        tmp_path,
        [_compare_call([DOC_A]), AIMessage(content="Reviewed.")],
    )
    thread_id = "thread-compare-named"
    _commit(graph, thread_id, _snapshot(("corpus", DOC_A, VERSION)))

    out = _run(graph, thread_id)

    payload = _tool_payload(out, "compare_facts")
    assert "error" not in payload
    assert [locator["document_id"] for locator in payload["comparison"]["locators"]] == [
        DOC_A
    ]


def test_a_comparison_naming_both_documents_is_answered_narrowed(
    tmp_path: Path,
) -> None:
    """One named document outside the selection refuses the WHOLE request.

    A comparison is a single grid the caller reads as one answer, so dropping
    the excluded row would silently change what was asked. The exclusion is
    therefore decided on the request and no row is read — which also means no
    value from the excluded document leaks through a sibling row.
    """
    graph = _compile(
        tmp_path,
        [_compare_call([DOC_A, DOC_B]), AIMessage(content="Reviewed.")],
    )
    thread_id = "thread-compare-mixed"
    _commit(graph, thread_id, _snapshot(("corpus", DOC_A, VERSION)))

    out = _run(graph, thread_id)

    payload = _tool_payload(out, "compare_facts")
    assert payload == {"error": SCOPE_COMPANY_NOT_PERMITTED}
    assert str(VALUE_A) not in json.dumps([str(m.content) for m in out["messages"]])
    assert str(VALUE_B) not in json.dumps([str(m.content) for m in out["messages"]])


def test_a_comparison_naming_no_document_has_its_answer_narrowed(
    tmp_path: Path,
) -> None:
    """With neither ids nor issuers the request reaches every filing.

    The caller cannot know which documents that is, so the request is not
    refused on a guess: the implementation walks the fact table and the answer
    is narrowed to the documents the selection names. The excluded document's
    row, value, and locator are all absent from what the model sees, while the
    counts are recomputed to describe the grid that survived.
    """
    graph = _compile(
        tmp_path,
        [_compare_call([]), AIMessage(content="Reviewed.")],
    )
    thread_id = "thread-compare-open"
    _commit(graph, thread_id, _snapshot(("corpus", DOC_A, VERSION)))

    out = _run(graph, thread_id)

    payload = _tool_payload(out, "compare_facts")
    assert "error" not in payload
    comparison = payload["comparison"]
    assert [
        locator["document_id"] for locator in comparison["locators"]
    ] == [DOC_A]
    # The counts describe the grid the caller can still see: only the named
    # document contributes a cell, and the excluded document's row is left with
    # no cells at all rather than a locator, a value, or a hit.
    assert comparison["hit_count"] == 1
    assert comparison["cell_count"] == 1
    assert [
        [cell["locator"]["document_id"] for cell in row["cells"]]
        for row in comparison["rows"]
    ] == [[], [DOC_A]]
    assert str(VALUE_B) not in json.dumps([str(m.content) for m in out["messages"]])


def test_a_comparison_naming_an_issuer_has_its_answer_narrowed(
    tmp_path: Path,
) -> None:
    """An issuer resolves to documents the caller never named, so its rows are
    bounded by the selection's document ids rather than refused.

    ``document_ids`` is empty and ``issuers`` resolves ``AAPL`` across both
    filings — the request shape that cannot be decided from the arguments. The
    implementation resolves ONE row for the issuer, so which document that row
    lands on is its own business; what this pins is that whichever it picked,
    only the selected document's value can reach the model.
    """
    graph = _compile(
        tmp_path,
        [_compare_issuer_call(), AIMessage(content="Reviewed.")],
    )
    thread_id = "thread-compare-issuer"
    _commit(graph, thread_id, _snapshot(("corpus", DOC_A, VERSION)))

    out = _run(graph, thread_id)

    comparison = _tool_payload(out, "compare_facts")["comparison"]
    cells = [cell for row in comparison["rows"] for cell in row["cells"]]
    outside = [
        cell
        for cell in cells
        if cell["locator"] is not None
        and cell["locator"]["document_id"] not in (DOC_A, DOC_B)
    ]
    assert outside == []
    excluded = [
        cell
        for cell in cells
        if cell["locator"] is not None and cell["locator"]["document_id"] == DOC_B
    ]
    assert excluded == []
    assert comparison["hit_count"] == len(
        [cell for cell in cells if cell["status"] == "hit"]
    )


def test_an_attachment_only_selection_leaves_the_company_branch_alone(
    tmp_path: Path,
) -> None:
    """Naming attachments narrows the attachment branch, never the corpus.

    ADR 0088:10 — a selection that names no corpus document is not the
    ``company_excluded`` case, so the company tools keep reading exactly as
    they did before this gate existed.
    """
    graph = _compile(tmp_path, [_list_call(), AIMessage(content="Reviewed.")])
    thread_id = "thread-attachment-only"
    _commit(graph, thread_id, _snapshot(("attachment", "att-1", "1")))

    out = _run(graph, thread_id)

    assert sorted(_filings(_tool_payload(out, "list_files"))) == sorted(
        [DOC_A, DOC_B]
    )


# ---------------------------------------------------------------------------
# 2. an invalid snapshot NEVER widens — the mandatory no-widening proof
# ---------------------------------------------------------------------------


def test_a_malformed_snapshot_never_lists_the_whole_catalog(
    tmp_path: Path,
) -> None:
    """An unreadable selection is a typed refusal on the catalog boundary.

    The raw value is committed on purpose: the writer validated the real one,
    so this pins the READER. A reader that ignored it would answer with both
    documents — the widening this assertion exists to forbid.
    """
    graph = _compile(tmp_path, [_list_call(), AIMessage(content="Reviewed.")])
    thread_id = "thread-bad-list"
    _commit(graph, thread_id, {"schema_version": 99, "mode": "restricted"})

    out = _run(graph, thread_id)

    payload = _tool_payload(out, "list_files")
    assert payload == {"error": SCOPE_SNAPSHOT_UNREADABLE}
    assert "filings" not in payload


def test_a_malformed_snapshot_never_answers_a_fact_lookup(tmp_path: Path) -> None:
    graph = _compile(tmp_path, [_fact_call(DOC_A), AIMessage(content="Reviewed.")])
    thread_id = "thread-bad-fact"
    _commit(graph, thread_id, {"mode": "restricted", "sources": "nonsense"})

    out = _run(graph, thread_id)

    payload = _tool_payload(out, "get_xbrl_fact")
    assert payload == {"error": SCOPE_SNAPSHOT_UNREADABLE}
    assert "source_locator" not in payload


def test_a_malformed_snapshot_never_answers_a_comparison(tmp_path: Path) -> None:
    graph = _compile(
        tmp_path,
        [_compare_call([DOC_A]), AIMessage(content="Reviewed.")],
    )
    thread_id = "thread-bad-compare"
    _commit(graph, thread_id, {"mode": "restricted", "sources": "nonsense"})

    out = _run(graph, thread_id)

    payload = _tool_payload(out, "compare_facts")
    assert payload == {"error": SCOPE_SNAPSHOT_UNREADABLE}
    assert "comparison" not in payload


def test_a_stale_selection_refuses_rather_than_answering_the_excluded_document(
    tmp_path: Path,
) -> None:
    """A selection that current authority no longer stands behind refuses.

    The snapshot names an attachment the store does not hold, so ``recheck_scope``
    fails on the selection as a whole. The company tools answer that one cause
    with no permitted path rather than serving a document from a selection nobody
    can now honour — serving it would mean honouring a DIFFERENT selection than
    the accepted one, which is exactly what an accepted snapshot is not.

    All three are pinned together because they are three renderings of one
    refusal: if any of them fell back to the compile-time posture it would
    answer from the whole corpus.
    """
    store, thread_id = _attachment_world()
    graph = _compile(
        tmp_path,
        [
            _list_call("c-list"),
            _fact_call(DOC_A, "c-fact"),
            _compare_call([DOC_A], "c-compare"),
            AIMessage(content="Reviewed."),
        ],
        store=store,
    )
    _commit(
        graph,
        thread_id,
        _snapshot(("corpus", DOC_A, VERSION), ("attachment", "att-gone", "1")),
    )

    out = _run(graph, thread_id)

    for name in ("list_files", "get_xbrl_fact", "compare_facts"):
        assert _tool_payload(out, name) == {"error": SCOPE_ATTACHMENT_VERSION_STALE}
    serialized = json.dumps([str(m.content) for m in out["messages"]])
    assert str(VALUE_A) not in serialized
    assert "filings" not in serialized


# ---------------------------------------------------------------------------
# 3. no snapshot — byte-for-byte the pre-existing behaviour
# ---------------------------------------------------------------------------


def test_no_snapshot_leaves_the_compile_time_restriction_the_only_input(
    tmp_path: Path,
) -> None:
    """The regression guard on the gating path.

    An exclusive compile-time restriction still refuses all three company tools
    with no snapshot committed anywhere — the behaviour that exists today.
    """
    graph = _compile(
        tmp_path,
        [_list_call("c-a"), AIMessage(content="Reviewed.")],
        scope_restriction=ScopeRestriction((), "explicit", company_excluded=True),
    )

    out = _run(graph, "thread-restricted")

    assert _tool_payload(out, "list_files") == {
        "error": SCOPE_COMPANY_NOT_PERMITTED
    }


def test_a_thread_without_a_snapshot_answers_exactly_what_a_plain_tool_call_does(
    tmp_path: Path,
) -> None:
    """The byte-for-byte proof, not a restatement of expected values.

    A no-snapshot run through the real graph is compared against the SAME
    catalog and fact tables read by calling the implementations directly — the
    path that existed before any snapshot existed. Equality is on the JSON the
    model receives, so a gate that consulted something, added a field, or
    reordered a row would fail here even if every hand-written expectation in
    this file still held.
    """
    from fin.host.tools.compare_facts import compare_facts
    from fin.host.tools.get_xbrl_fact import get_xbrl_fact
    from fin.host.tools.list_files import list_files

    graph = _compile(
        tmp_path,
        [
            _list_call("c-list"),
            _fact_call(DOC_A, "c-a"),
            _fact_call(DOC_B, "c-b"),
            _compare_call([], "c-compare"),
            AIMessage(content="Reviewed."),
        ],
    )

    out = _run(graph, "thread-no-snapshot")

    catalog_path = _catalog(tmp_path)
    facts_root = _facts(tmp_path)
    assert _tool_payloads(out, "list_files") == [
        list_files(catalog_path=catalog_path)
    ]
    assert _tool_payloads(out, "get_xbrl_fact") == [
        get_xbrl_fact(
            concept=CONCEPT,
            period=PERIOD,
            document_id=DOC_A,
            facts_root=facts_root,
        ),
        get_xbrl_fact(
            concept=CONCEPT,
            period=PERIOD,
            document_id=DOC_B,
            facts_root=facts_root,
        ),
    ]
    assert _tool_payloads(out, "compare_facts") == [
        compare_facts(
            concept=CONCEPT,
            periods=[PERIOD],
            document_ids=None,
            issuers=None,
            facts_root=facts_root,
        )
    ]


def test_a_naming_only_compile_time_restriction_leaves_the_corpus_readable(
    tmp_path: Path,
) -> None:
    """ADR 0088:10 through these tools: naming attachments is not an exclusion.

    The refusal these three tools answer on a naming-only restriction must be
    ``None``, not a deny — a restriction that only names attachments leaves the
    company branch eligible.
    """
    graph = _compile(
        tmp_path,
        [_list_call(), AIMessage(content="Reviewed.")],
        scope_restriction=ScopeRestriction(("att-a",), "explicit"),
    )

    out = _run(graph, "thread-naming-only")

    payload = _tool_payload(out, "list_files")
    assert "error" not in payload
    assert sorted(_filings(payload)) == sorted([DOC_A, DOC_B])


def test_no_snapshot_and_no_restriction_answers_every_document(
    tmp_path: Path,
) -> None:
    """With nothing in force, the tools read exactly what they read before."""
    graph = _compile(tmp_path, [_list_call(), AIMessage(content="Reviewed.")])

    out = _run(graph, "thread-open-list")

    assert sorted(_filings(_tool_payload(out, "list_files"))) == sorted(
        [DOC_A, DOC_B]
    )


def test_no_snapshot_leaves_a_fact_lookup_for_either_document(
    tmp_path: Path,
) -> None:
    graph = _compile(
        tmp_path,
        [
            _fact_call(DOC_A, "c-a"),
            _fact_call(DOC_B, "c-b"),
            AIMessage(content="Reviewed."),
        ],
    )

    out = _run(graph, "thread-open-fact")

    messages = {
        m.tool_call_id: json.loads(str(m.content))
        for m in out["messages"]
        if isinstance(m, ToolMessage) and getattr(m, "name", None) == "get_xbrl_fact"
    }
    assert messages["c-a"]["value"] == VALUE_A
    assert messages["c-b"]["value"] == VALUE_B


def test_an_unrestricted_snapshot_is_not_a_narrowing(tmp_path: Path) -> None:
    """``mode="unrestricted"`` is the explicit whole-corpus posture."""
    graph = _compile(tmp_path, [_list_call(), AIMessage(content="Reviewed.")])
    thread_id = "thread-unrestricted"
    _commit(graph, thread_id, _snapshot(mode="unrestricted"))

    out = _run(graph, thread_id)

    assert sorted(_filings(_tool_payload(out, "list_files"))) == sorted(
        [DOC_A, DOC_B]
    )
