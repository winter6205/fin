"""Unit contracts for the issuer fan-out plan / join (T6, spec Focus 4).

These are the pure halves of the fan-out: ``plan_issuer_fanout``
normalizes and caps the issuer list the model asked for, and
``join_issuer_findings`` folds the per-issuer worker findings into the
``fin.issuer_fanout.v1`` envelope the join node hands back. Neither
touches LangGraph, the retrieve port, or the citation verifier — the
graph wiring is exercised in ``test_issuer_fanout_in_thread.py``.
"""

from __future__ import annotations

from fin.host.stream_map import format_tool_summary
from fin.host.tools.issuer_fanout import (
    ISSUER_FANOUT_SCHEMA,
    ISSUER_FANOUT_TOOL,
    MAX_ISSUERS,
    join_issuer_findings,
    normalize_issuers,
    plan_issuer_fanout,
)


def _locator(document_id: str, page: int = 1) -> dict:
    return {"document_id": document_id, "version": "v" * 64, "page": page}


# ----- the cap itself -----


def test_default_cap_is_the_adr_0034_recommendation() -> None:
    """ADR 0034 recommends 5; the implementer may tune it, but the
    constant is the single place the number lives."""
    assert MAX_ISSUERS == 5


# ----- normalization -----


def test_normalize_issuers_uppercases_trims_and_dedupes() -> None:
    assert normalize_issuers(["aapl", " MSFT ", "Aapl", "", None]) == [
        "AAPL",
        "MSFT",
    ]


def test_normalize_issuers_tolerates_none() -> None:
    assert normalize_issuers(None) == []


# ----- planning -----


def test_plan_under_cap_returns_the_normalized_roster() -> None:
    plan = plan_issuer_fanout(
        issuers=["aapl", "msft"], query="services revenue", cap=5
    )
    assert "error" not in plan
    assert plan["issuers"] == ["AAPL", "MSFT"]
    assert plan["query"] == "services revenue"
    assert plan["cap"] == 5
    assert plan["requested"] == 2


def test_plan_at_the_cap_is_accepted() -> None:
    roster = ["AAPL", "MSFT", "NVDA", "GOOG", "AMZN"]
    plan = plan_issuer_fanout(issuers=roster, query="revenue", cap=5)
    assert "error" not in plan
    assert plan["issuers"] == roster


def test_plan_one_over_the_cap_is_a_typed_overflow() -> None:
    """SC7: N+1 issuers must be a typed overflow, never a silent drop
    down to N."""
    roster = ["AAPL", "MSFT", "NVDA", "GOOG", "AMZN", "TSLA"]
    plan = plan_issuer_fanout(issuers=roster, query="revenue", cap=5)
    assert plan["error"] == "issuer_overflow"
    assert plan["limit"] == 5
    assert plan["requested"] == 6
    # The roster is echoed in full: the operator sees what was asked
    # for, not a truncated list that hides the drop.
    assert plan["issuers"] == roster


def test_overflow_counts_after_dedupe() -> None:
    """Six spellings of five issuers is a five-issuer ask."""
    plan = plan_issuer_fanout(
        issuers=["AAPL", "aapl", "MSFT", "NVDA", "GOOG", "AMZN"],
        query="revenue",
        cap=5,
    )
    assert "error" not in plan
    assert plan["requested"] == 5


def test_plan_without_issuers_is_a_typed_empty_fanout() -> None:
    plan = plan_issuer_fanout(issuers=[], query="revenue", cap=5)
    assert plan["error"] == "empty_fanout"
    assert plan["issuers"] == []


def test_plan_without_a_query_is_a_typed_empty_fanout() -> None:
    plan = plan_issuer_fanout(issuers=["AAPL"], query="   ", cap=5)
    assert plan["error"] == "empty_fanout"


def test_every_typed_exit_still_carries_the_envelope_skeleton() -> None:
    """Mirrors ``compare_facts``: the SPA and the turn row never have
    to branch on a missing key."""
    for plan in (
        plan_issuer_fanout(issuers=[], query="revenue", cap=5),
        plan_issuer_fanout(
            issuers=[f"IS{n}" for n in range(6)], query="revenue", cap=5
        ),
    ):
        fanout = plan["fanout"]
        assert fanout["schema"] == ISSUER_FANOUT_SCHEMA
        assert fanout["issuers"] == []
        assert fanout["contributing"] == []
        assert fanout["locator_count"] == 0


# ----- joining -----


def test_join_orders_findings_by_the_planned_roster() -> None:
    """Fan-out workers finish in whatever order LangGraph merges them;
    the envelope is read by a human, so it follows the ask."""
    plan = plan_issuer_fanout(
        issuers=["AAPL", "MSFT", "NVDA"], query="revenue", cap=5
    )
    findings = [
        {"issuer": "NVDA", "status": "hit", "chunk_count": 1,
         "locators": [_locator("NVDA-2023Q3")]},
        {"issuer": "AAPL", "status": "hit", "chunk_count": 2,
         "locators": [_locator("AAPL-2023Q3"), _locator("AAPL-2023Q3", 2)]},
        {"issuer": "MSFT", "status": "empty", "chunk_count": 0,
         "locators": []},
    ]
    fanout = join_issuer_findings(plan=plan, findings=findings)["fanout"]
    assert [row["issuer"] for row in fanout["issuers"]] == [
        "AAPL",
        "MSFT",
        "NVDA",
    ]
    assert fanout["contributing"] == ["AAPL", "NVDA"]
    assert fanout["empty"] == ["MSFT"]
    assert fanout["locator_count"] == 3


