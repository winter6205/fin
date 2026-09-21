"""Empty Fast graph on Chinese 资料 is a 具名失败 (spec SC5), not a silent
empty index and not a healthy miss.

CONTEXT. Fast 抽图 (NLP co-occurrence) on a Chinese 资料 corpus can build
an **empty entity-relation graph** — the noun-phrase regex matched
nothing. Spec graph-index-standard-fast SC5 names that outcome a 具名失败:
it must NOT read as 资料未就绪 (``corpus_not_ready`` — the documents are
there, the method failed them) and NOT as 档未开 (``tier_not_open`` —
Fast is an extract method, never a 检索档 token, and nothing is
"unopened"). Leiden / community-summary / embedding job failures belong
to the sibling plan; this module pins only this one exit.

The public JSON/SSE/MCP **code spelling** (the spec's Open Question 1)
was confirmed by the operator 2026-09-21: ``empty_fast_graph_failure``,
following the same construct as the reserved
``community_summary_index_failure`` token. What IS settled and
pinned by name: the decision reads an explicit recorded job fact (fin
never invents it from chunk absence alone), the code is distinct from
every existing 具名失败, and it resolves through the refuse-side
ordinary-person copy channel (``status_reason`` → SSE) — a different
channel from the routing-time 400 the 未开 tiers use, which never
carries a copy at all.

The acceptance chain pinned here: Fake/recorded job fact → the
retrieve tool's ``retrieve_outcomes`` record → pure decision → host
run payload.
"""

from __future__ import annotations

import pytest

from fin.host import named_failures
from fin.host.named_failures import (
    COMMUNITY_SUMMARY_INDEX_FAILURE,
    CORPUS_NOT_READY,
    EMPTY_FAST_GRAPH_FAILURE,
    MAPPING_DROP,
    REAL_DB_NOT_WIRED,
    RETRIEVE_OUTCOME_FAST_GRAPH_EMPTY,
    TIER_NOT_OPEN,
    TIER_UNAVAILABLE_CODE,
    named_refusal_reason,
    retrieve_blocking_failure,
    retrieve_outcomes_delta,
)
from fin.host.run_payload import (
    ORDINARY_PERSON_COPY,
    _run_to_response_payload,
    ordinary_person_copy,
)
from fin.retrieve.fake_port import FakeRetrievePort


def _job_fail_record(**overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        "chunks": 0,
        "mapping_failures": 0,
        RETRIEVE_OUTCOME_FAST_GRAPH_EMPTY: True,
    }
    record.update(overrides)
    return record


# ----- the public token: reserved, typed, distinct -----


def test_empty_fast_graph_failure_is_public_with_its_wire_token() -> None:
    """Asserted by NAME and exact value, never by matching prose —
    same construct as the reserved community-summary token. The
    operator confirmed this spelling 2026-09-21 (spec Open Question
    1); the TYPE and distinctness are what this pins."""
    assert named_failures.EMPTY_FAST_GRAPH_FAILURE == (
        "empty_fast_graph_failure"
    )
    assert "EMPTY_FAST_GRAPH_FAILURE" in named_failures.__all__


def test_the_code_is_distinct_from_every_existing_named_failure() -> None:
    """An accidental alias would merge two product facts into one exit
    the operator cannot act on."""
    codes = {
        EMPTY_FAST_GRAPH_FAILURE,
        CORPUS_NOT_READY,
        MAPPING_DROP,
        TIER_NOT_OPEN,
        REAL_DB_NOT_WIRED,
        COMMUNITY_SUMMARY_INDEX_FAILURE,
    }
    assert len(codes) == 6
    # Fast is an extract method, never a 检索档: the routing table for
    # 「档未开」 keeps its two codes only.
    assert EMPTY_FAST_GRAPH_FAILURE not in TIER_UNAVAILABLE_CODE
    assert EMPTY_FAST_GRAPH_FAILURE not in TIER_UNAVAILABLE_CODE.values()


# ----- the pure decision: a RECORDED job fact, never invented -----


def test_a_recorded_fast_job_fail_names_the_failure() -> None:
    assert (
        retrieve_blocking_failure([_job_fail_record()])
        == EMPTY_FAST_GRAPH_FAILURE
    )


