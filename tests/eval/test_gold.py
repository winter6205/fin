"""Nine hand-authored gold eval cases (T10, ADR 0012).

The nine cases live in three classes:

* **3 single-filing fact** — one ``read_source`` slice of a frozen
  filing unit, followed by a final answer that quotes a ≥32-char
  extractive span copied verbatim from the unit this run read. The
  gate accepts, the run ends ``done``, and the citation list contains
  exactly the read locator (ADR 0021: the citation comes from the
  matched evidence item, not from the model's claim).
* **3 thin cross-period** — two or three ``read_source`` calls
  spanning the 2023 Q1–Q3 slice (ADR 0003: cross-period questions
  use 2023 Q1–Q3 only). The final answer quotes verbatim spans
  from at least two distinct units; the citation list spans
  ≥2 distinct locators.
* **3 refuse** — three terminal stops the gate produces:
  ``unknown_locator`` (the answer cites a document_id that is not
  in the frozen 12-PDF corpus; refuses immediately, no repair),
  ``citation_repair_cap`` (3 bounces → terminal refuse), and
  ``citation_repair_cap`` produced by an answer that quotes real
  corpus text from a unit this run did NOT read (proves the gate
  only credits evidence from this run).

All cases are hand-authored against the frozen 12 PDFs. The
``data/upstream/qna_data.csv`` file is **not** consulted (ADR 0012
forbids bulk-import). Every verbatim span is copied from the
committed extracted tree at ``data/corpus/extracted/<doc>/page-NNN.json``
(``NNN`` is the 1-based frozen-unit ordinal the artifact name keeps
from ingest) and the source ``(document_id, unit)`` is documented
inline on each case.

The cases are deselected from the default ``pytest -q`` run by the
``pytest_collection_modifyitems`` hook in ``conftest.py``; run
them with ``uv run pytest -q -m eval``. The graph is built by the
``scripted_graph_factory`` fixture — a
``compile_graph(checkpointer=InMemorySaver(), model=ScriptedChatModel(script))``
call whose ``adapter`` is a ``FakeRetrievePort`` reading the worktree's
canonical corpus, so the scripted ``read_source`` calls get real unit
text and the gate's ``known_document_ids`` stays this run's evidence
plus any configured allow-list — ``FOO-2023Q1`` is in neither.
"""

from __future__ import annotations

from fin.corpus._private_fixtures import load_private_fixture

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

# Module-level marker makes every test in this file an opt-in eval
# case. Equivalent to decorating each test function with
# ``@pytest.mark.eval``; the module-level form keeps the marker
# visible in one place and matches pytest's recommended pattern.
# ``pytestmark`` must be iterable (pytest iterates it to apply each
# mark to every test function in the module) — a bare
# ``pytest.mark.eval`` is a single Mark object and is NOT applied.
import pytest

pytestmark = [pytest.mark.eval]

from fin.host import REPAIR_CAP, STEP_CAP, ScriptedChatModel, compile_graph


