"""Tests for the model-facing bounded ``read_source`` tool (T4 host leg).

The spec (``thread-attachment-source-read``) promises one tool call that
resolves exactly one frozen unit, returns at most one bounded slice, and
admits ONLY the text a call actually returned into this-run evidence.
These tests cover the host half of that contract:

* routing by ``source_ref["kind"]`` (company triple vs attachment ref);
* an absent leg answers the typed ``unknown_locator`` miss;
* the cheap position guard, so a garbage cursor never reaches a leg;
* continuation passes ``position`` through and the leg's dict is
  returned unchanged;
* one success slice yields exactly ONE ``EvidenceItem`` whose locator is
  the unit the slice addressed, and every typed error yields none;
* a known valid locator reads with NO prior ``retrieve`` hit in the run
  and still passes the citation gate (spec SC9);
* the attachment leg is bound to the run's trusted
  ``configurable.thread_id`` — the model's own arguments carry no
  thread id at all (spec SC4).

The legs here are LOCAL scripted fakes: the shared contract is the
:class:`~fin.retrieve.source_read.SourceReader` protocol, so the host
tool is testable before either leg's real implementation is wired.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from fin.corpus.extracted import Locator
from fin.host.graph import (
    _make_read_source_tool,
    _MissSourceReader,
    compile_graph,
)
from fin.host.stream_map import format_tool_summary
from fin.host.tools.read_source import read_source
from fin.retrieve.source_read import (
    INVALID_LOCATOR,
    SOURCE_BACKEND_CONTRACT_ERROR,
    SOURCE_READ_MAX_RESULT_BYTES,
    SOURCE_READ_MAX_TEXT_CODE_POINTS,
    SOURCE_UNAVAILABLE,
    UNKNOWN_LOCATOR,
    bounded_slice,
    serialize_result,
    source_error,
)

COMPANY_ID = "AAPL-2022Q3"
COMPANY_VERSION = "0" * 64
ATTACHMENT_ID = "att-1"
ATTACHMENT_VERSION = 3
# Long enough that a ≥32-char verbatim span can be copied out of it by
# the scripted answer, which is what the citation gate requires.
UNIT_TEXT = "Net sales rose to 89,493 million dollars in the quarter. " * 4


def _company_ref(**overrides: Any) -> dict[str, Any]:
    ref: dict[str, Any] = {
        "document_id": COMPANY_ID,
        "version": COMPANY_VERSION,
        "unit": 3,
    }
    ref.update(overrides)
    return ref


def _attachment_ref(**overrides: Any) -> dict[str, Any]:
    ref: dict[str, Any] = {
        "kind": "attachment",
        "attachment_id": ATTACHMENT_ID,
        "version": ATTACHMENT_VERSION,
        "unit": 2,
    }
    ref.update(overrides)
    return ref


class _ScriptedReader:
    """A :class:`SourceReader` leg that answers a canned dict."""

    def __init__(self, answer: dict[str, Any]) -> None:
        self.answer = answer
        self.calls: list[tuple[dict[str, Any], int]] = []

    def read_slice(
        self, source_ref: Mapping[str, Any], position: int = 0
    ) -> dict[str, Any]:
        self.calls.append((dict(source_ref), position))
        return self.answer


class _SlicingReader:
    """A leg that really bounds its text through the shared contract."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[tuple[dict[str, Any], int]] = []

    def read_slice(
        self, source_ref: Mapping[str, Any], position: int = 0
    ) -> dict[str, Any]:
        self.calls.append((dict(source_ref), position))
        return bounded_slice(dict(source_ref), self.text, position)


class _NoLegAdapter:
    """Retrieve port with no ``read_slice`` face at all (legacy BM25)."""

    def retrieve(self, query: str, **kwargs: Any) -> dict[str, Any]:
        return {"chunks": []}


class _FakeCompanyAdapter(_NoLegAdapter):
    """Retrieve port that also answers company ``read_slice`` calls."""

    def __init__(self, text: str = UNIT_TEXT) -> None:
        self.reader = _SlicingReader(text)

    def read_slice(
        self, source_ref: Mapping[str, Any], position: int = 0
    ) -> dict[str, Any]:
        return self.reader.read_slice(source_ref, position)


