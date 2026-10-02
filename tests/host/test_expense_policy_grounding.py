"""Enterprise expense-policy grounding through the existing host entry.

Product contract (specs/enterprise-kb-agent.md, ADR 0090): the operator
asks questions about authorized enterprise materials and the answer is
grounded in the source, through the SAME compiled host graph as every
other turn — not a parallel router.

* A question about a rule the policy DOES state enters the grounded
  knowledge-QA path, reaches the retrieve port, and completes with a
  citation whose locator resolves to the exact registered policy
  source (document id + manifest version + unit) and whose quote
  occurs there.
* A question about a rule the policy does NOT state must not be filed
  ``off_topic`` and must not answer plausibly; it ends in the named
  insufficient-evidence refusal.
* An amount inside a non-financial question does not select the XBRL /
  financial numeric lane.
* Existing financial routing keeps working: a filing question still
  reaches grounded ``read_source`` and cites the filing unit.

All runs are hermetic: ``ScriptedChatModel`` + ``FakeRetrievePort`` +
``InMemorySaver`` (no live LLM, no MCP, no network), and the policy
text is the committed, versioned corpus fixture registered in
``data/corpus/dataset/t1_manifest.json``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from fin.corpus.extracted import Locator, load_extracted_tree
from fin.host import ScriptedChatModel, compile_graph
from fin.host.state import reset_input_state
from fin.retrieve import Chunk
from fin.retrieve.fake_port import FakeRetrievePort

REPO_ROOT = Path(__file__).resolve().parents[2]
T1_MANIFEST_PATH = REPO_ROOT / "data" / "corpus" / "dataset" / "t1_manifest.json"
T1_TEXT_DIR = REPO_ROOT / "data" / "corpus" / "dataset" / "t1"

EXPENSE_DOCUMENT_ID = "t1-expense-001"

# The quotable present rule, verbatim from the committed fixture.
MEALS_SENTENCE = (
    "Employees may claim up to 75 US dollars per person for a working"
    " business lunch and up to 110 US dollars per person for a working"
    " business dinner."
)

QUESTION_PRESENT_RULE = (
    "According to the corporate travel expense policy, what is the"
    " maximum reimbursable amount for a working business dinner per"
    " person?"
)

# The fixture deliberately states nothing about family members
# accompanying an employee; this question is the absent-rule probe.
QUESTION_ABSENT_RULE = (
    "According to the corporate travel expense policy, what is the"
    " nightly lodging cap for a family member who accompanies an"
    " employee on a business trip?"
)

# A dollar amount inside a non-financial policy question: the amount
# alone must not pull the turn into the XBRL / financial numeric lane.
QUESTION_WITH_AMOUNT = (
    "Under the corporate travel expense policy, may an employee"
    " expense a $95 business dinner, or is the per-person cap"
    " exceeded?"
)

# The named public result the product owes when authorized material
# simply does not answer the asked rule: not ``off_topic``, not a
# plausible answer, not the verifier's generic wording.
INSUFFICIENT_EVIDENCE = "insufficient_evidence"

AAPL_2022Q3 = "AAPL-2022Q3"
FINANCIAL_QUESTION = "What was AAPL net sales in 2022 Q3?"


# ----- fixture readers (independent of the implementation under test) -----


def _registered_expense_sample() -> dict[str, Any]:
    manifest = json.loads(T1_MANIFEST_PATH.read_text(encoding="utf-8"))
    for sample in manifest["samples"]:
        if sample.get("document_id") == EXPENSE_DOCUMENT_ID:
            return sample
    raise AssertionError(
        f"expense-policy fixture {EXPENSE_DOCUMENT_ID!r} is not registered"
        " in data/corpus/dataset/t1_manifest.json"
    )


def _expense_text_and_version() -> tuple[str, str]:
    """The committed policy text plus its manifest-registered version.

    The version the citation must carry is the manifest ``sha256``; the
    test re-hashes the frozen file to prove the registration is the
    live identity, not a copy the test invented.
    """
    sample = _registered_expense_sample()
    path = T1_TEXT_DIR / sample["path"]
    text = path.read_text(encoding="utf-8")
    disk_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    assert disk_sha == sample["sha256"], (
        f"fixture drift: on-disk sha256 {disk_sha} != registered"
        f" {sample['sha256']}"
    )
    return text, sample["sha256"]


def _retrieve_script(query: str, answer: str) -> list[AIMessage]:
    return [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "retrieve",
                    "args": {"query": query},
                    "id": "c-retrieve",
                    "type": "tool_call",
                }
            ],
        ),
        AIMessage(content=answer),
    ]


def _compile(
    *,
    script: list[AIMessage],
    port: FakeRetrievePort,
    known_document_ids: set[str],
) -> Any:
    return compile_graph(
        checkpointer=InMemorySaver(),
        model=ScriptedChatModel(script=script),
        adapter=port,
        known_document_ids=set(known_document_ids),
    )


def _run(graph: Any, question: str, thread_id: str) -> dict[str, Any]:
    return graph.invoke(
        reset_input_state(question),
        config={"configurable": {"thread_id": thread_id}},
    )


@pytest.fixture(scope="module")
def extracted_tree() -> dict[str, tuple[Any, ...]]:
    """The committed extracted filing tree (representative fin data)."""
    return load_extracted_tree(
        REPO_ROOT / "data" / "corpus" / "manifest.json",
        REPO_ROOT / "data" / "corpus" / "extracted",
    )


# ----- (a) present rule: grounded retrieve, resolvable citation -----


def test_present_expense_rule_reaches_grounded_retrieve_and_cites_the_policy_unit() -> (
    None
):
    """The asked rule is in the policy: the turn enters the grounded
    knowledge-QA path through the existing host entry, the retrieve
    port answers it, and the completed run carries a citation whose
    locator resolves to the registered policy document/version/unit
    and whose quote occurs verbatim in that committed source."""
    text, version = _expense_text_and_version()
    port = FakeRetrievePort(
        hits_by_query={
            QUESTION_PRESENT_RULE: [
                Chunk(
                    text=text,
                    source_ref=Locator(
                        document_id=EXPENSE_DOCUMENT_ID,
                        version=version,
                        unit=1,
                    ),
                    score=1.0,
                )
            ]
        },
        known_document_ids={EXPENSE_DOCUMENT_ID},
    )
    graph = _compile(
        script=_retrieve_script(
            QUESTION_PRESENT_RULE, f"Per the expense policy: {MEALS_SENTENCE}"
        ),
        port=port,
        known_document_ids={EXPENSE_DOCUMENT_ID},
    )

    out = _run(graph, QUESTION_PRESENT_RULE, "t-expense-present")

    assert out["track"] == "grounded", (
        f"enterprise-material question must enter the grounded"
        f" knowledge-QA path, got track={out['track']!r}"
        f" (tier={out['track_decision'].get('decision_tier')!r})"
    )
    assert port.calls, "grounded path must reach the retrieve port"
    assert port.calls[0]["query"] == QUESTION_PRESENT_RULE
    assert out["status"] == "done", out.get("status_reason")
    citations = tuple(out["citations"])
    assert len(citations) >= 1
    citation = citations[0]
    assert citation.document_id == EXPENSE_DOCUMENT_ID
    assert citation.version == version
    assert citation.unit == 1
    assert citation.quote == MEALS_SENTENCE
    # The quote occurs in the committed source the locator names.
    assert citation.quote in text


# ----- (b) absent rule: named insufficient-evidence, never off_topic ----


def test_absent_expense_rule_names_insufficient_evidence_not_off_topic() -> None:
    """The policy states nothing about lodging for accompanying family
    members: the turn must NOT be filed ``off_topic``, it runs the
    grounded retrieve path, finds no support for the asked rule, and
    ends in the named insufficient-evidence refusal with no citations —
    never a plausible answer."""
    text, version = _expense_text_and_version()
    # Retrieve answers with the related-but-not-asked policy excerpt:
    # the material exists; the asked rule simply is not in it.
    port = FakeRetrievePort(
        hits_by_query={
            QUESTION_ABSENT_RULE: [
                Chunk(
                    text=text,
                    source_ref=Locator(
                        document_id=EXPENSE_DOCUMENT_ID,
                        version=version,
                        unit=1,
                    ),
                    score=1.0,
                )
            ]
        },
        known_document_ids={EXPENSE_DOCUMENT_ID},
    )
    unanswered = (
        "The retrieved policy excerpt does not state any lodging cap"
        " for accompanying family members."
    )
    graph = _compile(
        script=_retrieve_script(QUESTION_ABSENT_RULE, unanswered),
        port=port,
        known_document_ids={EXPENSE_DOCUMENT_ID},
    )

    out = _run(graph, QUESTION_ABSENT_RULE, "t-expense-absent")

    assert out["track"] != "off_topic", (
        "an enterprise-material question about a rule the corpus does"
        " not state is still a knowledge-QA request, not off-topic"
    )
    assert out["track"] == "grounded"
    assert port.calls, "the absent-rule probe must reach the retrieve port"
    assert out["status"] == "refuse", (
        f"an unsupported rule claim must not complete as an answer;"
        f" got status={out['status']!r}"
    )
    assert out["status_reason"] == INSUFFICIENT_EVIDENCE
    assert len(tuple(out["citations"])) == 0


# ----- (c) a non-financial amount does not select the financial lane ----


def test_dollar_amount_in_expense_question_does_not_trigger_xbrl_tooling() -> None:
    """``$95`` inside a policy question is an expense figure, not a
    tagged financial fact: the turn is grounded knowledge-QA, cites the
    policy sentence that answers it, and the run invokes no XBRL /
    financial numeric tooling along the way."""
    text, version = _expense_text_and_version()
    port = FakeRetrievePort(
        hits_by_query={
            QUESTION_WITH_AMOUNT: [
                Chunk(
                    text=text,
                    source_ref=Locator(
                        document_id=EXPENSE_DOCUMENT_ID,
                        version=version,
                        unit=1,
                    ),
                    score=1.0,
                )
            ]
        },
        known_document_ids={EXPENSE_DOCUMENT_ID},
    )
    graph = _compile(
        script=_retrieve_script(
            QUESTION_WITH_AMOUNT, f"Per the expense policy: {MEALS_SENTENCE}"
        ),
        port=port,
        known_document_ids={EXPENSE_DOCUMENT_ID},
    )

    out = _run(graph, QUESTION_WITH_AMOUNT, "t-expense-amount")

    assert out["track"] == "grounded"
    tool_names = {entry["name"] for entry in out.get("tool_summaries") or []}
    assert "get_xbrl_fact" not in tool_names, (
        "a non-financial amount must not select the XBRL lookup"
    )
    assert "compare_facts" not in tool_names, (
        "a non-financial amount must not select the financial"
        " comparison tool"
    )
    assert out["status"] == "done", out.get("status_reason")
    citation = tuple(out["citations"])[0]
    assert citation.document_id == EXPENSE_DOCUMENT_ID
    assert citation.version == version


# ----- (d) retention guard: financial routing and citation unchanged ----


def test_filing_question_still_routes_grounded_and_cites_the_filing_unit(
    extracted_tree: dict[str, tuple[Any, ...]],
) -> None:
    """The financial lane is retained: a company+period filing ask still
    classifies grounded through the rules tier, reads its frozen unit
    via ``read_source``, and completes with a citation bound to the
    filing's document/version/unit with a verbatim quote."""
    pages = extracted_tree[AAPL_2022Q3]
    manifest = json.loads(
        (REPO_ROOT / "data" / "corpus" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    version = next(
        doc["sha256"]
        for doc in manifest["documents"]
        if doc["document_id"] == AAPL_2022Q3
    )
    quoted = pages[0].text[:80]
    port = FakeRetrievePort(
        pages_by_doc={AAPL_2022Q3: pages},
        known_document_ids={AAPL_2022Q3},
    )
    script = [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "read_source",
                    "args": {
                        "source_ref": {
                            "document_id": AAPL_2022Q3,
                            "version": version,
                            "unit": 1,
                        },
                        "position": 0,
                    },
                    "id": "c-read",
                    "type": "tool_call",
                }
            ],
        ),
        AIMessage(content=f"Per the filing: {quoted}"),
    ]
    graph = _compile(
        script=script,
        port=port,
        known_document_ids={AAPL_2022Q3},
    )

    out = _run(graph, FINANCIAL_QUESTION, "t-financial-guard")

    assert out["track"] == "grounded"
    assert out["track_decision"]["decision_tier"] == "rules"
    assert out["status"] == "done", out.get("status_reason")
    citation = tuple(out["citations"])[0]
    assert citation.document_id == AAPL_2022Q3
    assert citation.version == version
    assert citation.unit == 1
    assert citation.quote == quoted