# ----- frozen manifest sha256s (ADR 0004) -----
#
# Source: ``data/corpus/manifest.json``. These values are part of the
# frozen corpus; ``read_source`` answers ``{"error":
# "unknown_locator"}`` on any other version. Keep the literal strings
# in lockstep with the manifest; a drift would surface here, not deep
# inside the graph runtime.
AAPL_2022Q3 = "AAPL-2022Q3"
AAPL_2022Q3_SHA = load_private_fixture("f8d906a074fb0edaa7b850fc49d304023186190ed8e2bf9cdcdffb49ee802cff")
AAPL_2023Q1 = "AAPL-2023Q1"
AAPL_2023Q1_SHA = load_private_fixture("a28b2881ebe49cb51622b3a2e2ca4403de79a75bf02ec4a966576871361604ba")
AAPL_2023Q2 = "AAPL-2023Q2"
AAPL_2023Q2_SHA = load_private_fixture("efd763b312776361940819cfdb174744dc93668dbec3bb83fffedda39f7fb308")
AAPL_2023Q3 = "AAPL-2023Q3"
AAPL_2023Q3_SHA = load_private_fixture("5cae37674ca6bf538f54bca53ca28b15aaf96dd1b7ae386553bd32387f254b73")
MSFT_2023Q1 = "MSFT-2023Q1"
MSFT_2023Q1_SHA = load_private_fixture("0e0048f89b57908899434fc7a8cbbe2c415b927b5a5332f84693455887a825b7")
MSFT_2023Q2 = "MSFT-2023Q2"
MSFT_2023Q2_SHA = load_private_fixture("b429e643b9642191f649b103f5a5e4e08c96c6b9b14afdb5a5b11f6cb969c081")
MSFT_2023Q3 = "MSFT-2023Q3"
MSFT_2023Q3_SHA = load_private_fixture("5a8558db7c94112d0a7ff48be9f8fcf09d80f8a8f053645952c03d65ed1eb5e2")
AMZN_2023Q1 = "AMZN-2023Q1"
AMZN_2023Q1_SHA = load_private_fixture("426a5a6853e04d766101642748133fcdadf4cd9fae162e3adf8fb64fb44ab3e1")
AMZN_2023Q2 = "AMZN-2023Q2"
AMZN_2023Q2_SHA = load_private_fixture("a8d7e17eb2a9229732414f292b101be7b6ff98013690f9c241be266e3f02477d")
AMZN_2023Q3 = "AMZN-2023Q3"
AMZN_2023Q3_SHA = load_private_fixture("12df13ef728d22e59ab8bd3bdd71209cfecf5b092f12086de145b1bddbc9039a")


# ----- verbatim spans sourced from the committed extracted tree -----
#
# Each span was verified to be a contiguous substring of the
# source unit's ``text`` field and to be ≥32 characters (the
# ``min_span_len`` ADR 0021 default). Sources are documented inline
# on each test. The ``_P<n>`` suffix of each constant name is the
# on-disk artifact ordinal (``page-<n>.json``) of the unit quoted.
SPAN_AAPL_2022Q3_P4 = load_private_fixture("bdd6399f393a6d3344e5f4003ca3c1c4e25675190c06ccf877271bc8ce465218")
SPAN_MSFT_2023Q1_P5 = load_private_fixture("db75bd39f8313c63959234ead3790c460dfdb4a27c17dd35608b57d40b031463")
SPAN_AMZN_2023Q1_P4 = load_private_fixture("f0191acfa72b5299ff8e89bbe5f745b2884d0f54c3b1cbb120e427a67ed9a00a")
SPAN_AAPL_2023Q1_P4 = load_private_fixture("fdb797e83f682c2d5230d395abe4692e55e761809126791cba7aa11359ea1416")
SPAN_AAPL_2023Q3_P4 = load_private_fixture("dca567ff0dface67f516ac0ca280d7e705c44541e4e9091c945de82327f60ebd")
SPAN_MSFT_2023Q1_P4 = load_private_fixture("e78ab450546d8701e669787783b2ae43aade3388dca44b57cb569df5df0c67ba")
SPAN_MSFT_2023Q3_P4 = load_private_fixture("aebdebca1f5cec3b5789559a751709b4268cdbebd6eeff90a27240764bee6590")
SPAN_AMZN_2023Q3_P4 = load_private_fixture("211f944d98fbaac2e9da788f935f047f9f2b477b02197827b728823df1a7dd84")


# ----- helpers -----


def _read_call(document_id: str, version: str, unit: int, call_id: str) -> AIMessage:
    """One scripted ``read_source`` tool call of one frozen unit.

    ``position`` is the leading 0 so the call reads the unit from its
    first code point — every gold unit fits the bounded slice (8,000
    code points / 32 KiB serialized), so one call conveys the whole
    unit text and no continuation cursor is needed.
    """
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "read_source",
                "args": {
                    "source_ref": {
                        "document_id": document_id,
                        "version": version,
                        "unit": unit,
                    },
                    "position": 0,
                },
                "id": call_id,
                "type": "tool_call",
            }
        ],
    )


def _thread_config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


