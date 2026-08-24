"""Call-scoped host instructions for Soft-Track model leaves.

Per-track system prompts are prepended at model call time and are
NOT written back to the checkpoint, so follow-ups do not stack
copies and GET history never dumps them as a turn.
"""

from __future__ import annotations

from typing import Literal

from langchain_core.messages import BaseMessage, SystemMessage

from fin.host.track_decision import Track

HOST_INSTRUCTIONS_MARKER: str = "[FIN_HOST_INSTRUCTIONS]"
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
    "- retrieve: BM25 over page passages. Prefer this to locate a "
    "figure. Pass document_id when you already know the filing.\n"
    "- read_file: page text. document_id + version required; page is "
    "1-based; page_limit is capped at 3.\n"
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

_TRACK_PROMPTS: dict[str, tuple[str, str]] = {
    "research": (HOST_INSTRUCTIONS_MARKER, HOST_SYSTEM_PROMPT),
    "dialogue": (DIALOGUE_INSTRUCTIONS_MARKER, DIALOGUE_SYSTEM_PROMPT),
    "off_topic": (OFF_TOPIC_INSTRUCTIONS_MARKER, OFF_TOPIC_SYSTEM_PROMPT),
    "clarify": (CLARIFY_INSTRUCTIONS_MARKER, CLARIFY_SYSTEM_PROMPT),
}


def messages_for_model(
    messages: list[BaseMessage],
    track: Track | Literal["research"] = "research",
) -> list[BaseMessage]:
    """Return the model-facing list with the track prompt first.

    Research is the default so existing callers keep the citation /
    tool contract. Call-scoped only: do not write the SystemMessage
    into checkpointed ``messages``.
    """
    marker, prompt = _TRACK_PROMPTS.get(
        track, (HOST_INSTRUCTIONS_MARKER, HOST_SYSTEM_PROMPT)
    )
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
    "messages_for_model",
    "messages_for_track",
]
