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
``InMemorySaver`` (no live LLM, no MCP, no network). The default suite
points ``HOME`` at an empty directory, so this file plants the policy
text and one filing unit under that home's corpus
(``<HOME>/.fin/corpus``) instead of reading the checkout
``data/corpus`` path.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

import fin.host.graph as host_graph
from fin.corpus.extracted import Locator, load_extracted_tree
from fin.host import ScriptedChatModel, compile_graph
from fin.host.fin_home import corpus_dir
from fin.host.named_failures import CORPUS_NOT_READY
from fin.host.state import reset_input_state
from fin.retrieve import Chunk
from fin.retrieve.fake_port import FakeRetrievePort


def _corpus_root() -> Path:
    """The operator corpus, resolved the same way the fin home does."""
    return corpus_dir()


def _t1_manifest_path() -> Path:
    return _corpus_root() / "dataset" / "t1_manifest.json"


def _t1_text_dir() -> Path:
    return _corpus_root() / "dataset" / "t1"

EXPENSE_DOCUMENT_ID = "t1-expense-001"
EXPENSE_POLICY_SHA = "9c0216d90a3c4cf61af7c76875ce466022ff08df63da7a58246eba9cd665b489"
_POLICY_FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "expense-policy-001.md"
)

# One frozen filing unit. The question still routes on the AAPL/2022 Q3
# rule; the page only has to be the unit ``read_source`` cites.
FILING_UNIT_TEXT = (
    "Net sales for the quarter are stated in this frozen filing unit "
    "so the citation can quote them verbatim."
)
FILING_VERSION = hashlib.sha256(FILING_UNIT_TEXT.encode()).hexdigest()

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


