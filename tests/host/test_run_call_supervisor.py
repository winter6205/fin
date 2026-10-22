"""Cancellation ownership at the sync graph / async model boundary."""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessageChunk, BaseMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from pydantic import PrivateAttr

from fin.host.run_call_supervisor import (
    RunCallCancelled,
    RunCallSupervisor,
    register_native_async_stream,
    run_call_context,
    stream_model_call,
)
from fin.host.run_control import StopToken


class _NativeAsyncModel(BaseChatModel):
    _started: threading.Event = PrivateAttr(default_factory=threading.Event)
    _cancelled: threading.Event = PrivateAttr(default_factory=threading.Event)
    _third_yielded: threading.Event = PrivateAttr(default_factory=threading.Event)
    _cleanup_started: threading.Event = PrivateAttr(default_factory=threading.Event)
    _block: bool = PrivateAttr()
    _hold_cleanup: bool = PrivateAttr(default=False)
    _cleanup_release: asyncio.Event | None = PrivateAttr(default=None)
    _loop: asyncio.AbstractEventLoop | None = PrivateAttr(default=None)

    def __init__(self, *, block: bool, hold_cleanup: bool = False) -> None:
        super().__init__()
        self._block = block
        self._hold_cleanup = hold_cleanup

    @property
    def _llm_type(self) -> str:
        return "native-async-test-model"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        raise AssertionError("sync model path must not be used")

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> Any:
        raise AssertionError("sync model path must not be used")

    def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> Any:
        stream = self._native_stream()
        register_native_async_stream(stream)
        return stream

    async def _native_stream(self) -> Any:
        self._started.set()
        self._loop = asyncio.get_running_loop()
        if self._hold_cleanup:
            self._cleanup_release = asyncio.Event()
        try:
            yield ChatGenerationChunk(message=AIMessageChunk(content="first"))
            yield ChatGenerationChunk(message=AIMessageChunk(content="second"))
            self._third_yielded.set()
            yield ChatGenerationChunk(message=AIMessageChunk(content="third"))
            if self._block:
                await asyncio.Future()
        finally:
            self._cleanup_started.set()
            if self._hold_cleanup:
                assert self._cleanup_release is not None
                await self._cleanup_release.wait()
            self._cancelled.set()


def _cancel_stream(
    model: _NativeAsyncModel,
    supervisor: RunCallSupervisor,
    *,
    index: int,
) -> None:
    token = StopToken(thread_id=f"thread-{index}", run_id=f"run-{index}")
    with run_call_context(token, supervisor):
        stream = stream_model_call(model, [HumanMessage(content="question")])
        assert next(stream).content == "first"
        assert model._started.wait(timeout=1)
        token.stop(reason="client_disconnect")
        with pytest.raises(RunCallCancelled):
            next(stream)
    assert model._cancelled.wait(timeout=1)


def test_native_async_model_call_is_cancelled_and_releases_capacity() -> None:
    supervisor = RunCallSupervisor(cleanup_grace_s=1)
    try:
        for index in range(4):
            _cancel_stream(
                _NativeAsyncModel(block=True), supervisor, index=index
            )
            assert supervisor.active_calls == 0

        live_token = StopToken(thread_id="thread-live", run_id="run-live")
        with run_call_context(live_token, supervisor):
            stream = stream_model_call(
                _NativeAsyncModel(block=False), [HumanMessage(content="next")]
            )
            assert "".join(chunk.content for chunk in stream) == "firstsecondthird"
        assert supervisor.active_calls == 0
    finally:
        supervisor.close()


def test_cancel_waits_for_backpressured_async_stream_cleanup() -> None:
    supervisor = RunCallSupervisor(cleanup_grace_s=1)
    model = _NativeAsyncModel(block=True, hold_cleanup=True)
    token = StopToken(thread_id="thread-held", run_id="run-held")
    try:
        with run_call_context(token, supervisor):
            stream = stream_model_call(model, [HumanMessage(content="question")])
            assert next(stream).content == "first"
        assert model._third_yielded.wait(timeout=1)
        assert stream._queue.full()

        cancellation_done = threading.Event()

        def cancel_next() -> None:
            token.stop(reason="client_disconnect")
            with pytest.raises(RunCallCancelled):
                next(stream)
            cancellation_done.set()

        cancellation = threading.Thread(target=cancel_next)
        cancellation.start()
        assert model._cleanup_started.wait(timeout=1)
        assert not cancellation_done.is_set()
        assert supervisor.active_calls == 1

        assert model._loop is not None
        assert model._cleanup_release is not None
        model._loop.call_soon_threadsafe(model._cleanup_release.set)
        cancellation.join(timeout=1)
        assert not cancellation.is_alive()
        assert cancellation_done.is_set()
        assert model._cancelled.is_set()
        assert supervisor.active_calls == 0
    finally:
        if model._cleanup_release is not None and model._loop is not None:
            model._loop.call_soon_threadsafe(model._cleanup_release.set)
        supervisor.close()
