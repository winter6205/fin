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
* :func:`reasoning_delta_from_chunk` /
  :func:`split_stream_delta` - the T1 (#33) token-delta mapping:
  the first pulls a thinking delta off a chunk's reasoning
  channel; the second classifies streamed content deltas into
  thinking vs answer with an inline-block filter that survives
  tags split across chunk boundaries.
* :func:`reasoning_entries_from_delta` /
  :func:`reasoning_entries_from_raw_chunk` /
  :func:`reasoning_preserving_chat_openai` - the thinking
  transport converter (spec line 28). Providers spell the split
  channel three ways (``reasoning_details`` / ``reasoning_content``
  / ``reasoning``) and ``langchain-openai`` targets the official
  OpenAI spec only, so it drops all three before the chunk reaches
  us. The first two normalise whichever shape arrived onto the
  ``reasoning_details`` entry shape the rest of the host reads; the
  third is the thin ``ChatOpenAI`` subclass that re-attaches the
  channel during streaming and, via
  :func:`replay_reasoning_into_payload`, puts the checkpointed
  entries back into the next request so the provider's chain of
  thought survives the turn. When the channel is absent entirely,
  the inline ``<think>`` strip above stays the fallback - thinking
  is surfaced or stripped, never swallowed into the answer.
* :func:`summarize_list_files` / :func:`summarize_retrieve` /
  :func:`summarize_read_file` / :func:`summarize_get_xbrl_fact` /
  :func:`format_tool_summary` - the
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
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage

from fin.host.degradation_notice import DEGRADATION_NOTICE_FIELD
from fin.host.tools.issuer_fanout import ISSUER_FANOUT_TOOL
from fin.retrieve.degradation import degraded_reasons_for_display


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
    # The per-chunk reasoning entries BEFORE the merge flattens them.
    # ``_normalize_reasoning_details`` needs the originals to tell an
    # entry's identity metadata from the prose the deltas split.
    streamed_entries: list[dict[str, Any]] = []
    for chunk in chunks:
        streamed_entries.extend(
            reasoning_entries_from_delta(chunk.additional_kwargs)
        )
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
    additional_kwargs = dict(accumulator.additional_kwargs)
    _normalize_reasoning_details(additional_kwargs, streamed_entries)
    return AIMessage(
        content=accumulator.content,
        tool_calls=list(accumulator.tool_calls),
        additional_kwargs=additional_kwargs,
    )


def _repair_merged_reasoning_entries(
    merged: list[Any], streamed: list[dict[str, Any]]
) -> list[Any]:
    """Undo the chunk merge's concatenation of identity metadata.

    ``AIMessageChunk`` addition folds same-``index`` reasoning
    entries together with ``merge_dicts``, which concatenates EVERY
    string field it has no special case for. Only the prose is
    genuinely split across deltas; the other string fields are the
    entry's identity, repeated verbatim on every delta. Live
    MiniMax-M3 repeats ``format`` that way, so a 37-delta scratchpad
    checkpoints ``"MiniMax-response-v1"`` 37 times over - junk that
    grows with the length of the model's thinking and rides every
    subsequent checkpoint write.

    The first delta carrying an ``index`` is authoritative for that
    entry's metadata. An entry whose ``id`` disagrees with that
    delta's is left untouched rather than repaired from the wrong
    source: keeping a duplicated ``format`` is recoverable, writing
    another entry's identity is not.
    """
    first_by_index: dict[Any, Mapping[str, Any]] = {}
    for entry in streamed:
        index = entry.get("index")
        if index is not None and index not in first_by_index:
            first_by_index[index] = entry

    repaired: list[Any] = []
    for entry in merged:
        source = (
            first_by_index.get(entry.get("index"))
            if isinstance(entry, dict)
            else None
        )
        source_id = source.get("id") if source is not None else None
        entry_id = entry.get("id") if isinstance(entry, dict) else None
        other_entry = bool(source_id and entry_id and source_id != entry_id)
        if source is None or other_entry:
            repaired.append(entry)
            continue
        repaired.append(
            {
                key: (
                    source[key]
                    if (
                        key not in _REASONING_PROSE_FIELDS
                        and isinstance(value, str)
                        and isinstance(source.get(key), str)
                    )
                    else value
                )
                for key, value in entry.items()
            }
        )
    return repaired


def _normalize_reasoning_details(
    additional_kwargs: dict[str, Any],
    streamed_entries: list[dict[str, Any]],
) -> None:
    """Ensure a replayable ``reasoning_details`` list, in place.

    ``graph._extract_thinking_text`` and the next model call both
    read ``reasoning_details``. A provider that spells the split
    channel ``reasoning_content`` (or ``reasoning``) would otherwise
    stream thinking to the wire and then lose it at the checkpoint.

    An existing non-empty list keeps the provider's own entries -
    re-deriving it would drop replay fields they carry - but its
    identity metadata is repaired against ``streamed_entries``, the
    per-chunk originals from before the merge.
    """
    existing = additional_kwargs.get("reasoning_details")
    if isinstance(existing, list) and existing:
        additional_kwargs["reasoning_details"] = (
            _repair_merged_reasoning_entries(existing, streamed_entries)
        )
        return
    entries = reasoning_entries_from_delta(additional_kwargs)
    if entries:
        additional_kwargs["reasoning_details"] = entries


# ----- thinking strip -----


# Match a ``<thinking>...</thinking>`` block interleaved into the
# answer prose. Non-greedy + DOTALL so multi-line blocks collapse in
# one pass; case-insensitive because proxy implementations disagree
# on casing. SC60 binds: thinking prose must not pass the citation
# gate as the answer, and must never appear inside ``answer`` events.
# MiniMax-M3 inlines ``<think>...</think>`` when the split channel
# is missing; some proxies use ``<thinking>``. ``\b`` after ``think``
# keeps ``<thinking>`` from matching the short tag.
_THINKING_BLOCK_RE: re.Pattern[str] = re.compile(
    r"<(thinking|think)\b[^>]*>(.*?)</\1\s*>",
    re.IGNORECASE | re.DOTALL,
)


def inline_thinking_segments(content: str) -> list[str]:
    """Inner prose of every closed ``<think>`` / ``<thinking>`` block."""
    if not content:
        return []
    return [match.group(2) for match in _THINKING_BLOCK_RE.finditer(content)]


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

    Block removal is the ONLY edit. This function is the persisted /
    terminal-frame side of the same contract
    :func:`split_stream_delta` implements delta by delta, and the SPA
    keeps the streamed prose when the persisted status is ``done``
    (``mergeReconciledTurn``). Any extra rewrite here — the
    whitespace fold this used to apply over the whole answer —
    reaches only this side, so the reconcile silently repaints
    indentation and blank runs the operator already read.
    """
    if not answer:
        return answer
    return _THINKING_BLOCK_RE.sub("", answer)


# ----- token-delta mapping (T1, #33) -----


@dataclass(frozen=True)
class InlineFilterState:
    """Carry state for :func:`split_stream_delta`.

    ``buffer`` holds characters not yet classified because they may
    be part of a tag split across chunk boundaries (a suffix like
    ``"</thin"``); ``inside`` records whether the delta stream is
    currently between an opening and a closing ``<thinking>`` tag.
    Frozen dataclass so the route treats it as plain data threading
    through the stream loop — no hidden mutable state.
    """

    buffer: str = ""
    inside: bool = False


@dataclass(frozen=True)
class DeltaSplit:
    """Result of classifying one content delta.

    ``thinking`` / ``answer`` are the prose pieces to emit NOW
    (either may be empty — the caller skips the SSE frame); ``state``
    is the carry for the next delta.
    """

    state: InlineFilterState
    thinking: str
    answer: str


# Streaming-tag variants of the strip patterns above: searched one
# at a time inside the running buffer so a tag that spans chunk
# boundaries resolves on the delta that completes it.
_OPEN_TAG_RE: re.Pattern[str] = re.compile(
    r"<(thinking|think)\b[^>]*>", re.IGNORECASE
)
_CLOSE_TAG_RE: re.Pattern[str] = re.compile(
    r"</(thinking|think)\s*>", re.IGNORECASE
)
# The trailing fragment of a buffer that could still grow into one
# of the tags above once more deltas arrive. Two alternatives, both
# anchored at end of buffer:
#
# * an incomplete stem (``<``, ``<thi``, and — the case a
#   character-at-a-time provider hits — the COMPLETE ``<thinking``,
#   whose ``>`` has not arrived yet);
# * a complete stem plus the run the tag regex allows before ``>``
#   (``<think attr='x'`` / ``</think ``).
#
# A stem-length hold-back cannot express the second case, and
# capping the first at ``len(stem) - 1`` releases the full stem as
# prose — after which the ``>`` arrives alone and the tag never
# matches, so the whole block streams into the answer.
_PARTIAL_OPEN_RE: re.Pattern[str] = re.compile(
    r"(?:<(?:t(?:h(?:i(?:n(?:k(?:i(?:n(?:g)?)?)?)?)?)?)?)?"
    r"|<(?:thinking|think)\b[^>]*)$",
    re.IGNORECASE,
)
_PARTIAL_CLOSE_RE: re.Pattern[str] = re.compile(
    r"(?:<(?:/(?:t(?:h(?:i(?:n(?:k(?:i(?:n(?:g)?)?)?)?)?)?)?)?)?"
    r"|</(?:thinking|think)\s*)$",
    re.IGNORECASE,
)


def _holdback_len(buffer: str, partial: re.Pattern[str]) -> int:
    """Length of the trailing fragment that may still become a tag.

    ``search`` returns the leftmost match and both alternatives are
    anchored at ``$``, so the result is the LONGEST such suffix.
    Zero when the buffer cannot end mid-tag — the common case, where
    every buffered character is emittable now.
    """
    match = partial.search(buffer)
    return len(match.group(0)) if match else 0


def split_stream_delta(
    state: InlineFilterState, delta: str, *, final: bool = False
) -> DeltaSplit:
    """Classify one streamed content ``delta`` into thinking / answer.

    Handles the proxy's inline fallback shape (ADR 0022): a
    ``<thinking>...</thinking>`` block inlined into ``content`` and
    split across arbitrary chunk boundaries. The split-channel shape
    (``reasoning_details``) never inlines blocks, so for those chunks
    this function passes the content through unchanged.

    ``final=True`` (stream end) releases any held-back characters:
    outside a block they flush as answer, inside as thinking —
    nothing is silently dropped.
    """
    buffer = state.buffer + delta
    inside = state.inside
    thinking_parts: list[str] = []
    answer_parts: list[str] = []

    while buffer:
        pattern = _CLOSE_TAG_RE if inside else _OPEN_TAG_RE
        match = pattern.search(buffer)
        if match is not None:
            (thinking_parts if inside else answer_parts).append(
                buffer[: match.start()]
            )
            buffer = buffer[match.end() :]
            inside = not inside
            continue
        partial = _PARTIAL_CLOSE_RE if inside else _PARTIAL_OPEN_RE
        keep = 0 if final else _holdback_len(buffer, partial)
        if keep >= len(buffer):
            # The whole buffer is a potential tag fragment (e.g. a
            # delta that ended mid-tag). Nothing is emittable; wait
            # for more input (or the ``final`` flush). Breaking is
            # what keeps this loop productive — re-scanning the
            # same buffer would never progress.
            break
        emit = buffer[: len(buffer) - keep] if keep else buffer
        if emit:
            (thinking_parts if inside else answer_parts).append(emit)
        buffer = buffer[len(buffer) - keep :] if keep else ""

    if final:
        # The loop already flushed everything (``keep=0`` on final);
        # normalize the inside flag so the returned state cannot
        # carry a dangling open block past end of stream.
        inside = False

    return DeltaSplit(
        state=InlineFilterState(buffer=buffer, inside=inside),
        thinking="".join(thinking_parts),
        answer="".join(answer_parts),
    )


# ----- the split reasoning channel (T2) -----


# The keys a provider may use for the split reasoning channel, in
# preference order. ``reasoning_details`` is the canonical shape
# (ADR 0022 / OpenRouter); ``reasoning_content`` is what most
# OpenAI-compatible proxies emit; ``reasoning`` is the third
# spelling seen in the wild. Only the FIRST key that yields entries
# is used — a provider that echoes the same prose under two keys
# must not have it counted twice.
_REASONING_KEYS: tuple[str, ...] = (
    "reasoning_details",
    "reasoning_content",
    "reasoning",
)

# Fields an entry may carry its prose under. ``summary`` covers
# ``reasoning.summary`` entries, which have no ``text``.
_REASONING_PROSE_FIELDS: tuple[str, ...] = ("text", "summary", "content")


def _entry_prose(entry: Any) -> str:
    """Prose of one reasoning entry, ``""`` when it carries none.

    Non-dict entries and entries whose prose fields are absent or
    non-string contribute nothing rather than raising: a malformed
    entry mid-stream must not kill the SSE body.
    """
    if not isinstance(entry, Mapping):
        return ""
    for field in _REASONING_PROSE_FIELDS:
        value = entry.get(field)
        if isinstance(value, str) and value:
            return value
    return ""


def _entries_from_value(value: Any) -> list[dict[str, Any]]:
    """Normalise one reasoning-channel value into entries.

    A list keeps its dict entries VERBATIM so replay-relevant fields
    (``type`` / ``format`` / ``index`` / signatures) survive to the
    checkpoint. A bare string or a single object is synthesised into
    one ``reasoning.text`` entry so downstream code sees one shape.
    """
    if isinstance(value, str):
        return [{"type": "reasoning.text", "text": value}] if value else []
    if isinstance(value, list):
        return [entry for entry in value if isinstance(entry, dict)]
    prose = _entry_prose(value)
    if not prose:
        return []
    entry_type = value.get("type") if isinstance(value, Mapping) else None
    if not isinstance(entry_type, str):
        entry_type = "reasoning.text"
    return [{"type": entry_type, "text": prose}]


def reasoning_entries_from_delta(
    delta: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    """Normalise a reasoning channel into ``reasoning_details`` entries.

    ``delta`` is either a raw provider delta dict or an
    ``AIMessageChunk.additional_kwargs`` mapping — the same key
    names appear on both sides of the converter, so one function
    serves both readers.

    Returns ``[]`` when no reasoning key carries usable prose. That
    empty result is the fallback signal: the caller falls through to
    the inline ``<think>`` strip rather than emitting a blank
    thinking frame.
    """
    if not isinstance(delta, Mapping):
        return []
    for key in _REASONING_KEYS:
        if key not in delta:
            continue
        value = delta[key]
        if key == "reasoning_details" and not isinstance(value, list):
            # The canonical key is a list by contract; a scalar there
            # is a mistyped frame, not prose to render as thinking.
            continue
        entries = _entries_from_value(value)
        if any(_entry_prose(entry) for entry in entries):
            return entries
    return []


def reasoning_entries_from_raw_chunk(chunk: Any) -> list[dict[str, Any]]:
    """Normalise one raw ``chat.completion.chunk`` payload's reasoning.

    Digs the first choice's ``delta`` out of the provider frame
    before delegating to :func:`reasoning_entries_from_delta`. Every
    malformed shape (no ``choices``, an empty list, a ``None``
    delta) yields ``[]`` — this runs inside the streaming loop, where
    a raise would truncate the SSE body mid-run.
    """
    if not isinstance(chunk, Mapping):
        return []
    choices = chunk.get("choices")
    if not isinstance(choices, list) or not choices:
        return []
    first = choices[0]
    if not isinstance(first, Mapping):
        return []
    return reasoning_entries_from_delta(first.get("delta"))


def reasoning_delta_from_chunk(chunk: AIMessageChunk) -> str:
    """Extract THIS chunk's thinking prose delta.

    Reads whichever split-channel key the provider used (ADR 0022
    ``reasoning_split=true``), concatenating the entries' prose in
    order. Malformed entries contribute nothing: the caller skips
    the SSE frame when the result is empty and the inline strip in
    :func:`split_stream_delta` remains the fallback.
    """
    return "".join(
        _entry_prose(entry)
        for entry in reasoning_entries_from_delta(chunk.additional_kwargs)
    )


def replay_reasoning_into_payload(
    payload: dict[str, Any], messages: list[BaseMessage]
) -> dict[str, Any]:
    """Re-attach each assistant turn's reasoning to the request body.

    ``langchain-openai``'s ``_convert_message_to_dict`` copies only
    ``name`` / ``tool_calls`` / ``function_call`` / ``audio`` off an
    ``AIMessage``'s ``additional_kwargs``; every other key — including
    the ``reasoning_details`` the inbound converter worked to keep —
    is dropped on the floor. So capture alone leaves the model's
    prior-turn scratchpad stranded in the checkpoint, and the next
    call reasons from scratch (spec line 28: the converter copies
    ``reasoning_details`` onto the checkpointed ``AIMessage`` AND
    replays it on the next model call).

    Entries go out VERBATIM. The provider correlates them by ``id`` /
    ``index`` and carries its own ``format`` tag; re-deriving the
    prose would drop that identity, which is exactly what
    :func:`_repair_merged_reasoning_entries` protects on the way in.
    A turn checkpointed under one of the other spellings
    (``reasoning_content`` / ``reasoning``) is normalised onto the
    canonical key here rather than forwarded as-is, so the request
    body stays one shape regardless of which provider wrote the
    checkpoint.

    Mutates and returns ``payload``. Nothing happens when the payload
    is not a Chat Completions body (the Responses API keys its turns
    under ``input``, a different contract this host does not use) or
    when the two sides disagree on length — a silent misalignment
    would attach one turn's reasoning to another's message, which is
    worse than replaying none.
    """
    dicts = payload.get("messages")
    if not isinstance(dicts, list) or len(dicts) != len(messages):
        return payload
    for message, message_dict in zip(messages, dicts):
        if not isinstance(message, AIMessage) or not isinstance(
            message_dict, dict
        ):
            continue
        if message_dict.get("role") != "assistant":
            continue
        entries = reasoning_entries_from_delta(message.additional_kwargs)
        if entries:
            message_dict["reasoning_details"] = entries
    return payload


# One subclass per resolved base class. ``compile`` builds the model
# once per process, but tests swap in a fake ``langchain_openai``;
# keying on the base keeps each of them with its own subclass
# instead of handing back a stale one.
_REASONING_CONVERTERS: dict[type, type] = {}


def reasoning_preserving_chat_openai() -> type:
    """Return a ``ChatOpenAI`` subclass that keeps provider reasoning.

    ``langchain-openai`` documents that it targets the official
    OpenAI specification only: its delta converter builds
    ``additional_kwargs`` from ``function_call`` alone, so
    ``reasoning_details`` / ``reasoning_content`` / ``reasoning`` are
    dropped before the chunk reaches the host. With the channel gone
    the model's scratchpad either vanishes or (when the provider
    falls back to inlining) rides in ``content``, which is exactly
    what the inline strip then has to catch.

    The inbound override re-attaches the normalised entries so the
    SSE mapper emits ``thinking`` frames and the checkpoint holds the
    channel; the outbound override then puts those entries back on
    the wire, because ``langchain-openai`` drops arbitrary
    ``additional_kwargs`` in BOTH directions. Together they are spec
    line 28's thinking transport converter: reasoning rides
    ``additional_kwargs.reasoning_details`` and is replayed on the
    next model call.

    ``ChatOpenAI`` is imported lazily (and the subclass built on
    first use) so this module stays importable without the heavy
    runtime dep and so tests can inject a stub via ``sys.modules``.
    """
    from langchain_openai import ChatOpenAI

    cached = _REASONING_CONVERTERS.get(ChatOpenAI)
    if cached is not None:
        return cached

    class ReasoningPreservingChatOpenAI(ChatOpenAI):  # type: ignore[misc]
        """``ChatOpenAI`` plus the provider's split reasoning channel."""

        def _convert_chunk_to_generation_chunk(
            self,
            chunk: dict,
            default_chunk_class: type,
            base_generation_info: dict | None,
        ) -> Any:
            generated = super()._convert_chunk_to_generation_chunk(
                chunk, default_chunk_class, base_generation_info
            )
            # The base class returns ``None`` for frames it skips
            # (e.g. beta-stream ``content.delta`` events).
            if generated is None:
                return None
            entries = reasoning_entries_from_raw_chunk(chunk)
            if entries:
                kwargs = generated.message.additional_kwargs
                existing = kwargs.get("reasoning_details")
                kwargs["reasoning_details"] = (
                    [*existing, *entries]
                    if isinstance(existing, list)
                    else entries
                )
            return generated

        def _get_request_payload(
            self,
            input_: Any,
            *,
            stop: list[str] | None = None,
            **kwargs: Any,
        ) -> dict:
            payload = super()._get_request_payload(
                input_, stop=stop, **kwargs
            )
            # ``_convert_input`` is what the base class itself used to
            # build the payload's message list, so calling it again is
            # what makes the two sides line up index for index.
            return replay_reasoning_into_payload(
                payload, self._convert_input(input_).to_messages()
            )

    _REASONING_CONVERTERS[ChatOpenAI] = ReasoningPreservingChatOpenAI
    return ReasoningPreservingChatOpenAI


