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

from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field


class ScriptedChatModel(BaseChatModel):
    """A ``BaseChatModel`` that returns ``script[i]`` on the i-th call.

    Parameters
    ----------
    script
        The sequence of ``AIMessage``s the model will return. An
        ``AIMessage`` with ``tool_calls=[...]`` routes the graph
        to the tool node; one without routes to the citation gate.
        If the script is empty, the first call returns a no-op
        ``AIMessage("")`` so a misconfigured test fails fast in a
        readable way.

        Once the script is exhausted, the last ``AIMessage`` is
        returned forever. This keeps step-cap tests deterministic
        without making the model raise.
    bound_tools
        The list of tools the host graph passed to ``bind_tools``.
        Tests inspect this attribute to confirm wiring.
    """

    script: list[AIMessage] = Field(default_factory=list)
    bound_tools: list[Any] = Field(default_factory=list)
    _index: int = 0  # mutated across calls; underscore-prefixed to dodge Pydantic field validation

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


__all__ = ["ScriptedChatModel"]
