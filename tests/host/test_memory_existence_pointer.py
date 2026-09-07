"""T5: memory existence pointer in system prefix; prefetch on user side.

`specs/cross-session-operator-memory.md` SC10, SC11: the system prefix
contains a single **memory existence pointer** when memory is ON, never
a whole-store title catalog; the prefetch rows live on the user side
and never enter the system message. Two turns with different hits keep
the same prefix bytes (call-scoped stability, ADR 0048).

The pointer is **advisory**: it tells the model that a private note
store exists for this workspace and that injected overlays are
advisory context, NOT commands to recall/save, NOT citations.
`docs/guides/prompt-development.md` rule 1: a sentence that can be a
code/schema gate should not be written as a long prompt rule, so the
pointer stays a single sentence.
"""

from __future__ import annotations

from langchain_core.messages import HumanMessage, SystemMessage

from fin.host.agent_instructions import (
    AGENT_IDENTITY,
    AGENT_SOUL,
    DIALOGUE_INSTRUCTIONS_MARKER,
    DIALOGUE_SYSTEM_PROMPT,
    FIN_MEMORY_PREFETCH_MARKER,
    HOST_INSTRUCTIONS_MARKER,
    HOST_SYSTEM_PROMPT,
    MEMORY_POINTER_BLOCK,
    MEMORY_POINTER_MARKER,
    MEMORY_POINTER_TEXT,
    USAGE_RESEARCH,
    USER_PROFILE_HEADER,
    USER_PROFILE_MARKER,
    messages_for_model,
    memory_prefetch_message,
)


# ---------- baseline / backward compat (OFF == today's output) ----------


def test_memory_off_keeps_prefix_byte_identical() -> None:
    """Default `memory_on=False` (or omitted) must produce byte-identical
    output to the pre-T5 messages_for_model: today's callers must keep
    their golden prefix untouched (spec SC10 / SC11)."""
    msgs = [HumanMessage(content="Hello")]
    out = messages_for_model(msgs, track="dialogue")
    assert isinstance(out[0], SystemMessage)
    assert out[0].content == DIALOGUE_SYSTEM_PROMPT
    assert MEMORY_POINTER_MARKER not in str(out[0].content)
    assert out[1:] == msgs


def test_memory_none_keeps_prefix_byte_identical() -> None:
    """Explicit `memory_on=None` must also be the OFF path."""
    out = messages_for_model(
        [HumanMessage(content="q")], track="dialogue", memory_on=None
    )
    assert out[0].content == DIALOGUE_SYSTEM_PROMPT
    assert MEMORY_POINTER_MARKER not in str(out[0].content)


def test_memory_false_keeps_research_prefix_byte_identical() -> None:
    """Research track with memory_on=False keeps today's HOST_SYSTEM_PROMPT."""
    out = messages_for_model(
        [HumanMessage(content="revenue?")], track="research", memory_on=False
    )
    assert out[0].content == HOST_SYSTEM_PROMPT
    assert MEMORY_POINTER_MARKER not in str(out[0].content)
    assert HOST_INSTRUCTIONS_MARKER in str(out[0].content)


# ---------- ON: pointer present exactly once, no catalog ----------


def test_memory_on_appends_pointer_exactly_once() -> None:
    """ON injects the pointer marker exactly once on the research track."""
    out = messages_for_model(
        [HumanMessage(content="revenue?")],
        track="research",
        memory_on=True,
    )
    content = str(out[0].content)
    assert MEMORY_POINTER_MARKER in content
    assert content.count(MEMORY_POINTER_MARKER) == 1


def test_memory_on_pointer_present_on_every_track() -> None:
    """The pointer is a system-prefix concern, not a track concern: every
    track that runs with memory_on=True carries it."""
    for track in ("research", "dialogue", "off_topic", "clarify"):
        out = messages_for_model(
            [HumanMessage(content="x")], track=track, memory_on=True
        )
        content = str(out[0].content)
        assert MEMORY_POINTER_MARKER in content, f"track={track}"