def _invoke(graph, thread_id: str, question: str):
    """Invoke the compiled graph with a fresh, empty input state.

    Each call constructs its own checkpointer + model via the
    factory, so this helper only standardizes the input dict and
    thread config — the graph object is the one the factory
    returned.
    """
    return graph.invoke(
        {
            "messages": [HumanMessage(question)],
            "evidence": [],
            "repair_count": 0,
            "step_count": 0,
        },
        config=_thread_config(thread_id),
    )


# ============================== 3 fact cases ==============================


def test_fact_01_aapl_2022q3_total_net_sales(scripted_graph_factory):
    """Single-filing fact: Apple's total net sales for the quarter
    ended June 25, 2022 (Q3 FY22) come from unit 4 of the 2022 Q3
    filing — the condensed consolidated statements of operations.
    Source span: ``data/corpus/extracted/AAPL-2022Q3/page-004.json``."""
    script = [
        _read_call(AAPL_2022Q3, AAPL_2022Q3_SHA, unit=4, call_id="call-fact-01"),
        AIMessage(
            content=(
                "Apple's total net sales for the three months ended "
                "June 25, 2022, per the condensed consolidated "
                "statements of operations, were "
                f"{SPAN_AAPL_2022Q3_P4}."
            )
        ),
    ]
    out = _invoke(
        scripted_graph_factory(script),
        thread_id="eval-fact-01-aapl-q2-2022",
        question=(
            "What were Apple's total net sales for the three "
            "months ended June 25, 2022?"
        ),
    )

    assert out["status"] == "done"
    assert out["status_reason"] == "done"
    citations = out["citations"]
    assert len(citations) >= 1
    # The first citation's locator is the unit this run read; the
    # gate fills the list from matched evidence items, never from
    # the model's claim (ADR 0021).
    first = citations[0]
    assert first.document_id == AAPL_2022Q3
    assert first.version == AAPL_2022Q3_SHA
    assert first.unit == 4
    assert first.quote == SPAN_AAPL_2022Q3_P4
    assert out["repair_count"] == 0


def test_fact_02_msft_2023q1_cash_and_short_term_investments(scripted_graph_factory):
    """Single-filing fact: Microsoft's total cash, cash equivalents,
    and short-term investments as of December 31, 2022 come from
    unit 5 of the 2023 Q1 filing — the condensed balance sheet.
    Source span: ``data/corpus/extracted/MSFT-2023Q1/page-005.json``."""
    script = [
        _read_call(MSFT_2023Q1, MSFT_2023Q1_SHA, unit=5, call_id="call-fact-02"),
        AIMessage(
            content=(
                "Microsoft's balance sheet at December 31, 2022 "
                "reported "
                f"{SPAN_MSFT_2023Q1_P5} "
                "(in millions)."
            )
        ),
    ]
    out = _invoke(
        scripted_graph_factory(script),
        thread_id="eval-fact-02-msft-q1",
        question=(
            "What were Microsoft's total cash, cash equivalents, "
            "and short-term investments as of December 31, 2022?"
        ),
    )

    assert out["status"] == "done"
    assert out["status_reason"] == "done"
    citations = out["citations"]
    assert len(citations) >= 1
    first = citations[0]
    assert first.document_id == MSFT_2023Q1
    assert first.version == MSFT_2023Q1_SHA
    assert first.unit == 5
    assert first.quote == SPAN_MSFT_2023Q1_P5
    assert out["repair_count"] == 0


def test_fact_03_amzn_2023q1_total_net_sales(scripted_graph_factory):
    """Single-filing fact: Amazon's total net sales for the quarter
    ended March 31, 2023 come from unit 4 of the 2023 Q1 filing —
    the consolidated statements of operations.
    Source span: ``data/corpus/extracted/AMZN-2023Q1/page-004.json``.

    The span includes a newline so the cite reaches the
    ``Operating expenses:`` header; that pulls the span past the
    32-character minimum while staying a single contiguous
    substring of the unit text."""
    script = [
        _read_call(AMZN_2023Q1, AMZN_2023Q1_SHA, unit=4, call_id="call-fact-03"),
        AIMessage(
            content=(
                f"Amazon reported {SPAN_AMZN_2023Q1_P4}{load_private_fixture('da8fc97db07ef4db1ac681f2ee47a2c4aaeeabf1c7c7079994e7acce06a8de15')}"
            )
        ),
    ]
    out = _invoke(
        scripted_graph_factory(script),
        thread_id="eval-fact-03-amzn-q1",
        question=(
            "What was Amazon's total net sales for the three "
            "months ended March 31, 2023?"
        ),
    )

    assert out["status"] == "done"
    assert out["status_reason"] == "done"
    citations = out["citations"]
    assert len(citations) >= 1
    first = citations[0]
    assert first.document_id == AMZN_2023Q1
    assert first.version == AMZN_2023Q1_SHA
    assert first.unit == 4
    assert first.quote == SPAN_AMZN_2023Q1_P4
    assert out["repair_count"] == 0