# ----- thinking duration clock (T4, #37) -----


def _monotonic() -> float:
    import time

    return time.monotonic()


class ThinkingClock:
    """Accumulate wall-clock time the run spent streaming thinking.

    Spec line 31: after the run, the thinking region collapses to a
    one-line duration label. The clock opens a segment on the first
    ``thinking`` frame, closes it when any other frame follows (the
    model moved on to a tool call or the answer), and closes again
    at stream end. Multiple model calls in one run yield multiple
    segments whose durations sum.

    ``now_fn`` is injectable so tests drive deterministic elapsed
    times; the default reads ``time.monotonic`` once per tick.
    """

    def __init__(self, now_fn=None) -> None:
        self._now = now_fn or _monotonic
        self._start: float | None = None
        self._total: float = 0.0

    def tick_thinking(self) -> None:
        """A ``thinking`` frame: open a segment if none is open."""
        if self._start is None:
            self._start = self._now()

    def tick_other(self) -> None:
        """Any non-thinking frame: close an open segment."""
        if self._start is not None:
            self._total += self._now() - self._start
            self._start = None

    def close(self) -> None:
        """Stream end: close any open segment."""
        self.tick_other()

    @property
    def total_ms(self) -> int:
        """Total thinking time in whole milliseconds."""
        return int(round(self._total * 1000))


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
    not the tools region. Optional ``company`` / ``period`` / ``form``
    filters from the model's call arguments render as ``[key=value]``
    so the operator can see why the tool ran: a bare ``0 filings``
    reads as an empty corpus, ``0 filings [form=8-K]`` as a corpus
    that holds no 8-K.
    """
    count = len(filings) if filings else 0
    noun = "filing" if count == 1 else "filings"
    suffix = ""
    if args:
        parts = [
            f"{key}={args[key]}"
            for key in ("company", "period", "form")
            if args.get(key)
        ]
        if parts:
            suffix = " [" + ", ".join(parts) + "]"
    return f"{count} {noun}{suffix}"


# Prose for the retrieve port's typed EXITs
# (``specs/s1-corpus-retrieve.md``). A typed EXIT means retrieval
# never ran, so it must NOT read like a miss: "0 hits" would tell
# the operator the corpus has nothing.
_RETRIEVE_ERROR_COPY: dict[str, str] = {
    "retrieve_unavailable": "retrieve backend unavailable",
}


def _locator_fields(chunk: Any) -> tuple[str, int] | None:
    """Pull ``(document_id, page)`` off a retrieved chunk's ``source_ref``.

    Both shapes occur: the live adapter returns ``Locator`` objects, the
    serialized ToolMessage JSON carries dicts. A chunk missing either
    field yields ``None`` so the caller can skip it rather than render a
    half locator the operator cannot open.
    """
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


def _retrieve_locator_suffix(chunks: list[Any] | None) -> str:
    """Render ``" -> doc p.N, doc p.M"`` for up to three retrieved chunks.

    Three is the scan budget: enough for the operator to see which pages
    were touched, short enough to stay on one line.
    """
    locators: list[str] = []
    for chunk in (chunks or [])[:3]:
        fields = _locator_fields(chunk)
        if fields is not None:
            locators.append(f"{fields[0]} p.{fields[1]}")
    return " -> " + ", ".join(locators) if locators else ""


def _retrieve_error_line(args: dict[str, Any], error: str) -> str:
    """Render a typed EXIT as prose, keeping the query prefix if present."""
    query_text = args.get("query")
    prefix = (
        f"{query_text!r}: " if isinstance(query_text, str) and query_text else ""
    )
    return prefix + _RETRIEVE_ERROR_COPY.get(
        error, f"retrieve failed ({error})"
    )


def _degradation_clause(result: dict[str, Any] | None) -> str:
    """The visible degradation note for one ``retrieve`` row (T20 / SC39).

    Prefers the notice the host already rendered; falls back to expanding
    the verbatim ``degraded_reasons`` so a result that reached this
    renderer another way still names every reason rather than passing as
    an ordinary hit count.
    """
    if not isinstance(result, dict):
        return ""
    notice = result.get(DEGRADATION_NOTICE_FIELD)
    if isinstance(notice, str) and notice:
        return f" | {notice}"
    reasons = degraded_reasons_for_display(result)
    if not reasons:
        return ""
    return f" | degraded: {'; '.join(reasons)}"


def summarize_retrieve(
    *,
    chunks: list[Any] | None = None,
    args: dict[str, Any] | None = None,
    error: str | None = None,
    degradation: str = "",
) -> str:
    """Human-readable one-liner for ``retrieve`` calls.

    Spec line 32: ``retrieve`` -> ``query + hit count + locators``.

    ``chunks`` carries ``source_ref`` locators (objects or dicts -
    both shapes occur: the live adapter returns ``Locator`` objects,
    the serialized ToolMessage JSON carries dicts). We render the
    query, the hit count, and up to three locators so the operator
    can scan which pages were touched without reading raw JSON.

    ``error`` is the tool's typed EXIT token (``retrieve_unavailable``);
    when set it replaces the hit count with prose, and an unmapped
    token still renders readably rather than blank.

    ``degradation`` is the §B3 note (T20): appended to either shape,
    because a degraded response is neither an ordinary hit count nor an
    ordinary outage and must say so where the operator reads the run.
    """
    args = args or {}
    if error:
        return _retrieve_error_line(args, error) + degradation

    hits = len(chunks) if chunks else 0
    query_value = args.get("query")
    query = query_value if isinstance(query_value, str) else ""

    doc_filter_value = args.get("document_id")
    doc_filter = (
        f" ({doc_filter_value})"
        if isinstance(doc_filter_value, str) and doc_filter_value
        else ""
    )
    locator_suffix = _retrieve_locator_suffix(chunks)
    hits_noun = "hit" if hits == 1 else "hits"
    head = f"{query!r}: {hits} {hits_noun}" if query else f"{hits} {hits_noun}"
    return f"{head}{doc_filter}{locator_suffix}{degradation}"


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


def _format_fact_value(value: Any) -> str:
    """Render a fact row's value with thousands separators.

    Integral floats print without a trailing ``.0`` so a value the
    JSON round-trip widened (``[private source value removed]`` -> ``[private source value removed].0``) still reads
    the way the filing prints it.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return str(value)
    if float(value).is_integer():
        return f"{int(value):,}"
    return f"{value:,}"


