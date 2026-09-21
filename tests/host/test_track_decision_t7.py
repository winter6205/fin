"""T7 boundary contracts for the Jev gray-zone tier (ADR 0080).

Confidence clamping and label validation moved into the Jev client
(T1): a compliant call returns a finite ``[0, 1]`` confidence and a
closed-set label or raises a typed ``JevChoiceError``. The cascade
contract left here is fail-closed routing.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest
from langchain_core.messages import HumanMessage

from fin.host.jev_choice import (
    JEV_CHOICE_EXIT_SCHEMA,
    JEV_CHOICE_EXIT_TRANSPORT,
    JevChoiceError,
)
from fin.host.track_decision import decide_track
from jev_fake import FakeJevChoice


@pytest.mark.soft_track
def test_empty_messages_keep_deterministic_dialogue_fallback() -> None:
    decision = decide_track([])

    assert decision.track == "dialogue"
    assert decision.decision_tier == "conditional_policy"
    assert decision.confidence == 0.4


@pytest.mark.soft_track
def test_zero_confidence_judge_clarifies() -> None:
    fake = FakeJevChoice([("grounded", 0.0)])

    decision = decide_track(
        [HumanMessage(content="maybe compare these?")],
        jev_client=fake,
    )

    assert decision.track == "clarify"
    assert decision.confidence == 0.0
    assert decision.signals["jev_exit"] == "low_confidence"


@pytest.mark.soft_track
def test_max_confidence_judge_decides_jev_tier() -> None:
    fake = FakeJevChoice([("grounded", 1.0)])

    decision = decide_track(
        [HumanMessage(content="maybe compare these?")],
        jev_client=fake,
    )

    assert decision.track == "grounded"
    assert decision.decision_tier == "jev"
    assert decision.confidence == 1.0


@pytest.mark.soft_track
def test_concurrent_default_decisions_remain_independent() -> None:
    def classify(_: int):
        return decide_track([HumanMessage(content="maybe compare these?")])

    with ThreadPoolExecutor(max_workers=4) as executor:
        decisions = list(executor.map(classify, range(8)))

    assert len(decisions) == 8
    # ADR 0080: no client at the gray zone is clarify, deterministically
    # and independently per call.
    assert all(decision.track == "clarify" for decision in decisions)
    assert all(
        decision.decision_tier == "clarify" for decision in decisions
    )


@pytest.mark.soft_track
@pytest.mark.parametrize(
    "exit_code",
    [JEV_CHOICE_EXIT_TRANSPORT, JEV_CHOICE_EXIT_SCHEMA],
)
def test_judge_typed_exit_is_fail_closed_clarify_with_named_code(
    exit_code: str,
) -> None:
    fake = FakeJevChoice([JevChoiceError("down", exit_code=exit_code)])

    decision = decide_track(
        [HumanMessage(content="maybe compare these?")],
        jev_client=fake,
    )

    assert decision.track == "clarify"
    assert decision.signals["jev_exit"] == exit_code
    assert decision.decision_tier == "clarify"


@pytest.mark.soft_track
def test_unexpected_judge_raise_still_fail_closes() -> None:
    """A non-typed throw must not crash the cascade — clarify wins."""

    class _Broken:
        def choose(self, **kwargs):  # noqa: ANN003, ANN201
            raise RuntimeError("classifier unavailable")

    decision = decide_track(
        [HumanMessage(content="maybe compare these?")],
        jev_client=_Broken(),
    )

    assert decision.track == "clarify"
    assert decision.signals["jev_exit"] == "unexpected_error"
    assert decision.signals["jev_error_type"] == "RuntimeError"