def _invoke_tool(
    tool: Any,
    *,
    source_ref: Mapping[str, Any],
    position: int | Any = 0,
    thread_id: str = "t-run",
) -> Any:
    """Call the tool the way the ToolNode does, and return its Command."""
    return tool.invoke(
        {
            "name": "read_source",
            "args": {"source_ref": dict(source_ref), "position": position},
            "id": "c-read-source",
            "type": "tool_call",
        },
        config={"configurable": {"thread_id": thread_id}},
    )


def _command_update(command: Any) -> dict[str, Any]:
    return dict(command.update)


def _tool_message(command: Any) -> ToolMessage:
    """The single ``ToolMessage`` the tool handed back."""
    messages = _command_update(command)["messages"]
    assert len(messages) == 1
    message = messages[0]
    assert isinstance(message, ToolMessage)
    assert message.name == "read_source"
    return message


def _tool_payload(command: Any) -> dict[str, Any]:
    """The JSON dict the model actually received as the tool result."""
    return json.loads(_tool_message(command).content)


def _thread_config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


# ----- the wrapper: routing and the cheap guards -----


def test_company_locator_routes_to_the_company_leg() -> None:
    company = _ScriptedReader(bounded_slice(_company_ref(), UNIT_TEXT, 0))
    attachment = _ScriptedReader(bounded_slice(_attachment_ref(), UNIT_TEXT, 0))

    result = read_source(
        _company_ref(), 0, company_reader=company, attachment_reader=attachment
    )

    assert result is company.answer
    assert attachment.calls == []
    assert company.calls == [(_company_ref(), 0)]


def test_attachment_kind_routes_to_the_attachment_leg() -> None:
    company = _ScriptedReader(bounded_slice(_company_ref(), UNIT_TEXT, 0))
    attachment = _ScriptedReader(bounded_slice(_attachment_ref(), UNIT_TEXT, 0))

    result = read_source(
        _attachment_ref(),
        0,
        company_reader=company,
        attachment_reader=attachment,
    )

    assert result is attachment.answer
    assert company.calls == []
    assert attachment.calls == [(_attachment_ref(), 0)]


@pytest.mark.parametrize(
    "company_reader",
    [None, _NoLegAdapter()],
    ids=["none", "object_without_read_slice"],
)
def test_missing_company_leg_answers_the_typed_miss(
    company_reader: Any,
) -> None:
    """An unwired leg is the same observable answer as an unknown
    locator — it reveals no text and raises nothing."""
    result = read_source(_company_ref(), 0, company_reader=company_reader)

    assert result == {"error": UNKNOWN_LOCATOR}


def test_missing_attachment_leg_answers_the_typed_miss() -> None:
    company = _ScriptedReader(bounded_slice(_company_ref(), UNIT_TEXT, 0))

    result = read_source(_attachment_ref(), 0, company_reader=company)

    assert result == {"error": UNKNOWN_LOCATOR}
    assert company.calls == []


@pytest.mark.parametrize(
    "position",
    [True, False, "0", 1.5, None, -1],
    ids=["bool_true", "bool_false", "str", "float", "none", "negative"],
)
def test_garbage_position_never_reaches_a_leg(
    position: Any,
) -> None:
    leg = _ScriptedReader(bounded_slice(_company_ref(), UNIT_TEXT, 0))

    result = read_source(_company_ref(), position, company_reader=leg)

    assert result["error"] == "invalid_position"
    assert leg.calls == []


def test_non_integer_position_carries_the_reason_field() -> None:
    result = read_source(_company_ref(), "0", company_reader=None)

    assert result == {"error": "invalid_position", "reason": "position_not_an_integer"}


def test_negative_position_carries_the_position() -> None:
    result = read_source(_company_ref(), -1, company_reader=None)

    assert result == {"error": "invalid_position", "position": -1}