def test_join_marks_a_planned_issuer_that_never_reported() -> None:
    """A worker that produced no finding at all is ``empty``, not
    absent: a dropped row would hide the gap the operator asked
    about."""
    plan = plan_issuer_fanout(issuers=["AAPL", "MSFT"], query="q", cap=5)
    fanout = join_issuer_findings(
        plan=plan,
        findings=[
            {"issuer": "AAPL", "status": "hit", "chunk_count": 1,
             "locators": [_locator("AAPL-2023Q3")]}
        ],
    )["fanout"]
    assert [row["status"] for row in fanout["issuers"]] == ["hit", "empty"]
    assert fanout["empty"] == ["MSFT"]


def test_join_keeps_an_unavailable_issuer_distinct_from_an_empty_one() -> None:
    """``retrieve_unavailable`` is not "the corpus has nothing": the
    two must not collapse into the same row (typed EXIT, ADR 0030)."""
    plan = plan_issuer_fanout(issuers=["AAPL", "MSFT"], query="q", cap=5)
    fanout = join_issuer_findings(
        plan=plan,
        findings=[
            {"issuer": "AAPL", "status": "empty", "chunk_count": 0,
             "locators": []},
            {"issuer": "MSFT", "status": "unavailable", "chunk_count": 0,
             "locators": [], "error": "retrieve_unavailable"},
        ],
    )["fanout"]
    assert fanout["empty"] == ["AAPL"]
    assert fanout["unavailable"] == ["MSFT"]
    assert fanout["contributing"] == []


def test_join_takes_the_first_finding_when_an_issuer_reports_twice() -> None:
    """Two findings for one issuer means the graph fanned out twice;
    the join keeps the first so the envelope does not depend on the
    order LangGraph happened to merge the branches in."""
    plan = plan_issuer_fanout(issuers=["AAPL"], query="q", cap=5)
    fanout = join_issuer_findings(
        plan=plan,
        findings=[
            {"issuer": "aapl", "status": "hit", "chunk_count": 1,
             "locators": [_locator("AAPL-2023Q3")]},
            {"issuer": "AAPL", "status": "unavailable", "chunk_count": 9,
             "locators": [_locator("AAPL-2023Q4")], "error": "boom"},
        ],
    )["fanout"]
    assert fanout["contributing"] == ["AAPL"]
    assert fanout["unavailable"] == []
    assert fanout["locator_count"] == 1


def test_join_buckets_an_unrecognized_status_as_empty() -> None:
    """A worker status outside the three known outcomes must not fall
    out of every bucket: the row would then be counted nowhere and the
    operator would lose the issuer they asked about."""
    plan = plan_issuer_fanout(issuers=["AAPL"], query="q", cap=5)
    fanout = join_issuer_findings(
        plan=plan,
        findings=[
            {"issuer": "AAPL", "status": "weird", "chunk_count": 0,
             "locators": []}
        ],
    )["fanout"]
    assert fanout["issuers"][0]["status"] == "empty"
    assert fanout["empty"] == ["AAPL"]
    assert fanout["contributing"] == []
    assert fanout["unavailable"] == []


def test_join_ignores_a_finding_for_an_unplanned_issuer() -> None:
    plan = plan_issuer_fanout(issuers=["AAPL"], query="q", cap=5)
    fanout = join_issuer_findings(
        plan=plan,
        findings=[
            {"issuer": "AAPL", "status": "hit", "chunk_count": 1,
             "locators": [_locator("AAPL-2023Q3")]},
            {"issuer": "TSLA", "status": "hit", "chunk_count": 9,
             "locators": [_locator("TSLA-2023Q3")]},
        ],
    )["fanout"]
    assert [row["issuer"] for row in fanout["issuers"]] == ["AAPL"]
    assert fanout["locator_count"] == 1


# ----- workspace narration -----


def test_summary_reports_the_shape_not_the_payload() -> None:
    plan = plan_issuer_fanout(
        issuers=["AAPL", "MSFT", "NVDA"], query="services revenue", cap=5
    )
    result = join_issuer_findings(
        plan=plan,
        findings=[
            {"issuer": "AAPL", "status": "hit", "chunk_count": 1,
             "locators": [_locator("AAPL-2023Q3")]},
            {"issuer": "MSFT", "status": "hit", "chunk_count": 1,
             "locators": [_locator("MSFT-2023Q3")]},
            {"issuer": "NVDA", "status": "empty", "chunk_count": 0,
             "locators": []},
        ],
    )
    summary = format_tool_summary(ISSUER_FANOUT_TOOL, result=result)
    assert "services revenue" in summary
    assert "3 issuers" in summary
    assert "2/3" in summary
    # Raw JSON never reaches a summary string (spec line 32).
    assert "{" not in summary


def test_summary_surfaces_the_overflow_token() -> None:
    plan = plan_issuer_fanout(
        issuers=[f"IS{n}" for n in range(6)], query="revenue", cap=5
    )
    summary = format_tool_summary(ISSUER_FANOUT_TOOL, result=plan)
    assert "issuer_overflow" in summary
    assert "{" not in summary


def test_summary_falls_back_to_the_args_when_the_run_ended_early() -> None:
    summary = format_tool_summary(
        ISSUER_FANOUT_TOOL, args={"query": "revenue", "issuers": ["AAPL"]}
    )
    assert "revenue" in summary
