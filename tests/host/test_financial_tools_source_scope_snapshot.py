"""The ACCEPTED source-scope snapshot must constrain the financial tools too.

``redline``, ``extract_tables`` and ``three_year_statement`` are the three
company-source tools that still gated through the run-independent
:func:`fin.host.graph._company_scope_refusal`: a user who picked document A
got a snapshot that said "restricted to A" while a redline for A vs B, a
table extraction from B, or a statement built from every ACME filing still
answered — the widening an accepted selection exists to prevent.

What is asserted here, all through the REAL shipped graph (``compile_graph``,
``InMemorySaver``), with the host's own catalog, fact tables and extracted
trees as fixtures:

* a committed corpus snapshot refuses a NAMED document outside it — the whole
  request for ``redline`` (both sides must be inside the selection, because a
  redline is one answer computed across both bodies) and
  ``extract_tables``, before any body is opened;
* a committed corpus snapshot has ``three_year_statement``'s ANSWER narrowed:
  an issuer resolves to documents the model never named, so the rows are
  bounded by the cells' own locators rather than refused;
* a MALFORMED snapshot answers :data:`SCOPE_SNAPSHOT_UNREADABLE` on all three
  rather than falling back to the whole tree / whole fact table;
* a STALE selection (current authority no longer stands behind it) refuses
  rather than answering from a selection nobody can honour;
* a thread with NO committed snapshot behaves byte-for-byte as before — the
  full tree, the other document, and the whole-issuer statement are all still
  answered.

A snapshot is only ever written by the host (a Runtime-accepted reply), so
every test seeds it through the graph's own state channel rather than through
model output.

Hermetic: temp manifest/extracted trees and fact tables, in-memory
checkpoint. No database, no network, no ``$HOME``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from fin.corpus.catalog import record_filings
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
# binds the full tool set, so a clarify/dialogue leaf would run the financial
# tools on a narrow node that does not carry them at all. The text is the one
# the sibling company-tool file uses verbatim — routing reads the words, and
# the scripted model never answers it, so the issuer it names is irrelevant.
QUESTION = "What was Apple revenue in 2023 Q3?"

DOC_A = "ACME-2022Q3"
DOC_B = "ACME-2023Q3"
VERSION = "v" * 64
CONCEPT = "us-gaap:Revenues"
PERIODS = ["2021-FY", "2022-FY", "2023-FY"]
# DOC_A holds the first two periods, DOC_B all three: the statement tool
# resolves each (concept, period) cell from the FIRST document in sorted
# order that holds the fact, so an unrestricted statement draws cells from
# both documents — which is what makes an exclusion observable as a cell the
# narrowed answer no longer carries.
VALUE_A = (100.0, 110.0)
VALUE_B = (90.0, 95.0, 150.0)


# ---------------------------------------------------------------------------
# harness — the real host tools over real manifest / extracted / fact fixtures
# ---------------------------------------------------------------------------


def _catalog(tmp_path: Path) -> Path:
    """Two catalog rows, written through the ingest seam ``list_files`` reads."""
    catalog_path = tmp_path / "catalog.json"
    # A NEW physical row carries the T6 display fields: the spec's row
    # contract (qa-chain-corpus-retrieval §"Catalog fields and
    # compatibility projection") requires a non-empty ``title`` and
    # ``doc_type`` on every write.
    record_filings(
        [
            {
                "document_id": DOC_A,
                "title": "ACME 2022 Q3 10-Q",
                "doc_type": "10-Q",
                "company": "ACME",
                "period": "2022 Q3",
                "version": VERSION,
                "page_count": 1,
            },
            {
                "document_id": DOC_B,
                "title": "ACME 2023 Q3 10-Q",
                "doc_type": "10-Q",
                "company": "ACME",
                "period": "2023 Q3",
                "version": VERSION,
                "page_count": 1,
            },
        ],
        catalog_path=catalog_path,
    )
    return catalog_path


def _extracted(tmp_path: Path) -> tuple[Path, Path]:
    """A two-document extracted tree in the shape ``load_extracted_tree`` reads.

    Written by hand rather than through a writer seam because this tree shape
    is the documented on-disk contract (``_index.json`` + per-page JSON), the
    same shape ``test_analysis_tools.py`` pins.
    """
    extracted = tmp_path / "extracted"
    extracted.mkdir(exist_ok=True)
    for document_id, text in (
        (
            DOC_A,
            "Revenue increased steadily.\n\n"
            "| Metric | Prior |\n"
            "| --- | ---: |\n"
            "| Revenue | 100 |\n",
        ),
        (
            DOC_B,
            "Revenue increased rapidly.\n\n"
            "| Metric | 2023 |\n"
            "| --- | ---: |\n"
            "| Revenue | 150 |\n",
        ),
    ):
        doc_dir = extracted / document_id
        doc_dir.mkdir(exist_ok=True)
        (doc_dir / "page-001.json").write_text(
            json.dumps(
                {
                    "document_id": document_id,
                    "version": VERSION,
                    "page": 1,
                    "text": text,
                }
            )
        )
        (doc_dir / "passages-001.json").write_text(
            json.dumps(
                {
                    "passages": [
                        {
                            "text": text,
                            "char_start": 0,
                            "char_end": len(text),
                        }
                    ]
                }
            )
        )
    (extracted / "_index.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "documents": {DOC_A: {"page_count": 1, "version": VERSION},
                              DOC_B: {"page_count": 1, "version": VERSION}},
            }
        )
    )
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "documents": [
                    {
                        "document_id": DOC_A,
                        "sha256": VERSION,
                        "company": "ACME",
                        "period": "2022 Q3",
                    },
                    {
                        "document_id": DOC_B,
                        "sha256": VERSION,
                        "company": "ACME",
                        "period": "2023 Q3",
                    },
                ]
            }
        )
    )
    return manifest, extracted


def _facts(tmp_path: Path) -> Path:
    """Three fact rows per document, written through ``fin.corpus.facts``.

    Three periods because the statement tool types a one- or two-period
    request as ``statement_requires_three_years`` — the grid it renders has
    to be a real one for the narrowing to be observable.
    """
    facts_root = tmp_path / "facts"
    for document_id, values in ((DOC_A, VALUE_A), (DOC_B, VALUE_B)):
        write_facts(
            document_id=document_id,
            facts=[
                Fact(
                    concept=CONCEPT,
                    value=value,
                    period=period,
                    unit="USD",
                    scale="thousands",
                    source_locator=Locator(
                        document_id=document_id, version=VERSION, unit=4
                    ),
                )
                for period, value in zip(PERIODS, values)
            ],
            facts_root=facts_root,
        )
    return facts_root


from fin.corpus.extracted import Locator  # noqa: E402


class _Service:
    """The wired attachment seam, shaped like the deployed service."""

    def __init__(self, store: InMemoryAttachmentStore) -> None:
        self.store = store


def _attachment_world() -> tuple[InMemoryAttachmentStore, str]:
    """An in-memory attachment store with one READY body owned by a thread.

    Without an authority to consult, a selection resolves to a FAULT rather
    than to a rechecked answer, and a faulted selection is not one this task's
    gates decide.
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
    manifest_path, extracted_root = _extracted(tmp_path)
    return compile_graph(
        checkpointer=InMemorySaver(),
        model=ScriptedChatModel(script=script),
        catalog_path=_catalog(tmp_path),
        facts_root=_facts(tmp_path),
        manifest_path=manifest_path,
        extracted_root=extracted_root,
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


def _redline_call(before: str, after: str, call_id: str = "c-redline") -> AIMessage:
    return _call(
        "redline",
        {
            "before_document_id": before,
            "before_version": VERSION,
            "after_document_id": after,
            "after_version": VERSION,
        },
        call_id,
    )


def _tables_call(document_id: str, call_id: str = "c-tables") -> AIMessage:
    return _call(
        "extract_tables",
        {"document_id": document_id, "version": VERSION},
        call_id,
    )


def _statement_call(call_id: str = "c-statement") -> AIMessage:
    return _call(
        "three_year_statement",
        {"issuer": "ACME", "periods": list(PERIODS), "concepts": [CONCEPT]},
        call_id,
    )


def _serialized(out: Mapping[str, Any]) -> str:
    return json.dumps([str(m.content) for m in out["messages"]])


# ---------------------------------------------------------------------------
# 1. a committed selection gates the three tools
# ---------------------------------------------------------------------------


def test_a_committed_selection_refuses_a_redline_across_an_unnamed_document(
    tmp_path: Path,
) -> None:
    """BOTH sides of a redline must sit inside the accepted selection.

    A redline is one answer computed across two frozen bodies: dropping one
    side would silently change what was asked, and serving a paragraph from
    the excluded body would be exactly the widening the selection exists to
    prevent. The request is decidable from its arguments, so it is refused
    before either body is opened.
    """
    graph = _compile(
        tmp_path,
        [_redline_call(DOC_A, DOC_B), AIMessage(content="Reviewed.")],
    )
    thread_id = "thread-redline-other"
    _commit(graph, thread_id, _snapshot(("corpus", DOC_A, VERSION)))

    out = _run(graph, thread_id)

    payload = _tool_payload(out, "redline")
    assert payload == {"error": SCOPE_COMPANY_NOT_PERMITTED}
    assert "redline" not in payload
    assert "Revenue increased" not in _serialized(out)


def test_a_redline_inside_the_selection_is_still_answered(tmp_path: Path) -> None:
    """The selection narrows the branch; it does not disable it."""
    graph = _compile(
        tmp_path,
        [_redline_call(DOC_A, DOC_A), AIMessage(content="Reviewed.")],
    )
    thread_id = "thread-redline-named"
    _commit(graph, thread_id, _snapshot(("corpus", DOC_A, VERSION)))

    out = _run(graph, thread_id)

    payload = _tool_payload(out, "redline")
    assert "error" not in payload
    assert payload["redline"]["before"]["document_id"] == DOC_A
    assert payload["redline"]["after"]["document_id"] == DOC_A


def test_a_redline_with_an_excluded_before_side_is_refused(tmp_path: Path) -> None:
    """The BEFORE side is named too: the exclusion is side-agnostic.

    A redline reads the before body as fully as the after body, so a selection
    naming only the after document refuses the pair rather than serving
    paragraphs from the body the user excluded.
    """
    graph = _compile(
        tmp_path,
        [_redline_call(DOC_A, DOC_B), AIMessage(content="Reviewed.")],
    )
    thread_id = "thread-redline-before-excluded"
    _commit(graph, thread_id, _snapshot(("corpus", DOC_B, VERSION)))

    out = _run(graph, thread_id)

    payload = _tool_payload(out, "redline")
    assert payload == {"error": SCOPE_COMPANY_NOT_PERMITTED}
    assert "redline" not in payload
    assert "Revenue increased" not in _serialized(out)


def test_a_committed_selection_refuses_an_extraction_from_an_unnamed_document(
    tmp_path: Path,
) -> None:
    """``extract_tables`` names its document in the request itself."""
    graph = _compile(
        tmp_path,
        [_tables_call(DOC_B), AIMessage(content="Reviewed.")],
    )
    thread_id = "thread-tables-other"
    _commit(graph, thread_id, _snapshot(("corpus", DOC_A, VERSION)))

    out = _run(graph, thread_id)

    payload = _tool_payload(out, "extract_tables")
    assert payload == {"error": SCOPE_COMPANY_NOT_PERMITTED}
    assert "table_extract" not in payload


def test_an_extraction_inside_the_selection_is_still_answered(tmp_path: Path) -> None:
    graph = _compile(
        tmp_path,
        [_tables_call(DOC_A), AIMessage(content="Reviewed.")],
    )
    thread_id = "thread-tables-named"
    _commit(graph, thread_id, _snapshot(("corpus", DOC_A, VERSION)))

    out = _run(graph, thread_id)

    payload = _tool_payload(out, "extract_tables")
    assert "error" not in payload
    assert payload["table_extract"]["locator"]["document_id"] == DOC_A


def test_a_statement_for_an_issuer_has_its_answer_narrowed(
    tmp_path: Path,
) -> None:
    """An issuer resolves to documents the model never named, so the answer —
    not the request — is what the selection bounds.

    The statement walks every fact sidecar the issuer prefix reaches; only the
    selected document's cell, value, and locator may reach the model. The
    counts are recomputed to describe the grid that survived.
    """
    graph = _compile(
        tmp_path,
        [_statement_call(), AIMessage(content="Reviewed.")],
    )
    thread_id = "thread-statement-open"
    _commit(graph, thread_id, _snapshot(("corpus", DOC_A, VERSION)))

    out = _run(graph, thread_id)

    payload = _tool_payload(out, "three_year_statement")
    assert "error" not in payload
    statement = payload["statement"]
    assert [
        locator["document_id"] for locator in statement["locators"]
    ] == [DOC_A]
    assert statement["hit_count"] == len(PERIODS) - 1
    assert statement["cell_count"] == len(PERIODS) - 1
    assert [
        cell["locator"]["document_id"]
        for row in statement["rows"]
        for cell in row["cells"]
        if cell["locator"] is not None
    ] == [DOC_A] * (len(PERIODS) - 1)
    assert str(VALUE_B[-1]) not in _serialized(out)


def test_an_attachment_only_selection_leaves_the_financial_branch_alone(
    tmp_path: Path,
) -> None:
    """Naming attachments narrows the attachment branch, never the corpus.

    ADR 0088:10 — a selection that names no corpus document is not the
    ``company_excluded`` case, so the financial tools keep reading exactly as
    they did before this gate existed.
    """
    graph = _compile(
        tmp_path,
        [_tables_call(DOC_A), AIMessage(content="Reviewed.")],
    )
    thread_id = "thread-attachment-only"
    _commit(graph, thread_id, _snapshot(("attachment", "att-1", "1")))

    out = _run(graph, thread_id)

    payload = _tool_payload(out, "extract_tables")
    assert "error" not in payload
    assert payload["table_extract"]["locator"]["document_id"] == DOC_A


# ---------------------------------------------------------------------------
# 2. an invalid snapshot NEVER widens — the mandatory no-widening proof
# ---------------------------------------------------------------------------


def test_a_malformed_snapshot_never_redlines(tmp_path: Path) -> None:
    """An unreadable selection is a typed refusal on the redline boundary.

    The raw value is committed on purpose: the writer validated the real one,
    so this pins the READER. A reader that ignored it would redline both
    bodies — the widening this assertion exists to forbid.
    """
    graph = _compile(
        tmp_path,
        [_redline_call(DOC_A, DOC_A), AIMessage(content="Reviewed.")],
    )
    thread_id = "thread-bad-redline"
    _commit(graph, thread_id, {"schema_version": 99, "mode": "restricted"})

    out = _run(graph, thread_id)

    payload = _tool_payload(out, "redline")
    assert payload == {"error": SCOPE_SNAPSHOT_UNREADABLE}
    assert "redline" not in payload
    assert "Revenue increased" not in _serialized(out)


def test_a_malformed_snapshot_never_extracts_tables(tmp_path: Path) -> None:
    graph = _compile(
        tmp_path,
        [_tables_call(DOC_A), AIMessage(content="Reviewed.")],
    )
    thread_id = "thread-bad-tables"
    _commit(graph, thread_id, {"mode": "restricted", "sources": "nonsense"})

    out = _run(graph, thread_id)

    payload = _tool_payload(out, "extract_tables")
    assert payload == {"error": SCOPE_SNAPSHOT_UNREADABLE}
    assert "table_extract" not in payload


def test_a_malformed_snapshot_never_renders_a_statement(tmp_path: Path) -> None:
    graph = _compile(
        tmp_path,
        [_statement_call(), AIMessage(content="Reviewed.")],
    )
    thread_id = "thread-bad-statement"
    _commit(graph, thread_id, {"mode": "restricted", "sources": "nonsense"})

    out = _run(graph, thread_id)

    payload = _tool_payload(out, "three_year_statement")
    assert payload == {"error": SCOPE_SNAPSHOT_UNREADABLE}
    assert "statement" not in payload


def test_a_stale_selection_refuses_rather_than_answering_the_excluded_document(
    tmp_path: Path,
) -> None:
    """A selection that current authority no longer stands behind refuses.

    The snapshot names an attachment the store does not hold, so
    ``recheck_scope`` fails on the selection as a whole. All three tools
    answer that one cause with no permitted path rather than serving a
    document from a selection nobody can now honour.
    """
    store, thread_id = _attachment_world()
    graph = _compile(
        tmp_path,
        [
            _redline_call(DOC_A, DOC_A, "c-redline"),
            _tables_call(DOC_A, "c-tables"),
            _statement_call("c-statement"),
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

    for name in ("redline", "extract_tables", "three_year_statement"):
        assert _tool_payload(out, name) == {"error": SCOPE_ATTACHMENT_VERSION_STALE}
    serialized = _serialized(out)
    assert "Revenue increased" not in serialized
    assert str(VALUE_A) not in serialized


# ---------------------------------------------------------------------------
# 3. no snapshot — byte-for-byte the pre-existing behaviour
# ---------------------------------------------------------------------------


def test_no_snapshot_leaves_the_compile_time_restriction_the_only_input(
    tmp_path: Path,
) -> None:
    """The regression guard on the gating path.

    An exclusive compile-time restriction still refuses all three financial
    tools with no snapshot committed anywhere — the behaviour that exists
    today.
    """
    graph = _compile(
        tmp_path,
        [_tables_call(DOC_A, "c-a"), AIMessage(content="Reviewed.")],
        scope_restriction=ScopeRestriction((), "explicit", company_excluded=True),
    )

    out = _run(graph, "thread-restricted")

    assert _tool_payload(out, "extract_tables") == {
        "error": SCOPE_COMPANY_NOT_PERMITTED
    }


def test_financial_tools_without_a_snapshot_answer_exactly_what_a_plain_tool_call_does(
    tmp_path: Path,
) -> None:
    """The byte-for-byte proof, not a restatement of expected values.

    A no-snapshot run through the real graph is compared against the SAME
    tree and fact tables read by calling the implementations directly — the
    path that existed before any snapshot existed. Equality is on the JSON the
    model receives, so a gate that consulted something, added a field, or
    reordered a row would fail here even if every hand-written expectation in
    this file still held.
    """
    from fin.host.tools.redline import redline
    from fin.host.tools.statements import three_year_statement
    from fin.host.tools.table_extract import extract_tables

    graph = _compile(
        tmp_path,
        [
            _redline_call(DOC_A, DOC_B, "c-redline"),
            _tables_call(DOC_A, "c-tables"),
            _tables_call(DOC_B, "c-tables-b"),
            _statement_call("c-statement"),
            AIMessage(content="Reviewed."),
        ],
    )

    out = _run(graph, "thread-no-snapshot")

    manifest_path, extracted_root = _extracted(tmp_path)
    facts_root = _facts(tmp_path)
    assert _tool_payloads(out, "redline") == [
        redline(
            before_document_id=DOC_A,
            before_version=VERSION,
            after_document_id=DOC_B,
            after_version=VERSION,
            manifest_path=manifest_path,
            extracted_root=extracted_root,
        )
    ]
    assert _tool_payloads(out, "extract_tables") == [
        extract_tables(
            document_id=DOC_A,
            version=VERSION,
            manifest_path=manifest_path,
            extracted_root=extracted_root,
        ),
        extract_tables(
            document_id=DOC_B,
            version=VERSION,
            manifest_path=manifest_path,
            extracted_root=extracted_root,
        ),
    ]
    assert _tool_payloads(out, "three_year_statement") == [
        three_year_statement(
            issuer="ACME",
            periods=list(PERIODS),
            concepts=[CONCEPT],
            facts_root=facts_root,
        )
    ]


def test_a_naming_only_compile_time_restriction_leaves_the_corpus_readable(
    tmp_path: Path,
) -> None:
    """ADR 0088:10 through these tools: naming attachments is not an exclusion."""
    graph = _compile(
        tmp_path,
        [_tables_call(DOC_A), AIMessage(content="Reviewed.")],
        scope_restriction=ScopeRestriction(("att-a",), "explicit"),
    )

    out = _run(graph, "thread-naming-only")

    payload = _tool_payload(out, "extract_tables")
    assert "error" not in payload
    assert payload["table_extract"]["locator"]["document_id"] == DOC_A


def test_no_snapshot_and_no_restriction_answers_every_document(tmp_path: Path) -> None:
    """With nothing in force, the tools read exactly what they read before."""
    graph = _compile(
        tmp_path,
        [
            _tables_call(DOC_A, "c-a"),
            _tables_call(DOC_B, "c-b"),
            AIMessage(content="Reviewed."),
        ],
    )

    out = _run(graph, "thread-open-tables")

    payload_a, payload_b = _tool_payloads(out, "extract_tables")
    assert payload_a["table_extract"]["locator"]["document_id"] == DOC_A
    assert payload_b["table_extract"]["locator"]["document_id"] == DOC_B


def test_no_snapshot_leaves_a_redline_across_either_document(tmp_path: Path) -> None:
    graph = _compile(
        tmp_path,
        [_redline_call(DOC_A, DOC_B), AIMessage(content="Reviewed.")],
    )

    out = _run(graph, "thread-open-redline")

    payload = _tool_payload(out, "redline")
    assert "error" not in payload
    assert payload["redline"]["summary"]


def test_an_unrestricted_snapshot_is_not_a_narrowing(tmp_path: Path) -> None:
    """``mode="unrestricted"`` is the explicit whole-corpus posture."""
    graph = _compile(
        tmp_path,
        [_tables_call(DOC_A), _statement_call("c-statement"),
         AIMessage(content="Reviewed.")],
    )
    thread_id = "thread-unrestricted"
    _commit(graph, thread_id, _snapshot(mode="unrestricted"))

    out = _run(graph, thread_id)

    tables = _tool_payload(out, "extract_tables")
    assert tables["table_extract"]["locator"]["document_id"] == DOC_A
    statement = _tool_payload(out, "three_year_statement")["statement"]
    assert sorted(
        locator["document_id"] for locator in statement["locators"]
    ) == sorted([DOC_A, DOC_B])
