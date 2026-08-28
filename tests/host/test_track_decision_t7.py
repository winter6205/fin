"""T7 boundary contracts for confidence-aware optional classification."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from fin.host.track_decision import decide_track


class _ClassifyResponse:
    def __init__(
        self,
        content: str | None = None,
        error: Exception | None = None,
    ) -> None:
        self.content = content
        self.error = error

    def invoke(self, messages: Any, **kwargs: Any) -> AIMessage:
        if self.error is not None:
            raise self.error
        return AIMessage(content=self.content or "")


@pytest.mark.soft_track
def test_empty_messages_keep_deterministic_dialogue_fallback() -> None:
    decision = decide_track([])

    assert decision.track == "dialogue"
    assert decision.decision_tier == "conditional_policy"
    assert decision.confidence == 0.4


@pytest.mark.soft_track
def test_negative_classify_confidence_becomes_zero_and_clarifies() -> None:
    model = _ClassifyResponse(
        '{"track": "research", "confidence": -0.25}'
    )

    decision = decide_track(
        [HumanMessage(content="maybe compare these?")],
        classify_model=model,
    )

    assert decision.track == "clarify"
    assert decision.confidence == 0.0
    assert decision.signals["classify_exit"] == "low_confidence"


@pytest.mark.soft_track
def test_overflow_classify_confidence_is_capped() -> None:
    model = _ClassifyResponse(
        '{"track": "research", "confidence": 1.25}'
    )

    decision = decide_track(
        [HumanMessage(content="maybe compare these?")],
        classify_model=model,
    )

    assert decision.track == "research"
    assert decision.decision_tier == "llm"
    assert decision.confidence == 1.0


@pytest.mark.soft_track
def test_concurrent_default_decisions_remain_independent() -> None:
    def classify(_: int):
        return decide_track([HumanMessage(content="maybe compare these?")])

    with ThreadPoolExecutor(max_workers=4) as executor:
        decisions = list(executor.map(classify, range(8)))

    assert len(decisions) == 8
    assert all(decision.track == "dialogue" for decision in decisions)
    assert all(
        decision.decision_tier == "conditional_policy"
        for decision in decisions
    )


@pytest.mark.soft_track
def test_classify_exception_is_typed_and_does_not_crash() -> None:
    model = _ClassifyResponse(error=RuntimeError("classifier unavailable"))

    decision = decide_track(
        [HumanMessage(content="maybe compare these?")],
        classify_model=model,
    )

    assert decision.track == "dialogue"
    assert decision.signals["classify_exit"] == "model_error"
    assert decision.decision_tier == "conditional_policy"