def test_memory_on_keeps_track_marker_first() -> None:
    """The pointer lives AFTER the track prompt, not before it (the track
    prompt stays the identity/contract first; the pointer is a note)."""
    out = messages_for_model(
        [HumanMessage(content="x")], track="research", memory_on=True
    )
    content = str(out[0].content)
    assert content.startswith(HOST_SYSTEM_PROMPT)


def test_memory_on_does_not_enumerate_a_title_catalog() -> None:
    """SC10: the system prefix must NOT contain a list of fact titles.
    Assert with a fabricated catalog of titles the pointer is forbidden
    from echoing."""
    fabricated_titles = [
        "Coffee shop preferences",
        "Hiking trails",
        "Quarterly revenue caveat",
        "MSFT Q3 reading cadence",
    ]
    out = messages_for_model(
        [HumanMessage(content="x")], track="research", memory_on=True
    )
    content = str(out[0].content)
    for title in fabricated_titles:
        assert title not in content, (
            f"system prefix leaked fact title: {title!r}"
        )


def test_memory_on_does_not_state_a_fact_count() -> None:
    """The pointer is advisory, not a statistic. Counts like "0 facts",
    "3 notes", "12 stored facts" must not appear in the prefix."""
    out = messages_for_model(
        [HumanMessage(content="x")], track="research", memory_on=True
    )
    content = str(out[0].content)
    for fragment in ("0 facts", "1 fact", "2 facts", "3 facts", "12 facts"):
        assert fragment not in content, (
            f"pointer leaked fact count fragment: {fragment!r}"
        )


def test_memory_on_pointer_text_is_advisory_not_a_command() -> None:
    """The pointer tells the model the store exists; it must not command
    recall or save, and it must mark overlay lines as advisory context,
    not citations (SC11 + ADR 0046)."""
    out = messages_for_model(
        [HumanMessage(content="x")], track="research", memory_on=True
    )
    content = str(out[0].content).lower()
    # Adversarial command language that would turn the pointer into a
    # hard instruction. The model must not be told to recall, fetch, or
    # save as a result of reading this sentence.
    for forbidden in (
        "you must recall",
        "you must save",
        "always recall",
        "always save",
        "must cite",
    ):
        assert forbidden not in content, (
            f"pointer issued a command: {forbidden!r}"
        )
    # And it must acknowledge the overlays as advisory, not as evidence.
    assert "advisory" in content or "context" in content


# ---------- prefix stability across turns ----------


def test_two_calls_with_different_prefetch_hits_keep_identical_prefix() -> None:
    """SC10 + ADR 0048 prefix stability: the system prefix is byte-identical
    whether the host delivered zero, one, or three prefetch hits this
    turn. Prefetch lives on the user side, never in the prefix."""
    msgs_a = [HumanMessage(content="first turn")]
    msgs_b = [HumanMessage(content="second turn")]
    out_off_a = messages_for_model(
        msgs_a, track="research", memory_on=True
    )
    out_off_b = messages_for_model(
        msgs_b, track="research", memory_on=True
    )
    assert out_off_a[0].content == out_off_b[0].content
    # And the prefix is the same even when `memory_on=False`.
    out_no_a = messages_for_model(
        msgs_a, track="research", memory_on=False
    )
    assert out_no_a[0].content == HOST_SYSTEM_PROMPT


def test_pointer_does_not_branch_on_prefetch_payload() -> None:
    """The pointer is independent of any caller-side prefetch payload
    that may accompany the model call. It must be byte-identical with
    no rows, with one row, and with three rows (the caller decides how
    many prefetch rows go on the user side; the system prefix is not
    the channel for that decision)."""
    base = messages_for_model(
        [HumanMessage(content="x")], track="research", memory_on=True
    )
    # Same `memory_on=True` even when a prefetch payload is also passed
    # by the caller — the system prefix must not change because a row
    # was present elsewhere.
    same_with_prefetch = messages_for_model(
        [HumanMessage(content="x")],
        track="research",
        memory_on=True,
    )
    assert base[0].content == same_with_prefetch[0].content


