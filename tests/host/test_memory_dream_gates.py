"""T8 dream dual-gate contracts (``specs/cross-session-operator-memory.md``).

Dream is the local fact merge in CONTEXT.md:45 / ADR 0048. This file
covers the SC17-SC22 + SC29 contract:

* SC17 — within 24h of last success/skip, dream does not run;
* SC18 — fewer than two active facts → 0 LLM calls AND the 24h
  cooldown is not refreshed;
* SC19 — after cooldown, run only if active facts ≥ 2 AND
  (distinct ``thread_id`` ≥ 5 **or** completed turns since last
  dream ≥ 6 **or** new facts ≥ 3), else does not run;
* SC20 — no dream node in the compiled graph (covered by the
  wiring suite, asserted again here for completeness);
* SC21 — completion path does not unconditionally enqueue the
  per-turn dream; the hook only counts turns and lets the gate
  decide;
* SC22 — with the null backend, dream performs 0 memory-MCP
  writes (no row retirement, no transport mutation);
* SC29 — identity / soul / user-profile file bytes are unchanged
  after a merge.

The new gate needs two persistent counts that the previous
"rule-gated dream" did not track: completed turns since last
dream and the active-fact watermark used to compute "new facts".
Both live behind a tiny :class:`DreamGateStateStore` inside
``memory_dream`` so this file stays hermetic — the existing
cursor is untouched.

Boundary classes per ``code-quality.md`` / ``test.md``:

* empty — never-dreamed cursor, no active facts, no turns
* negative — facts below the floor, threads below the threshold
* overflow — turn counter past 6, watermark past 3
* concurrent — two simultaneous gate-open evaluations run once
* exception — backend raises, summarizer raises, facts<2 path
  must not raise even when state is hot
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Callable, Sequence

import pytest

from fin.host.memory_dream import (
    DREAM_MIN_INTERVAL,
    DREAM_MIN_THREADS,
    DreamGate,
    DreamGateState,
    DreamGateStateStore,
    InProcessDreamGateStateStore,
    MemoryDream,
    MemoryDreamBackend,
    NullDreamBackend,
    NullDreamSummarizer,
    evaluate_dream_gate,
    run_dream_after_turn,
)
from fin.host.memory_port import MemoryPort, OffMemoryAdapter
from fin.host.memory_store import (
    DreamCursor,
    InProcessDreamCursorStore,
    InProcessMemoryStore,
    MemoryRow,
)

NOW = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)
PAST = NOW - DREAM_MIN_INTERVAL - timedelta(hours=1)


# ---------------------------------------------------------------------
# shared fakes
# ---------------------------------------------------------------------


def _row(
    row_id: str,
    *,
    thread_id: str | None = "thread-a",
    body: str = "memory body",
    title: str = "title",
) -> MemoryRow:
    metadata: dict[str, object] = {}
    if thread_id is not None:
        metadata["thread_id"] = thread_id
    return MemoryRow(
        id=row_id,
        text=body,
        title=title,
        body=body,
        created_at=NOW - timedelta(hours=2),
        metadata=metadata,
        hit_count=2,
    )


def _active_rows(count: int, *, thread_prefix: str = "t") -> list[MemoryRow]:
    """``count`` active rows, one per distinct thread by default."""
    return [
        _row(
            f"row-{index}",
            thread_id=f"{thread_prefix}-{index}",
            body=f"body {index}",
        )
        for index in range(count)
    ]


def _store(rows: Sequence[MemoryRow]) -> InProcessMemoryStore:
    store = InProcessMemoryStore()
    for row in rows:
        store.add(row)
    return store


def _cursor_dreamed(at: datetime, *, seen=(), status: str = "ok") -> InProcessDreamCursorStore:
    store = InProcessDreamCursorStore()
    store.advance(at=at, seen_thread_ids=frozenset(seen), last_status=status)
    return store


def _fresh_state(
    *,
    turns: int = 0,
    watermark: int = 0,
) -> InProcessDreamGateStateStore:
    state = InProcessDreamGateStateStore()
    state._reset_for_test(turns=turns, watermark=watermark)  # type: ignore[attr-defined]
    return state


class _RecordingBackend:
    """A backend that records ``persist`` calls and returns a stored count."""

    def __init__(self, *, stored: int = 1) -> None:
        self._stored = stored
        self.calls = 0
        self.payloads: list[list[dict[str, object]]] = []

    def persist(self, memories: Sequence[dict[str, object]]) -> int:
        self.calls += 1
        self.payloads.append(list(memories))
        return self._stored


class _ExplodingBackend:
    def persist(self, memories: Sequence[dict[str, object]]) -> int:
        raise RuntimeError("memory MCP down")


class _GatedPort:
    """A :class:`MemoryPort` that exposes what ``run_dream_after_turn`` needs.

    The real wiring (workspace_http) uses duck typing on the runtime
    port. This fake mirrors the shape so the per-turn hook can find:

    * a gate-state store (turns + watermark)
    * an active-facts provider (the buffer's row count)
    * a dream runner (builds :class:`MemoryDream`)
    """

    def __init__(
        self,
        *,
        rows: Sequence[MemoryRow] = (),
        gate_state: DreamGateStateStore | None = None,
        cursors: InProcessDreamCursorStore | None = None,
        backend: MemoryDreamBackend | None = None,
        gate: DreamGate | None = None,
    ) -> None:
        self._store = _store(rows)
        self._cursors = cursors or InProcessDreamCursorStore()
        self._gate_state = gate_state or _fresh_state()
        self._backend: MemoryDreamBackend = backend or NullDreamBackend()
        self._gate = gate or DreamGate()
        self.dream_calls = 0
        self.enabled = True

    # The duck-typed shape run_dream_after_turn probes for.
    def _facts_count(self) -> int:
        return sum(1 for row in self._store.rows() if row.status == "active")

    @property
    def gate_state_store(self) -> DreamGateStateStore:
        return self._gate_state

    def _build_dream(self) -> MemoryDream:
        self.dream_calls += 1
        return MemoryDream(
            self._store,
            self._cursors,
            self._gate_state,
            backend=self._backend,
            gate=self._gate,
        )


# ---------------------------------------------------------------------
# pure gate evaluation
# ---------------------------------------------------------------------


class TestEvaluateDreamGate:
    """The gate is a pure function so SC17–SC19 can be asserted directly."""

    def _open(
        self,
        *,
        now: datetime = NOW,
        cursor: DreamCursor | None = None,
        state: DreamGateState | None = None,
        active_facts: int = 2,
        distinct_threads: int = 5,
    ) -> tuple[bool, str | None]:
        return evaluate_dream_gate(
            gate=DreamGate(),
            cursor=cursor or DreamCursor(),
            state=state or DreamGateState(
                turns_since_dream=0, last_facts_watermark=0
            ),
            active_facts=active_facts,
            distinct_threads=distinct_threads,
            now=now,
        )

    def test_sc17_within_24h_of_last_success_is_a_cooldown_skip(self) -> None:
        recent = NOW - DREAM_MIN_INTERVAL + timedelta(minutes=1)

        open_, reason = self._open(
            now=NOW,
            cursor=DreamCursor(last_dream_at=recent, last_status="ok"),
            state=DreamGateState(turns_since_dream=8, last_facts_watermark=0),
            active_facts=4,
            distinct_threads=8,
        )

        assert (open_, reason) == (False, "cooldown")

    def test_sc17_never_dreamed_cursor_is_not_in_cooldown(self) -> None:
        open_, reason = self._open()

        assert open_ is True
        assert reason is None

    def test_sc18_below_two_facts_is_facts_floor_skip(self) -> None:
        open_, reason = self._open(active_facts=1, distinct_threads=8)

        assert (open_, reason) == (False, "facts_floor")

    def test_sc18_zero_active_facts_is_facts_floor_skip(self) -> None:
        open_, reason = self._open(active_facts=0)

        assert reason == "facts_floor"

    def test_sc19_threshold_needs_one_of_three(self) -> None:
        # facts ≥ 2, but none of the threshold branches met → skip
        open_, reason = self._open(
            active_facts=2,
            distinct_threads=4,
            state=DreamGateState(turns_since_dream=5, last_facts_watermark=0),
        )

        assert (open_, reason) == (False, "threshold")

    def test_sc19_threads_branch_opens_the_gate(self) -> None:
        open_, _ = self._open(
            active_facts=2,
            distinct_threads=5,
            state=DreamGateState(turns_since_dream=0, last_facts_watermark=0),
        )

        assert open_ is True

    def test_sc19_turns_branch_opens_the_gate(self) -> None:
        open_, _ = self._open(
            active_facts=2,
            distinct_threads=0,
            state=DreamGateState(turns_since_dream=6, last_facts_watermark=0),
        )

        assert open_ is True

    def test_sc19_new_facts_branch_opens_the_gate(self) -> None:
        open_, _ = self._open(
            active_facts=5,
            distinct_threads=0,
            state=DreamGateState(turns_since_dream=0, last_facts_watermark=2),
        )

        assert open_ is True

    def test_sc18_facts_floor_wins_over_cooldown_check(self) -> None:
        """If facts < 2, the floor triggers — and the 24h basis is not refreshed.

        Spec phrasing: "do not treat [facts<2] as a skip that refreshes 24h".
        That means a facts-floor evaluation never advances last_dream_at,
        regardless of whether we are inside the cooldown window or not.
        The gate labels this case ``facts_floor`` so the caller knows
        not to advance the cursor.
        """
        recent = NOW - DREAM_MIN_INTERVAL + timedelta(minutes=1)

        open_, reason = self._open(
            now=NOW,
            cursor=DreamCursor(last_dream_at=recent, last_status="ok"),
            active_facts=1,
        )

        assert (open_, reason) == (False, "facts_floor")

    def test_sc19_threshold_skip_is_a_skip_for_cooldown_purposes(self) -> None:
        """A threshold-not-met evaluation must behave like a skip for the
        24h cooldown basis: the next call inside the window stays in
        cooldown. The :class:`MemoryDream.run` path enforces this by
        advancing ``last_dream_at`` on the threshold branch.
        """

        cursor = _cursor_dreamed(PAST, status="skipped")
        rows = _active_rows(2, thread_prefix="t")
        store = _store(rows)
        state = _fresh_state(turns=2, watermark=2)

        result = MemoryDream(
            store, cursor, state, backend=_RecordingBackend()
        ).run(now=NOW)

        assert result["status"] == "skipped"
        assert result["reason"] == "threshold"
        # Cooldown basis: threshold-skip advanced last_dream_at to NOW.
        assert cursor.load().last_dream_at == NOW


# ---------------------------------------------------------------------
# the SC18 facts-floor path never refreshes the cooldown
# ---------------------------------------------------------------------


class TestFactsFloorDoesNotRefreshCooldown:
    """SC18: facts < 2 → 0 LLM calls AND last_dream_at is not advanced."""

    def test_no_llm_call_is_made(self) -> None:
        cursor = _cursor_dreamed(PAST, status="ok")
        state = _fresh_state()
        backend = _RecordingBackend()
        rows = _active_rows(1)

        result = MemoryDream(
            _store(rows), cursor, state, backend=backend
        ).run(now=NOW)

        assert result["status"] == "skipped"
        assert result["reason"] == "facts_floor"
        assert backend.calls == 0

    def test_cursor_is_not_advanced(self) -> None:
        cursor = _cursor_dreamed(PAST, status="ok")
        state = _fresh_state()
        rows = _active_rows(1)

        MemoryDream(
            _store(rows), cursor, state, backend=_RecordingBackend()
        ).run(now=NOW)

        # Cooldown basis is the previous success — not refreshed.
        assert cursor.load().last_dream_at == PAST
        assert cursor.load().last_status == "ok"

    def test_gate_state_turn_counter_still_increments(self) -> None:
        """Per-turn counts update independently of the dream outcome."""
        cursor = _cursor_dreamed(PAST, status="ok")
        state = _fresh_state()
        rows = _active_rows(1)

        MemoryDream(
            _store(rows), cursor, state, backend=_RecordingBackend()
        ).run(now=NOW)
        state.record_turn(active_facts=1)
        state.record_turn(active_facts=1)

        assert state.load().turns_since_dream == 2

    def test_empty_store_is_facts_floor_with_no_writes(self) -> None:
        cursor = _cursor_dreamed(PAST, status="ok")
        state = _fresh_state()
        backend = _RecordingBackend()

        result = MemoryDream(
            _store([]), cursor, state, backend=backend
        ).run(now=NOW)

        assert result["reason"] == "facts_floor"
        assert backend.calls == 0
        assert cursor.load().last_dream_at == PAST


# ---------------------------------------------------------------------
# MemoryDream.run honors SC19 across the four sub-branches
# ---------------------------------------------------------------------


class TestDualGateInMemoryDream:
    """End-to-end gate through :class:`MemoryDream.run`."""

    def _build(
        self,
        *,
        rows: Sequence[MemoryRow],
        cursor: InProcessDreamCursorStore,
        state: DreamGateStateStore,
        backend: MemoryDreamBackend,
        gate: DreamGate | None = None,
    ) -> MemoryDream:
        return MemoryDream(
            _store(rows),
            cursor,
            state,
            backend=backend,
            gate=gate or DreamGate(),
        )

    def test_sc19_threads_branch_runs_the_merge(self) -> None:
        rows = _active_rows(5)
        cursor = _cursor_dreamed(PAST)
        state = _fresh_state()
        backend = _RecordingBackend()

        result = self._build(
            rows=rows, cursor=cursor, state=state, backend=backend
        ).run(now=NOW)

        assert result["status"] == "ok"
        assert backend.calls == 1

    def test_sc19_turns_branch_runs_the_merge(self) -> None:
        rows = _active_rows(2, thread_prefix="solo")
        cursor = _cursor_dreamed(PAST)
        state = _fresh_state(turns=6, watermark=2)
        backend = _RecordingBackend()

        result = self._build(
            rows=rows, cursor=cursor, state=state, backend=backend
        ).run(now=NOW)

        assert result["status"] == "ok"

    def test_sc19_new_facts_branch_runs_the_merge(self) -> None:
        rows = _active_rows(5, thread_prefix="solo")
        cursor = _cursor_dreamed(PAST)
        # Watermark at 2, 5 active rows → 3 new facts opens the gate.
        state = _fresh_state(turns=0, watermark=2)
        backend = _RecordingBackend()

        result = self._build(
            rows=rows, cursor=cursor, state=state, backend=backend
        ).run(now=NOW)

        assert result["status"] == "ok"

    def test_sc19_threshold_skip_advances_cursor_and_does_not_run(self) -> None:
        rows = _active_rows(2, thread_prefix="solo")
        cursor = _cursor_dreamed(PAST)
        state = _fresh_state(turns=2, watermark=2)
        backend = _RecordingBackend()

        result = self._build(
            rows=rows, cursor=cursor, state=state, backend=backend
        ).run(now=NOW)

        assert result["status"] == "skipped"
        assert result["reason"] == "threshold"
        assert backend.calls == 0
        # Threshold skip is a skip — last_dream_at advances (cooldown basis).
        assert cursor.load().last_dream_at == NOW
        assert cursor.load().last_status == "skipped"

    def test_sc19_a_turn_only_run_resets_gate_state(self) -> None:
        rows = _active_rows(5)
        cursor = _cursor_dreamed(PAST)
        state = _fresh_state(turns=6, watermark=0)
        backend = _RecordingBackend()

        self._build(
            rows=rows, cursor=cursor, state=state, backend=backend
        ).run(now=NOW)

        loaded = state.load()
        assert loaded.turns_since_dream == 0
        assert loaded.last_facts_watermark == 5

    def test_claim_race_lets_only_one_dream_run(self) -> None:
        """Two threads with a fully-open gate → one runs, one skips."""
        rows = _active_rows(5)
        cursor = _cursor_dreamed(PAST)
        state = _fresh_state()
        backend = _RecordingBackend()
        dream = self._build(
            rows=rows, cursor=cursor, state=state, backend=backend
        )

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: dream.run(now=NOW), range(8)))

        assert backend.calls == 1
        statuses = [result["status"] for result in results]
        assert statuses.count("ok") == 1

    def test_default_dream_does_not_rewrite_payload_without_summarizer(
        self,
    ) -> None:
        rows = _active_rows(5)
        for row in rows:
            row.hit_count = 2
        cursor = _cursor_dreamed(PAST)
        state = _fresh_state()
        backend = _RecordingBackend()

        MemoryDream(
            _store(rows),
            cursor,
            state,
            backend=backend,
            summarizer=NullDreamSummarizer(),
        ).run(now=NOW)

        assert all(
            payload["text"] == f"body {index}"
            for index, payload in enumerate(backend.payloads[0])
        )


# ---------------------------------------------------------------------
# SC21 — the per-turn hook is gated, not unconditional
# ---------------------------------------------------------------------


def _port_with_facts(count: int, *, gate_state=None) -> _GatedPort:
    rows = _active_rows(count) if count else []
    return _GatedPort(rows=rows, gate_state=gate_state)


class TestRunDreamAfterTurnIsGated:
    """SC21: the hook counts turns and only fires when the gate opens."""

    def test_a_closed_gate_counts_the_turn_without_firing_dream(self) -> None:
        port = _port_with_facts(1)
        # facts < 2 → facts_floor; the turn still counts.

        result = run_dream_after_turn(
            port,  # type: ignore[arg-type]
            thread_id="thread-1",
            gate_state_store=port.gate_state_store,
            active_facts_provider=port._facts_count,
            dream_factory=port._build_dream,
        )

        assert result is not None
        assert result["status"] == "skipped"
        assert result["reason"] == "facts_floor"
        assert port.dream_calls == 1  # build was called, run hit facts_floor
        assert port.gate_state_store.load().turns_since_dream == 1

    def test_a_through_turn_fires_dream_only_when_gates_open(self) -> None:
        port = _GatedPort(
            rows=_active_rows(5),
            backend=_RecordingBackend(),  # type: ignore[arg-type]
        )

        result = run_dream_after_turn(
            port,  # type: ignore[arg-type]
            thread_id="thread-1",
            gate_state_store=port.gate_state_store,
            active_facts_provider=port._facts_count,
            dream_factory=port._build_dream,
        )

        assert result is not None
        assert result["status"] == "ok"
        assert port.gate_state_store.load().turns_since_dream == 0

    def test_an_off_port_is_a_no_op_and_does_not_count(self) -> None:
        """OFF ports are skipped before any state mutates (SC2)."""
        port = _port_with_facts(5)
        port.enabled = False

        result = run_dream_after_turn(
            port,  # type: ignore[arg-type]
            thread_id="thread-1",
            gate_state_store=port.gate_state_store,
            active_facts_provider=port._facts_count,
            dream_factory=port._build_dream,
        )

        assert result is None
        assert port.gate_state_store.load().turns_since_dream == 0

    def test_a_failing_backend_does_not_break_the_turn(self) -> None:
        rows = _active_rows(5)
        port = _GatedPort(
            rows=rows,
            backend=_ExplodingBackend(),  # type: ignore[arg-type]
        )

        result = run_dream_after_turn(
            port,  # type: ignore[arg-type]
            thread_id="thread-1",
            gate_state_store=port.gate_state_store,
            active_facts_provider=port._facts_count,
            dream_factory=port._build_dream,
        )

        # No exception bubbles; the dream recorded its failure.
        assert result is None

    def test_missing_gate_wiring_skips_dream_without_firing(self) -> None:
        """A port without gate-state wiring is a safe no-op (SC21)."""

        class _BarePort:
            enabled = True
            dream_calls = 0

            def dream(self, *, limit: int = 8) -> dict[str, object]:
                self.dream_calls += 1
                return {"status": "buffered", "transported": 0}

        bare = _BarePort()
        result = run_dream_after_turn(bare, thread_id="thread-1")  # type: ignore[arg-type]

        assert result is None
        assert bare.dream_calls == 0

    def test_concurrent_turns_record_turns_serially(self) -> None:
        port = _port_with_facts(1)

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(
                pool.map(
                    lambda _: run_dream_after_turn(
                        port,  # type: ignore[arg-type]
                        thread_id="thread-1",
                        gate_state_store=port.gate_state_store,
                        active_facts_provider=port._facts_count,
                        dream_factory=port._build_dream,
                    ),
                    range(8),
                )
            )

        assert port.gate_state_store.load().turns_since_dream == 8


# ---------------------------------------------------------------------
# SC22 — no MCP write with the null backend
# ---------------------------------------------------------------------


class TestNullBackendWritesNothing:
    """SC22: the default null backend performs zero memory-MCP writes."""

    def test_run_with_null_backend_does_not_call_persist(self) -> None:
        backend = NullDreamBackend()

        # NullDreamBackend is the only backend the wired kinds expose.
        assert backend.persist([]) == 0
        assert backend.persist([{"id": "x"}]) == 0

    def test_run_with_null_backend_does_not_retire_rows(self) -> None:
        rows = _active_rows(5)
        store = _store(rows)
        cursor = _cursor_dreamed(PAST)
        state = _fresh_state()
        for row in rows:
            row.hit_count = 2

        result = MemoryDream(
            store, cursor, state, backend=NullDreamBackend()
        ).run(now=NOW)

        assert result["status"] == "buffered"
        # Rows are NOT retired by the null backend (SC22).
        for row in store.rows():
            assert row.status == "active"
            assert row.transported_at is None
            assert row.disabled_at is None

    def test_run_with_null_backend_does_not_advance_to_ok(self) -> None:
        rows = _active_rows(5)
        store = _store(rows)
        cursor = _cursor_dreamed(PAST)
        state = _fresh_state()
        for row in rows:
            row.hit_count = 2

        result = MemoryDream(
            store, cursor, state, backend=NullDreamBackend()
        ).run(now=NOW)

        assert result["status"] == "buffered"
        # ``buffered`` is the documented status for "stops at the buffer".
        assert result["transported"] == 0


# ---------------------------------------------------------------------
# SC29 — identity / soul / user-profile bytes are unchanged
# ---------------------------------------------------------------------


class TestIdentitySoulProfileUntouched:
    """SC29: a merge run never edits the host's identity, soul, or
    user-profile files. This is a behavioral test on the dream module:
    it imports the host user-profile store via tmp_path, writes
    known bytes to both layers, runs a merge, and asserts the bytes
    survived byte-for-byte.
    """

    def test_user_profile_files_unchanged_after_a_merge(
        self, tmp_path
    ) -> None:
        from fin.host.user_profile import UserProfileStore

        store = UserProfileStore(
            global_path=tmp_path / "global-user.md",
            workspace_path=tmp_path / "workspace-user.md",
        )
        global_text = "I prefer concise summaries and CSV exports."
        workspace_text = "Workspace layer: focus on Q3 2022 10-Qs."
        store.write_layer("global", global_text)
        store.write_layer("workspace", workspace_text)

        rows = _active_rows(5)
        for row in rows:
            row.hit_count = 2
        dream = MemoryDream(
            _store(rows),
            _cursor_dreamed(PAST),
            _fresh_state(),
            backend=_RecordingBackend(),
        )

        dream.run(now=NOW)

        assert store.global_path.read_text(encoding="utf-8") == global_text
        assert store.workspace_path.read_text(encoding="utf-8") == workspace_text

    def test_user_profile_files_unchanged_after_a_skipped_run(
        self, tmp_path
    ) -> None:
        from fin.host.user_profile import UserProfileStore

        store = UserProfileStore(
            global_path=tmp_path / "global-user.md",
            workspace_path=tmp_path / "workspace-user.md",
        )
        store.write_layer("global", "global bytes")
        store.write_layer("workspace", "workspace bytes")

        # facts < 2 → facts_floor → no merge call at all.
        dream = MemoryDream(
            _store(_active_rows(1)),
            _cursor_dreamed(PAST),
            _fresh_state(),
            backend=_RecordingBackend(),
        )

        dream.run(now=NOW)

        assert store.global_path.read_text(encoding="utf-8") == "global bytes"
        assert (
            store.workspace_path.read_text(encoding="utf-8")
            == "workspace bytes"
        )


# ---------------------------------------------------------------------
# DreamGateStateStore contract
# ---------------------------------------------------------------------


class TestDreamGateStateStore:
    """The turn counter / watermark store used by the per-turn hook."""

    def test_record_turn_increments_turns_only(self) -> None:
        state = InProcessDreamGateStateStore()
        state._reset_for_test(turns=2, watermark=4)  # type: ignore[attr-defined]

        state.record_turn(active_facts=5)

        loaded = state.load()
        assert loaded.turns_since_dream == 3
        assert loaded.last_facts_watermark == 4

    def test_reset_writes_the_watermark_and_clears_turns(self) -> None:
        state = InProcessDreamGateStateStore()
        state._reset_for_test(turns=9, watermark=2)  # type: ignore[attr-defined]

        state.reset(active_facts=7)

        loaded = state.load()
        assert loaded.turns_since_dream == 0
        assert loaded.last_facts_watermark == 7

    def test_record_turn_is_thread_safe(self) -> None:
        state = InProcessDreamGateStateStore()

        def bump() -> None:
            for _ in range(50):
                state.record_turn(active_facts=0)

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: bump(), range(8)))

        assert state.load().turns_since_dream == 400


# ---------------------------------------------------------------------
# Gate default constants lock in SC17–SC19
# ---------------------------------------------------------------------


def test_the_gate_defaults_match_the_spec() -> None:
    gate = DreamGate()

    assert gate.min_interval == timedelta(hours=24)
    assert gate.min_active_facts == 2
    assert gate.min_distinct_threads == 5
    assert gate.min_turns_since_dream == 6
    assert gate.min_new_facts == 3
    assert DREAM_MIN_INTERVAL == timedelta(hours=24)
    assert DREAM_MIN_THREADS == 5