def test_continuation_position_passes_through_and_result_is_verbatim() -> None:
    """The wrapper bounds nothing itself: the leg's slice shape,
    including ``next_position``, reaches the model unchanged."""
    text = "alpha " * 3000  # longer than one 8000-code-point slice
    leg = _SlicingReader(text)

    first = read_source(_company_ref(), 0, company_reader=leg)
    second = read_source(
        _company_ref(), first["next_position"], company_reader=leg
    )

    assert len(first["text"]) == 8000
    assert first["next_position"] == 8000
    assert second["text"] == text[8000:16000]
    assert leg.calls[1] == (_company_ref(), 8000)


def test_a_bare_non_record_ref_fails_typed_before_routing() -> None:
    """A ref that is not a record carries no routing key and no identity,
    and ``dict()`` of one would raise: the wrapper names the shape with
    the same answer the company leg gives before any lookup."""
    company = _ScriptedReader(bounded_slice(_company_ref(), UNIT_TEXT, 0))
    attachment = _ScriptedReader(bounded_slice(_attachment_ref(), UNIT_TEXT, 0))

    result = read_source(
        "AAPL-2022Q3/0/3",
        0,
        company_reader=company,
        attachment_reader=attachment,
    )

    assert result == {"error": INVALID_LOCATOR, "reason": "locator_not_a_record"}
    assert company.calls == []
    assert attachment.calls == []


def test_every_deeper_validation_stays_in_the_leg() -> None:
    """A malformed locator is the leg's call to make; the wrapper just
    forwards whatever typed answer it gets."""
    leg = _ScriptedReader(source_error(INVALID_LOCATOR, reason="no_unit"))

    result = read_source({"document_id": COMPANY_ID}, 0, company_reader=leg)

    assert result == {"error": "invalid_locator", "reason": "no_unit"}
    assert leg.calls == [({"document_id": COMPANY_ID}, 0)]


# ----- the graph tool: evidence accounting -----


def test_success_slice_yields_exactly_one_mapped_evidence_item() -> None:
    leg = _SlicingReader(UNIT_TEXT)
    tool = _make_read_source_tool(leg, None)

    command = _invoke_tool(tool, source_ref=_company_ref())
    update = _command_update(command)

    assert len(update["evidence"]) == 1
    item = update["evidence"][0]
    assert item.locator == Locator(
        document_id=COMPANY_ID, version=COMPANY_VERSION, page=3
    )
    # The unit is shorter than one slice, so the whole text was returned.
    assert item.page_text == UNIT_TEXT


def test_slice_text_shorter_than_the_unit_is_what_evidence_carries() -> None:
    """Evidence must hold exactly the returned slice, not the unit: a
    quote from an unread later slice stays unadmissible."""
    text = "x" * 20000
    leg = _SlicingReader(text)
    tool = _make_read_source_tool(leg, None)

    command = _invoke_tool(tool, source_ref=_company_ref())
    item = _command_update(command)["evidence"][0]

    assert 0 < len(item.page_text) <= 8000
    assert item.page_text == text[: len(item.page_text)]


def test_attachment_slice_maps_the_attachment_identity() -> None:
    leg = _SlicingReader(UNIT_TEXT)
    tool = _make_read_source_tool(None, lambda thread_id: leg)

    command = _invoke_tool(tool, source_ref=_attachment_ref())
    item = _command_update(command)["evidence"][0]

    assert item.locator == Locator(
        document_id=ATTACHMENT_ID, version=str(ATTACHMENT_VERSION), page=2
    )


@pytest.mark.parametrize(
    "error",
    [
        source_error(UNKNOWN_LOCATOR),
        source_error(INVALID_LOCATOR, reason="no_unit"),
        source_error("invalid_position", position=999999),
        source_error(SOURCE_UNAVAILABLE),
        source_error(SOURCE_BACKEND_CONTRACT_ERROR, reason="oversized"),
    ],
    ids=[
        "unknown_locator",
        "invalid_locator",
        "invalid_position",
        "source_unavailable",
        "backend_contract_error",
    ],
)
def test_typed_error_produces_no_evidence_and_reaches_the_model(
    error: dict[str, Any],
) -> None:
    leg = _ScriptedReader(error)
    tool = _make_read_source_tool(leg, None)

    command = _invoke_tool(tool, source_ref=_company_ref())

    assert _command_update(command)["evidence"] == []
    assert _tool_payload(command) == error


