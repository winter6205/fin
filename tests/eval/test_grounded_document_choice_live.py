"""Live-model acceptance for a grounded document choice (QA-chain T9 / GE-4).

The two filings below are synthetic, temporary in-process retrieval hits. A
period-free revenue question is genuinely ambiguous after both current hits
arrive. This case exercises the real model and shipped host graph; the default
pytest lane deselects it, while ``-m eval`` requires live model credentials.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from fin.corpus.catalog import record_filings
from fin.corpus.extracted import ExtractedPage, Locator
from fin.host import ScriptedChatModel, compile_graph
from fin.host.compile import _build_default_model
from fin.host.agent_instructions import USAGE_RESEARCH
from fin.host.graph import DOCUMENT_CHOICE_INTERRUPT_KIND
from fin.host.state import reset_input_state
from fin.host.task.task_runtime import accept_input
from fin.host.task.task_runtime_types import (
    INPUT_MESSAGE,
    OTHER_CHOICE_ID,
    PURPOSE_DOCUMENT_CHOICE,
    TaskRuntimeState,
)
from fin.retrieve import Chunk
from fin.retrieve.fake_port import FakeRetrievePort
from fin.retrieve.port import DocumentScope
from tests.conftest import _PROCESS_SNAPSHOT

SEARCH_QUERY = "Quasar Beacon Systems quarterly subscription revenue"
QUESTION = (
    "What was Quasar Beacon Systems' quarterly subscription revenue? "
    f'Search the exact phrase "{SEARCH_QUERY}".'
)
DOCUMENT_TEXTS = {
    "SYNTH-QBS-2024Q1": (
        "# Financial Results\n"
        "Quasar Beacon Systems is synthetic test data. For the quarter ended "
        "March 31, 2024, quarterly subscription revenue was 14 million "
        "synthetic units."
    ),
    "SYNTH-QBS-2025Q1": (
        "# Financial Results\n"
        "Quasar Beacon Systems is synthetic test data. For the quarter ended "
        "March 31, 2025, quarterly subscription revenue was 19 million "
        "synthetic units."
    ),
}
DOCUMENT_VERSIONS = {
    document_id: hashlib.sha256(text.encode("utf-8")).hexdigest()
    for document_id, text in DOCUMENT_TEXTS.items()
}
PROMPT_VERSION = hashlib.sha256(USAGE_RESEARCH.encode("utf-8")).hexdigest()


class _SyntheticDocumentRetrievePort(FakeRetrievePort):
    """Serve both synthetic documents for any nonempty document query.

    The live model owns the wording of its search query. The fake still
    validates that query and records it, while this fixture keeps the two
    authorized synthetic documents available for retrieval regardless of
    that wording.
    """

    def _ordinary_hits(
        self,
        query: str,
        filters: Mapping[str, str | None],
        limit: int,
        document_scope: DocumentScope | None,
    ) -> dict[str, Any]:
        return super()._ordinary_hits(
            SEARCH_QUERY, filters, limit, document_scope
        )


def _synthetic_retrieval_adapter() -> _SyntheticDocumentRetrievePort:
    chunks = [
        Chunk(
            text=text,
            source_ref=Locator(
                document_id=document_id,
                version=DOCUMENT_VERSIONS[document_id],
                unit=1,
            ),
            score=1.0,
        )
        for document_id, text in DOCUMENT_TEXTS.items()
    ]
    pages_by_doc = {
        document_id: (
            ExtractedPage(
                locator=Locator(
                    document_id=document_id,
                    version=DOCUMENT_VERSIONS[document_id],
                    unit=1,
                ),
                text=text,
                passages=(),
            ),
        )
        for document_id, text in DOCUMENT_TEXTS.items()
    }
    adapter = _SyntheticDocumentRetrievePort(
        hits_by_query={SEARCH_QUERY: chunks},
        known_document_ids=set(DOCUMENT_TEXTS),
        pages_by_doc=pages_by_doc,
    )
    return adapter


def _write_synthetic_catalog(catalog_path: Path) -> None:
    periods = {
        "SYNTH-QBS-2024Q1": "2024 Q1",
        "SYNTH-QBS-2025Q1": "2025 Q1",
    }
    rows = [
        {
            "document_id": document_id,
            "company": "Quasar Beacon Systems",
            "period": periods[document_id],
            "version": DOCUMENT_VERSIONS[document_id],
            "page_count": 1,
            # The catalog WRITE requires these two (src/fin/corpus/catalog.py
            # ``_REQUIRED_ROW_STRING_FIELDS``); the synthetic rows must carry
            # them or the write is refused as a malformed row.
            "title": f"Quasar Beacon Systems {periods[document_id]} report",
            "doc_type": "narrative",
        }
        for document_id in DOCUMENT_TEXTS
    ]
    record_filings(rows, catalog_path=catalog_path)


def _safe_source_ref(source_ref: Any) -> dict[str, str | int]:
    if not isinstance(source_ref, Mapping):
        return {}
    safe: dict[str, str | int] = {}
    for field in ("document_id", "version"):
        value = source_ref.get(field)
        if isinstance(value, str):
            safe[field] = value
    unit = source_ref.get("unit")
    if type(unit) is int:
        safe["unit"] = unit
    return safe


def _tool_call_args(call: Mapping[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {"name": call.get("name")}
    args = call.get("args")
    if not isinstance(args, Mapping):
        return safe
    query = args.get("query")
    if isinstance(query, str):
        safe["query"] = query
    source_ref = _safe_source_ref(args.get("source_ref"))
    safe.update(source_ref)
    return safe


def _tool_message_outcome(message: ToolMessage) -> dict[str, Any]:
    outcome: dict[str, Any] = {"name": message.name}
    if isinstance(message.tool_call_id, str):
        outcome["tool_call_id"] = message.tool_call_id
    if not isinstance(message.content, str):
        outcome["payload_type"] = type(message.content).__name__
        return outcome
    try:
        payload = json.loads(message.content)
    except json.JSONDecodeError:
        outcome["payload_type"] = "non_json"
        # A tool that raises is surfaced by the prebuilt ToolNode as a plain
        # error string. Record its shape, never its text: the failure report
        # leaves this test directory, and the message may quote fixture
        # content. An exception class name is enough to tell an adapter fault
        # from a malformed envelope.
        outcome["content_length"] = len(message.content)
        error_type = re.search(
            r"([A-Za-z_][\w.]*(?:Error|Exception))", message.content
        )
        if error_type:
            outcome["error_type"] = error_type.group(1)
        return outcome
    if not isinstance(payload, Mapping):
        outcome["payload_type"] = type(payload).__name__
        return outcome

    for field in ("error", "reason", "status"):
        value = payload.get(field)
        if isinstance(value, str):
            outcome[field] = value
    source_ref = _safe_source_ref(payload.get("source_ref"))
    if source_ref:
        outcome["source_ref"] = source_ref
    text = payload.get("text")
    if isinstance(text, str):
        outcome["has_text"] = bool(text)
    filings = payload.get("filings")
    if isinstance(filings, list):
        outcome["filings"] = [
            {
                "document_id": row.get("document_id"),
                "version": row.get("version"),
            }
            for row in filings
            if isinstance(row, Mapping)
        ]
    return outcome


def _assert_synthetic_catalog_and_sources(
    graph: Any,
) -> None:
    thread_config = {
        "configurable": {"thread_id": "synthetic-fixture-check"}
    }
    tools = graph._tool_node.tools_by_name
    listing = tools["list_files"].invoke({}, config=thread_config)
    assert isinstance(listing, Mapping)
    listed = listing["filings"]
    assert len(listed) == len(DOCUMENT_TEXTS)
    assert {
        (row["document_id"], row["version"]) for row in listed
    } == {
        (document_id, DOCUMENT_VERSIONS[document_id])
        for document_id in DOCUMENT_TEXTS
    }
    for document_id, text in DOCUMENT_TEXTS.items():
        source_ref = {
            "document_id": document_id,
            "version": DOCUMENT_VERSIONS[document_id],
            "unit": 1,
        }
        result = tools["read_source"].invoke(
            {
                "name": "read_source",
                "args": {"source_ref": source_ref, "position": 0},
                "id": f"synthetic-read-{document_id}",
                "type": "tool_call",
            },
            config=thread_config,
        )
        messages = result.update["messages"]
        assert len(messages) == 1
        assert isinstance(messages[0], ToolMessage)
        payload = json.loads(messages[0].content)
        assert payload.get("error") is None
        assert payload.get("source_ref") == source_ref
        assert payload.get("text") == text


def _synthetic_corpus_fixture(
    catalog_path: Path,
) -> _SyntheticDocumentRetrievePort:
    adapter = _synthetic_retrieval_adapter()
    _write_synthetic_catalog(catalog_path)
    return adapter


def test_synthetic_catalog_and_source_authority_are_consistent(
    tmp_path: Path,
) -> None:
    """Graph-bound tools list and read the same synthetic source versions."""
    catalog_path = tmp_path / "catalog.json"
    adapter = _synthetic_corpus_fixture(catalog_path)
    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=ScriptedChatModel(script=[AIMessage(content="unused")]),
        adapter=adapter,
        known_document_ids=set(DOCUMENT_TEXTS),
        catalog_path=catalog_path,
    )
    _assert_synthetic_catalog_and_sources(graph)


def test_synthetic_adapter_serves_both_documents_without_a_model() -> None:
    """The live fixture's override must keep the port hook's arity.

    A stale override used to raise ``TypeError`` inside the retrieve tool, so
    the live test saw two plain-text error blocks, read both sources, and
    refused — with no retrieval outcome recorded. This case exercises the same
    adapter through the public port without a model, so the arity drift is
    caught in the default lane.
    """
    adapter = _synthetic_retrieval_adapter()
    result = adapter.retrieve(SEARCH_QUERY, limit=8)
    document_ids = {
        chunk.source_ref.document_id for chunk in result["chunks"]
    }
    assert document_ids == set(DOCUMENT_TEXTS)


def test_non_json_tool_failures_are_reported_without_their_text() -> None:
    """The failure report names the error class, never the tool's text.

    The prebuilt ToolNode surfaces a raising tool as ``Error: TypeError(...)``.
    That string can quote fixture content and leaves this test directory in a
    report, so only its shape is recorded.
    """
    content = "Error: TypeError('secret fixture passage that must not travel')"
    outcome = _tool_message_outcome(
        ToolMessage(
            content=content,
            name="retrieve",
            tool_call_id="call-redaction",
        )
    )

    assert outcome["payload_type"] == "non_json"
    assert outcome["content_length"] == len(content)
    assert outcome["error_type"] == "TypeError"
    assert "secret fixture passage" not in json.dumps(outcome)


@pytest.mark.eval
def test_live_model_opens_one_choice_from_two_current_retrieved_documents(
    record_property,
    tmp_path: Path,
) -> None:
    """The real model asks which filing period the operator means."""
    record_property("model_mode", "live")
    record_property("research_prompt_sha256", PROMPT_VERSION)
    record_property(
        "synthetic_source_versions",
        ",".join(
            f"{document_id}@{DOCUMENT_VERSIONS[document_id]}"
            for document_id in sorted(DOCUMENT_VERSIONS)
        ),
    )

    if not _PROCESS_SNAPSHOT.get("FIN_API_KEY"):
        pytest.fail(
            "Live grounded document-choice evaluation requires FIN_API_KEY "
            "in tests.conftest._PROCESS_SNAPSHOT",
            pytrace=False,
        )

    catalog_path = tmp_path / "catalog.json"
    adapter = _synthetic_corpus_fixture(catalog_path)
    model = _build_default_model(
        model_name=None,
        base_url=None,
        env=_PROCESS_SNAPSHOT,
    )
    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        adapter=adapter,
        known_document_ids=set(DOCUMENT_TEXTS),
        catalog_path=catalog_path,
    )

    run_id = f"live-document-choice-{uuid.uuid4().hex}"
    thread_id = f"live-document-choice-thread-{uuid.uuid4().hex}"
    envelope, accepted = accept_input(
        TaskRuntimeState(),
        kind=INPUT_MESSAGE,
        payload=QUESTION,
        provenance="workspace_send",
        run_id=run_id,
    )
    state = {
        **reset_input_state(QUESTION),
        "task_runtime": envelope.to_state_dict(),
    }
    out = graph.invoke(
        state,
        config={
            "configurable": {
                "thread_id": thread_id,
                "run_id": run_id,
                "accepted_task_input": accepted.to_state_dict(),
            }
        },
    )

    decision = out.get("grounded_decision") or {}
    if decision.get("outcome") != "choice":
        tool_calls = [
            _tool_call_args(call)
            for message in out.get("messages", ())
            if isinstance(message, AIMessage)
            for call in message.tool_calls
        ]
        tool_message_outcomes = [
            _tool_message_outcome(message)
            for message in out.get("messages", ())
            if isinstance(message, ToolMessage)
        ]
        track_decision = out.get("track_decision") or {}
        retrieve_outcomes = out.get("retrieve_outcomes") or []
        pytest.fail(
            "Expected the live model to admit a document choice; "
            f"observed status={out.get('status')!r}, "
            f"status_reason={out.get('status_reason')!r}, "
            f"track={track_decision.get('track')!r}, "
            f"route_intent={out.get('route_intent')!r}, "
            f"grounded_decision={decision.get('outcome')!r}, "
            f"retrieve_calls={len(adapter.calls)}, "
            f"retrieval_outcomes={len(retrieve_outcomes)}, "
            f"tool_calls={tool_calls!r}, "
            f"tool_message_outcomes={tool_message_outcomes!r}",
            pytrace=False,
        )
    assert decision.get("candidate_count") == 2

    runtime = TaskRuntimeState.from_state_dict(out.get("task_runtime"))
    pending = runtime.pending_question
    assert pending is not None
    assert pending.purpose == PURPOSE_DOCUMENT_CHOICE
    assert pending.origin_run_id == run_id
    assert len(pending.choice_details) == 2
    offered = pending.to_state_dict()["choices"]
    assert offered[-1] == OTHER_CHOICE_ID
    assert offered.count(OTHER_CHOICE_ID) == 1
    assert offered[:-1] == [
        row.choice_id for row in pending.choice_details
    ]
    assert {
        row.source_ref.source_id for row in pending.choice_details
    } == set(DOCUMENT_TEXTS)
    assert {
        row.source_ref.version for row in pending.choice_details
    } == set(DOCUMENT_VERSIONS.values())

    interrupts = out.get("__interrupt__") or ()
    assert len(interrupts) == 1
    assert interrupts[0].value.get("kind") == DOCUMENT_CHOICE_INTERRUPT_KIND

    # The paused graph has exactly one terminal assistant response for the
    # retrieved evidence set. No second synthesis/decision generation runs
    # after the choice is admitted.
    terminal_responses = [
        message
        for message in out.get("messages", ())
        if isinstance(message, AIMessage)
        and not message.tool_calls
        and message.content
    ]
    assert len(terminal_responses) == 1
