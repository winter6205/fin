"""T7 gated auto-extract contracts.

``specs/cross-session-operator-memory.md`` SC14-SC16 / ADR 0048. The
auto-extract path turns a multi-turn dialogue into at most one memory
fact per firing, gated by code (not prompt):

* SC14 — auto-extract does not run after 2 completed turns; it may
  run after 3; this-turn explicit save -> 0 extract writes.
* SC15 — an extract candidate that is a retrieve-able filing claim,
  or that duplicates user profile text, is not persisted.
* SC16 — one qualifying extract window persists at most one new
  memory fact.

The deny classes live as CODE gates per ``docs/guides/prompt-development.md``
("否决类能 tokenize 重叠的走代码，不把唯一否决写在 prompt"). The LLM only
PROPOSES the candidate; the gate decides.

The extractor is callable standalone (``maybe_extract``) so an
integration ticket can wire it into a turn-completion node or an
HTTP/SSE hook without touching the extract module's tests.

Boundary classes per ``code-quality.md`` / ``test.md``:

* empty — completed turns below threshold; empty message list;
  LLM returns nothing
* negative — filing-claim / profile-duplicate / identity content
  / already-persisted duplicate / this-turn save -> 0 writes
* overflow — multiple candidate facts -> cap 1
* concurrent — same turn count re-fires don't double-write
* exception — LLM raises -> typed failure; misconfigured inputs
  raise a typed error (config errors only)
"""

from __future__ import annotations

from typing import Sequence

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from fin.host.memory_extract import (
    ExtractResult,
    FactCandidate,
    FactProposer,
    InProcessExtractCursor,
    NullExtractor,
    count_completed_turns,
    extract_filing_claim_locator,
    is_filing_claim,
    is_identity_content,
    maybe_extract,
    normalize_for_overlap,
    overlaps_user_profile,
)
from fin.host.memory_buffer import LocalMemoryBuffer
from fin.host.memory_store import InProcessMemoryStore
from fin.host.user_profile import UserProfileStore


@pytest.fixture(autouse=True)
def _reset_default_cursor() -> "InProcessExtractCursor":
    """Reset the shared default cursor before every test.

    ``maybe_extract`` falls back to a module-level
    :data:`_DEFAULT_EXTRACT_CURSOR` when no cursor is injected; that
    is the production default but it makes every test in this file
    see prior tests' thread entries. Each test gets a fresh cursor
    for isolation.
    """
    from fin.host import memory_extract

    memory_extract._DEFAULT_EXTRACT_CURSOR.reset()
    return memory_extract._DEFAULT_EXTRACT_CURSOR


# ---------------------------------------------------------------------
# shared fakes
# ---------------------------------------------------------------------


class _StaticProposer:
    """A trivial :class:`FactProposer` that returns a pre-set candidate.

    Used to inject the candidate without an LLM call: the gates are
    what this suite covers.
    """

    def __init__(self, candidate: FactCandidate | None) -> None:
        self._candidate = candidate
        self.calls = 0

    def propose(
        self, messages: Sequence
    ) -> FactCandidate | None:
        self.calls += 1
        return self._candidate


class _MultiProposer:
    """A :class:`FactProposer` that returns candidates in order per call."""

    def __init__(self, candidates: list[FactCandidate | None]) -> None:
        self._candidates = list(candidates)
        self.calls = 0

    def propose(
        self, messages: Sequence
    ) -> FactCandidate | None:
        if not self._candidates:
            return None
        self.calls += 1
        return self._candidates.pop(0)


class _RaisingProposer:
    def propose(self, messages: Sequence) -> FactCandidate | None:
        raise RuntimeError("LLM down")


def _state_with_messages(messages: list) -> dict:
    """Build a minimal state dict carrying the message list."""
    return {"messages": list(messages)}


def _completed_turns(
    pairs: int, *, in_progress_human: bool = False
) -> list:
    """``pairs`` completed turns plus optionally an in-progress human turn.

    A completed turn is a (HumanMessage, AIMessage) pair. The
    in-progress human turn is the trailing HumanMessage WITHOUT a
    trailing AI reply yet — its count comes from a follow-up call,
    not from the current one.
    """
    messages: list = []
    for index in range(pairs):
        messages.append(HumanMessage(content=f"question {index}"))
        messages.append(AIMessage(content=f"reply {index}"))
    if in_progress_human:
        messages.append(HumanMessage(content="next question"))
    return messages


