"""The served refusal copy comes from ONE owner, in Chinese, and is distinct.

The QA-chain T10 acceptance (``plans/qa-chain-04-grounded-experience.md``)
says three things about the default view that a refusal has to satisfy at
once, and this module pins all three against the copy that actually ships:

* the six-question scan finds none of ``document_id`` / ``list_files`` /
  ``10-Q`` / ``XBRL``;
* index / busy / evidence refusal / retrieval degradation use their DISTINCT
  safe copy;
* 「一般中文文案」 is what an ordinary person reads.

The projection (``fin.host.grounded.readable_projection``) is the owner: the four
frozen sentences and the reason-keyed table both live there, and
``run_payload.ordinary_person_copy`` is a delegate over them. These tests
are what makes the delegation real — a table that only the module's own
tests read would leave the English table in ``run_payload`` serving the
user while a second, better table sat unused.
"""

from __future__ import annotations

import re

import pytest

from fin.host.degradation_notice import (
    DegradationSignal,
    degradation_notice_entry,
    degradation_notice_line,
)
from fin.host.named_failures import (
    COMMUNITY_SUMMARY_INDEX_FAILURE,
    CORPUS_NOT_READY,
    EMPTY_FAST_GRAPH_FAILURE,
    INSUFFICIENT_EVIDENCE,
    MAPPING_DROP,
)
from fin.host.grounded.readable_projection import (
    DEGRADATION_SAFE_COPY,
    FAILURE_CLASS_BY_REASON,
    FAILURE_COPY,
    REASON_COPY,
    FailureClass,
    classify_failure,
    failure_copy,
    leaked_fragments,
    ordinary_person_copy,
)
from fin.host.run_payload import (
    ORDINARY_PERSON_COPY,
    _run_to_response_payload,
)

# The four fragments the T10 acceptance names literally.
FORBIDDEN_SCAN = ("document_id", "list_files", "10-Q", "XBRL")

# Every serveable refusal reason a run can end on, read off the owner rather
# than restated here — a reason this list misses would silently escape the
# scan.
SERVED_REASONS = tuple(REASON_COPY) + tuple(FAILURE_CLASS_BY_REASON)


def _served(reason: str) -> str:
    return ordinary_person_copy("refuse", reason)


def _latin_runs(text: str) -> list[str]:
    """Runs of three or more latin letters — English prose left in the copy."""
    return re.findall(r"[A-Za-z]{3,}", text)


# ----- (1) the four classes reach the user, and they are four sentences -----

CLASSES: tuple[FailureClass, ...] = (
    "index_work",
    "busy",
    "evidence_refusal",
    "retrieval_degraded",
)


def test_the_four_classes_are_four_distinct_chinese_sentences() -> None:
    copies = [failure_copy(cls) for cls in CLASSES]

    assert len(set(copies)) == 4
    for copy in copies:
        assert copy.strip()
        assert not _latin_runs(copy), copy


def test_each_class_has_a_reason_that_serves_its_own_sentence() -> None:
    """Reachability: every class is reachable THROUGH the served copy, not
    only through ``failure_copy``. A class no reason maps to would be the
    dead table this module exists to prevent."""

    served = {_served(reason) for reason in SERVED_REASONS}
    served |= {
        ordinary_person_copy(
            "refuse", reason, readiness={"error": "not_ready", "cause": "index", "stage": "index"}
        )
        for reason in SERVED_REASONS
    }
    served |= {
        ordinary_person_copy(
            "refuse",
            reason,
            readiness={"error": "not_ready", "cause": "busy", "stage": "queue"},
        )
        for reason in SERVED_REASONS
    }
    served |= {
        ordinary_person_copy("refuse", reason, degraded=True)
        for reason in SERVED_REASONS
    }

    assert set(failure_copy(cls) for cls in CLASSES) <= served


def test_the_index_class_is_not_the_refusal_class() -> None:
    """GE-5: index copy and insufficient-evidence copy are DISTINCT. Before
    the fix both rode the one no-evidence sentence.

    ``corpus_not_ready`` is the only reason that moves: it leaves the
    refusal sentence for the index one. Every other 具名失败 reason keeps a
    sentence distinct from its neighbours, which is what the committed
    ``test_the_three_exits_are_distinguishable_on_the_wire`` (SC4/SC5)
    and ``test_numeric_gate_fail_copy_differs_from_other_refusals`` pin."""

    assert _served(CORPUS_NOT_READY) == FAILURE_COPY["index_work"]
    assert _served(CORPUS_NOT_READY) != _served(INSUFFICIENT_EVIDENCE)
    assert _served(MAPPING_DROP) != _served(INSUFFICIENT_EVIDENCE)
    assert _served(MAPPING_DROP).strip()


# A degraded response the way §B3 raises one: the boolean set, one
# recorded reason. The reasons themselves stay on the ledger.
SIGNAL = DegradationSignal(
    degraded=True, degraded_reasons=("vector:attempt_failed",)
)


def test_the_degradation_notice_and_the_degradation_class_are_one_string() -> None:
    """One owner: the tool-fold notice and the terminal card render the SAME
    sentence, so a degraded run cannot read differently in two places."""

    line = degradation_notice_line(SIGNAL)

    assert line == FAILURE_COPY["retrieval_degraded"] == DEGRADATION_SAFE_COPY
    for reason in SIGNAL.degraded_reasons:
        assert reason not in line