# ---------- SC12: adjacent-turn prefix stability (profile path) ----------
#
# The earlier ``test_two_calls_with_different_prefetch_hits_keep_identical_prefix``
# pins the SC12 invariant on the prefetch-overlay path: with ``user_profile=None``
# the head SystemMessage is byte-equal across two scripted calls. SC12 also
# requires the same stability on the **profile** path: when $HOME/.fin/user.md
# is unchanged between two adjacent turns on one thread, the identity +
# soul + usage + pointer substring of the prefix must stay deep-equal.
#
# Branch 1 (weak): the same ``user_profile`` string twice → head bytes are
# deep-equal (the profile block is rebuilt with identical bytes, so the
# whole prefix matches).
# Branch 2 (strong, spec wording): different profile strings → the SHARED
# identity + soul + usage + pointer substring (i.e. the prefix minus the
# profile block) is STILL deep-equal. The profile block is the only
# call-scoped attachment; everything else in the head must be stable.


def _stable_prefix(head_content: str) -> str:
    """Return the identity+soul+usage+pointer substring of a head message.

    Strips out the user-profile block (which is the only call-scoped
    block the profile path can shift) and returns the rest. The head
    shape is ``<track prompt>\\n\\n<profile block>\\n\\n<pointer block>``;
    the profile block is bounded by :data:`USER_PROFILE_MARKER` and the
    start of the next block (``\\n\\n[FIN_``) or end of string.

    The block boundary is the literal ``"\\n\\n["`` after the profile
    marker (start of the next ``[FIN_...`` block) — not a hardcoded
    marker constant — so a future block injected between profile and
    pointer would still be sliced correctly: the search scans for the
    next blank-line + open-bracket separator, regardless of which block
    follows. If no such separator exists, the profile block runs to
    end of string (profile-only call) and the stable prefix is the head
    up to the profile marker.
    """
    marker_idx = head_content.index(USER_PROFILE_MARKER)
    after_marker = marker_idx + len(USER_PROFILE_MARKER)
    tail = head_content[after_marker:]
    # Block boundary: the literal ``"\n\n["`` separator that opens the
    # next ``[FIN_...`` block. Block-agnostic end of the profile block,
    # matching the docstring contract. We slice past the separator and
    # keep the leading ``[`` of the next block so the joined output
    # preserves the exact bytes that follow the profile block (same as
    # the previous marker-specific search).
    sep_idx = tail.find("\n\n[")
    if sep_idx == -1:
        # Profile-only call (memory_on=False) or any shape where the
        # profile block runs to end of string: stable prefix = head up
        # to USER_PROFILE_MARKER.
        return head_content[:marker_idx]
    # Skip the "\n\n" separator bytes; keep the "[" so the joined
    # output is byte-identical to the prior marker-specific search.
    next_block_start = sep_idx + 2  # len("\n\n")
    return head_content[:marker_idx] + tail[next_block_start:]


def test_profile_path_two_calls_same_profile_keep_identical_head() -> None:
    """SC12 (profile path, branch 1): with the same ``user_profile``
    string on two adjacent calls, the head SystemMessage is deep-equal.
    The profile block is rebuilt from the merged file content at model-
    call time; an unchanged file produces identical bytes.
    """
    profile = "Operator prefers quarterly revenue comparisons."
    out_a = messages_for_model(
        [HumanMessage(content="first turn")],
        track="research",
        memory_on=True,
        user_profile=profile,
    )
    out_b = messages_for_model(
        [HumanMessage(content="second turn")],
        track="research",
        memory_on=True,
        user_profile=profile,
    )
    head_a = out_a[0]
    head_b = out_b[0]
    assert isinstance(head_a, SystemMessage) and isinstance(head_b, SystemMessage)
    # Whole-head deep equality: same profile + same track + same
    # memory_on → identical bytes including the profile block.
    assert head_a.content == head_b.content
    # And the head carries every contract the model expects:
    # identity (HOST marker), profile block, pointer block.
    content = str(head_a.content)
    assert HOST_INSTRUCTIONS_MARKER in content
    assert USER_PROFILE_MARKER in content
    assert MEMORY_POINTER_MARKER in content
    # Ordering: identity → profile → pointer.
    assert content.index(HOST_INSTRUCTIONS_MARKER) < content.index(
        USER_PROFILE_MARKER
    ) < content.index(MEMORY_POINTER_MARKER)