# =========================== 3 cross-period cases ==========================


def test_xperiod_01_aapl_total_net_sales_q1_to_q3_2023(scripted_graph_factory):
    """Cross-period trajectory (2023 Q1–Q3 only): Apple's quarterly
    total net sales declined from the December 2022 quarter to the
    September 2023 quarter, with the model consulting unit 4 of
    all three 2023 filings. Citations span ≥2 distinct locators.
    Sources: ``AAPL-2023Q1/page-004.json``,
    ``AAPL-2023Q3/page-004.json``."""
    script = [
        _read_call(AAPL_2023Q1, AAPL_2023Q1_SHA, unit=4, call_id="call-xperiod-01-q1"),
        _read_call(AAPL_2023Q2, AAPL_2023Q2_SHA, unit=4, call_id="call-xperiod-01-q2"),
        _read_call(AAPL_2023Q3, AAPL_2023Q3_SHA, unit=4, call_id="call-xperiod-01-q3"),
        AIMessage(
            content=(
                "Across calendar 2023, Apple's quarterly total net "
                "sales softened from "
                f"{SPAN_AAPL_2023Q1_P4} "
                "in Q1 to "
                f"{SPAN_AAPL_2023Q3_P4} "
                "in Q3, per the condensed consolidated statements "
                "of operations."
            )
        ),
    ]
    out = _invoke(
        scripted_graph_factory(script),
        thread_id="eval-xperiod-01-aapl-q1q3",
        question=(
            "How did Apple's quarterly total net sales move across "
            "the March, June, and September 2023 quarters?"
        ),
    )

    assert out["status"] == "done"
    citations = out["citations"]
    assert len(citations) >= 2
    distinct_locators = {(c.document_id, c.unit) for c in citations}
    assert len(distinct_locators) >= 2, distinct_locators
    # The two quoted spans come from distinct documents, so the
    # distinct-locator set must include both Q1 and Q3 filings.
    doc_ids = {c.document_id for c in citations}
    assert AAPL_2023Q1 in doc_ids
    assert AAPL_2023Q3 in doc_ids
    assert out["repair_count"] == 0


def test_xperiod_02_msft_net_income_q1_q2_q3_2023(scripted_graph_factory):
    """Cross-period trajectory (2023 Q1–Q3 only): Microsoft's
    quarterly net income rose from the December 2022 quarter to
    the June 2023 quarter, with the model consulting unit 4 of
    the Q1 and Q3 filings. Sources:
    ``MSFT-2023Q1/page-004.json``, ``MSFT-2023Q3/page-004.json``."""
    script = [
        _read_call(MSFT_2023Q1, MSFT_2023Q1_SHA, unit=4, call_id="call-xperiod-02-q1"),
        _read_call(MSFT_2023Q2, MSFT_2023Q2_SHA, unit=4, call_id="call-xperiod-02-q2"),
        _read_call(MSFT_2023Q3, MSFT_2023Q3_SHA, unit=4, call_id="call-xperiod-02-q3"),
        AIMessage(
            content=(
                "Microsoft's quarterly net income moved from "
                f"{SPAN_MSFT_2023Q1_P4} "
                "in the December 2022 quarter to "
                f"{SPAN_MSFT_2023Q3_P4} "
                "in the September 2023 quarter, per the "
                "comprehensive income statements."
            )
        ),
    ]
    out = _invoke(
        scripted_graph_factory(script),
        thread_id="eval-xperiod-02-msft-q1q3",
        question=(
            "How did Microsoft's quarterly net income trend across "
            "the December 2022, March 2023, and September 2023 "
            "quarters?"
        ),
    )

    assert out["status"] == "done"
    citations = out["citations"]
    assert len(citations) >= 2
    distinct_locators = {(c.document_id, c.unit) for c in citations}
    assert len(distinct_locators) >= 2, distinct_locators
    doc_ids = {c.document_id for c in citations}
    assert MSFT_2023Q1 in doc_ids
    assert MSFT_2023Q3 in doc_ids
    assert out["repair_count"] == 0