def _fact_head(result: dict[str, Any], args: dict[str, Any]) -> str:
    """Render the ``concept period`` head shared by hit and miss rows.

    The result wins over the arguments (the tool echoes the resolved
    concept, which may differ from the alias the model asked for), and
    the arguments still describe the lookup when no ToolMessage landed.
    Neither side present leaves a readable placeholder rather than an
    empty cell.
    """
    concept = str(result.get("concept") or args.get("concept") or "")
    period = str(result.get("period") or args.get("period") or "")
    return " ".join(part for part in (concept, period) if part) or "xbrl fact"


def _fact_qualifiers(result: dict[str, Any]) -> str:
    """Render ``" (unit, scale)"`` for a hit, ``""`` when neither is set."""
    qualifiers = ", ".join(
        str(field)
        for field in (result.get("unit"), result.get("scale"))
        if field
    )
    return f" ({qualifiers})" if qualifiers else ""


def _fact_locator_suffix(result: dict[str, Any]) -> str:
    """Render ``" -> document_id p.N"`` for a hit's ``source_locator``.

    A locator without a ``document_id`` cannot be opened, so it renders
    as nothing; a page-less locator still names the document.
    """
    locator = result.get("source_locator")
    if not isinstance(locator, dict) or not locator.get("document_id"):
        return ""
    suffix = f" -> {locator['document_id']}"
    page = locator.get("page")
    return f"{suffix} p.{page}" if page is not None else suffix