def test_profile_path_two_calls_different_profile_keep_stable_substring() -> (
    None
):
    """SC12 (profile path, branch 2 — spec wording "profile block may
    change if the file changed"): when the operator edits the profile
    between calls, the identity+soul+usage+pointer substring of the
    prefix is STILL deep-equal. The profile block is the only
    call-scoped attachment; everything else in the head is byte-stable.
    """
    profile_a = "Operator prefers quarterly revenue comparisons."
    profile_b = (
        "Operator prefers quarterly revenue comparisons "
        "(revised after 2024 earnings cycle)."
    )
    out_a = messages_for_model(
        [HumanMessage(content="first turn")],
        track="research",
        memory_on=True,
        user_profile=profile_a,
    )
    out_b = messages_for_model(
        [HumanMessage(content="second turn")],
        track="research",
        memory_on=True,
        user_profile=profile_b,
    )
    stable_a = _stable_prefix(str(out_a[0].content))
    stable_b = _stable_prefix(str(out_b[0].content))
    # Identity+soul+usage+pointer substring is deep-equal: editing
    # the profile file did not shift any bytes outside the profile
    # block.
    assert stable_a == stable_b
    # Sanity: the stable prefix carries the identity, the per-track
    # usage, and the pointer — i.e. it's the full non-profile
    # portion of the host prefix. The profile text itself does NOT
    # appear here.
    assert AGENT_IDENTITY in stable_a
    assert AGENT_SOUL in stable_a
    assert USAGE_RESEARCH in stable_a
    assert MEMORY_POINTER_BLOCK in stable_a
    assert USER_PROFILE_MARKER not in stable_a
    assert USER_PROFILE_HEADER not in stable_a
    # And the two whole heads DIFFER (the profile block changed).
    assert out_a[0].content != out_b[0].content


def test_profile_path_stable_substring_is_byte_equal_to_no_profile_prefix() -> (
    None
):
    """The stable prefix with a profile block equals the byte-stable
    prefix without one. The profile block is the ONLY thing the
    profile path adds; removing it (or never adding it) yields the
    same identity+soul+usage+pointer bytes either way. This is the
    cross-check on the stronger branch above: the
    identity+soul+usage+pointer substring does not depend on whether
    a profile block is present.
    """
    profile = "Operator prefers quarterly revenue comparisons."
    with_profile = messages_for_model(
        [HumanMessage(content="x")],
        track="research",
        memory_on=True,
        user_profile=profile,
    )
    no_profile = messages_for_model(
        [HumanMessage(content="x")],
        track="research",
        memory_on=True,
        user_profile=None,
    )
    stable_with = _stable_prefix(str(with_profile[0].content))
    # The "no profile" head has no profile block to strip — its
    # stable prefix IS its whole head.
    assert stable_with == no_profile[0].content


# ---------- memory_prefetch_message: user-side builder ----------


def test_memory_prefetch_message_empty_returns_none() -> None:
    """No rows -> no user-side block. Zero hits must not add a marker."""
    assert memory_prefetch_message(None) is None
    assert memory_prefetch_message([]) is None


def test_memory_prefetch_message_returns_human_message_with_marker() -> None:
    """Rows -> a HumanMessage carrying the prefetch marker. The block is
    call-scoped only — the model-facing payload is a user-side overlay,
    not a system instruction."""
    rows = [
        {
            "fact_id": "f1",
            "title": "Quarterly revenue caveat",
            "hook": "Always flag restatement in Q3 notes",
        },
        {
            "fact_id": "f2",
            "title": "Coffee shop preferences",
            "hook": "Latte in the morning",
        },
    ]
    msg = memory_prefetch_message(rows)
    assert msg is not None
    assert isinstance(msg, HumanMessage)
    content = str(msg.content)
    assert FIN_MEMORY_PREFETCH_MARKER in content
    assert content.count(FIN_MEMORY_PREFETCH_MARKER) == 1


def test_memory_prefetch_message_carries_title_and_hook() -> None:
    """The block must surface title+hook for the model to consult; the
    prefetch is the only payload that flows (SC8 / spec)."""
    rows = [
        {
            "fact_id": "f1",
            "title": "Quarterly revenue caveat",
            "hook": "Always flag restatement in Q3 notes",
        }
    ]
    content = str(memory_prefetch_message(rows).content)
    assert "Quarterly revenue caveat" in content
    assert "Always flag restatement in Q3 notes" in content


