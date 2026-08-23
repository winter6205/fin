"""Call-scoped host instructions for the agent loop.

The compiled graph historically forwarded ``state["messages"]``
straight to the model: HumanMessage + tool results + the gate's
hidden bounce SystemMessage. MiniMax then answers from parametric
memory, the citation gate bounces three times, and the workspace
run refuses. These instructions are prepended at ``model_node``
call time and are NOT written back to the checkpoint, so follow-ups
do not stack copies and GET history never dumps them as a turn.
"""

from __future__ import annotations

from langchain_core.messages import BaseMessage, SystemMessage

HOST_INSTRUCTIONS_MARKER: str = "[FIN_HOST_INSTRUCTIONS]"

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


def messages_for_model(messages: list[BaseMessage]) -> list[BaseMessage]:
    """Return the model-facing list with host instructions first.

    If the checkpoint already starts with this SystemMessage (should
    not happen on the call-scoped path), do not prepend a second copy.
    """
    if (
        messages
        and isinstance(messages[0], SystemMessage)
        and HOST_INSTRUCTIONS_MARKER in str(messages[0].content)
    ):
        return list(messages)
    return [SystemMessage(content=HOST_SYSTEM_PROMPT), *messages]


__all__ = [
    "HOST_INSTRUCTIONS_MARKER",
    "HOST_SYSTEM_PROMPT",
    "messages_for_model",
]
