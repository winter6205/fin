"""T6b dream contracts: scoring, the double gate, and post-transport state.

``specs/memory-dream.md`` SC12-SC21. dream is the rule-gated consolidate
step of ``CONTEXT.md:45``: it picks *useful* buffer rows and hands them to
an injected backend. With no backend configured it stops at the buffer.

Boundary classes: empty (empty buffer, never-dreamed cursor, no
candidates); negative (non-integer ``hit_count``, malformed summarizer
output, corrupt cursor); overflow (more candidates than
``MEMORY_LIMIT_CAP``); concurrent (one dream per window under contention);
exception (backend raises, summarizer raises, cursor corrupt).
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from fin.host.memory_buffer import LocalMemoryBuffer
from fin.host.memory_dream import (
    DREAM_MIN_INTERVAL,
    DREAM_MIN_THREADS,
    LONGEVITY_DAYS,
    TRANSPORT_RETENTION_DAYS,
    DreamGate,
    DreamInputError,
    DreamTransportError,
    MemoryDream,
    MemoryDreamError,
    MemoryDreamScorer,
    NullDreamBackend,
    NullDreamSummarizer,
    ScoredMemory,
)
from fin.host.memory_port import (
    MEMORY_LIMIT_CAP,
    DreamCursorError,
    MemoryPortError,
)
from fin.host.memory_store import (
    InProcessDreamCursorStore,
    InProcessMemoryStore,
    MemoryRow,
    parse_dream_cursor,
)

NOW = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)


def _thread_ids(count: int) -> list[str]:
    return [f"thread-{index}" for index in range(count)]


def _row(
    row_id: str,
    *,
    hit_count: int = 0,
    age_days: int = 0,
    thread_id: str | None = None,
    supersedes: str | None = None,
) -> MemoryRow:
    metadata: dict[str, object] = {}
    if thread_id is not None:
        metadata["thread_id"] = thread_id
    if supersedes is not None:
        metadata["supersedes"] = supersedes
    return MemoryRow(
        id=row_id,
        text=f"memory {row_id}",
        created_at=NOW - timedelta(days=age_days),
        metadata=metadata,
        hit_count=hit_count,
    )


class _RecordingBackend:
    """Backend that records payloads and reports a configurable count."""

    def __init__(self, stored: int | None = None) -> None:
        self._stored = stored
        self.payloads: list[list[dict[str, object]]] = []
        self.calls = 0

    def persist(self, memories) -> int:
        self.calls += 1
        self.payloads.append(list(memories))
        return len(memories) if self._stored is None else self._stored


class _ExplodingBackend:
    def persist(self, memories) -> int:
        raise OSError("memory MCP is down")


class _MarkingSummarizer:
    """Rewrites the payload so an enabled summarizer is observable."""

    def __init__(self) -> None:
        self.calls = 0

    def summarize(self, memories):
        self.calls += 1
        return [
            ScoredMemory(
                MemoryRow(
                    id=item.row.id,
                    text=f"{item.row.text} (condensed)",
                    created_at=item.row.created_at,
                    metadata=dict(item.row.metadata),
                    hit_count=item.row.hit_count,
                ),
                item.score,
                item.reasons,
            )
            for item in memories
        ]


class _ExplodingSummarizer:
    def summarize(self, memories):
        raise RuntimeError("model unavailable")


class _UnparseableSummarizer:
    def summarize(self, memories):
        return "not a sequence of scored memories"


def _cursors_dreamed_at(at: datetime) -> InProcessDreamCursorStore:
    store = InProcessDreamCursorStore()
    store.advance(at=at, seen_thread_ids=frozenset(), last_status="ok")
    return store


def _store_with_threads(thread_ids: list[str]) -> InProcessMemoryStore:
    store = InProcessMemoryStore()
    for index, thread_id in enumerate(thread_ids):
        store.add(_row(f"row-{index}", thread_id=thread_id))
    return store


def _store_with_useful_rows(thread_ids: list[str]) -> InProcessMemoryStore:
    """Rows that clear the score threshold, so the dream has a batch."""
    store = _store_with_threads(thread_ids)
    for row in store.rows():
        store.bump_hit_counts([row.id] * 2)
    return store


def _buffer_with_useful_memories(**kwargs) -> LocalMemoryBuffer:
    """A local buffer whose rows all clear the score threshold."""
    buffer = LocalMemoryBuffer(**kwargs)
    for index in range(DREAM_MIN_THREADS):
        buffer.extract(f"memory {index}", metadata={"thread_id": f"thread-{index}"})
    buffer.recall("memory")
    buffer.recall("memory")
    return buffer


# ----- SC12: the scorer is a pure function -----


def test_scoring_is_deterministic_and_side_effect_free() -> None:
    rows = [_row("a", hit_count=2), _row("b", hit_count=1, age_days=8)]

    first = MemoryDreamScorer().score(rows, NOW)
    second = MemoryDreamScorer().score(rows, NOW)

    assert first == second
    assert rows[0].hit_count == 2


def test_repeated_hits_score_three_and_are_selected() -> None:
    scored = MemoryDreamScorer().score([_row("a", hit_count=2)], NOW)

    assert [(item.row.id, item.score) for item in scored] == [("a", 3)]
    assert scored[0].reasons == ("repeated_hits",)


def test_a_single_hit_scores_one_and_is_not_selected() -> None:
    scored = MemoryDreamScorer().score([_row("a", hit_count=1)], NOW)

    assert scored[0].score == 1
    assert MemoryDreamScorer().select(scored) == []


def test_longevity_adds_two() -> None:
    scored = MemoryDreamScorer().score([_row("a", age_days=LONGEVITY_DAYS)], NOW)

    assert scored[0].score == 2
    assert scored[0].reasons == ("longevity",)


def test_a_single_hit_plus_longevity_reaches_the_threshold() -> None:
    scored = MemoryDreamScorer().score(
        [_row("a", hit_count=1, age_days=LONGEVITY_DAYS)], NOW
    )

    assert scored[0].score == 3
    assert [item.row.id for item in MemoryDreamScorer().select(scored)] == ["a"]


def test_repeated_hits_plus_longevity_score_five() -> None:
    scored = MemoryDreamScorer().score(
        [_row("a", hit_count=3, age_days=LONGEVITY_DAYS)], NOW
    )

    assert scored[0].score == 5


def test_superseded_rows_are_excluded() -> None:
    rows = [
        _row("old", hit_count=5),
        _row("new", hit_count=5, supersedes="old"),
    ]

    scored = MemoryDreamScorer().score(rows, NOW)

    assert [item.row.id for item in scored] == ["new"]


def test_empty_input_scores_nothing() -> None:
    assert MemoryDreamScorer().score([], NOW) == []


@pytest.mark.parametrize("hit_count", ["2", None, 1.5, True])
def test_a_non_integer_hit_count_is_a_typed_input_error(hit_count: object) -> None:
    rows = [MemoryRow(id="a", text="x", created_at=NOW, hit_count=hit_count)]

    with pytest.raises(DreamInputError):
        MemoryDreamScorer().score(rows, NOW)


def test_non_row_input_is_a_typed_input_error() -> None:
    with pytest.raises(DreamInputError):
        MemoryDreamScorer().score(["not a row"], NOW)


# ----- SC14, SC15: the double gate -----


def test_the_time_gate_blocks_a_dream_inside_the_interval() -> None:
    store = _store_with_threads(_thread_ids(DREAM_MIN_THREADS))
    cursors = _cursors_dreamed_at(NOW - DREAM_MIN_INTERVAL + timedelta(minutes=1))
    backend = _RecordingBackend()

    result = MemoryDream(store, cursors, backend=backend).run(now=NOW)

    assert result["status"] == "skipped"
    assert result["transported"] == 0
    assert backend.calls == 0
    assert cursors.load().last_status == "ok"  # the window is not consumed


def test_the_thread_gate_blocks_a_dream_with_too_few_conversations() -> None:
    store = _store_with_threads(_thread_ids(DREAM_MIN_THREADS - 1))
    cursors = _cursors_dreamed_at(NOW - DREAM_MIN_INTERVAL - timedelta(hours=1))
    backend = _RecordingBackend()

    result = MemoryDream(store, cursors, backend=backend).run(now=NOW)

    assert result["status"] == "skipped"
    assert backend.calls == 0


def test_both_gates_open_runs_the_dream() -> None:
    store = _store_with_useful_rows(_thread_ids(DREAM_MIN_THREADS))
    cursors = _cursors_dreamed_at(NOW - DREAM_MIN_INTERVAL - timedelta(hours=1))
    backend = _RecordingBackend()

    result = MemoryDream(store, cursors, backend=backend).run(now=NOW)

    assert result["status"] == "ok"
    assert backend.calls == 1


def test_the_gate_counts_threads_not_runs() -> None:
    """SC15: five runs on one conversation is still one conversation."""
    store = _store_with_threads(["thread-1"] * 5)
    cursors = _cursors_dreamed_at(NOW - DREAM_MIN_INTERVAL - timedelta(hours=1))
    backend = _RecordingBackend()

    result = MemoryDream(store, cursors, backend=backend).run(now=NOW)

    assert result["status"] == "skipped"
    assert backend.calls == 0


def test_a_never_dreamed_cursor_opens_the_time_gate() -> None:
    store = _store_with_useful_rows(_thread_ids(DREAM_MIN_THREADS))
    backend = _RecordingBackend()

    result = MemoryDream(store, InProcessDreamCursorStore(), backend=backend).run(
        now=NOW
    )

    assert result["status"] == "ok"


def test_rows_without_a_thread_id_are_not_counted_but_are_reported(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """SC8: the thread gate degrades on missing thread_id, loudly once."""
    store = _store_with_threads(_thread_ids(DREAM_MIN_THREADS - 1))
    store.add(_row("orphan"))
    cursors = _cursors_dreamed_at(NOW - DREAM_MIN_INTERVAL - timedelta(hours=1))

    with caplog.at_level(logging.WARNING, logger="fin.host.memory_dream"):
        result = MemoryDream(store, cursors, backend=_RecordingBackend()).run(
            now=NOW
        )

    assert result["status"] == "skipped"
    assert [
        record for record in caplog.records if "thread_id" in record.message
    ]


# ----- SC16: concurrency and cursor advance -----


def test_a_window_runs_once_under_concurrency() -> None:
    store = _store_with_threads(_thread_ids(DREAM_MIN_THREADS))
    for row in store.rows():
        store.bump_hit_counts([row.id] * 2)
    cursors = _cursors_dreamed_at(NOW - DREAM_MIN_INTERVAL - timedelta(hours=1))
    backend = _RecordingBackend()
    dream = MemoryDream(store, cursors, backend=backend)

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _: dream.run(now=NOW), range(16)))

    assert backend.calls == 1
    assert sum(1 for item in results if item["status"] == "ok") == 1


def test_the_cursor_advances_on_success() -> None:
    store = _store_with_useful_rows(_thread_ids(DREAM_MIN_THREADS))
    cursors = InProcessDreamCursorStore()

    MemoryDream(store, cursors, backend=_RecordingBackend()).run(now=NOW)

    cursor = cursors.load()
    assert cursor.last_dream_at == NOW
    assert cursor.seen_thread_ids == set(_thread_ids(DREAM_MIN_THREADS))
    assert cursor.last_status == "ok"


def test_the_cursor_advances_even_when_the_backend_buffers() -> None:
    """SC16: a buffered dream still consumes the window (SC20)."""
    store = _store_with_useful_rows(_thread_ids(DREAM_MIN_THREADS))
    cursors = InProcessDreamCursorStore()

    result = MemoryDream(store, cursors, backend=NullDreamBackend()).run(now=NOW)

    assert result["status"] == "buffered"
    assert cursors.load().last_dream_at == NOW
    assert cursors.load().last_status == "buffered"


def test_the_cursor_advances_when_there_are_no_candidates() -> None:
    store = _store_with_threads(_thread_ids(DREAM_MIN_THREADS))
    cursors = InProcessDreamCursorStore()

    result = MemoryDream(store, cursors, backend=_RecordingBackend()).run(now=NOW)

    assert result["status"] == "empty"
    assert cursors.load().last_dream_at == NOW


# ----- cursor corruption (spec:104) -----


@pytest.mark.parametrize(
    ("last_dream_at", "seen_thread_ids", "last_status"),
    [
        ("not-a-date", [], "ok"),
        (NOW, [1, 2], "ok"),
        (NOW, {"a": 1}, "ok"),
        (NOW, [], 17),
    ],
)
def test_a_corrupt_cursor_payload_is_a_typed_cursor_error(
    last_dream_at: object,
    seen_thread_ids: object,
    last_status: object,
) -> None:
    with pytest.raises(DreamCursorError):
        parse_dream_cursor(last_dream_at, seen_thread_ids, last_status)


def test_a_well_formed_cursor_payload_parses() -> None:
    cursor = parse_dream_cursor(NOW, ["a", "b"], "ok")

    assert cursor.last_dream_at == NOW
    assert cursor.seen_thread_ids == frozenset({"a", "b"})
    assert cursor.last_status == "ok"


def test_a_corrupt_cursor_surfaces_from_run() -> None:
    class _CorruptCursors:
        def load(self):
            return parse_dream_cursor("garbage", [], "ok")

        def advance(self, **kwargs) -> None:
            return None

        def claim(self, **kwargs) -> bool:
            return True

    with pytest.raises(DreamCursorError):
        MemoryDream(
            _store_with_threads(["thread-1"]),
            _CorruptCursors(),
            backend=NullDreamBackend(),
        ).run(now=NOW)


# ----- SC13: the summarizer is injectable and off by default -----


def test_the_default_dream_does_not_rewrite_the_payload() -> None:
    """SC13: with no summarizer injected, no model touches the batch."""
    store = _store_with_threads(_thread_ids(DREAM_MIN_THREADS))
    for row in store.rows():
        store.bump_hit_counts([row.id] * 2)
    backend = _RecordingBackend()

    MemoryDream(store, InProcessDreamCursorStore(), backend=backend).run(now=NOW)

    assert [item["text"] for item in backend.payloads[0]] == [
        f"memory row-{index}" for index in range(DREAM_MIN_THREADS)
    ]


def test_an_injected_summarizer_rewrites_the_payload() -> None:
    store = _store_with_threads(_thread_ids(DREAM_MIN_THREADS))
    for row in store.rows():
        store.bump_hit_counts([row.id] * 2)
    backend = _RecordingBackend()
    summarizer = _MarkingSummarizer()

    MemoryDream(
        store,
        InProcessDreamCursorStore(),
        backend=backend,
        summarizer=summarizer,
    ).run(now=NOW)

    assert summarizer.calls == 1
    assert all(
        item["text"].endswith("(condensed)") for item in backend.payloads[0]
    )


def test_null_summarizer_is_the_documented_off_switch() -> None:
    store = _store_with_threads(_thread_ids(DREAM_MIN_THREADS))
    for row in store.rows():
        store.bump_hit_counts([row.id] * 2)
    backend = _RecordingBackend()

    MemoryDream(
        store,
        InProcessDreamCursorStore(),
        backend=backend,
        summarizer=NullDreamSummarizer(),
    ).run(now=NOW)

    assert all(not item["text"].endswith("(condensed)") for item in backend.payloads[0])


def test_memory_off_dream_makes_zero_model_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SC13: with memory OFF, dream never even builds the pipeline.

    "A summarizer I never wired received zero calls" would assert nothing,
    so this makes constructing the dream step itself fail the test.
    """
    from fin.host import memory_buffer as buffer_module
    from fin.host import memory_dream as dream_module
    from fin.host.memory_port import build_memory_port

    def forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("dream must not run while memory is OFF")

    # Both bindings: ``memory_buffer`` imports the name, so patching only
    # the defining module would leave the OFF path's own reference live.
    monkeypatch.setattr(dream_module, "MemoryDream", forbidden)
    monkeypatch.setattr(buffer_module, "MemoryDream", forbidden)
    monkeypatch.delenv("FIN_MEMORY_ENABLED", raising=False)

    port = build_memory_port()

    assert port.dream() == {"status": "disabled", "transported": 0}