def summarize_get_xbrl_fact(
    *,
    result: dict[str, Any] | None = None,
    args: dict[str, Any] | None = None,
) -> str:
    """Human-readable one-liner for ``get_xbrl_fact`` calls.

    ADR 0031 shapes: a hit carries ``concept`` / ``value`` /
    ``period`` / ``unit`` / ``scale`` / ``source_locator``; a miss
    carries ``{"error": "fact_not_found", ...}``. The hit renders as
    ``concept period = value (unit, scale) -> document_id p.N``; the
    miss appends the error token the same way
    :func:`summarize_read_file` does, so the operator can see why the
    numeric gate had nothing to verify against.
    """
    src_args = args or {}
    src_result = result if isinstance(result, dict) else {}
    head = _fact_head(src_result, src_args)

    error = src_result.get("error")
    if error:
        return f"{head} ({error})"
    if "value" not in src_result:
        # No ToolMessage for this call (the run ended before the
        # tool returned): the arguments still say what was looked up.
        return head

    value = _format_fact_value(src_result.get("value"))
    qualifiers = _fact_qualifiers(src_result)
    return f"{head} = {value}{qualifiers}{_fact_locator_suffix(src_result)}"


def summarize_compare_facts(
    *,
    result: dict[str, Any] | None = None,
    args: dict[str, Any] | None = None,
) -> str:
    """Human-readable one-liner for ``compare_facts`` calls (T3).

    The tools region gets the SHAPE of the table
    (``concept · 2 rows × 2 periods · 4/4 facts``); the table itself
    renders as structure on the turn, never as JSON in this string
    (spec line 32). A typed EXIT (``empty_comparison`` /
    ``comparison_overflow``) appends its token the same way
    :func:`summarize_read_file` does.
    """
    src_args = args or {}
    src_result = result if isinstance(result, dict) else {}
    comparison = src_result.get("comparison")
    if not isinstance(comparison, dict):
        comparison = {}
    concept = str(
        comparison.get("concept") or src_args.get("concept") or ""
    )
    head = concept or "compare"

    error = src_result.get("error")
    if error:
        return f"{head} ({error})"

    rows = comparison.get("rows")
    columns = comparison.get("columns")
    if not isinstance(rows, list) or not isinstance(columns, list):
        # No ToolMessage for this call yet (the run ended before the
        # tool returned): the arguments still say what was asked.
        return head
    row_noun = "row" if len(rows) == 1 else "rows"
    period_noun = "period" if len(columns) == 1 else "periods"
    hits = comparison.get("hit_count", 0)
    cells = comparison.get("cell_count", 0)
    return (
        f"{head} · {len(rows)} {row_noun} × {len(columns)} {period_noun} "
        f"· {hits}/{cells} facts"
    )


