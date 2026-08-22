"""Streaming primitives shared by the host graph and the SSE surface.

T3 introduces token-by-token streaming for the workspace send path
(spec line 27 + ADR 0026). The host graph still produces ONE complete
``AIMessage`` per ``model_node`` entry (spec line 37 - a checkpoint
must contain a complete ``AIMessage`` with full ``tool_calls`` so T4
can resume from this node without regenerating it). Token deltas are
a side effect observed during stream consumption, never the durable
state.

This module owns the pure functions that translate streaming deltas
into the shapes downstream code renders:

* :func:`assemble_final_ai_message` - concatenate a sequence of
  ``AIMessageChunk`` objects into one ``AIMessage``. Preserves
  ``tool_calls`` (joined from ``tool_call_chunks``) and
  ``additional_kwargs.reasoning_details`` so the next model call
  sees the prior turn's reasoning.
* :func:`strip_thinking_from_answer` - remove any
  ``<thinking>...</thinking>`` block the proxy put directly into the
  answer prose so the citation gate and the user-visible
  ``answer`` event never see the model's private scratchpad
  (SC60 binding).
* :func:`summarize_list_files` / :func:`summarize_retrieve` /
  :func:`summarize_read_file` / :func:`format_tool_summary` - the
  human-readable one-liners the SPA renders in the tools region
  (spec line 32). Raw ``ToolMessage`` JSON never reaches the wire;
  events carry name + short summary, correlated by tool call id.

Why one module
--------------
The chunk assembly, the thinking strip, and the tool-summary
formatters share one wire contract and the same callers
(``graph.model_node`` builds summaries from ``ToolMessage``s,
``workspace_http.send_message`` maps chunks to SSE frames,
``scripted_model`` reuses the assembly primitive). Keeping them in
one module keeps the contract single-sourced (Standards #14) and
matches the spec reviewer's note that "stream mapping, thinking
converter, turn persistence, and graph nodes stay separate modules"
- this is THE stream-mapping module.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from langchain_core.messages import AIMessage, AIMessageChunk


# ----- chunk assembly -----


def assemble_final_ai_message(
    chunks: Iterable[AIMessageChunk],
) -> AIMessage:
    """Concatenate ``AIMessageChunk``s into ONE complete ``AIMessage``.

    LangChain's chunk add (``chunk1 + chunk2``) concatenates string
    ``content``, joins ``tool_call_chunks`` into a single
    ``tool_calls`` list, and merges ``additional_kwargs`` (concat for
    list values - which is how ``reasoning_details`` round-trips:
    each streamed thinking delta arrives as one entry appended by
    the merge).

    Why an explicit accumulator (vs. ``sum(chunks)``): ``sum`` starts
    from ``0`` and ``int + AIMessageChunk`` raises; the explicit loop
    keeps the empty-iterable branch readable. An empty chunk sequence
    returns an empty ``AIMessage`` so a misconfigured caller fails at
    a readable point instead of ``None`` dereferencing deep in
    LangGraph.

    Returns an ``AIMessage`` (not a chunk) because the checkpointer
    persists message objects with stable ids; chunks carry ephemeral
    run-scoped ids that ``add_messages`` would treat as separate
    messages on every replay.
    """
    accumulator: AIMessageChunk | None = None
    for chunk in chunks:
        if accumulator is None:
            accumulator = chunk
            continue
        accumulator = accumulator + chunk
    if accumulator is None:
        return AIMessage(content="")
    # ``AIMessageChunk`` has no ``to_message`` in langchain-core 1.6;
    # the conversion is an explicit field copy. ``tool_calls`` (the
    # parsed view over ``tool_call_chunks``) and ``additional_kwargs``
    # (which carries ``reasoning_details`` for replay) are the two
    # fields downstream code reads - both are copied by value so the
    # checkpoint holds its own instance.
    return AIMessage(
        content=accumulator.content,
        tool_calls=list(accumulator.tool_calls),
        additional_kwargs=dict(accumulator.additional_kwargs),
    )


# ----- thinking strip -----


# Match a ``<thinking>...</thinking>`` block interleaved into the
# answer prose. Non-greedy + DOTALL so multi-line blocks collapse in
# one pass; case-insensitive because proxy implementations disagree
# on casing. SC60 binds: thinking prose must not pass the citation
# gate as the answer, and must never appear inside ``answer`` events.
_THINKING_BLOCK_RE: re.Pattern[str] = re.compile(
    r"<thinking\b[^>]*>.*?</thinking\s*>",
    re.IGNORECASE | re.DOTALL,
)


def strip_thinking_from_answer(answer: str) -> str:
    """Remove ``<thinking>...</thinking>`` blocks from ``answer``.

    The MiniMax proxy (ADR 0022, ``reasoning_split``) normally routes
    thinking through a dedicated field
    (``additional_kwargs.reasoning_details``); in that shape the
    citation gate never sees thinking because ``_last_ai_text`` reads
    only ``content``. When the proxy falls back to inline blocks,
    this helper strips them so the gate and the wire answer agree on
    thinking-stripped prose.

    Pure function: returns the input unchanged (same object semantics
    as ``str`` immutability allows) when no block is present, so the
    common no-thinking path costs one regex scan.
    """
    if not answer:
        return answer
    stripped = _THINKING_BLOCK_RE.sub("", answer)
    # Fold the whitespace seams a removed block leaves behind so the
    # citation gate's contiguous-substring match does not see a
    # doubled newline where the block used to live. Leading/trailing
    # whitespace belongs to the model's real answer and stays.
    stripped = re.sub(r"[ \t]+\n", "\n", stripped)
    stripped = re.sub(r"\n[ \t]+", "\n", stripped)
    stripped = re.sub(r"\n{3,}", "\n\n", stripped)
    return stripped


# ----- tool summary formatters (spec line 32) -----


def summarize_list_files(
    *,
    filings: list[Any] | None = None,
    args: dict[str, Any] | None = None,
) -> str:
    """Human-readable one-liner for ``list_files`` calls.

    Spec line 32: ``list_files`` -> ``"n filings [+ filters]"``.

    ``filings`` is the tool's return value (the catalog rows); we
    render the count only - filing ids belong to the sources list,
    not the tools region. Optional ``company`` / ``period`` filters
    from the model's call arguments render as ``[key=value]`` so the
    operator can see why the tool ran.
    """
    count = len(filings) if filings else 0
    noun = "filing" if count == 1 else "filings"
    suffix = ""
    if args:
        parts = [
            f"{key}={args[key]}"
            for key in ("company", "period")
            if args.get(key)
        ]
        if parts:
            suffix = " [" + ", ".join(parts) + "]"
    return f"{count} {noun}{suffix}"


def summarize_retrieve(
    *,
    chunks: list[Any] | None = None,
    args: dict[str, Any] | None = None,
) -> str:
    """Human-readable one-liner for ``retrieve`` calls.

    Spec line 32: ``retrieve`` -> ``query + hit count + locators``.

    ``chunks`` carries ``source_ref`` locators (objects or dicts -
    both shapes occur: the live adapter returns ``Locator`` objects,
    the serialized ToolMessage JSON carries dicts). We render the
    query, the hit count, and up to three locators so the operator
    can scan which pages were touched without reading raw JSON.
    """
    hits = len(chunks) if chunks else 0
    args = args or {}
    query_value = args.get("query")
    query = query_value if isinstance(query_value, str) else ""

    def _locator_fields(chunk: Any) -> tuple[str, int] | None:
        ref = getattr(chunk, "source_ref", None)
        if ref is None and isinstance(chunk, dict):
            ref = chunk.get("source_ref")
        if ref is None:
            return None
        doc_id = getattr(ref, "document_id", None)
        page = getattr(ref, "page", None)
        if doc_id is None and isinstance(ref, dict):
            doc_id = ref.get("document_id")
            page = ref.get("page")
        if doc_id is None or page is None:
            return None
        return str(doc_id), int(page)

    locators: list[str] = []
    if chunks:
        for chunk in chunks[:3]:
            fields = _locator_fields(chunk)
            if fields is not None:
                locators.append(f"{fields[0]} p.{fields[1]}")

    doc_filter_value = args.get("document_id")
    doc_filter = (
        f" ({doc_filter_value})"
        if isinstance(doc_filter_value, str) and doc_filter_value
        else ""
    )
    locator_suffix = " -> " + ", ".join(locators) if locators else ""
    hits_noun = "hit" if hits == 1 else "hits"
    head = f"{query!r}: {hits} {hits_noun}" if query else f"{hits} {hits_noun}"
    return f"{head}{doc_filter}{locator_suffix}"


def summarize_read_file(
    *,
    result: dict[str, Any] | None = None,
    args: dict[str, Any] | None = None,
) -> str:
    """Human-readable one-liner for ``read_file`` calls.

    Spec line 32: ``read_file`` -> ``document_id p.N`` plus the
    page range when ``page_limit > 1``. A miss
    (``{"error": "unknown_locator"}``) appends the error token so
    the operator can see why no evidence landed.
    """
    src_args = args or {}
    src_result = result or {}
    doc_id = (
        src_args.get("document_id") or src_result.get("document_id") or ""
    )
    page_raw = src_args.get("page") or src_result.get("page") or 1
    limit_raw = src_args.get("page_limit") or src_result.get("page_limit") or 1

    def _as_int(value: Any) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    page = _as_int(page_raw) or 1
    page_limit = _as_int(limit_raw) or 1
    head = f"{doc_id} p.{page}"
    if page_limit > 1:
        head += f"-{page + page_limit - 1}"
    if isinstance(src_result, dict) and src_result.get("error"):
        head += f" ({src_result['error']})"
    return head


def format_tool_summary(
    name: str,
    *,
    result: dict[str, Any] | None = None,
    args: dict[str, Any] | None = None,
) -> str:
    """Dispatch to the correct ``summarize_*`` based on tool ``name``.

    Exhaustive over the host graph's three tools (ADR 0009). An
    unknown name falls back to the bare tool name so a future tool
    still surfaces a readable row in the tools region instead of an
    empty cell - never raw JSON.
    """
    result_dict = result if isinstance(result, dict) else None
    if name == "list_files":
        filings = None
        if result_dict is not None and isinstance(result_dict.get("filings"), list):
            filings = result_dict["filings"]
        return summarize_list_files(filings=filings, args=args)
    if name == "retrieve":
        chunks = None
        if result_dict is not None and isinstance(result_dict.get("chunks"), list):
            chunks = result_dict["chunks"]
        return summarize_retrieve(chunks=chunks, args=args)
    if name == "read_file":
        return summarize_read_file(result=result_dict, args=args)
    return name or "tool"


__all__ = [
    "assemble_final_ai_message",
    "format_tool_summary",
    "strip_thinking_from_answer",
    "summarize_list_files",
    "summarize_read_file",
    "summarize_retrieve",
]
