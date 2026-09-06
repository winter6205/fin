"""Call-scoped host instructions for Soft-Track model leaves.

Per-track system prompts are prepended at model call time and are
NOT written back to the checkpoint, so follow-ups do not stack
copies and GET history never dumps them as a turn.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

from fin.host.track_decision import Track

HOST_INSTRUCTIONS_MARKER: str = "[FIN_HOST_INSTRUCTIONS]"
THREAD_ATTACHMENTS_MARKER: str = "[FIN_THREAD_ATTACHMENTS]"
DIALOGUE_INSTRUCTIONS_MARKER: str = "[FIN_DIALOGUE_INSTRUCTIONS]"
OFF_TOPIC_INSTRUCTIONS_MARKER: str = "[FIN_OFF_TOPIC_INSTRUCTIONS]"
CLARIFY_INSTRUCTIONS_MARKER: str = "[FIN_CLARIFY_INSTRUCTIONS]"

# ----- user profile (spec SC13, cross-session-operator-memory) -----
# The user profile is operator-profile machinery, NOT memory-fact
# machinery (operator decision on PR #240): its READ path (merged
# profile text into the system prefix) must work regardless of
# ``FIN_MEMORY_ENABLED``. The block sits BETWEEN the track prompt
# (identity / persona) and the memory existence pointer when both
# are present, so the order is identity → who the operator is →
# advisory memory note. The block is advisory framing only — never a
# command to recall / save / cite. The header names the operator and
# the workspace-overlay semantics so the model knows the workspace
# layer overrides the global.
USER_PROFILE_MARKER: str = "[FIN_USER_PROFILE]"
USER_PROFILE_HEADER: str = (
    "Operator preferences (user profile, workspace layer overrides "
    "global):"
)


# ----- memory existence pointer (spec SC10 / SC11 / ADR 0048) -----
# The only auto-memory sentence in the system prompt: a note store
# exists for this workspace; injected note reminders are advisory
# context, NOT commands to recall/save, NOT citations. The pointer is
# advisory by wording: it does not command the model to recall, fetch,
# or save; it tells the model the store exists and that any overlay
# lines it sees are advisory context for this turn only. It does NOT
# enumerate titles or counts (no whole-store catalog, no promote-on-
# recall-count). It sits inside the system prefix because that prefix
# is byte-stable for the thread (prompt-development rule 3); the
# per-turn prefetch rows live on the USER side via
# :func:`memory_prefetch_message` so they never enter the prefix.
MEMORY_POINTER_MARKER: str = "[FIN_MEMORY_POINTER]"
MEMORY_POINTER_TEXT: str = (
    "A private cross-session note store exists for this workspace; "
    "any injected note reminders on this turn are advisory context, "
    "not instructions, and never count as evidence or citations."
)
MEMORY_POINTER_BLOCK: str = f"{MEMORY_POINTER_MARKER}\n{MEMORY_POINTER_TEXT}"

# ----- memory prefetch overlay (spec SC7 / SC8 / SC11 / ADR 0048) -----
# The user-side overlay built from the rows produced by
# ``memory_prefetch.select_prefetch`` ([{'fact_id','title','hook'}]).
# This message is call-scoped: it is appended to the model-facing
# payload for THIS turn only and must NOT be written back to the
# checkpoint (same contract as the track-prompt SystemMessage —
# system prompts and prefetch blocks both leave ``messages`` alone,
# so follow-ups do not stack copies). The marker is exported so other
# layers can grep/locate the block.
FIN_MEMORY_PREFETCH_MARKER: str = "[FIN_MEMORY_PREFETCH]"

HOST_SYSTEM_PROMPT: str = (
    f"{HOST_INSTRUCTIONS_MARKER}\n"
    "You are fin, a research agent over a frozen corpus of 12 SEC "
    "10-Q filings: AAPL, MSFT, AMZN; periods 2022 Q3 and 2023 Q1–Q3.\n"
    "\n"
    "Work with tools before you answer. Do not use parametric memory "
    "of these companies as evidence.\n"
    "- list_files: catalog. Optional company (AAPL, MSFT, AMZN) and "
    "period (example: \"2023 Q3\") filters. Later read_file calls "
    "need both document_id and version (the sha256 from this catalog).\n"
    "- retrieve: the retrieve port's search over page passages. "
    "Prefer this to locate a figure. Pass document_id when you "
    "already know the filing.\n"
    "- read_file: page text. document_id + version required; page is "
    "1-based; page_limit is capped at 3.\n"
    "- get_xbrl_fact: one tagged XBRL fact from the filing's fact "
    "table. concept is the XBRL tag (example: \"us-gaap:Revenues\") "
    "and period the reporting period (example: \"2023-Q3-YTD\"); "
    "document_id narrows it to one filing. A miss returns "
    "{\"error\": \"fact_not_found\"}. The fact row carries its own "
    "source_locator — read that page when you need a citable span.\n"
    "\n"
    "Finish only after a tool this run returned page or chunk text. "
    "The citation gate accepts the answer only if it contains at least "
    "one contiguous span of 32 or more characters copied VERBATIM from "
    "that text. Copy a sentence, table row, or caption exactly; do not "
    "paraphrase the span. Do not invent document_id values. After you "
    "have evidence, write ordinary prose that includes those copied "
    "spans, then stop calling tools."
)

DIALOGUE_SYSTEM_PROMPT: str = (
    f"{DIALOGUE_INSTRUCTIONS_MARKER}\n"
    "You are fin, a conversational research assistant for a frozen "
    "SEC 10-Q corpus (AAPL, MSFT, AMZN; 2022 Q3 and 2023 Q1–Q3).\n"
    "Respond warmly and briefly to greetings and social small-talk. "
    "Do not invent filing facts. Do not claim you retrieved documents. "
    "Invite the operator to ask about a company, period, or metric "
    "when appropriate."
)

OFF_TOPIC_SYSTEM_PROMPT: str = (
    f"{OFF_TOPIC_INSTRUCTIONS_MARKER}\n"
    "You are fin. Your capability is SEC 10-Q research over a frozen "
    "12-filing corpus (AAPL, MSFT, AMZN; 2022 Q3 and 2023 Q1–Q3).\n"
    "The operator's ask is outside that scope. Reply with a soft, "
    "polite capability-bound refusal in ordinary prose. Do not invent "
    "filing facts. Do not use a fixed redirect string as the only "
    "reply — write a natural soft decline and briefly name what you "
    "can help with instead."
)

CLARIFY_SYSTEM_PROMPT: str = (
    f"{CLARIFY_INSTRUCTIONS_MARKER}\n"
    "You are fin. The operator asked about a 10-Q metric or filing "
    "but company and/or period is missing.\n"
    "Ask briefly which slots are still needed. Ground options ONLY in "
    "the frozen corpus: companies AAPL, MSFT, AMZN; periods 2022 Q3, "
    "2023 Q1, 2023 Q2, 2023 Q3. Do not invent tickers or periods "
    "outside that set. Do not invent filing facts. Do not claim you "
    "retrieved documents."
)

# Operator-facing ask per slot the clarify track can be missing.
# A slot absent from this map is never named in the prompt: the
# corpus only has company and period as routing slots, and an
# unknown label would put a term we cannot ground in front of the
# operator.
_CLARIFY_SLOT_ASKS: dict[str, str] = {
    "company": "which company (AAPL, MSFT, or AMZN)",
    "period": "which period (2022 Q3, 2023 Q1, 2023 Q2, or 2023 Q3)",
}


def clarify_prompt(missing_slots: Sequence[str] | None = None) -> str:
    """Return the clarify prompt naming THIS turn's missing slots.

    ``missing_slots`` comes from the turn's track decision or the
    latest status snapshot. With no recognized slot the static
    ``CLARIFY_SYSTEM_PROMPT`` stands — a snapshot's dependent
    fields may legitimately be empty (CONTEXT *status snapshot*),
    and a guess about which slot is missing is worse than the
    generic ask.
    """
    named = [
        slot
        for slot in dict.fromkeys(missing_slots or ())
        if slot in _CLARIFY_SLOT_ASKS
    ]
    if not named:
        return CLARIFY_SYSTEM_PROMPT
    asks = " and ".join(_CLARIFY_SLOT_ASKS[slot] for slot in named)
    return (
        f"{CLARIFY_SYSTEM_PROMPT}\n"
        f"Still missing on this turn: {', '.join(named)}. Ask for "
        f"{asks} — and do not re-ask for anything the operator "
        "already gave."
    )


def attachment_context_message(
    attachments: Sequence[dict[str, Any]] | None,
) -> SystemMessage | None:
    """Return the call-scoped attachment block, or ``None`` for none.

    corpus-serve-ingest T4 (ADR 0047 **thread attach**): the operator
    may drag a file into the open research thread; the model may read
    it on THIS turn only. Like the track prompts, the returned
    ``SystemMessage`` is prepended at model call time and must never
    be written back to the checkpoint — the caller drops it after the
    call, and ``reset_input_state`` clears the ``attachments`` state
    channel on the next run, so the text disappears next turn.

    Citation discipline (ADR 0046): the block says explicitly that
    attachments are NOT citable corpus. The gate's real protection is
    structural, not prompt-level — attachment text never enters the
    ``evidence`` channel, so an attachment-only span cannot match.
    """
    if not attachments:
        return None
    sections = []
    for attachment in attachments:
        filename = str(attachment.get("filename") or "attachment")
        text = str(attachment.get("text") or "")
        sections.append(f"--- attached file: {filename} ---\n{text}")
    joined = "\n\n".join(sections)
    return SystemMessage(
        content=(
            f"{THREAD_ATTACHMENTS_MARKER}\n"
            "The operator attached the following file(s) to THIS turn. "
            "They are readable context for this turn only and vanish "
            "on the next turn. They are NOT part of the citable "
            "corpus: the citation gate only accepts spans copied from "
            "this run's retrieve / read_file tool text, so never "
            "present attachment content as retrieved evidence.\n\n"
            f"{joined}"
        )
    )


_TRACK_PROMPTS: dict[str, tuple[str, str]] = {
    "research": (HOST_INSTRUCTIONS_MARKER, HOST_SYSTEM_PROMPT),
    "dialogue": (DIALOGUE_INSTRUCTIONS_MARKER, DIALOGUE_SYSTEM_PROMPT),
    "off_topic": (OFF_TOPIC_INSTRUCTIONS_MARKER, OFF_TOPIC_SYSTEM_PROMPT),
    "clarify": (CLARIFY_INSTRUCTIONS_MARKER, CLARIFY_SYSTEM_PROMPT),
}


def messages_for_model(
    messages: list[BaseMessage],
    track: Track | Literal["research"] = "dialogue",
    *,
    missing_slots: Sequence[str] | None = None,
    memory_on: bool | None = False,
    user_profile: str | None = None,
) -> list[BaseMessage]:
    """Return the model-facing list with the track prompt first.

    Default is ``"dialogue"`` — there is no single global default
    track (specs/soft-track.md); a call here is call-scoped and
    must never pin the host onto a single branch by virtue of
    missing input. Call-scoped only: do not write the
    SystemMessage into checkpointed ``messages``.

    ``missing_slots`` applies to the clarify track only: it names
    the slots this turn still needs so the ask is specific rather
    than static.

    ``memory_on`` (spec SC10 / SC11 / ADR 0048, T5): when truthy, a
    single **memory existence pointer** sentence is appended to the
    SystemMessage after the track prompt. The pointer is advisory
    text (a note that a private store exists for this workspace,
    that any injected overlays are advisory context, not commands,
    and never citations) and never enumerates fact titles or
    counts. The OFF path is byte-identical to today's output —
    callers that do not pass ``memory_on`` (or pass ``False`` /
    ``None``) keep their golden prefix unchanged (existing tests
    stay green). The pointer is independent of any per-turn
    prefetch payload: prefetch rows live on the USER side via
    :func:`memory_prefetch_message`, never in the system prefix.

    ``user_profile`` (spec SC13, PR #240 operator decision): the
    raw merged text from :meth:`UserProfileStore.read_merged`. The
    user profile is operator-profile machinery, NOT memory-fact
    machinery — its READ path is INDEPENDENT of ``memory_on`` and
    surfaces into the system prefix regardless of whether memory
    is enabled. The block sits between the track prompt and the
    memory existence pointer when both are present (ordering:
    identity → who the operator is → advisory memory note). The
    block is advisory framing only — never a command. ``None``,
    empty, or whitespace-only text yields no profile block; the
    SystemMessage bytes are then byte-identical to the no-profile
    call so callers that have no profile files on disk keep their
    prefix stable.
    """
    marker, prompt = _TRACK_PROMPTS.get(
        track, (DIALOGUE_INSTRUCTIONS_MARKER, DIALOGUE_SYSTEM_PROMPT)
    )
    if track == "clarify":
        prompt = clarify_prompt(missing_slots)
    if (
        messages
        and isinstance(messages[0], SystemMessage)
        and marker in str(messages[0].content)
    ):
        existing = list(messages)
    else:
        existing = [SystemMessage(content=prompt), *messages]

    # Profile block: independent of memory_on. Inserted BETWEEN the
    # track prompt and the memory existence pointer when both are
    # present. ``None`` / empty / whitespace-only → no block
    # (byte-identical output for callers with no profile files).
    profile_text = user_profile.strip() if isinstance(user_profile, str) else ""
    if profile_text:
        existing = _inject_profile_block(existing, profile_text)

    if not memory_on:
        # OFF path: byte-identical to today's output for callers that
        # do not opt in. The track prompt + tail of messages is exactly
        # what messages_for_model produced before T5.
        return existing

    # ON path: append the existence pointer to the SystemMessage
    # exactly once. The pointer is appended AFTER the track prompt so
    # the track prompt stays the contract first; it is a NOTE about a
    # second channel, not a re-write of the track contract.
    head = existing[0]
    if not isinstance(head, SystemMessage):
        # Defensive: a non-SystemMessage head is not the expected shape,
        # but we must not crash a model call over a pointer that the
        # spec marks advisory. Skip silently rather than inject a
        # second SystemMessage (which would break call-scoped-only).
        return existing
    head_content = str(head.content)
    if MEMORY_POINTER_MARKER in head_content:
        # Pointer already present (caller pre-injected it, or this is
        # a re-entry): leave the message unchanged. Idempotent.
        return existing
    new_content = f"{head_content}\n\n{MEMORY_POINTER_BLOCK}"
    rebuilt = list(existing)
    rebuilt[0] = SystemMessage(content=new_content)
    return rebuilt


def _inject_profile_block(
    messages: list[BaseMessage], profile_text: str
) -> list[BaseMessage]:
    """Insert the user-profile block into the head SystemMessage.

    Idempotent: if the head already carries the profile marker
    (re-entrant call, caller pre-injected), the block is left
    untouched. The block is built fresh from ``profile_text`` so a
    file edit between calls is visible on the next ``messages_for_model``
    call (no caching at this seam). Whitespace-only input is the
    caller's responsibility — this helper assumes a non-empty
    ``profile_text`` because the caller filters ``None`` / ``""`` /
    whitespace-only strings before invoking.
    """
    if not messages:
        return [SystemMessage(content=_build_profile_block(profile_text))]
    head = messages[0]
    if not isinstance(head, SystemMessage):
        # Defensive: non-SystemMessage head (unexpected shape). Drop
        # a fresh SystemMessage in front so the model still sees the
        # block; never crash a model call over an advisory block.
        return [
            SystemMessage(content=_build_profile_block(profile_text)),
            *messages,
        ]
    head_content = str(head.content)
    if USER_PROFILE_MARKER in head_content:
        # Block already present (re-entry): leave the message unchanged
        # so a second call with the same text doesn't shift bytes.
        return messages
    new_content = (
        f"{head_content}\n\n{_build_profile_block(profile_text)}"
    )
    rebuilt = list(messages)
    rebuilt[0] = SystemMessage(content=new_content)
    return rebuilt


def _build_profile_block(profile_text: str) -> str:
    """Render the profile block as a marker-prefixed string.

    One advisory header line naming the workspace-overlay semantics,
    then the verbatim file content. Header wording follows
    ``docs/guides/prompt-development.md``: framing only, never a
    command to the model.
    """
    return f"{USER_PROFILE_MARKER}\n{USER_PROFILE_HEADER}\n{profile_text}"


def memory_prefetch_message(
    prefetch_rows: Sequence[Mapping[str, Any]] | None,
) -> HumanMessage | None:
    """Render the per-turn prefetch rows as a USER-side HumanMessage.

    ``prefetch_rows`` is the list produced by
    :func:`fin.host.memory_prefetch.select_prefetch` — each row is a
    mapping carrying ``fact_id``, ``title``, ``hook``. The block
    surfaces ``title + hook`` per row and is prefixed with the
    :data:`FIN_MEMORY_PREFETCH_MARKER` so other layers can locate
    it. ``body`` MUST NOT flow into the block (the prefetch payload
    is title+hook only — ``body`` bytes are accounted by the
    occupancy ledger but never reach the model).

    Call-scoped only: like the system-prompt injection, this message
    is appended to the model-facing payload for THIS turn only and
    must NEVER be written back to the checkpoint. The caller drops
    it after the call.

    Returns ``None`` when there are no rows (zero lexical hits
    surface nothing, per spec SC7). The caller can then skip the
    append entirely — adding an empty marker block would change the
    token count and break the "zero hits inject nothing" rule.
    """
    if not prefetch_rows:
        return None

    lines: list[str] = []
    for row in prefetch_rows:
        if not isinstance(row, Mapping):
            # Skip a malformed row rather than crashing the model call;
            # the assembler contract is "best-effort advisory context",
            # not a hard gate (the gate on prefetch content lives in
            # the citation verifier, which never sees this block).
            continue
        fact_id = str(row.get("fact_id", "")).strip()
        title = str(row.get("title", "")).strip()
        hook = str(row.get("hook", "")).strip()
        if not title and not hook:
            continue
        # Single readable line per row: "<title> — <hook>" with the
        # fact_id as a parenthetical anchor so a downstream tool call
        # can resolve to the source row. ``body`` is intentionally
        # NEVER read here — see docstring.
        head = f"- {title}" if title else "- (untitled)"
        if hook:
            head = f"{head} — {hook}"
        if fact_id:
            head = f"{head} ({fact_id})"
        lines.append(head)

    if not lines:
        return None

    body = "\n".join(lines)
    content = (
        f"{FIN_MEMORY_PREFETCH_MARKER}\n"
        "The following notes from your private workspace memory "
        "matched this turn lexically. They are advisory context "
        "for this turn only and never count as evidence or "
        "citations.\n"
        f"{body}"
    )
    return HumanMessage(content=content)


# Architecture alias used by Soft-Track leaves.
messages_for_track = messages_for_model


__all__ = [
    "CLARIFY_INSTRUCTIONS_MARKER",
    "CLARIFY_SYSTEM_PROMPT",
    "DIALOGUE_INSTRUCTIONS_MARKER",
    "DIALOGUE_SYSTEM_PROMPT",
    "FIN_MEMORY_PREFETCH_MARKER",
    "HOST_INSTRUCTIONS_MARKER",
    "HOST_SYSTEM_PROMPT",
    "MEMORY_POINTER_BLOCK",
    "MEMORY_POINTER_MARKER",
    "MEMORY_POINTER_TEXT",
    "OFF_TOPIC_INSTRUCTIONS_MARKER",
    "OFF_TOPIC_SYSTEM_PROMPT",
    "THREAD_ATTACHMENTS_MARKER",
    "USER_PROFILE_HEADER",
    "USER_PROFILE_MARKER",
    "attachment_context_message",
    "clarify_prompt",
    "memory_prefetch_message",
    "messages_for_model",
    "messages_for_track",
]