# ---------------------------------------------------------------------
# turn counting
# ---------------------------------------------------------------------


def test_count_completed_turns_zero_for_empty_messages() -> None:
    assert count_completed_turns([]) == 0


def test_count_completed_turns_zero_for_unanswered_human_turn() -> None:
    messages = [HumanMessage(content="hi")]
    assert count_completed_turns(messages) == 0


def test_count_completed_turns_one_for_one_completed_pair() -> None:
    messages = _completed_turns(1)
    assert count_completed_turns(messages) == 1


def test_count_completed_turns_two_for_two_completed_pairs() -> None:
    messages = _completed_turns(2)
    assert count_completed_turns(messages) == 2


def test_count_completed_turns_three_for_three_completed_pairs() -> None:
    messages = _completed_turns(3)
    assert count_completed_turns(messages) == 3


def test_count_completed_turns_ignores_in_progress_human_message() -> None:
    """An unanswered trailing HumanMessage is NOT a completed turn."""
    messages = _completed_turns(2, in_progress_human=True)
    assert count_completed_turns(messages) == 2


def test_count_completed_turns_accepts_state_dict() -> None:
    """Both the state dict and a bare list must work — caller-open."""
    state = _state_with_messages(_completed_turns(2))
    assert count_completed_turns(state) == 2


# ---------------------------------------------------------------------
# NullExtractor default
# ---------------------------------------------------------------------


def test_null_extractor_proposes_nothing() -> None:
    assert NullExtractor().propose([]) is None


def test_null_extractor_used_by_default_means_no_llm_call() -> None:
    """Spec acceptance: NullExtractor default -> no LLM (zero proposal)."""
    buffer = LocalMemoryBuffer()
    result = maybe_extract(
        _state_with_messages(_completed_turns(3)),
        buffer,
        thread_id="thread-1",
        workspace="ws-1",
    )
    assert result.status == "skipped"
    assert result.reason == "no_proposal"
    assert result.fact is None
    # No row landed in the buffer.
    assert buffer.entries(workspace="ws-1") == []


# ---------------------------------------------------------------------
# SC14 — N=3 completed turns gate
# ---------------------------------------------------------------------


def test_two_completed_turns_does_not_fire() -> None:
    """SC14: 2 completed turns -> 0 auto facts."""
    proposer = _StaticProposer(
        FactCandidate(title="likes bullet points", body="Operator likes bullet points.")
    )
    buffer = LocalMemoryBuffer()
    result = maybe_extract(
        _state_with_messages(_completed_turns(2)),
        buffer,
        thread_id="thread-1",
        workspace="ws-1",
        llm=proposer,
    )
    assert result.status == "skipped"
    assert result.reason == "below_turn_threshold"
    assert result.fact is None
    assert buffer.entries(workspace="ws-1") == []


def test_three_completed_turns_may_write_at_most_one_fact() -> None:
    """SC14 + SC16: 3 completed turns -> at most ONE fact."""
    proposer = _MultiProposer(
        [
            FactCandidate(
                title="likes bullet points",
                body="Operator likes bullet points.",
            ),
            FactCandidate(
                title="prefers morning updates",
                body="Operator prefers morning updates.",
            ),
        ]
    )
    buffer = LocalMemoryBuffer()
    result = maybe_extract(
        _state_with_messages(_completed_turns(3)),
        buffer,
        thread_id="thread-1",
        workspace="ws-1",
        llm=proposer,
    )
    assert result.status == "extracted"
    assert result.fact is not None
    assert result.fact["title"] == "likes bullet points"
    rows = buffer.entries(workspace="ws-1")
    assert len(rows) == 1
    assert rows[0]["title"] == "likes bullet points"


def test_three_completed_turns_with_in_progress_human_does_not_fire() -> None:
    """The in-progress turn is NOT counted: 2 completed + 1 in-progress -> skip."""
    proposer = _StaticProposer(
        FactCandidate(title="x", body="y")
    )
    buffer = LocalMemoryBuffer()
    result = maybe_extract(
        _state_with_messages(_completed_turns(2, in_progress_human=True)),
        buffer,
        thread_id="thread-1",
        workspace="ws-1",
        llm=proposer,
    )
    assert result.status == "skipped"
    assert result.reason == "below_turn_threshold"