def test_the_job_fail_outranks_a_mapping_drop_on_the_same_run() -> None:
    """Root cause wins: an empty Fast graph explains why nothing
    mapped; charging the drop ledger alone would send the operator to
    re-run ingest while the extract method is the actual failure."""
    assert (
        retrieve_blocking_failure([_job_fail_record(mapping_failures=2)])
        == EMPTY_FAST_GRAPH_FAILURE
    )


def test_a_healthy_hit_anywhere_clears_the_job_fail() -> None:
    """Same guard as the other retrieve-side codes: once a call
    produced chunks, the refusal is the verifier's to make."""
    assert (
        retrieve_blocking_failure(
            [_job_fail_record(), {"chunks": 1, "mapping_failures": 0}]
        )
        is None
    )


@pytest.mark.parametrize("flag", [False, 1, "true", None])
def test_fin_never_invents_the_job_fail_from_anything_but_the_flag(
    flag: object,
) -> None:
    """Direction of the mapping: only an explicit True flag is the
    recorded job fact. A truthy lookalike (int 1, string) must not
    upgrade an ordinary empty collection into the Fast failure —
    that would blame a job that never reported one."""
    outcomes = [{"chunks": 0, "mapping_failures": 0}]
    if flag is not None:
        outcomes[0][RETRIEVE_OUTCOME_FAST_GRAPH_EMPTY] = flag  # type: ignore[index]

    assert retrieve_blocking_failure(outcomes) == CORPUS_NOT_READY


def test_a_record_outside_the_contract_still_contributes_nothing() -> None:
    """The pre-existing malformed-record rule is unchanged: an entry
    without well-formed counts is skipped even if it carries a flag —
    no half-read record names anything."""
    assert (
        retrieve_blocking_failure(
            [{"fast_graph_empty_x": True, RETRIEVE_OUTCOME_FAST_GRAPH_EMPTY: True}]
        )
        is None
    )


def test_the_delta_carries_the_job_fact_forward() -> None:
    """The tool seam rebuilds records through ``retrieve_outcomes_delta``;
    the job fact must survive that rebuild or the decision never sees
    it."""
    assert retrieve_outcomes_delta([_job_fail_record()], baseline=0) == [
        {
            "chunks": 0,
            "mapping_failures": 0,
            RETRIEVE_OUTCOME_FAST_GRAPH_EMPTY: True,
        }
    ]


def test_the_delta_drops_a_non_bool_flag_instead_of_coercing_it() -> None:
    assert retrieve_outcomes_delta(
        [{"chunks": 0, "mapping_failures": 0, "fast_graph_empty": "yes"}],
        baseline=0,
    ) == [{"chunks": 0, "mapping_failures": 0}]


def test_the_refusal_relabels_only_a_run_with_nothing_to_cite() -> None:
    """Same boundary as the other retrieve-side codes: evidence from
    any other route keeps the verifier's verdict."""
    assert (
        named_refusal_reason(
            evidence=[],
            outcomes=[_job_fail_record()],
            verifier_reason="unknown_locator",
        )
        == EMPTY_FAST_GRAPH_FAILURE
    )
    assert (
        named_refusal_reason(
            evidence=[object()],
            outcomes=[_job_fail_record()],
            verifier_reason="citation_repair_cap",
        )
        == "citation_repair_cap"
    )


# ----- the host exit: refuse-side run payload with its own copy -----


def test_the_recorded_run_surfaces_the_failure_at_the_host_exit() -> None:
    """The acceptance surface (录制 path): the job fact decides to the
    new code, the payload carries it UNCHANGED, and the operator gets
    a copy that is neither 未就绪 wording nor the verifier's."""
    reason = named_refusal_reason(
        evidence=[],
        outcomes=[_job_fail_record()],
        verifier_reason="unknown_locator",
    )
    payload = _run_to_response_payload(
        {"status": "refuse", "status_reason": reason}
    )

    assert payload["status"] == "refuse"
    assert payload["reason"] == EMPTY_FAST_GRAPH_FAILURE
    assert payload["citations"] == []
    assert (
        payload["assistant_message"]
        == ORDINARY_PERSON_COPY[EMPTY_FAST_GRAPH_FAILURE]
    )
    assert (
        payload["assistant_message"] != ORDINARY_PERSON_COPY[CORPUS_NOT_READY]
    )