def test_terminal_empty_slice_admits_no_evidence() -> None:
    """``position == len(text)`` is a legal slice with no text; there is
    nothing to quote, and empty text would trip the gate's
    malformed-evidence check."""
    tool = _make_read_source_tool(_SlicingReader("short text"), None)

    command = _invoke_tool(tool, source_ref=_company_ref(), position=10)

    assert _command_update(command)["evidence"] == []
    assert _tool_payload(command) == {
        "source_ref": _company_ref(),
        "text": "",
        "next_position": None,
    }


def test_unwired_leg_reports_the_miss_through_the_tool() -> None:
    tool = _make_read_source_tool(_MissSourceReader(), None)

    command = _invoke_tool(tool, source_ref=_company_ref())

    assert _command_update(command)["evidence"] == []
    assert _tool_payload(command) == {"error": UNKNOWN_LOCATOR}


# ----- the byte cap on what the model is handed -----


# Both are exactly one legal under-cap slice measured in the contract's
# canonical form (3 and 4 UTF-8 bytes per code point respectively), so a
# leg is entitled to return all 8000 code points. ASCII-escaping them at
# emit time — 6 and 12 bytes per code point — would hand the model a
# result over the 32-KiB cap the contract promised (spec
# ``thread-attachment-source-read``: the measure covers the complete JSON
# tool-result content for the model).
_CJK_TEXT = "净" * SOURCE_READ_MAX_TEXT_CODE_POINTS
_ASTRAL_TEXT = "\U0001f600" * SOURCE_READ_MAX_TEXT_CODE_POINTS


@pytest.mark.parametrize(
    "text",
    [_CJK_TEXT, _ASTRAL_TEXT],
    ids=["cjk_3_bytes", "astral_4_bytes"],
)
def test_company_slice_fitting_the_cap_is_emitted_within_the_cap(
    text: str,
) -> None:
    leg = _SlicingReader(text)
    tool = _make_read_source_tool(leg, None)

    command = _invoke_tool(tool, source_ref=_company_ref())
    content = _tool_message(command).content

    assert len(_tool_payload(command)["text"]) == SOURCE_READ_MAX_TEXT_CODE_POINTS
    assert len(content.encode("utf-8")) <= SOURCE_READ_MAX_RESULT_BYTES
    # One serializer for measure and emission: the content is exactly
    # the canonical form the leg bounded the slice against.
    assert content == serialize_result(_tool_payload(command))


@pytest.mark.parametrize(
    "text",
    [_CJK_TEXT, _ASTRAL_TEXT],
    ids=["cjk_3_bytes", "astral_4_bytes"],
)
def test_attachment_slice_fitting_the_cap_is_emitted_within_the_cap(
    text: str,
) -> None:
    tool = _make_read_source_tool(None, lambda thread_id: _SlicingReader(text))

    command = _invoke_tool(tool, source_ref=_attachment_ref())
    content = _tool_message(command).content

    assert len(_tool_payload(command)["text"]) == SOURCE_READ_MAX_TEXT_CODE_POINTS
    assert len(content.encode("utf-8")) <= SOURCE_READ_MAX_RESULT_BYTES


# ----- the attachment leg's trusted thread binding -----


def test_attachment_leg_receives_the_runs_thread_id() -> None:
    seen: list[str] = []

    def factory(thread_id: str) -> Any:
        seen.append(thread_id)
        return _SlicingReader(UNIT_TEXT)

    tool = _make_read_source_tool(None, factory)

    _invoke_tool(tool, source_ref=_attachment_ref(), thread_id="t-trusted")

    assert seen == ["t-trusted"]


def test_model_supplied_thread_id_cannot_widen_the_scope() -> None:
    """The bound scope is the run's thread, whatever the model puts in
    ``source_ref``."""
    seen: list[str] = []
    tool = _make_read_source_tool(
        None, lambda thread_id: seen.append(thread_id) or _SlicingReader(UNIT_TEXT)
    )

    _invoke_tool(
        tool,
        source_ref=_attachment_ref(thread_id="other-thread"),
        thread_id="t-trusted",
    )

    assert seen == ["t-trusted"]