@pytest.mark.parametrize(
    "summarizer",
    [_ExplodingSummarizer(), _UnparseableSummarizer()],
    ids=["raises", "unparseable"],
)
def test_a_failed_summarization_leaves_the_batch_untouched(
    summarizer: object,
) -> None:
    """SC13: no partial transport and no dropped rows."""
    store = _store_with_threads(_thread_ids(DREAM_MIN_THREADS))
    for row in store.rows():
        store.bump_hit_counts([row.id] * 2)
    backend = _RecordingBackend()

    with pytest.raises(DreamInputError):
        MemoryDream(
            store,
            InProcessDreamCursorStore(),
            backend=backend,
            summarizer=summarizer,
        ).run(now=NOW)

    assert backend.calls == 0
    assert all(row.status == "active" for row in store.rows())


# ----- SC18, SC19, SC20: post-transport state -----


def test_a_confirmed_transport_soft_disables_the_rows() -> None:
    buffer = _buffer_with_useful_memories(backend=_RecordingBackend())

    result = buffer.dream()

    assert result["status"] == "ok"
    rows = buffer.entries()
    assert {row["status"] for row in rows} == {"disabled"}
    assert {row["disabled_reason"] for row in rows} == {"transported"}


def test_transported_rows_stop_coming_back_from_recall() -> None:
    buffer = _buffer_with_useful_memories(backend=_RecordingBackend())

    buffer.dream()

    assert buffer.recall("memory")["memories"] == []


