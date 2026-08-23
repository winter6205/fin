"""Scripted chat model for tests (T6).

This module ships a ``BaseChatModel`` subclass that returns a
pre-scripted sequence of ``AIMessage``s. Every test of the host
graph runs through this model — no live LLM, no network, no
cache, no key in the environment (ADR 0014).

``ScriptedChatModel(script)`` walks the ``script`` list once,
returning ``script[0]`` on the first ``_generate`` call,
``script[1]`` on the second, and so on. After the script is
exhausted, the final ``AIMessage`` is returned forever so tests
can build "loop forever" scenarios (e.g. step-cap) without the
model raising on the next tick.

``bind_tools`` is a no-op: the host graph calls it to attach the
tool schemas, but the script is the source of truth for what the
model actually returns. The bound tools are recorded on
``bound_tools`` for tests that want to assert what the host
graph asked the model to handle.

This module imports ``langchain_core`` only — no LangGraph, no
PDF extraction, no corpus code.
"""

from __future__ import annotations

from typing import Any, Iterator

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from pydantic import Field


class ScriptedChatModel(BaseChatModel):
    """A ``BaseChatModel`` that returns ``script[i]`` on the i-th call.

    Parameters
    ----------
    script
        The sequence of ``AIMessage``s the model will return via
        sync ``_generate``. An ``AIMessage`` with
        ``tool_calls=[...]`` routes the graph to the tool node; one
        without routes to the citation gate. If the script is
        empty, the first call returns a no-op ``AIMessage("")`` so
        a misconfigured test fails fast in a readable way.

        Once the script is exhausted, the last ``AIMessage`` is
        returned forever. This keeps step-cap tests deterministic
        without making the model raise.
    stream_script
        The sequence of ``AIMessageChunk``s ``_stream`` yields when
        the test wants to exercise the token-by-token path (T3
        workspace SSE). When non-empty, ``stream()`` yields chunks
        from this list (replaying the last one forever once
        exhausted); when empty, ``stream()`` delegates to
        ``script`` via the sync ``_generate`` path so the model is
        useful regardless of which surface a test exercises.

        Both ``script`` and ``stream_script`` may be set; they
        drive the sync and streaming code paths respectively and
        never cross over.
    bound_tools
        The list of tools the host graph passed to ``bind_tools``.
        Tests inspect this attribute to confirm wiring.
    """

    script: list[AIMessage] = Field(default_factory=list)
    stream_script: list[AIMessageChunk] = Field(default_factory=list)
    # T1 (#33): one entry per ``stream()`` call, each entry a FULL
    # response as a list of chunks. Lets a test script two distinct
    # streamed model calls (tool-call response, then thinking +
    # answer deltas) which the flat ``stream_script`` cannot express.
    # Takes precedence over ``stream_script`` on the streamed path.
    stream_responses: list[list[AIMessageChunk]] = Field(default_factory=list)
    bound_tools: list[Any] = Field(default_factory=list)
    _index: int = 0  # mutated across calls; underscore-prefixed to dodge Pydantic field validation
    _stream_index: int = 0  # mutated across stream() calls

    @property
    def _llm_type(self) -> str:
        # Tag used by LangChain tracing. Tests don't assert on it;
        # present so the model looks "real" to chat-model tooling.
        return "scripted"

    def bind_tools(
        self,
        tools: list[Any],
        **kwargs: Any,
    ) -> "ScriptedChatModel":
        """Record the bound tools and return ``self``.

        The script is the source of truth for ``_generate`` output;
        we do not actually inject tool schemas here. The host graph
        treats ``bind_tools`` as the wire-up seam; tests use
        ``bound_tools`` to confirm the host graph registered the
        expected tool callables.
        """
        # Make a shallow copy so the original list passed by the
        # caller is not mutated by later tests.
        self.bound_tools = list(tools)
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        # Empty script: return a deterministic no-op message so the
        # failure mode is observable instead of raising deep in
        # LangGraph. Tests that drive the agent loop must supply a
        # non-empty script.
        if not self.script:
            fallback = AIMessage(content="")
            return ChatResult(generations=[ChatGeneration(message=fallback)])

        if self._index >= len(self.script):
            # Script exhausted: keep returning the last message. This
            # is the pattern that lets a test force the step-cap
            # path (every model_node tick produces the same shape)
            # without the model crashing mid-run.
            template = self.script[-1]
        else:
            template = self.script[self._index]
            self._index += 1

        # Return a COPY with a fresh message id on every call.
        # LangGraph's ``add_messages`` reducer deduplicates by
        # message id; re-emitting the same scripted object twice
        # would REPLACE the earlier copy in the history instead of
        # appending, which breaks loop-shaped scripts (step-cap
        # tests) and confuses the tool-call router.
        message = AIMessage(
            content=template.content,
            tool_calls=list(template.tool_calls),
        )

        return ChatResult(generations=[ChatGeneration(message=message)])

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        """Yield one response's worth of ``AIMessageChunk``s.

        How this maps to the host graph's loop: the host graph's
        ``model_node`` calls ``model.stream(state["messages"])``
        ONCE per agent-loop iteration and consumes every chunk of
        that response. ``_stream`` therefore yields the entire
        current response on each invocation, not a single chunk.

        Response selection, in priority order:

        * ``stream_responses`` (T1 #33) — one entry per call; the
          entry's chunks are yielded in full. Once exhausted the
          LAST entry replays (the step-cap loop contract).
        * ``stream_script`` — the first ``stream()`` call walks the
          entire script in order; subsequent calls delegate to the
          sync ``script`` when set (mixed models), else replay the
          whole script.
        * neither set — ``_generate`` result wrapped in one chunk.

        A parallel ``_stream_index`` (vs. sharing ``_index`` with
        ``_generate``) keeps the two surfaces independent; a test
        that exercises both paths can mutate one without disturbing
        the other.
        """
        if self.stream_responses:
            templates = (
                self.stream_responses[self._stream_index]
                if self._stream_index < len(self.stream_responses)
                else self.stream_responses[-1]
            )
            if self._stream_index < len(self.stream_responses):
                self._stream_index += 1
            for template in templates:
                yield ChatGenerationChunk(
                    message=AIMessageChunk(
                        content=template.content,
                        tool_call_chunks=list(template.tool_call_chunks),
                        additional_kwargs=dict(template.additional_kwargs),
                    )
                )
            return

        if not self.stream_script:
            result = self._generate(
                messages, stop=stop, run_manager=run_manager, **kwargs
            )
            for generation in result.generations:
                chunk = AIMessageChunk(
                    content=generation.message.content,
                    tool_calls=list(generation.message.tool_calls),
                    additional_kwargs=dict(generation.message.additional_kwargs),
                )
                yield ChatGenerationChunk(message=chunk)
            return

        templates = list(self.stream_script)
        if self._stream_index == 0:
            # First ``stream()`` call: walk the entire script in
            # order so the caller sees one response assembled from
            # N chunks. Advance the index past the end so subsequent
            # calls hit the exhausted branch.
            self._stream_index = len(self.stream_script)
            for template in templates:
                yield ChatGenerationChunk(
                    message=AIMessageChunk(
                        content=template.content,
                        tool_call_chunks=list(template.tool_call_chunks),
                        additional_kwargs=dict(template.additional_kwargs),
                    )
                )
            return

        # ``stream_script`` exhausted. Mixed models (both
        # ``stream_script`` and ``script`` set) want the sync path
        # to take over so the next model call sees the first
        # ``script`` entry instead of looping the streamed tool
        # call. The sync path also handles step-cap loop determinism
        # via ``_generate``'s own ``script[-1]`` replay. Delegating
        # keeps the two surfaces aligned: stream is one response
        # per ``stream()`` call, sync is one response per
        # ``_generate`` call, and a model that mixes both gets a
        # single coherent iteration.
        if self.script:
            result = self._generate(
                messages, stop=stop, run_manager=run_manager, **kwargs
            )
            for generation in result.generations:
                chunk = AIMessageChunk(
                    content=generation.message.content,
                    tool_calls=list(generation.message.tool_calls),
                    additional_kwargs=dict(generation.message.additional_kwargs),
                )
                yield ChatGenerationChunk(message=chunk)
            return

        # Pure ``stream_script`` (no sync script): replay the ENTIRE
        # final response (every chunk in order) so the assembled
        # ``AIMessage`` is identical across calls. Mirrors the sync
        # ``_generate`` contract (``script[-1]`` replays forever as a
        # complete message) on the streaming surface.
        #
        # Why ``all chunks`` and not ``last chunk only``: replaying a
        # bare continuation fragment (the typical last-chunk shape
        # when a tool call is split across chunks) produces an
        # ``AIMessage`` whose ``tool_calls`` have ``name`` and
        # ``id`` as ``None`` / empty. The downstream ``ToolNode``
        # then trips its "invalid tool name" error path, which
        # tries to construct a ``ToolMessage(tool_call_id=None)``
        # and raises a Pydantic ``ValidationError`` — masking the
        # step-cap loop determinism this behaviour was meant to
        # provide. Replaying the full response keeps every tool
        # call fully-formed so step-cap loops land on a stable
        # AIMessage shape on every iteration.
        for template in templates:
            yield ChatGenerationChunk(
                message=AIMessageChunk(
                    content=template.content,
                    tool_call_chunks=list(template.tool_call_chunks),
                    additional_kwargs=dict(template.additional_kwargs),
                )
            )


__all__ = ["ScriptedChatModel"]