def test_run_without_a_thread_id_has_no_attachment_leg() -> None:
    leg = _SlicingReader(UNIT_TEXT)
    tool = _make_read_source_tool(None, lambda thread_id: leg)

    command = tool.invoke(
        {
            "name": "read_source",
            "args": {"source_ref": _attachment_ref(), "position": 0},
            "id": "c-read-source",
            "type": "tool_call",
        },
        config={"configurable": {}},
    )

    assert _command_update(command)["evidence"] == []
    assert _tool_payload(command) == {"error": UNKNOWN_LOCATOR}


def test_tool_arguments_expose_only_source_ref_and_position() -> None:
    """No thread id, no path, no store handle — the model-facing schema
    is the two contract fields (spec SC4 / SC11)."""
    tool = _make_read_source_tool(_MissSourceReader(), None)

    assert set(tool.args) == {"source_ref", "position"}
    schema = tool.tool_call_schema.model_json_schema()
    assert set(schema["properties"]) == {"source_ref", "position"}


# ----- graph wiring -----


def test_read_source_is_bound_next_to_read_file() -> None:
    """Both tools coexist until T5 removes ``read_file``; ``read_source``
    is bound last so no existing tool shifts position."""
    model = _RecordingModel()
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        adapter=_FakeCompanyAdapter(),
    )

    names = [tool.name for tool in g._bound_tools]
    assert "read_file" in names
    assert names[-1] == "read_source"


class _RecordingModel:
    """Minimal stand-in that captures the bound tool list."""

    def __init__(self) -> None:
        self.bound_tools: list[Any] = []

    def bind_tools(self, tools: list[Any], **kwargs: Any) -> "_RecordingModel":
        self.bound_tools = list(tools)
        return self

    def bind(self, **kwargs: Any) -> "_RecordingModel":
        return self


def _scripted_read_source_run(answer: str, source_ref: Mapping[str, Any]) -> list[AIMessage]:
    return [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "read_source",
                    "args": {"source_ref": dict(source_ref), "position": 0},
                    "id": "c-read-source",
                    "type": "tool_call",
                }
            ],
        ),
        AIMessage(content=answer),
    ]


def _fresh_state(
    ask: str = "What was AAPL net sales in 2023 Q3?",
) -> dict[str, Any]:
    """One grounded ask: the rules tier needs a full filing+period slot
    set before the grounded leaf (and therefore the tool node) runs."""
    return {
        "messages": [HumanMessage(ask)],
        "evidence": [],
        "repair_count": 0,
        "step_count": 0,
    }


def test_direct_read_without_retrieve_passes_the_gate() -> None:
    """SC9: a known valid locator needs no same-run ``retrieve`` hit —
    the text the read returned is what the citation rests on."""
    from fin.host.scripted_model import ScriptedChatModel

    quoted = UNIT_TEXT[:60]
    model = ScriptedChatModel(
        script=_scripted_read_source_run(
            f"Per the source: {quoted}", _company_ref()
        )
    )
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        adapter=_FakeCompanyAdapter(),
    )

    out = g.invoke(_fresh_state(), config=_thread_config("t-direct-read"))

    assert out["status"] == "done"
    assert len(out["evidence"]) == 1
    citation = out["citations"][0]
    assert citation.document_id == COMPANY_ID
    assert citation.version == COMPANY_VERSION
    assert citation.page == 3