def test_a_different_copy_channel_from_the_unopened_tiers() -> None:
    """档未开 answers a routing-time 400 and never rides the run's
    refuse copy; the Fast job fact is the opposite shape — a run fact
    with ordinary-person copy and no 400 body. If they ever shared a
    channel, one of these two assertions breaks."""
    assert ordinary_person_copy("refuse", EMPTY_FAST_GRAPH_FAILURE).strip()
    assert ordinary_person_copy("refuse", TIER_NOT_OPEN) == ""


@pytest.mark.parametrize("reason", [EMPTY_FAST_GRAPH_FAILURE])
def test_each_named_failure_has_its_own_copy(reason: str) -> None:
    copy = ORDINARY_PERSON_COPY[reason]
    assert copy.strip()
    assert reason not in copy


# ----- the Fake seam: the job fact on the port result -----


def test_the_fake_can_port_the_recorded_fast_job_fact() -> None:
    """Default pytest stays hermetic: the fake models the job outcome
    — an empty graph over the 资料 set — as a NAMED fact on the result,
    never as the silent ``{"chunks": []}`` shape."""
    port = FakeRetrievePort(
        hits_by_query={"中文资料问题": []},
        fast_graph_empty=True,
    )

    result = port.retrieve("中文资料问题")

    assert result == {
        "chunks": [],
        RETRIEVE_OUTCOME_FAST_GRAPH_EMPTY: True,
    }


def test_a_default_fake_result_keeps_the_ordinary_shape() -> None:
    """Sends that never hit a Fast job must not learn the new key —
    otherwise every empty corpus would read as a Fast failure."""
    result = FakeRetrievePort().retrieve("what was revenue")

    assert RETRIEVE_OUTCOME_FAST_GRAPH_EMPTY not in result
    assert result == {"chunks": []}


def test_the_fake_job_fact_reaches_the_host_exit_through_the_decision() -> None:
    """The full recorded chain: Fake's result → outcome record (the
    shape the tool seam carries) → decision → refusal reason."""
    result = FakeRetrievePort(fast_graph_empty=True).retrieve("中文资料问题")
    outcomes = [
        {
            "chunks": len(result["chunks"]),
            "mapping_failures": 0,
            RETRIEVE_OUTCOME_FAST_GRAPH_EMPTY: result.get(
                RETRIEVE_OUTCOME_FAST_GRAPH_EMPTY, False
            ),
        }
    ]

    reason = named_refusal_reason(
        evidence=[],
        outcomes=outcomes,
        verifier_reason="unknown_locator",
    )
    payload = _run_to_response_payload(
        {"status": "refuse", "status_reason": reason}
    )

    assert payload["reason"] == EMPTY_FAST_GRAPH_FAILURE
    assert (
        payload["assistant_message"]
        == ORDINARY_PERSON_COPY[EMPTY_FAST_GRAPH_FAILURE]
    )


def test_the_retrieve_tool_forwards_the_job_fact_into_the_run_record() -> None:
    """The tool seam itself — not a hand-built record — must carry the
    recorded Fast job fact onto ``retrieve_outcomes``, so a run over
    the Fake port reaches the host exit as EMPTY_FAST_GRAPH_FAILURE."""
    from fin.host.graph import _make_retrieve_tool
    from fin.retrieve.hit_mapping import MappingFailureLedger

    tool = _make_retrieve_tool(
        FakeRetrievePort(fast_graph_empty=True),
        mapping_ledger=MappingFailureLedger(),
    )
    command = tool.invoke(
        {
            "name": "retrieve",
            "args": {"query": "中文资料问题"},
            "id": "c-fast-empty",
            "type": "tool_call",
        }
    )

    assert command.update["retrieve_outcomes"] == [
        {
            "chunks": 0,
            "mapping_failures": 0,
            RETRIEVE_OUTCOME_FAST_GRAPH_EMPTY: True,
        }
    ]
    reason = retrieve_blocking_failure(command.update["retrieve_outcomes"])
    assert reason == EMPTY_FAST_GRAPH_FAILURE