def _analysis_result(
    result: dict[str, Any] | None, key: str
) -> tuple[dict[str, Any], str | None]:
    src = result if isinstance(result, dict) else {}
    value = src.get(key)
    return (value if isinstance(value, dict) else {}), (
        str(src["error"]) if src.get("error") else None
    )


def summarize_redline(
    *, result: dict[str, Any] | None = None, args: dict[str, Any] | None = None
) -> str:
    """Summarize a structured paragraph redline without raw JSON."""
    redline, error = _analysis_result(result, "redline")
    if error:
        return f"redline ({error})"
    changes = redline.get("changes")
    if not isinstance(changes, list):
        return "redline"
    return f"redline · {len(changes)} paragraphs"


def summarize_table_extract(
    *, result: dict[str, Any] | None = None, args: dict[str, Any] | None = None
) -> str:
    """Summarize a locator-bound table extraction."""
    table_extract, error = _analysis_result(result, "table_extract")
    if error:
        return f"tables ({error})"
    tables = table_extract.get("tables")
    return f"tables · {len(tables)} extracted" if isinstance(tables, list) else "tables"


def summarize_statement(
    *, result: dict[str, Any] | None = None, args: dict[str, Any] | None = None
) -> str:
    """Summarize an XBRL-backed three-year statement."""
    statement, error = _analysis_result(result, "statement")
    if error:
        return f"statement ({error})"
    rows = statement.get("rows")
    periods = statement.get("periods")
    if isinstance(rows, list) and isinstance(periods, list):
        return f"statement · {len(rows)} rows × {len(periods)} years"
    return "statement"