def test_continuation_read_accumulates_both_slices() -> None:
    """Two reads of one unit admit exactly the text returned, which is
    what makes a spanning quote admissible and a gap inadmissible."""
    from fin.host.scripted_model import ScriptedChatModel

    text = "A" * 8000 + "B" * 8000
    model = ScriptedChatModel(script=[])
    model.script = [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "read_source",
                    "args": {"source_ref": _company_ref(), "position": 0},
                    "id": "c-1",
                    "type": "tool_call",
                }
            ],
        ),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "read_source",
                    "args": {"source_ref": _company_ref(), "position": 8000},
                    "id": "c-2",
                    "type": "tool_call",
                }
            ],
        ),
        AIMessage(content=f"Both halves: {'A' * 40} and {'B' * 40}"),
    ]
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        adapter=_FakeCompanyAdapter(text=text),
    )

    out = g.invoke(_fresh_state(), config=_thread_config("t-continuation"))

    assert out["status"] == "done"
    assert [len(item.page_text) for item in out["evidence"]] == [8000, 8000]


def test_attachment_read_goes_through_the_injected_factory() -> None:
    from fin.host.scripted_model import ScriptedChatModel

    calls: list[str] = []

    def factory(thread_id: str) -> Any:
        calls.append(thread_id)
        return _SlicingReader(UNIT_TEXT)

    quoted = UNIT_TEXT[:60]
    model = ScriptedChatModel(
        script=_scripted_read_source_run(
            f"The memo says: {quoted}", _attachment_ref()
        )
    )
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        adapter=_NoLegAdapter(),
        attachment_source_reader_factory=factory,
    )

    out = g.invoke(
        _fresh_state("What was AAPL net sales in 2023 Q3 in the memo?"),
        config=_thread_config("t-attach-read"),
    )

    assert calls == ["t-attach-read"]
    assert out["status"] == "done"
    assert out["citations"][0].document_id == ATTACHMENT_ID


def test_idempotence_cache_holds_the_reader_factory_object_itself() -> None:
    """The cache keys on the factory with a strong reference (the tracked change).

    A numeric ``id()`` slot lets a later factory allocated at a recycled
    address hit a stale cached compile; holding the object in the key
    removes the whole failure class. Direct ``_make_locked_graph`` call
    so nothing but the cache key can keep the factory alive.
    """
    import gc
    from weakref import ref

    from fin.host.graph import _make_locked_graph

    checkpointer = InMemorySaver()
    model = _RecordingModel()
    factory = _SlicingReader(UNIT_TEXT)
    weak = ref(factory)

    first = _make_locked_graph(
        compiled_graph=object(),
        recursion_limit=1,
        checkpointer=checkpointer,
        model=model,
        step_cap=1,
        attachment_source_reader_factory=factory,
    )
    del factory
    gc.collect()
    assert weak() is not None

    second = _make_locked_graph(
        compiled_graph=object(),
        recursion_limit=1,
        checkpointer=checkpointer,
        model=model,
        step_cap=1,
        attachment_source_reader_factory=weak(),
    )
    assert second is first

    third = _make_locked_graph(
        compiled_graph=object(),
        recursion_limit=1,
        checkpointer=checkpointer,
        model=model,
        step_cap=1,
        attachment_source_reader_factory=_SlicingReader(UNIT_TEXT),
    )
    assert third is not first


def test_company_read_with_no_attachment_leg_still_routes_company() -> None:
    """A graph with neither leg wired answers the typed miss rather
    than crashing (mirrors ``MissReadPort`` for read_file)."""
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=_RecordingModel(),
        adapter=_NoLegAdapter(),
    )

    read_source_tool = g._bound_tools[-1]
    command = _invoke_tool(read_source_tool, source_ref=_company_ref())

    assert _command_update(command)["evidence"] == []
    assert _tool_payload(command) == {"error": UNKNOWN_LOCATOR}


# ----- operator trace -----


def test_trace_summary_shows_the_slice_read() -> None:
    summary = format_tool_summary(
        "read_source",
        result={
            "source_ref": _company_ref(),
            "text": UNIT_TEXT[:100],
            "next_position": 100,
        },
        args={"source_ref": _company_ref()},
    )

    assert summary == f"{COMPANY_ID} u.3 -> 100"


def test_trace_summary_shows_a_miss_code() -> None:
    summary = format_tool_summary(
        "read_source",
        result={"error": UNKNOWN_LOCATOR},
        args={"source_ref": _attachment_ref()},
    )

    assert summary == f"{ATTACHMENT_ID} u.2 ({UNKNOWN_LOCATOR})"