def test_four_completed_turns_writes_at_most_one() -> None:
    """SC16 cap-1 must hold even with 4 completed turns (one firing)."""
    proposer = _MultiProposer(
        [
            FactCandidate(title="first", body="first fact"),
            FactCandidate(title="second", body="second fact"),
            FactCandidate(title="third", body="third fact"),
        ]
    )
    buffer = LocalMemoryBuffer()
    result = maybe_extract(
        _state_with_messages(_completed_turns(4)),
        buffer,
        thread_id="thread-1",
        workspace="ws-1",
        llm=proposer,
    )
    assert result.status == "extracted"
    rows = buffer.entries(workspace="ws-1")
    assert len(rows) == 1


# ---------------------------------------------------------------------
# SC14 — this-turn explicit save skips extract
# ---------------------------------------------------------------------


def test_this_turn_save_memory_skips_extract() -> None:
    """SC14: this-turn save -> 0 extract writes."""
    last_ai = AIMessage(
        content="",
        tool_calls=[
            {"id": "call-1", "name": "save_memory", "args": {"title": "t", "body": "b"}},
        ],
    )
    messages = [
        HumanMessage(content="q0"),
        AIMessage(content="r0"),
        HumanMessage(content="q1"),
        AIMessage(content="r1"),
        HumanMessage(content="q2"),
        last_ai,
        ToolMessage(content='{"status": "ok"}', tool_call_id="call-1", name="save_memory"),
    ]
    proposer = _StaticProposer(
        FactCandidate(title="auto", body="auto body")
    )
    buffer = LocalMemoryBuffer()
    # Seed a row so the manual save is observable.
    buffer.extract("manual row", metadata={"title": "manual"})
    before = len(buffer.entries())
    result = maybe_extract(
        _state_with_messages(messages),
        buffer,
        thread_id="thread-1",
        workspace="ws-1",
        llm=proposer,
    )
    assert result.status == "skipped"
    assert result.reason == "this_turn_save"
    assert result.fact is None
    assert len(buffer.entries()) == before  # nothing new


def test_unrelated_tool_call_does_not_skip_extract() -> None:
    """A tool call to a non-save tool must NOT skip extract."""
    last_ai = AIMessage(
        content="",
        tool_calls=[
            {"id": "call-1", "name": "retrieve", "args": {"query": "q"}},
        ],
    )
    messages = [
        HumanMessage(content="q0"),
        AIMessage(content="r0"),
        HumanMessage(content="q1"),
        AIMessage(content="r1"),
        HumanMessage(content="q2"),
        last_ai,
        ToolMessage(content='{"chunks": []}', tool_call_id="call-1", name="retrieve"),
    ]
    proposer = _StaticProposer(
        FactCandidate(title="auto", body="auto body")
    )
    buffer = LocalMemoryBuffer()
    result = maybe_extract(
        _state_with_messages(messages),
        buffer,
        thread_id="thread-1",
        workspace="ws-1",
        llm=proposer,
    )
    assert result.status == "extracted"


# ---------------------------------------------------------------------
# SC15 — deny classes are code gates
# ---------------------------------------------------------------------


def test_filing_claim_with_citation_regex_is_not_persisted() -> None:
    """A candidate whose body names a corpus document like AAPL-2022Q3
    must be denied as a retrieve-able filing claim (SC15)."""
    proposer = _StaticProposer(
        FactCandidate(
            title="AAPL revenue",
            body="Per AAPL-2022Q3 the company reported 394328M revenue.",
        )
    )
    buffer = LocalMemoryBuffer()
    result = maybe_extract(
        _state_with_messages(_completed_turns(3)),
        buffer,
        thread_id="thread-1",
        workspace="ws-1",
        llm=proposer,
    )
    assert result.status == "denied"
    assert result.reason == "filing_claim"
    assert result.fact is None
    assert buffer.entries(workspace="ws-1") == []