@pytest.fixture(autouse=True)
def _home_corpus(
    _redirect_home_off_operator: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Path:
    """Plant the fixtures under this test's own fin-home corpus.

    The suite fixture already points ``HOME`` at an empty directory.
    Override it after that redirect so the planted corpus cannot leak
    into the shared hermetic home.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("FIN_HOME", str(home / ".fin"))
    root = corpus_dir()
    policy = _POLICY_FIXTURE.read_bytes()
    assert hashlib.sha256(policy).hexdigest() == EXPENSE_POLICY_SHA
    text_dir = root / "dataset" / "t1"
    text_dir.mkdir(parents=True)
    (text_dir / "expense-policy-001.md").write_bytes(policy)
    (root / "dataset" / "t1_manifest.json").write_text(
        json.dumps(
            {
                "samples": [
                    {
                        "document_id": EXPENSE_DOCUMENT_ID,
                        "path": "expense-policy-001.md",
                        "sha256": EXPENSE_POLICY_SHA,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    doc_dir = root / "extracted" / AAPL_2022Q3
    doc_dir.mkdir(parents=True)
    (doc_dir / "page-001.json").write_text(
        json.dumps(
            {
                "document_id": AAPL_2022Q3,
                "version": FILING_VERSION,
                "page": 1,
                "text": FILING_UNIT_TEXT,
            }
        ),
        encoding="utf-8",
    )
    (doc_dir / "passages-001.json").write_text(
        json.dumps({"passages": []}),
        encoding="utf-8",
    )
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "documents": [
                    {"document_id": AAPL_2022Q3, "sha256": FILING_VERSION}
                ]
            }
        ),
        encoding="utf-8",
    )
    return root


# ----- fixture readers (independent of the implementation under test) -----


def _registered_expense_sample() -> dict[str, Any]:
    manifest = json.loads(_t1_manifest_path().read_text(encoding="utf-8"))
    for sample in manifest["samples"]:
        if sample.get("document_id") == EXPENSE_DOCUMENT_ID:
            return sample
    raise AssertionError(
        f"expense-policy fixture {EXPENSE_DOCUMENT_ID!r} is not registered"
        " in the fin-home corpus t1_manifest.json"
    )


def _expense_text_and_version() -> tuple[str, str]:
    """The committed policy text plus its manifest-registered version.

    The version the citation must carry is the manifest ``sha256``; the
    test re-hashes the frozen file to prove the registration is the
    live identity, not a copy the test invented.
    """
    sample = _registered_expense_sample()
    path = _t1_text_dir() / sample["path"]
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


@pytest.fixture
def extracted_tree(_home_corpus: Path) -> dict[str, tuple[Any, ...]]:
    """The one filing unit planted in this run's home corpus."""
    return load_extracted_tree(
        _home_corpus / "manifest.json",
        _home_corpus / "extracted",
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
        (_corpus_root() / "manifest.json").read_text(encoding="utf-8")
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


# ----- R8: Chinese question, bound material already retrievable -----

CHINESE_ENTERPRISE_QUESTION = "差旅报销制度里，工作晚餐每人最多可以报销多少？"


def test_chinese_enterprise_question_with_retrievable_material_is_grounded_and_cites(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No company token and no English policy words. A retrievable
    bound material routes the turn grounded, then the existing host
    graph completes with a citation. Nothing retrieves before the
    track decision returns."""
    text, version = _expense_text_and_version()
    port = FakeRetrievePort(
        hits_by_query={
            CHINESE_ENTERPRISE_QUESTION: [
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
    calls_at_return: list[list[dict[str, Any]]] = []
    real_decide_track = host_graph.decide_track

    def _recording_decide_track(*args: Any, **kwargs: Any) -> Any:
        decision = real_decide_track(*args, **kwargs)
        calls_at_return.append(list(port.calls))
        return decision

    monkeypatch.setattr(host_graph, "decide_track", _recording_decide_track)
    graph = _compile(
        script=_retrieve_script(
            CHINESE_ENTERPRISE_QUESTION,
            f"Per the expense policy: {MEALS_SENTENCE}",
        ),
        port=port,
        known_document_ids={EXPENSE_DOCUMENT_ID},
    )
    out = graph.invoke(
        reset_input_state(
            CHINESE_ENTERPRISE_QUESTION,
            attachments=[
                {
                    "attachment_id": "att-expense",
                    "filename": "expense-policy.txt",
                    "text": text,
                }
            ],
        ),
        config={"configurable": {"thread_id": "t-expense-zh"}},
    )

    assert out["track"] == "grounded", (
        "a Chinese enterprise question with a retrievable bound"
        f" material must be grounded, got track={out['track']!r}"
        f" tier={out['track_decision'].get('decision_tier')!r}"
    )
    assert out["track"] not in ("clarify", "off_topic")
    assert calls_at_return == [[]]
    assert port.calls, "the grounded turn still reaches retrieve after the decision"
    assert out["status"] == "done", out.get("status_reason")
    citation = tuple(out["citations"])[0]
    assert citation.document_id == EXPENSE_DOCUMENT_ID
    assert citation.version == version
    assert citation.unit == 1
    assert citation.quote == MEALS_SENTENCE


def test_chinese_enterprise_question_with_zero_bound_materials_retrieves_before_index_failure() -> (
    None
):
    """An ordinary question still retrieves when no bound material is known."""
    port = FakeRetrievePort(known_document_ids=set())
    graph = _compile(
        script=_retrieve_script(
            CHINESE_ENTERPRISE_QUESTION, "没有找到支持这项制度的资料。"
        ),
        port=port,
        known_document_ids=set(),
    )
    out = graph.invoke(
        reset_input_state(CHINESE_ENTERPRISE_QUESTION),
        config={"configurable": {"thread_id": "t-expense-zh-zero"}},
    )
    assert out["track"] == "grounded"
    assert out["route_intent"] == "retrieve"
    assert port.calls, "an empty catalog cannot replace the retrieval attempt"
    assert out["status"] == "refuse"
    assert out["status_reason"] == CORPUS_NOT_READY
    assert not out["citations"]