def summarize_research_issuers(
    *,
    result: dict[str, Any] | None = None,
    args: dict[str, Any] | None = None,
) -> str:
    """Human-readable one-liner for the issuer fan-out (T6).

    The tools region gets the SHAPE of the fan-out
    (``services revenue · 5 issuers · 4/5 with evidence``); the roster
    and its locators render as structure, never as JSON in this string
    (spec line 32). A typed EXIT (``empty_fanout`` /
    ``issuer_overflow``) appends its token the same way
    :func:`summarize_read_file` does, so an operator whose ask was over
    the cap can see why nothing was researched.
    """
    src_args = args or {}
    src_result = result if isinstance(result, dict) else {}
    fanout = src_result.get("fanout")
    if not isinstance(fanout, dict):
        fanout = {}
    head = str(fanout.get("query") or src_args.get("query") or "issuers")

    error = src_result.get("error")
    if error:
        return f"{head} ({error})"

    rows = fanout.get("issuers")
    if not isinstance(rows, list):
        # No ToolMessage for this call yet (the run ended before the
        # join): the arguments still say who was asked about.
        return head
    contributing = fanout.get("contributing")
    hits = len(contributing) if isinstance(contributing, list) else 0
    noun = "issuer" if len(rows) == 1 else "issuers"
    return f"{head} · {len(rows)} {noun} · {hits}/{len(rows)} with evidence"