def test_filing_claim_with_explicit_phrase_is_not_persisted() -> None:
    """A candidate that names 'the filing' / 'retrieved document' is also denied."""
    proposer = _StaticProposer(
        FactCandidate(
            title="revenue",
            body="Per the filing the issuer reported 394M revenue.",
        )
    )
    buffer = LocalMemoryBuffer()
    result = maybe_extract(
        _state_with_messages(_completed_turns(3)),
        buffer,
        thread_id="thread-1",
        workspace="ws-1",
        llm=proposer,
    )
    assert result.status == "denied"
    assert result.reason == "filing_claim"


def test_profile_duplicate_is_not_persisted(tmp_path) -> None:
    """A candidate that duplicates user-profile text is denied (SC15)."""
    profile_store = UserProfileStore(
        global_path=tmp_path / "global.md",
        workspace_path=tmp_path / "workspace.md",
    )
    profile_store.write_layer(
        "workspace",
        "# operator\nlikes bullet point answers and short receipts.\n",
    )
    proposer = _StaticProposer(
        FactCandidate(
            title="preferences",
            body="Operator likes bullet point answers and short receipts.",
        )
    )
    buffer = LocalMemoryBuffer()
    result = maybe_extract(
        _state_with_messages(_completed_turns(3)),
        buffer,
        thread_id="thread-1",
        workspace="ws-1",
        user_profile_store=profile_store,
        llm=proposer,
    )
    assert result.status == "denied"
    assert result.reason == "profile_duplicate"
    assert buffer.entries(workspace="ws-1") == []


def test_partial_profile_overlap_is_not_persisted(tmp_path) -> None:
    """A candidate that is mostly the profile text is still denied."""
    profile_store = UserProfileStore(
        global_path=tmp_path / "global.md",
        workspace_path=tmp_path / "workspace.md",
    )
    profile_store.write_layer(
        "workspace",
        "operator prefers quarterly revenue comparisons across the last six quarters\n",
    )
    proposer = _StaticProposer(
        FactCandidate(
            title="preferences",
            body="The operator prefers quarterly revenue comparisons across the last six quarters "
            "and the workspace owner prefers quarterly.",
        )
    )
    buffer = LocalMemoryBuffer()
    result = maybe_extract(
        _state_with_messages(_completed_turns(3)),
        buffer,
        thread_id="thread-1",
        workspace="ws-1",
        user_profile_store=profile_store,
        llm=proposer,
    )
    assert result.status == "denied"
    assert result.reason == "profile_duplicate"


def test_identity_content_is_not_persisted() -> None:
    """A candidate about the agent's own identity/persona is denied (SC15)."""
    proposer = _StaticProposer(
        FactCandidate(
            title="agent identity",
            body="I am fin, a workspace knowledge-base agent, and I answer with citations.",
        )
    )
    buffer = LocalMemoryBuffer()
    result = maybe_extract(
        _state_with_messages(_completed_turns(3)),
        buffer,
        thread_id="thread-1",
        workspace="ws-1",
        llm=proposer,
    )
    assert result.status == "denied"
    assert result.reason == "identity_content"
    assert buffer.entries(workspace="ws-1") == []


def test_already_persisted_duplicate_is_not_persisted() -> None:
    """A candidate whose (title, hook) already exists in the store is denied."""
    buffer = LocalMemoryBuffer()
    # Seed a row whose hook matches the candidate body's first line.
    buffer.extract(
        "Operator likes bullet point answers.",
        title="preferences",
        workspace="ws-1",
    )
    proposer = _StaticProposer(
        FactCandidate(
            title="preferences",
            body="Operator likes bullet point answers.",
        )
    )
    result = maybe_extract(
        _state_with_messages(_completed_turns(3)),
        buffer,
        thread_id="thread-1",
        workspace="ws-1",
        llm=proposer,
    )
    assert result.status == "denied"
    assert result.reason == "already_persisted"
    assert len(buffer.entries(workspace="ws-1")) == 1


def test_extract_skips_when_llm_returns_no_candidate() -> None:
    """Empty LLM output -> skip (no writes)."""
    buffer = LocalMemoryBuffer()
    result = maybe_extract(
        _state_with_messages(_completed_turns(3)),
        buffer,
        thread_id="thread-1",
        workspace="ws-1",
        llm=_StaticProposer(None),
    )
    assert result.status == "skipped"
    assert result.reason == "no_proposal"
    assert buffer.entries(workspace="ws-1") == []