def test_xperiod_03_amzn_total_net_sales_q1_to_q3_2023(scripted_graph_factory):
    """Cross-period trajectory (2023 Q1–Q3 only): Amazon's total
    net sales grew from the March 2023 quarter to the September
    2023 quarter, with the model consulting unit 4 of the Q1 and
    Q3 filings. Sources: ``AMZN-2023Q1/page-004.json``,
    ``AMZN-2023Q3/page-004.json``."""
    script = [
        _read_call(AMZN_2023Q1, AMZN_2023Q1_SHA, unit=4, call_id="call-xperiod-03-q1"),
        _read_call(AMZN_2023Q2, AMZN_2023Q2_SHA, unit=4, call_id="call-xperiod-03-q2"),
        _read_call(AMZN_2023Q3, AMZN_2023Q3_SHA, unit=4, call_id="call-xperiod-03-q3"),
        AIMessage(
            content=(
                "Amazon's total net sales rose from "
                f"{SPAN_AMZN_2023Q1_P4} "
                "in the March 2023 quarter to "
                f"{SPAN_AMZN_2023Q3_P4} "
                "in the September 2023 quarter, per the "
                "consolidated statements of operations."
            )
        ),
    ]
    out = _invoke(
        scripted_graph_factory(script),
        thread_id="eval-xperiod-03-amzn-q1q3",
        question=(
            "How did Amazon's total net sales move between the "
            "March 2023 and September 2023 quarters?"
        ),
    )

    assert out["status"] == "done"
    citations = out["citations"]
    assert len(citations) >= 2
    distinct_locators = {(c.document_id, c.unit) for c in citations}
    assert len(distinct_locators) >= 2, distinct_locators
    doc_ids = {c.document_id for c in citations}
    assert AMZN_2023Q1 in doc_ids
    assert AMZN_2023Q3 in doc_ids
    assert out["repair_count"] == 0


# ============================== 3 refuse cases =============================


def test_refuse_01_unknown_locator_no_repair(scripted_graph_factory):
    """``unknown_locator`` (no repair): the model answer cites a
    well-formed document_id (``AAPL-2099Q4``) that is not in the
    frozen 12-PDF corpus. The gate refuses immediately on the
    first failure — ``repair_count`` stays at 0 and the run
    status is ``refuse`` with reason ``unknown_locator``.

    No tool call is needed: ``unknown_locator`` is a regex
    pre-check on the answer text (``[A-Z]{2,5}-NNNNQ[N]`` shape),
    so it fires before span matching (citation_gate.py step 1).

    Why ``AAPL-2099Q4`` and not a fully fictional ticker like
    ``FOO-2023Q1`` (the original wording pre-Soft-Track):
    Soft-Track classifier (``fin.host.grounded.track_decision``) routes a
    filing-ask with no recognized company (``_COMPANY_CANON`` knows
    only AAPL/MSFT/AMZN) to ``clarify`` instead of ``research``, so
    the citation gate never runs. The spec intent — ``unknown_locator``
    fires on a model-fabricated locator — is preserved by keeping
    ``AAPL`` (a recognized company, so the question routes to
    research) and shifting the fabrication into the period slot
    (``2099Q4``: well-formed but not in the 12-PDF manifest),
    which the locator regex still flags."""
    script = [
        AIMessage(
            content=(
                "Per the AAPL-2099Q4 filing, revenue grew strongly "
                "across every segment this quarter and operating "
                "margin improved materially."
            )
        ),
    ]
    out = _invoke(
        scripted_graph_factory(script),
        thread_id="eval-refuse-01-unknown",
        question="What was AAPL's revenue in Q1 2023?",
    )

    assert out["status"] == "refuse"
    assert out["status_reason"] == "unknown_locator"
    # No repair accumulated: refuses on the first failure.
    assert out["repair_count"] == 0
    # No citations populated on refuse.
    assert out.get("citations", ()) == ()