def _list_files_filings(result: dict[str, Any] | None) -> list[Any] | None:
    """Pull the ``filings`` list off a ``list_files`` result, else ``None``."""
    if result is not None and isinstance(result.get("filings"), list):
        return result["filings"]
    return None


def _retrieve_chunks_error(
    result: dict[str, Any] | None,
) -> tuple[list[Any] | None, str | None]:
    """Split a ``retrieve`` result into ``(chunks, error token)``.

    Either half may be absent: a typed EXIT carries no chunks, and a hit
    carries no error.
    """
    if result is None:
        return None, None
    chunks = (
        result["chunks"] if isinstance(result.get("chunks"), list) else None
    )
    raw_error = result.get("error")
    return chunks, str(raw_error) if raw_error else None


def format_tool_summary(
    name: str,
    *,
    result: dict[str, Any] | None = None,
    args: dict[str, Any] | None = None,
) -> str:
    """Dispatch to the correct ``summarize_*`` based on tool ``name``.

    Exhaustive over the host graph's six model-facing tools (ADR 0009 +
    ``get_xbrl_fact`` from ADR 0031 + ``compare_facts`` from the
    enterprise-kb Focus 1 + ``research_issuers`` from Focus 4 /
    ADR 0034). An unknown name falls back to the bare tool name so a
    future tool still surfaces a readable row in the tools region
    instead of an empty cell - never raw JSON.
    """
    result_dict = result if isinstance(result, dict) else None
    if name == "list_files":
        filings = _list_files_filings(result_dict)
        return summarize_list_files(filings=filings, args=args)
    if name == "retrieve":
        chunks, error = _retrieve_chunks_error(result_dict)
        return summarize_retrieve(
            chunks=chunks,
            args=args,
            error=error,
            degradation=_degradation_clause(result_dict),
        )
    if name == "read_file":
        return summarize_read_file(result=result_dict, args=args)
    if name == "get_xbrl_fact":
        return summarize_get_xbrl_fact(result=result_dict, args=args)
    if name == "compare_facts":
        return summarize_compare_facts(result=result_dict, args=args)
    if name == "redline":
        return summarize_redline(result=result_dict, args=args)
    if name == "extract_tables":
        return summarize_table_extract(result=result_dict, args=args)
    if name == "three_year_statement":
        return summarize_statement(result=result_dict, args=args)
    if name == ISSUER_FANOUT_TOOL:
        return summarize_research_issuers(result=result_dict, args=args)
    return name or "tool"


__all__ = [
    "DeltaSplit",
    "InlineFilterState",
    "ThinkingClock",
    "assemble_final_ai_message",
    "format_tool_summary",
    "inline_thinking_segments",
    "reasoning_delta_from_chunk",
    "reasoning_entries_from_delta",
    "reasoning_entries_from_raw_chunk",
    "reasoning_preserving_chat_openai",
    "replay_reasoning_into_payload",
    "split_stream_delta",
    "strip_thinking_from_answer",
    "summarize_compare_facts",
    "summarize_get_xbrl_fact",
    "summarize_redline",
    "summarize_statement",
    "summarize_table_extract",
    "summarize_list_files",
    "summarize_read_file",
    "summarize_research_issuers",
    "summarize_retrieve",
]