# ---------------------------------------------------------------------
# SC16 — one firing = one evaluation, no double-write
# ---------------------------------------------------------------------


def test_repeat_call_with_same_turn_count_does_not_double_write() -> None:
    """Two calls at the same completed-turn count must write at most once."""
    proposer = _StaticProposer(
        FactCandidate(title="habit", body="Operator likes bullet points.")
    )
    buffer = LocalMemoryBuffer()
    messages = _completed_turns(3)
    first = maybe_extract(
        _state_with_messages(messages),
        buffer,
        thread_id="thread-1",
        workspace="ws-1",
        llm=proposer,
    )
    second = maybe_extract(
        _state_with_messages(messages),
        buffer,
        thread_id="thread-1",
        workspace="ws-1",
        llm=proposer,
    )
    assert first.status == "extracted"
    assert second.status == "skipped"
    assert second.reason == "already_evaluated"
    assert len(buffer.entries(workspace="ws-1")) == 1


def test_higher_turn_count_triggers_a_new_evaluation() -> None:
    """A new completed turn DOES open a new evaluation (cursor advances).

    With the same proposer, the second firing surfaces the SAME
    candidate, which the duplicate gate then denies — the firing
    ran, the LLM was asked, and the candidate was rejected. The
    critical assertion is that the cursor DID advance (so the gate
    entered, not short-circuited at ``already_evaluated``).
    """
    proposer = _StaticProposer(
        FactCandidate(title="habit", body="Operator likes bullet points.")
    )
    buffer = LocalMemoryBuffer()
    first_state = _state_with_messages(_completed_turns(3))
    second_state = _state_with_messages(_completed_turns(4))
    maybe_extract(
        first_state,
        buffer,
        thread_id="thread-A",
        workspace="ws-1",
        llm=proposer,
    )
    result = maybe_extract(
        second_state,
        buffer,
        thread_id="thread-A",
        workspace="ws-1",
        llm=proposer,
    )
    # A new firing at a higher turn count must NOT short-circuit
    # at "already_evaluated"; the duplicate gate catches the
    # repeated candidate instead.
    assert result.status == "denied"
    assert result.reason == "already_persisted"


def test_higher_turn_count_with_new_proposal_extracts_one_more_fact() -> None:
    """A higher turn count combined with a NEW candidate lands ONE more fact.

    Combines the cursor advance with the cap-1 invariant: at most
    one fact per firing, but a NEW firing on a higher turn count
    can land a second distinct fact.
    """
    proposer = _MultiProposer(
        [
            FactCandidate(
                title="habit-a",
                body="Operator likes bullet points.",
            ),
            FactCandidate(
                title="habit-b",
                body="Operator prefers quarterly comparisons.",
            ),
        ]
    )
    buffer = LocalMemoryBuffer()
    maybe_extract(
        _state_with_messages(_completed_turns(3)),
        buffer,
        thread_id="thread-A",
        workspace="ws-1",
        llm=proposer,
    )
    result = maybe_extract(
        _state_with_messages(_completed_turns(4)),
        buffer,
        thread_id="thread-A",
        workspace="ws-1",
        llm=proposer,
    )
    assert result.status == "extracted"
    assert len(buffer.entries(workspace="ws-1")) == 2


def test_threads_have_independent_cursors() -> None:
    """Per-thread isolation: thread A's cursor must not block thread B.

    Same workspace, but different threads — thread A's firing must
    not advance thread B's cursor. Distinct workspaces isolate the
    store too, so this assertion stays clean.
    """
    proposer = _StaticProposer(
        FactCandidate(title="habit", body="Operator likes bullet points.")
    )
    buffer = LocalMemoryBuffer()
    messages = _completed_turns(3)
    maybe_extract(
        _state_with_messages(messages),
        buffer,
        thread_id="thread-A",
        workspace="ws-1",
        llm=proposer,
    )
    result = maybe_extract(
        _state_with_messages(messages),
        buffer,
        thread_id="thread-B",
        workspace="ws-2",
        llm=proposer,
    )
    assert result.status == "extracted"


