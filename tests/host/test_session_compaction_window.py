from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from fin.host.session.compaction import (
    CompactionConfigError,
    CompactionResult,
    SessionCompactor,
    SummaryModelError,
    compact_session,
)


def _messages(count: int) -> list[HumanMessage | AIMessage]:
    return [
        HumanMessage(content=f"question-{index}")
        if index % 2 == 0
        else AIMessage(content=f"answer-{index}")
        for index in range(count)
    ]


def test_long_thread_is_shortened_and_latest_snapshot_survives() -> None:
    result = compact_session(
        _messages(10),
        window_size=6,
        keep_last=3,
        status_snapshots=[
            {
                "company": "AAPL",
                "period": "2023 Q3",
                "document_ids": ["AAPL-2023Q3"],
            }
        ],
    )

    assert isinstance(result, CompactionResult)
    assert result.compacted is True
    assert len(result.messages) == 4
    assert isinstance(result.messages[0], SystemMessage)
    assert "AAPL" in result.messages[0].content
    assert "2023 Q3" in result.messages[0].content
    assert result.messages[-1].content == "answer-9"
    assert result.evicted_count == 7


def test_incremental_summary_only_receives_evicted_history() -> None:
    calls: list[str] = []

    class RecordingSummaryModel:
        def invoke(self, messages: list[SystemMessage | HumanMessage]) -> AIMessage:
            calls.append("\n".join(str(message.content) for message in messages))
            return AIMessage(content=f"summary-{len(calls)}")

    compactor = SessionCompactor(
        window_size=3,
        keep_last=2,
        summary_model=RecordingSummaryModel(),
    )
    first = compactor.compact(_messages(8))
    second = compactor.compact(
        first.messages
        + [
            HumanMessage(content="new-question"),
            AIMessage(content="new-answer"),
        ],
        previous_summary=first.summary,
    )

    assert first.summary == "summary-1"
    assert second.summary == "summary-2"
    assert len(calls) == 2
    assert "answer-7" not in calls[0]
    assert "new-question" not in calls[1]
    assert "summary-1" in calls[1]
    assert second.messages[-1].content == "new-answer"


def test_recompaction_carries_forward_embedded_summary_and_snapshot() -> None:
    compactor = SessionCompactor(window_size=3, keep_last=1)
    first = compactor.compact(
        _messages(5),
        status_snapshots=[
            {
                "company": "AAPL",
                "period": "2023 Q3",
                "document_ids": ["AAPL-2023Q3"],
            }
        ],
    )

    second = compactor.compact(
        first.messages
        + [
            HumanMessage(content="new-question-1"),
            HumanMessage(content="new-question-2"),
            HumanMessage(content="new-question-3"),
        ]
    )

    assert second.compacted is True
    assert "question-0" in second.summary
    assert "AAPL" in second.messages[0].content
    assert "2023 Q3" in second.messages[0].content


def test_empty_history_is_a_noop() -> None:
    result = compact_session([], window_size=4, keep_last=2)

    assert result.compacted is False
    assert result.messages == []
    assert result.summary == ""
    assert result.evicted_count == 0


@pytest.mark.parametrize(
    ("window_size", "keep_last"),
    [(0, 1), (-1, 1), (3, 0), (2, -1), (2, 3)],
)
def test_negative_or_invalid_window_configuration_is_typed(
    window_size: int, keep_last: int
) -> None:
    with pytest.raises(CompactionConfigError):
        SessionCompactor(window_size=window_size, keep_last=keep_last)


def test_summary_overflow_is_bounded() -> None:
    result = compact_session(
        [HumanMessage(content="x" * 500) for _ in range(8)],
        window_size=4,
        keep_last=1,
        max_summary_chars=40,
    )

    assert result.compacted is True
    assert len(result.summary) <= 40
    assert result.summary.endswith("…")


def test_shared_compactor_is_safe_for_concurrent_calls() -> None:
    compactor = SessionCompactor(window_size=4, keep_last=2)

    def compact(index: int) -> CompactionResult:
        return compactor.compact(
            _messages(6) + [HumanMessage(content=f"thread-{index}")]
        )

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(compact, range(12)))

    assert [result.messages[-1].content for result in results] == [
        f"thread-{index}" for index in range(12)
    ]
    assert all(result.compacted for result in results)


def test_summary_model_failure_is_typed() -> None:
    class FailingSummaryModel:
        def invoke(self, messages: list[SystemMessage | HumanMessage]) -> AIMessage:
            raise RuntimeError("summary service unavailable")

    compactor = SessionCompactor(
        window_size=3,
        keep_last=1,
        summary_model=FailingSummaryModel(),
    )

    with pytest.raises(SummaryModelError, match="could not create session summary"):
        compactor.compact(_messages(5))