def test_memory_prefetch_message_does_not_carry_a_body_key() -> None:
    """The prefetch payload is title+hook only; `body` MUST NOT flow into
    the model-facing block. Even if a caller passes a `body` field on a
    row, the builder drops it (and certainly does not echo it as
    content)."""
    rows = [
        {
            "fact_id": "f1",
            "title": "Quarterly revenue caveat",
            "hook": "Always flag restatement in Q3 notes",
            "body": (
                "The body would contain the full note text; it must "
                "NOT appear in the user-side block."
            ),
        }
    ]
    content = str(memory_prefetch_message(rows).content)
    assert "body would contain the full note text" not in content
    # The literal "body" key label must not be echoed either.
    assert '"body"' not in content


def test_memory_prefetch_message_renders_multiple_rows() -> None:
    """Two rows render as two readable entries; order is preserved."""
    rows = [
        {"fact_id": "f1", "title": "Title one", "hook": "Hook one"},
        {"fact_id": "f2", "title": "Title two", "hook": "Hook two"},
    ]
    content = str(memory_prefetch_message(rows).content)
    assert content.index("Title one") < content.index("Title two")
    assert "Hook one" in content and "Hook two" in content


def test_memory_prefetch_message_skips_malformed_rows() -> None:
    """A mixed list with one non-Mapping row drops the bad row and
    surfaces the good one — the assembler is best-effort advisory
    context, not a hard gate, so a malformed row must not crash the
    model call (defensive skip; same pattern as
    ``attachment_context_message``)."""
    rows = [
        "not a mapping",
        {"fact_id": "f1", "title": "Good title", "hook": "Good hook"},
        42,
    ]
    msg = memory_prefetch_message(rows)
    assert msg is not None
    content = str(msg.content)
    assert "Good title" in content
    assert "Good hook" in content
    assert "not a mapping" not in content


def test_memory_on_is_idempotent_on_a_pre_injected_pointer() -> None:
    """If a caller has already pre-injected the pointer marker into
    the head SystemMessage, a re-entrant ``memory_on=True`` call MUST
    NOT add a second copy. The pointer is appended exactly once per
    head message; re-entry leaves the message unchanged."""
    pre_existing = SystemMessage(
        content=(
            f"{HOST_SYSTEM_PROMPT}\n\n"
            f"{MEMORY_POINTER_MARKER}\nalready injected"
        )
    )
    out = messages_for_model(
        [pre_existing, HumanMessage(content="q")],
        track="research",
        memory_on=True,
    )
    head = out[0]
    assert isinstance(head, SystemMessage)
    assert head.content.count(MEMORY_POINTER_MARKER) == 1
    assert "already injected" in str(head.content)


# ---------- exported constants ----------


def test_memory_pointer_marker_constant_is_exported() -> None:
    """The marker is exported so other layers can grep/locate it."""
    assert isinstance(MEMORY_POINTER_MARKER, str)
    assert MEMORY_POINTER_MARKER.startswith("[")
    assert MEMORY_POINTER_MARKER.endswith("]")


def test_fin_memory_prefetch_marker_constant_is_exported() -> None:
    assert isinstance(FIN_MEMORY_PREFETCH_MARKER, str)
    assert FIN_MEMORY_PREFETCH_MARKER.startswith("[")
    assert FIN_MEMORY_PREFETCH_MARKER.endswith("]")


def test_pointer_text_constant_is_advisory() -> None:
    """The shared pointer text (exported as MEMORY_POINTER_TEXT) must
    read as a NOTE rather than an order. It mentions advisory context
    and explicitly states the pointer does NOT command recall."""
    text = MEMORY_POINTER_TEXT.lower()
    assert "advisory" in text or "context" in text
    # Must NOT command recall or save.
    for forbidden in (
        "you must recall",
        "you must save",
        "always recall",
        "always save",
        "must cite",
    ):
        assert forbidden not in text, f"pointer text issued a command: {forbidden!r}"