def test_without_a_backend_the_dream_buffers_and_keeps_rows_active() -> None:
    """SC20: no backend is the CONTEXT.md:45 'stops at the buffer' shape."""
    buffer = LocalMemoryBuffer()
    for index in range(DREAM_MIN_THREADS):
        buffer.extract(f"memory {index}", metadata={"thread_id": f"thread-{index}"})
    buffer.recall("memory")
    buffer.recall("memory")

    result = buffer.dream()

    assert result["status"] == "buffered"
    assert result["transported"] == 0
    assert {row["status"] for row in buffer.entries()} == {"active"}


def test_a_backend_that_stores_nothing_leaves_rows_active() -> None:
    """SC18: only a confirmed write (count >= 1) retires the rows."""
    buffer = _buffer_with_useful_memories(backend=_RecordingBackend(stored=0))

    result = buffer.dream()

    assert result["status"] == "buffered"
    assert {row["status"] for row in buffer.entries()} == {"active"}


def test_a_failing_backend_leaves_rows_active_and_raises_typed() -> None:
    store = _store_with_threads(_thread_ids(DREAM_MIN_THREADS))
    for row in store.rows():
        store.bump_hit_counts([row.id] * 2)

    with pytest.raises(DreamTransportError):
        MemoryDream(
            store,
            InProcessDreamCursorStore(),
            backend=_ExplodingBackend(),
        ).run(now=NOW)

    assert all(row.status == "active" for row in store.rows())