def test_refuse_02_unciteable_answer_hits_repair_cap(scripted_graph_factory):
    """``citation_repair_cap``: the model consults a real filing but
    its final answer paraphrases without a ≥32-char verbatim span
    from the unit it read. The gate bounces once per attempt; after
    three bounces the run refuses with reason
    ``citation_repair_cap`` and ``repair_count == REPAIR_CAP``.

    The script is exhausted after the first answer AIMessage, so
    ``ScriptedChatModel`` repeats the same vague answer forever
    (T6 contract) — that is what drives three bounces."""
    script = [
        _read_call(AAPL_2022Q3, AAPL_2022Q3_SHA, unit=4, call_id="call-refuse-02"),
        AIMessage(
            content=(
                "I believe revenue moved higher this quarter "
                "compared with the prior-year period, though I "
                "cannot quote the exact figure from this filing."
            )
        ),
    ]
    out = _invoke(
        scripted_graph_factory(script),
        thread_id="eval-refuse-02-repair-cap",
        question=(
            "What were Apple's total net sales for the three "
            "months ended June 25, 2022?"
        ),
    )

    assert out["status"] == "refuse"
    assert out["status_reason"] == "citation_repair_cap"
    # Three bounces → repair counter saturates at REPAIR_CAP.
    assert out["repair_count"] == REPAIR_CAP
    # The repair fuse fires well below the step fuse: bouncing 3
    # times consumes ~5 model entries, far below STEP_CAP=32.
    assert out["step_count"] < STEP_CAP
    assert out.get("citations", ()) == ()


def test_refuse_03_cites_page_not_read_this_run(scripted_graph_factory):
    """Wrong-unit cite: the model reads unit 4 of MSFT-2023Q1 (the
    comprehensive income statement) and then answers by quoting
    a verbatim span that exists on unit 5 of the SAME filing
    (the balance sheet line). The unit 5 quote is real corpus
    text, so the answer is well-formed prose — but it rests on a
    unit this run never fetched. The gate bounces because no
    ≥32-char span from the answer appears in the read unit's
    evidence; three bounces → refuse (``citation_repair_cap``).

    Choice documented: the natural outcome is
    ``citation_repair_cap`` (NOT ``step_cap``). With one read
    followed by repeating the same bad answer, three bounces
    consume ~5 model entries, far below ``STEP_CAP`` (32); the
    repair fuse fires first. This case is the
    ``evidence-from-this-run-only`` property of the gate
    (ADR 0021) — it would be wrong to cite a real corpus span
    that the run did not actually read.
    Source span: ``data/corpus/extracted/MSFT-2023Q1/page-005.json``
    (NOT the unit this run reads)."""
    # Span we will quote in the answer (balance-sheet line):
    p5_quote = load_private_fixture("db75bd39f8313c63959234ead3790c460dfdb4a27c17dd35608b57d40b031463")
    # Page we actually read this run (comprehensive income):
    script = [
        _read_call(MSFT_2023Q1, MSFT_2023Q1_SHA, unit=4, call_id="call-refuse-03"),
        AIMessage(
            content=(
                "Liquidity stayed ample: "
                f"{p5_quote} "
                "at December 31, 2022, per the 10-Q."
            )
        ),
    ]
    out = _invoke(
        scripted_graph_factory(script),
        thread_id="eval-refuse-03-wrong-page",
        question=(
            "What were Microsoft's total cash, cash equivalents, "
            "and short-term investments as of December 31, 2022?"
        ),
    )

    assert out["status"] == "refuse"
    assert out["status_reason"] == "citation_repair_cap"
    assert out["repair_count"] == REPAIR_CAP
    assert out["step_count"] < STEP_CAP
    # Evidence was contributed (the read succeeded) but no span
    # in the answer matches it — the gate bounced for that
    # reason, not for a missing-evidence path.
    assert len(out["evidence"]) == 1
    assert out.get("citations", ()) == ()