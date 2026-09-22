"""Nothing retired leaves the host toward the retrieve port (rename).

The rename is only real once the value a run hands the port is the
lasting token. The port seam keeps EXACT matching (a bad send must stay
a named mistake, not a guess), so the whole burden is on the host side:
every entry point — an utterance naming a 档, an HTTP pin, a choice-window
pick, the cascade's own output — resolves through
``fin.retrieve.tiers.normalize_tier_token`` BEFORE anything downstream can
read the value.

Opt-in (``uv run pytest -q -m soft_track``; deselected by default).
Scripted model + :class:`~fin.retrieve.fake_port.FakeRetrievePort` only:
what is asserted is the TOKEN a call carries, never how iknow ranks
(Fake is not a 档).

* ``test_an_assigned_knowledge_qa_records_graph_before_retrieve`` — the
  audit lands at the classify node, i.e. before the port sees a call,
  and every call the port records carries exactly that lasting token.
* ``test_a_pinned_retired_letter_hands_the_port_its_lasting_token`` — the
  same for a pin sent as the retired letter an old client still uses.
* ``test_a_review_ask_names_global_on_the_audit_and_starts_no_letter`` —
  综述 is named as ``global`` on every audit field, so the trail and the
  seam agree.
* ``test_the_value_handed_to_the_port_is_never_a_letter`` — the port
  seam's own view of what normalization produces.
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import Field

from fin.host.agent_instructions import SEARCH_MODE_INSTRUCTIONS_MARKER
from fin.host.graph import compile_graph
from fin.host.named_failures import LASTING_RETRIEVE_TIERS, TIER_NOT_OPEN
from fin.host.search_mode_decision import normalize_search_mode
from fin.host.scripted_model import ScriptedChatModel
from fin.host.state import reset_input_state
from fin.retrieve.fake_port import FakeRetrievePort
from fin.retrieve.port import (
    InvalidSearchModeError,
    TierUnavailableError,
    resolve_search_mode,
)
from fin.retrieve.tiers import RETIRED_TIER_TOKENS

pytestmark = [pytest.mark.soft_track]

KNOWN_IDS = {"AAPL-2022Q3"}

# Every spelling a request can arrive with for 知识问答 / 综述: the lasting
# token, its retired letter, a case variant, and the CONTEXT label an
# utterance names it with.
KNOWLEDGE_QA_SPELLINGS = ("B", "b", "graph", "GRAPH", "知识问答")
REVIEW_SPELLINGS = ("C", "c", "global", "Global", "综述")
# A pin is a WIRE field, so only its token spellings can arrive; the
# CONTEXT label is an utterance form the pin seam never sees.
PIN_SPELLINGS = ("B", "b", "graph", "GRAPH")


class _RecordingScriptedModel(ScriptedChatModel):
    """Captures each model call's message payload (ordering proof below).

    ``_stream`` without a stream script delegates to ``_generate``, so
    overriding ``_generate`` covers both surfaces the graph uses.
    """

    captured: list = Field(default_factory=list)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> Any:
        self.captured.append(list(messages))
        return super()._generate(
            messages, stop=stop, run_manager=run_manager, **kwargs
        )


# Track rules ground a research ask; this is the same shape the cascade
# suite uses, so the only variable under test is the 档 spelling.
PLAIN_ASK = "what was AAPL net sales in 2023 Q3?"


def _utterance(spelling: str) -> str:
    return f"{spelling}档 {PLAIN_ASK}"


def _port() -> FakeRetrievePort:
    return FakeRetrievePort(
        hits_by_query={"revenue": []},
        known_document_ids=KNOWN_IDS,
    )


def _retrieve_then_answer() -> list[AIMessage]:
    return [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "retrieve",
                    "args": {"query": "revenue"},
                    "id": "c-retrieve-normalize",
                    "type": "tool_call",
                }
            ],
        ),
        AIMessage(content="Per the filings: revenue grew."),
    ]


def _run(
    text: str, *, pinned: str | None = None
) -> tuple[dict[str, Any], FakeRetrievePort, _RecordingScriptedModel]:
    """One grounded run whose retrieve call lands on a recording port."""
    port = _port()
    model = _RecordingScriptedModel(script=_retrieve_then_answer())
    graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        adapter=port,
        known_document_ids=KNOWN_IDS,
    )
    configurable: dict[str, Any] = {"thread_id": f"np-{pinned or 'utterance'}"}
    if pinned is not None:
        configurable["pinned_search_mode"] = pinned
    out = graph.invoke(
        {**reset_input_state(text), "evidence": []},
        config={"configurable": configurable},
    )
    return out, port, model


def _mode_copy_blocks(model: _RecordingScriptedModel) -> list[str]:
    return [
        str(call[0].content)
        for call in model.captured
        if call and SEARCH_MODE_INSTRUCTIONS_MARKER in str(call[0].content)
    ]


def _retired_letter_in(payload: Any) -> bool:
    """True if any string in the audit is EXACTLY a retired letter: the
    trail must read as the lasting tokens the port will carry."""
    if isinstance(payload, str):
        return payload in RETIRED_TIER_TOKENS
    if isinstance(payload, dict):
        return any(_retired_letter_in(value) for value in payload.values())
    if isinstance(payload, (list, tuple)):
        return any(_retired_letter_in(value) for value in payload)
    return False


# ----- the assigned-档 acceptance halves -----


@pytest.mark.parametrize("spelling", KNOWLEDGE_QA_SPELLINGS)
def test_an_assigned_knowledge_qa_records_graph_before_retrieve(
    spelling: str,
) -> None:
    """The 知识问答 档 is recorded as ``graph`` and the port is handed
    ``graph``. Ordering is proven, not assumed: the model call that emitted
    the retrieve tool call already carried 知识问答's usage copy, so the
    assignment existed before the port call it describes."""
    out, port, model = _run(_utterance(spelling))

    assert out["track"] == "grounded"
    audit = out["search_mode_decision"]
    assert audit["search_mode"] == "graph"
    assert not _retired_letter_in(audit)
    assert port.calls, "the grounded retrieve must have reached the port"
    # POSITIVE: the assigned 档 IS the value the port was handed — not
    # merely "not a letter", which an unassigned run would satisfy too.
    assert [call["search_mode"] for call in port.calls] == [
        "graph"
    ] * len(port.calls), [call["search_mode"] for call in port.calls]
    # The retrieve call is the SECOND model call of this run: the copy on
    # the first one is the audit's own token, visible before the port ran.
    blocks = _mode_copy_blocks(model)
    assert blocks and all("检索档 graph" in block for block in blocks), blocks


@pytest.mark.parametrize("spelling", REVIEW_SPELLINGS)
def test_a_review_ask_names_global_on_the_audit_and_starts_no_letter(
    spelling: str,
) -> None:
    """综述 is a plan slot, so nothing is assigned — but the refusal the
    operator can read names the LASTING token, and the retired letter
    appears nowhere in the trail the port would have been handed."""
    out, port, _model = _run(_utterance(spelling))

    audit = out["search_mode_decision"]
    assert audit["decision_tier"] == "typed_fail"
    assert audit["failure_code"] == TIER_NOT_OPEN
    assert audit["search_mode"] is None
    assert audit["signals"].get("requested_mode") == "global"
    assert not _retired_letter_in(audit)
    # POSITIVE: nothing was assigned, so the hop sends NO 档 — and it
    # must not smuggle ``global`` (an unopened 档) onto the wire either.
    assert port.calls, "the scripted retrieve still reached the port"
    assert [call["search_mode"] for call in port.calls] == [None] * len(
        port.calls
    ), [call["search_mode"] for call in port.calls]
    assert all(
        call["search_mode"] not in RETIRED_TIER_TOKENS for call in port.calls
    )


@pytest.mark.parametrize("spelling", PIN_SPELLINGS)
def test_a_pinned_retired_letter_hands_the_port_its_lasting_token(
    spelling: str,
) -> None:
    """Whatever the client spelled, the run's audit and anything the port
    is handed carry ``graph``."""
    out, port, _model = _run(PLAIN_ASK, pinned=spelling)

    audit = out["search_mode_decision"]
    assert audit["search_mode"] == "graph"
    assert audit["signals"].get("pinned") is True
    assert not _retired_letter_in(audit)
    # POSITIVE: a pin is an assignment, so the port is handed its lasting
    # token — the letter the client spelled must not survive the hop.
    assert port.calls, "the grounded retrieve must have reached the port"
    assert [call["search_mode"] for call in port.calls] == [
        "graph"
    ] * len(port.calls), [call["search_mode"] for call in port.calls]
    assert all(
        call["search_mode"] not in RETIRED_TIER_TOKENS for call in port.calls
    )


# ----- the port seam's own view -----


def test_the_value_handed_to_the_port_is_never_a_letter() -> None:
    """Normalization runs strictly before the seam: the token the host
    would send resolves on the port too. The seam's exactness is
    untouched — a letter sent WITHOUT normalization is still the wire's
    own mistake to name, which is why normalize cannot live behind the
    port."""
    for spelling in (*KNOWLEDGE_QA_SPELLINGS[:4], *REVIEW_SPELLINGS[:4]):
        lasting = normalize_search_mode(spelling)
        assert lasting in LASTING_RETRIEVE_TIERS, spelling
        assert lasting not in RETIRED_TIER_TOKENS, spelling

    assert resolve_search_mode(normalize_search_mode("B")) == "graph"
    try:
        resolve_search_mode(normalize_search_mode("C"))
    except TierUnavailableError as exc:
        assert exc.code == TIER_NOT_OPEN
    else:
        raise AssertionError("global accepted as an open 档")

    # And the seam keeps its own name for a letter that arrives UN-normalized:
    # the host's pin may be spelled either way, but the port forwards only the
    # closed set, so a leftover letter is a wiring bug it refuses rather than a
    # 档 it quietly reroutes.
    try:
        resolve_search_mode("B")
    except InvalidSearchModeError as exc:
        assert exc.code == "invalid_search_mode"
    else:
        raise AssertionError("a retired letter still crossed the port seam")