def test_workspaces_have_independent_cursors() -> None:
    """Per-workspace isolation: same thread, different workspaces.

    The store rows are workspace-scoped (spec SC6), so a row written
    in workspace 1 must not block an extract in workspace 2 even
    when the candidate is identical.
    """
    proposer = _StaticProposer(
        FactCandidate(title="habit", body="Operator likes bullet points.")
    )
    buffer = LocalMemoryBuffer()
    messages = _completed_turns(3)
    maybe_extract(
        _state_with_messages(messages),
        buffer,
        thread_id="thread-A",
        workspace="ws-1",
        llm=proposer,
    )
    result = maybe_extract(
        _state_with_messages(messages),
        buffer,
        thread_id="thread-A",
        workspace="ws-2",
        llm=proposer,
    )
    assert result.status == "extracted"


# ---------------------------------------------------------------------
# Helpers exposed for direct unit tests
# ---------------------------------------------------------------------


def test_extract_filing_claim_locator_matches_canonical_document_id() -> None:
    assert extract_filing_claim_locator("AAPL-2022Q3 revenue was 394328M") is not None
    assert extract_filing_claim_locator("per MSFT-2024Q1 the company") is not None


def test_extract_filing_claim_locator_returns_none_for_plain_text() -> None:
    assert extract_filing_claim_locator("operator likes bullet points") is None


def test_is_filing_claim_detects_filing_phrase() -> None:
    assert is_filing_claim("Per the filing the issuer reported ...") is True
    assert is_filing_claim("Per the retrieved document section 3") is True
    assert is_filing_claim("operator likes bullet points") is False


def test_is_identity_content_detects_self_referential_text() -> None:
    assert is_identity_content(
        "I am fin, a workspace knowledge-base agent."
    )
    assert is_identity_content("As an AI assistant I ...")
    assert not is_identity_content("operator likes bullet points")


def test_normalize_for_overlap_folds_whitespace_and_lowercases() -> None:
    assert normalize_for_overlap("Hello  WORLD\n\nFoo") == "hello world foo"


def test_overlaps_user_profile_above_threshold() -> None:
    profile = "operator prefers quarterly revenue comparisons"
    candidate = (
        "operator prefers quarterly revenue comparisons across six quarters"
    )
    assert overlaps_user_profile(candidate, profile, threshold=0.6)


def test_overlaps_user_profile_below_threshold() -> None:
    profile = "operator prefers quarterly revenue comparisons"
    candidate = "operator dislikes meetings"
    assert not overlaps_user_profile(candidate, profile, threshold=0.6)


def test_overlaps_user_profile_empty_profile_returns_false() -> None:
    assert not overlaps_user_profile("anything", "", threshold=0.6)


# ---------------------------------------------------------------------
# Exception paths
# ---------------------------------------------------------------------


def test_llm_raises_results_in_skipped_with_error_reason() -> None:
    """An LLM outage must not raise out of maybe_extract — deny/skip path only."""
    buffer = LocalMemoryBuffer()
    result = maybe_extract(
        _state_with_messages(_completed_turns(3)),
        buffer,
        thread_id="thread-1",
        workspace="ws-1",
        llm=_RaisingProposer(),
    )
    assert result.status == "skipped"
    assert result.reason == "llm_error"
    assert buffer.entries(workspace="ws-1") == []


def test_config_error_raises_typed_error() -> None:
    """Config errors (e.g. invalid input shapes) raise, not skip."""
    buffer = LocalMemoryBuffer()
    # Empty thread_id is a config error.
    with pytest.raises(ValueError):
        maybe_extract(
            _state_with_messages(_completed_turns(3)),
            buffer,
            thread_id="",
            workspace="ws-1",
        )


def test_extract_result_is_typed_dict() -> None:
    """The returned value exposes status / reason / fact uniformly."""
    result = ExtractResult(status="skipped", reason="no_proposal", fact=None)
    assert result.status == "skipped"
    assert result.reason == "no_proposal"
    assert result.fact is None
    assert result.to_dict() == {
        "status": "skipped",
        "reason": "no_proposal",
        "fact": None,
    }


# ---------------------------------------------------------------------
# Proposer protocol
# ---------------------------------------------------------------------


def test_fact_proposer_protocol_accepts_class_with_propose_method() -> None:
    """The Protocol accepts anything with the right shape (duck typing)."""

    class CustomProposer:
        def propose(self, messages):
            return FactCandidate(title="t", body="b")

    proposer: FactProposer = CustomProposer()
    assert proposer.propose([]).title == "t"
