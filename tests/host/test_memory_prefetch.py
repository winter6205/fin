"""Host-side memory prefetch assembly.

`specs/cross-session-operator-memory.md` SC7, SC8, SC9, SC24: per-turn
**memory prefetch** is a host-side overlay (NOT a model tool) that picks
the workspace's lexically-relevant memory facts and surfaces them on
the user side as ``title + hook`` rows. Already-shown ids on the same
``thread_id`` are skipped, and the resulting payload counts toward the
session-compaction occupancy ledger.

Boundary classes exercised here:

* empty — zero lexical hits inject nothing.
* negative — no lexical overlap on title AND body ⇒ row excluded;
  ``shown_ids`` skip overrides lexical ranking.
* overflow — four lexical hits still produce at most three rows.
* concurrent — not directly exercised here; the assembler is pure.
* exception — bad inputs to the public API raise typed errors.

Coverage is paired with the integration assertion against
:class:`~fin.host.session_compaction.OccupancyCompactor` so SC24
("prefetch increases the ledger") stays pinned at the unit seam
without dragging the host graph in (T5 owns host wiring).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from langchain_core.messages import HumanMessage

from fin.host.memory_prefetch import (
    PREFETCH_MAX_ROWS,
    PrefetchInputError,
    prefetch_overhead_tokens,
    select_prefetch,
)
from fin.host.memory_store import HOOK_MAX_CHARS, MemoryRow
from fin.host.session_compaction import OccupancyCompactor, session_token_budget


def _now() -> datetime:
    return datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)


def _row(
    row_id: str,
    *,
    title: str = "",
    body: str = "",
    created_at: datetime | None = None,
) -> MemoryRow:
    """Build a fact row the way the store would hand one to the assembler."""

    return MemoryRow(
        id=row_id,
        text=body or title,
        title=title,
        body=body,
        workspace="workspace-a",
        metadata={},
        created_at=created_at if created_at is not None else _now(),
    )


# ---------------------------------------------------------------------------
# Empty / zero lexical hits (SC7).
# ---------------------------------------------------------------------------


def test_zero_lexical_hits_returns_no_rows() -> None:
    """SC7: a user utterance with no token overlap produces zero rows."""

    rows = [
        _row("a", title="Coffee shop preferences", body="Latte in the morning."),
        _row("b", title="Hiking trails", body="Mountains on weekends."),
    ]

    result = select_prefetch("quarterly revenue 2023", rows)

    assert result == []


def test_zero_hits_keeps_payload_empty_even_when_a_title_matches() -> None:
    """A title-only overlap with no body overlap is still a hit on the
    title field — zero token overlap on BOTH title AND body is the
    exclusion rule (SC7).
    """

    rows = [
        _row("a", title="Picnic basket", body="Hiking gear checklist."),
    ]

    result = select_prefetch("quarterly", rows)

    assert result == []


def test_empty_user_utterance_yields_no_rows() -> None:
    """Empty / whitespace-only utterances never trigger a prefetch."""

    rows = [_row("a", title="Quarterly", body="Revenue comparisons.")]

    assert select_prefetch("", rows) == []
    assert select_prefetch("   \t\n  ", rows) == []


def test_no_rows_in_workspace_yields_no_prefetch_rows() -> None:
    """A workspace with zero stored facts returns no prefetch rows."""

    assert select_prefetch("quarterly revenue", []) == []


# ---------------------------------------------------------------------------
# Overflow: four hits still cap at <=3 (SC8).
# ---------------------------------------------------------------------------


def test_four_lexical_hits_inject_at_most_three_rows() -> None:
    """SC8: more than three lexical hits still produce <=3 rows."""

    rows = [
        _row("a", title="Quarterly preference", body="Use quarterly revenue."),
        _row("b", title="Quarterly comparisons", body="Prefer quarter-over-quarter."),
        _row("c", title="Revenue preference", body="Always show quarterly revenue."),
        _row("d", title="Annual fallback", body="Sometimes quarterly revenue."),
    ]

    result = select_prefetch("quarterly revenue", rows)

    assert len(result) <= PREFETCH_MAX_ROWS == 3
    assert len(result) == 3
    # Exactly three distinct ids surface; the fourth is dropped.
    assert len({row["fact_id"] for row in result}) == 3


def test_default_limit_is_three() -> None:
    """The default cap is 3 — the spec ceiling for research-track prefetch."""

    assert PREFETCH_MAX_ROWS == 3


def test_two_lexical_hits_return_two_rows() -> None:
    """Below the cap, every lexical hit survives."""

    rows = [
        _row("a", title="Quarterly", body="Prefer quarterly revenue."),
        _row("b", title="Revenue", body="Quarterly comparisons."),
        _row("c", title="Unrelated", body="No match here."),
    ]

    result = select_prefetch("quarterly revenue", rows)

    assert [row["fact_id"] for row in result] == ["a", "b"]


# ---------------------------------------------------------------------------
# Already-shown ids skip (SC9).
# ---------------------------------------------------------------------------


def test_already_shown_id_on_this_thread_is_skipped() -> None:
    """SC9: a fact id already shown on this ``thread_id`` is not prefetched again."""

    rows = [
        _row("a", title="Quarterly", body="Prefer quarterly revenue."),
        _row("b", title="Revenue", body="Quarterly comparisons."),
    ]

    result = select_prefetch("quarterly revenue", rows, shown_ids={"a"})

    assert [row["fact_id"] for row in result] == ["b"]


def test_shown_id_skip_does_not_promote_lower_scoring_rows() -> None:
    """Skipping an already-shown id does not promote zero-hit rows into the cap."""

    rows = [
        _row("a", title="Quarterly", body="Prefer quarterly revenue."),
        _row("b", title="Unrelated", body="No match here."),
    ]

    result = select_prefetch(
        "quarterly revenue", rows, shown_ids={"a", "b"}
    )

    assert result == []


def test_shown_ids_does_not_filter_workspace_rows() -> None:
    """``shown_ids`` is a thread-level filter, not a workspace filter."""

    rows = [
        _row("a", title="Quarterly", body="Prefer quarterly revenue."),
    ]

    result = select_prefetch("quarterly revenue", rows, shown_ids={"other-id"})

    assert [row["fact_id"] for row in result] == ["a"]


# ---------------------------------------------------------------------------
# Title-heavier-than-body weighting (tokenizer + scoring).
# ---------------------------------------------------------------------------


def test_title_token_overlap_outranks_equal_body_overlap() -> None:
    """A title hit outranks an equal body hit because title is weighted
    above body — same number of overlapping tokens must not yield the
    same score when the overlap is on the title vs the body.
    """

    rows = [
        _row(
            "body-only",
            title="Unrelated title",
            body="Quarterly revenue is the operator's preference.",
        ),
        _row(
            "title-only",
            title="Quarterly revenue preference",
            body="The body mentions something else entirely.",
        ),
    ]

    result = select_prefetch("quarterly revenue", rows)

    assert [row["fact_id"] for row in result] == ["title-only", "body-only"]


def test_body_only_hit_still_counts_as_a_hit() -> None:
    """A body hit alone (no title overlap) still surfaces the row,
    just below an equivalent title hit.
    """

    rows = [
        _row(
            "body-only",
            title="Unrelated title",
            body="Quarterly revenue preferences live here.",
        ),
    ]

    result = select_prefetch("quarterly revenue", rows)

    assert [row["fact_id"] for row in result] == ["body-only"]


def test_zero_token_overlap_on_either_field_excludes_the_row() -> None:
    """A row that has tokens in title and body but no overlap with the
    utterance on EITHER field is excluded — score 0.
    """

    rows = [
        _row(
            "no-overlap",
            title="Picnic basket",
            body="Hiking gear checklist.",
        ),
        _row(
            "title-only",
            title="Quarterly revenue",
            body="No body match.",
        ),
    ]

    result = select_prefetch("quarterly revenue", rows)

    assert [row["fact_id"] for row in result] == ["title-only"]


def test_tokenizer_is_case_insensitive_and_splits_on_non_alphanumeric() -> None:
    """Tokenisation is lowercase + non-alphanumeric split (same shape
    as ``MemoryBuffer.recall`` so prefetch and recall agree on what a
    token is).
    """

    rows = [
        _row("a", title="Quarterly-Revenue", body="prefers Q3 numbers."),
    ]

    result = select_prefetch("QUARTERLY revenue", rows)

    assert [row["fact_id"] for row in result] == ["a"]


# ---------------------------------------------------------------------------
# Payload shape: title + hook only (no full body leak).
# ---------------------------------------------------------------------------


def test_payload_contains_fact_id_title_and_hook_only() -> None:
    """SC7: payload rows carry fact_id, title, and hook. The full body
    is NOT in the row so a prefetch can never leak the body bytes into
    the model-facing payload.
    """

    rows = [
        _row(
            "a",
            title="Quarterly preference",
            body=(
                "Operator prefers quarterly revenue.\n"
                "This second sentence must not leak."
            ),
        ),
    ]

    [row] = select_prefetch("quarterly revenue", rows)

    assert set(row.keys()) == {"fact_id", "title", "hook"}
    assert row["fact_id"] == "a"
    assert row["title"] == "Quarterly preference"
    assert row["hook"] == "Operator prefers quarterly revenue."
    assert "This second sentence must not leak." not in row["hook"]


def test_hook_falls_back_to_legacy_text_when_body_is_empty() -> None:
    """A row whose ``body`` defaulted to ``text`` still produces a hook."""

    rows = [
        MemoryRow(
            id="legacy",
            text="Legacy memory line.",
            metadata={},
            created_at=_now(),
        ),
    ]

    [row] = select_prefetch("legacy", rows)

    assert row["fact_id"] == "legacy"
    assert row["hook"] == "Legacy memory line."


def test_hook_is_truncated_to_hook_max_chars() -> None:
    """Hook truncation follows ``HOOK_MAX_CHARS`` (memory-store SC5)."""

    long_line = "x" * 200
    rows = [_row("a", title="Long body", body=long_line)]

    [row] = select_prefetch("long body", rows)

    assert len(row["hook"]) == HOOK_MAX_CHARS


# ---------------------------------------------------------------------------
# Deterministic tiebreak (score desc, then stable).
# ---------------------------------------------------------------------------


def test_tiebreak_is_deterministic_with_equal_scores() -> None:
    """Equal-score rows break deterministically so repeated calls
    produce the same selection (no test-suite flakiness on hash order).
    """

    created = _now()
    later = created + timedelta(seconds=1)
    rows = [
        _row("z", title="Quarterly", body="Prefer quarterly revenue.", created_at=created),
        _row("a", title="Revenue", body="Quarterly comparisons.", created_at=later),
        _row("m", title="Revenue", body="Quarterly preferences.", created_at=created),
    ]

    first = select_prefetch("quarterly revenue", rows)
    second = select_prefetch("quarterly revenue", rows)

    assert first == second
    assert len(first) == 3
    # Tiebreak must be a total order — distinct ids at distinct slots.
    assert {row["fact_id"] for row in first} == {"a", "m", "z"}


# ---------------------------------------------------------------------------
# Occupancy overhead accounting (SC24).
# ---------------------------------------------------------------------------


def test_prefetch_overhead_tokens_is_zero_for_empty_payload() -> None:
    """Empty prefetch contributes zero overhead — nothing to account for."""

    assert prefetch_overhead_tokens([]) == 0


def test_prefetch_overhead_tokens_is_positive_for_non_empty_payload() -> None:
    """SC24: a non-empty prefetch contributes > 0 overhead tokens."""

    rows = select_prefetch(
        "quarterly revenue",
        [
            _row("a", title="Quarterly", body="Prefer quarterly revenue."),
            _row("b", title="Revenue", body="Quarterly comparisons."),
        ],
    )

    overhead = prefetch_overhead_tokens(rows)

    assert overhead > 0


def test_prefetch_overhead_uses_chars_per_token_approximation() -> None:
    """Overhead tokens follow the same chars/4 approximation that
    :mod:`session_compaction` uses so the two share one budget.
    """

    # title "Quarterly" (9 chars) + hook "Revenue is great" (16 chars)
    # = 25 chars ⇒ 6 tokens under chars/4.
    rows = [{"fact_id": "a", "title": "Quarterly", "hook": "Revenue is great"}]

    assert prefetch_overhead_tokens(rows) == 25 // 4


def test_occupancy_with_prefetch_overhead_exceeds_occupancy_without() -> None:
    """SC24 integration: the ledger sees a higher occupancy when the
    caller passes the prefetch overhead into ``occupancy``.
    """

    compactor = OccupancyCompactor()
    base_messages = [HumanMessage(content="ask me about quarterly revenue.")]

    rows = select_prefetch(
        "quarterly revenue",
        [
            _row("a", title="Quarterly", body="Prefer quarterly revenue."),
            _row("b", title="Revenue", body="Quarterly comparisons."),
        ],
    )
    overhead = prefetch_overhead_tokens(rows)

    with session_token_budget(1_600):
        without = compactor.occupancy(base_messages)
        with_overhead = compactor.occupancy(
            base_messages, overhead_tokens=overhead
        )

    assert with_overhead > without


def test_occupancy_ledger_actually_sees_a_real_prefetch_message() -> None:
    """A realistic mini integration: feeding a prefetch-shaped
    ``HumanMessage`` into the compactor raises occupancy versus an
    identical history without that message — pinning that the user-side
    overlay the host assembly produces moves the ledger, not just
    ``overhead_tokens``.
    """

    compactor = OccupancyCompactor()
    body = "ask me about quarterly revenue."
    without_prefetch = [HumanMessage(content=body)]
    prefetch_payload = (
        "Memory hooks:\n"
        "- Quarterly preference / Prefer quarterly revenue.\n"
        "- Revenue focus / Quarterly comparisons."
    )
    with_prefetch = [
        HumanMessage(content=body),
        HumanMessage(content=prefetch_payload),
    ]

    with session_token_budget(1_600):
        without = compactor.occupancy(without_prefetch)
        with_ = compactor.occupancy(with_prefetch)

    assert with_ > without


# ---------------------------------------------------------------------------
# Typed input errors.
# ---------------------------------------------------------------------------


def test_non_string_user_utterance_raises_typed_input_error() -> None:
    with pytest.raises(PrefetchInputError):
        select_prefetch(123, [])  # type: ignore[arg-type]


def test_non_iterable_rows_raises_typed_input_error() -> None:
    with pytest.raises(PrefetchInputError):
        select_prefetch("quarterly", "not a list")  # type: ignore[arg-type]


def test_non_set_shown_ids_raises_typed_input_error() -> None:
    with pytest.raises(PrefetchInputError):
        select_prefetch(
            "quarterly",
            [_row("a", title="Quarterly", body="Prefer quarterly revenue.")],
            shown_ids=["a"],  # type: ignore[arg-type]
        )


def test_prefetch_overhead_tokens_rejects_non_sequence_input() -> None:
    with pytest.raises(PrefetchInputError):
        prefetch_overhead_tokens("not a list")  # type: ignore[arg-type]