def test_gc_hard_deletes_transported_rows_past_the_retention_window() -> None:
    buffer = _buffer_with_useful_memories(backend=_RecordingBackend())
    buffer.dream()

    buffer.gc(now=NOW + timedelta(days=TRANSPORT_RETENTION_DAYS + 1))

    assert buffer.entries() == []


def test_gc_keeps_transported_rows_inside_the_retention_window() -> None:
    buffer = _buffer_with_useful_memories(backend=_RecordingBackend())
    buffer.dream()

    buffer.gc(now=NOW + timedelta(days=TRANSPORT_RETENTION_DAYS - 1))

    assert len(buffer.entries()) == DREAM_MIN_THREADS


def test_gc_still_only_soft_disables_untransported_rows() -> None:
    """SC19's contract reversal is scoped to transported rows only."""
    buffer = LocalMemoryBuffer()
    buffer.extract("expired memory", metadata={"expires_at": "2020-01-01T00:00:00Z"})

    buffer.gc(now=NOW)

    rows = buffer.entries()
    assert len(rows) == 1
    assert rows[0]["status"] == "disabled"
    assert rows[0]["disabled_reason"] == "expired"


def test_dream_failures_do_not_escape_the_port() -> None:
    """SC21: the port records the failure instead of raising it."""
    buffer = _buffer_with_useful_memories(backend=_ExplodingBackend())

    result = buffer.dream()

    assert result["status"] == "error"
    assert result["error"] == "DreamTransportError"
    assert result["transported"] == 0


