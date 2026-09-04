"""Call-scoped host instructions for Soft-Track model leaves.

Per-track system prompts are prepended at model call time and are
NOT written back to the checkpoint, so follow-ups do not stack
copies and GET history never dumps them as a turn.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal

from langchain_core.messages import BaseMessage, SystemMessage

from fin.host.track_decision import Track

HOST_INSTRUCTIONS_MARKER: str = "[FIN_HOST_INSTRUCTIONS]"
THREAD_ATTACHMENTS_MARKER: str = "[FIN_THREAD_ATTACHMENTS]"
DIALOGUE_INSTRUCTIONS_MARKER: str = "[FIN_DIALOGUE_INSTRUCTIONS]"
OFF_TOPIC_INSTRUCTIONS_MARKER: str = "[FIN_OFF_TOPIC_INSTRUCTIONS]"
CLARIFY_INSTRUCTIONS_MARKER: str = "[FIN_CLARIFY_INSTRUCTIONS]"

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
        return list(messages)
    return [SystemMessage(content=prompt), *messages]


# Architecture alias used by Soft-Track leaves.
messages_for_track = messages_for_model


__all__ = [
    "CLARIFY_INSTRUCTIONS_MARKER",
    "CLARIFY_SYSTEM_PROMPT",
    "DIALOGUE_INSTRUCTIONS_MARKER",
    "DIALOGUE_SYSTEM_PROMPT",
    "HOST_INSTRUCTIONS_MARKER",
    "HOST_SYSTEM_PROMPT",
    "OFF_TOPIC_INSTRUCTIONS_MARKER",
    "OFF_TOPIC_SYSTEM_PROMPT",
    "THREAD_ATTACHMENTS_MARKER",
    "attachment_context_message",
    "clarify_prompt",
    "messages_for_model",
    "messages_for_track",
]