def test_a_degraded_terminal_renders_the_degradation_sentence() -> None:
    payload = _run_to_response_payload(
        {"status": "refuse", "status_reason": INSUFFICIENT_EVIDENCE},
        degraded=True,
    )

    assert payload["assistant_message"] == DEGRADATION_SAFE_COPY
    assert payload["assistant_message"] != _served(INSUFFICIENT_EVIDENCE)


def test_a_degraded_run_is_read_off_its_own_tool_trace() -> None:
    """The production path: no caller passes ``degraded=`` — the payload
    reads the run's own ``tool_summaries``, the row
    ``degradation_notice_entry`` wrote. A payload that ignored that row
    would serve a refusal sentence on a degraded run, which is the
    unreachable-class defect this ticket closed."""

    payload = _run_to_response_payload(
        {
            "status": "refuse",
            "status_reason": INSUFFICIENT_EVIDENCE,
            "tool_summaries": [degradation_notice_entry(SIGNAL)],
        }
    )

    assert payload["assistant_message"] == DEGRADATION_SAFE_COPY


# ----- (2) no forbidden fragment in ANY served copy -----

@pytest.mark.parametrize("reason", sorted(SERVED_REASONS))
def test_no_served_refusal_copy_carries_a_forbidden_fragment(reason: str) -> None:
    copy = _served(reason)

    for fragment in FORBIDDEN_SCAN:
        assert fragment not in copy, (reason, fragment)
    assert not leaked_fragments(copy), (reason, leaked_fragments(copy))


@pytest.mark.parametrize("reason", sorted(SERVED_REASONS))
def test_every_served_refusal_copy_is_chinese(reason: str) -> None:
    """GE-5: 「一般中文文案」. A refusal the operator reads in English is a
    refusal the spec does not accept, whatever it says."""

    copy = _served(reason)

    assert any("一" <= ch <= "鿿" for ch in copy), copy
    assert _latin_runs(copy) == [], copy


def test_the_unknown_locator_refusal_does_not_reach_for_the_filing_vocabulary() -> None:
    """The English table's ``unknown_locator`` sentence named the frozen
    ``10-Qs``; T10's six-question scan forbids that fragment outright."""

    copy = _served("unknown_locator")

    assert "10-Q" not in copy
    assert copy == ORDINARY_PERSON_COPY["unknown_locator"]


@pytest.mark.parametrize("reason", sorted(SERVED_REASONS))
def test_no_served_copy_names_its_own_reason_token(reason: str) -> None:
    copy = _served(reason)

    assert reason not in copy
    assert "_" not in copy


def test_the_step_cap_card_is_chinese_too() -> None:
    payload = _run_to_response_payload(
        {"status": "step_cap", "status_reason": "step_cap"}
    )

    assert payload["assistant_message"].strip()
    assert _latin_runs(payload["assistant_message"]) == []
    for fragment in FORBIDDEN_SCAN:
        assert fragment not in payload["assistant_message"]


# ----- (3) the payload the wire and the turn row are built from -----

@pytest.mark.parametrize("reason", sorted(SERVED_REASONS))
def test_every_served_reason_reaches_the_wire_payload(reason: str) -> None:
    payload = _run_to_response_payload(
        {"status": "refuse", "status_reason": reason}
    )

    assert payload["answer"] == ""
    assert payload["citations"] == []
    assert payload["assistant_message"] == ORDINARY_PERSON_COPY[reason]
    assert payload["assistant_message"].strip()


def test_the_refusal_names_no_citation_and_no_figure() -> None:
    refusal = _served(CORPUS_NOT_READY)

    assert not [ch for ch in refusal if ch.isdigit()]
    assert refusal.count("（一）") + refusal.count("（二）") == 0


# ----- (4) one owner, not two tables that can disagree -----

def test_the_reason_table_is_built_from_the_class_table() -> None:
    """``ORDINARY_PERSON_COPY`` cannot carry a sentence the class table does
    not produce — a hand-maintained duplicate is exactly the drift this
    ticket removed."""

    for reason, cls in FAILURE_CLASS_BY_REASON.items():
        expected = REASON_COPY.get(reason, failure_copy(cls))
        assert ORDINARY_PERSON_COPY[reason] == expected, reason


def test_an_unknown_terminal_reason_still_renders_nothing_rather_than_guessing() -> None:
    assert ordinary_person_copy("refuse", "tier_not_open") == ""
    assert ordinary_person_copy("refuse", "something_never_declared") == ""


def test_the_diagnostics_classification_still_names_an_unknown_reason() -> None:
    """The strict accessor keeps its raise: a caller that must be told it has
    an unclassified terminal is a diagnostics surface, and the default view
    is not it."""

    from fin.host.grounded.readable_projection import UnclassifiableTerminal

    with pytest.raises(UnclassifiableTerminal):
        classify_failure(status_reason="tier_not_open")


def test_the_two_index_job_failures_keep_their_own_advice() -> None:
    """Both name an index, but one is a job that FAILED and the other is a
    method that does not fit the documents — collapsing them would send the
    operator to re-run the wrong job."""

    assert ORDINARY_PERSON_COPY[COMMUNITY_SUMMARY_INDEX_FAILURE] != ORDINARY_PERSON_COPY[
        EMPTY_FAST_GRAPH_FAILURE
    ]