# ----- overflow and the error hierarchy -----


def test_candidates_are_truncated_to_the_memory_limit_cap() -> None:
    count = MEMORY_LIMIT_CAP + 5
    store = InProcessMemoryStore()
    for index in range(count):
        store.add(_row(f"row-{index}", hit_count=2, thread_id=f"thread-{index}"))
    backend = _RecordingBackend()

    result = MemoryDream(
        store, InProcessDreamCursorStore(), backend=backend
    ).run(now=NOW, limit=count)

    assert len(backend.payloads[0]) == MEMORY_LIMIT_CAP
    assert result["transported"] == MEMORY_LIMIT_CAP


def test_the_dream_error_hierarchy_hangs_off_the_memory_port() -> None:
    assert issubclass(MemoryDreamError, MemoryPortError)
    assert issubclass(DreamInputError, MemoryDreamError)
    assert issubclass(DreamTransportError, MemoryDreamError)
    assert issubclass(DreamCursorError, MemoryDreamError)


@pytest.mark.parametrize("limit", [None, "8", 8.0, True])
def test_a_non_integer_limit_is_a_typed_input_error(limit: object) -> None:
    store = _store_with_threads(_thread_ids(DREAM_MIN_THREADS))
    dream = MemoryDream(store, InProcessDreamCursorStore())

    with pytest.raises(DreamInputError):
        dream.run(limit=limit, now=NOW)  # type: ignore[arg-type]


def test_a_backend_returning_a_non_integer_count_is_a_transport_error() -> None:
    """A backend that reports its result in any other shape is a bug."""
    store = _store_with_threads(_thread_ids(DREAM_MIN_THREADS))
    for row in store.rows():
        store.bump_hit_counts([row.id] * 2)

    class _NonIntegerBackend:
        def persist(self, memories: object) -> object:
            return "all of them"

    with pytest.raises(DreamTransportError):
        MemoryDream(
            store,
            InProcessDreamCursorStore(),
            backend=_NonIntegerBackend(),  # type: ignore[arg-type]
        ).run(now=NOW)

    assert all(row.status == "active" for row in store.rows())


def test_the_default_gate_constants_match_the_spec() -> None:
    gate = DreamGate()

    assert gate.min_interval == timedelta(hours=24)
    assert gate.min_threads == 5
    assert DREAM_MIN_INTERVAL == timedelta(hours=24)
    assert DREAM_MIN_THREADS == 5
    assert ScoredMemory(_row("a"), 3, ("repeated_hits",)).score == 3
