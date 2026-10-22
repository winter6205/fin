"""Cancellable asynchronous calls owned by one compiled host runtime."""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import inspect
import threading
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, cast

from fin.host.run_budgets import SPEC_CLEANUP_GRACE_MAX_S
from fin.host.run_control import StopToken


class RunCallCancelled(RuntimeError):
    """A run-owned external call was cancelled before its result was used."""


class RunCallSupervisorError(RuntimeError):
    """The runtime-owned asynchronous call bridge could not be started."""


class RunCallCleanupTimeout(RunCallSupervisorError):
    """An asynchronous call did not settle within the cleanup grace."""


@dataclass(frozen=True, slots=True)
class RunCallContext:
    stop_token: StopToken
    supervisor: RunCallSupervisor | None


_ACTIVE_RUN_CALL: contextvars.ContextVar[RunCallContext | None] = (
    contextvars.ContextVar("fin_active_run_call", default=None)
)
_NATIVE_STREAM_CLOSURES: contextvars.ContextVar[_NativeStreamClosures | None] = (
    contextvars.ContextVar("fin_native_stream_closures", default=None)
)


def _stream_aclose(stream: Any) -> Callable[[], Awaitable[Any]] | None:
    """The stream's async close, when it has one worth awaiting.

    An async generator's ``aclose`` and a native client's ``aclose`` have
    the same call shape, so one lookup serves both.
    """
    close = getattr(stream, "aclose", None)
    return cast("Callable[[], Awaitable[Any]]", close) if callable(close) else None


class _NativeStreamClosures:
    def __init__(self) -> None:
        self._closers: list[Callable[[], Awaitable[Any]]] = []

    def register(self, stream: Any) -> None:
        close = _stream_aclose(stream)
        if close is not None:
            self._closers.append(close)

    async def close(self) -> None:
        for close in reversed(self._closers):
            await close()


def register_native_async_stream(stream: Any) -> None:
    """Register a model transport stream for runtime cancellation cleanup."""
    closures = _NATIVE_STREAM_CLOSURES.get()
    if closures is not None:
        closures.register(stream)


@contextmanager
def run_call_context(
    stop_token: StopToken,
    supervisor: RunCallSupervisor | None,
) -> Iterator[RunCallContext]:
    """Expose cancellation only while one synchronous graph step runs."""
    context = RunCallContext(stop_token=stop_token, supervisor=supervisor)
    reset = _ACTIVE_RUN_CALL.set(context)
    try:
        yield context
    finally:
        _ACTIVE_RUN_CALL.reset(reset)


def current_run_call_context() -> RunCallContext | None:
    return _ACTIVE_RUN_CALL.get()


class RunCallContextRegistry:
    """Carry a graph run context into checkpoint callbacks keyed by thread id."""

    def __init__(self) -> None:
        self._contexts: dict[str, RunCallContext] = {}
        self._guard = threading.Lock()

    @contextmanager
    def bind_thread(self, thread_id: str) -> Iterator[None]:
        context = current_run_call_context()
        if context is None:
            yield
            return

        with self._guard:
            previous = self._contexts.get(thread_id)
            self._contexts[thread_id] = context
        try:
            yield
        finally:
            with self._guard:
                if self._contexts.get(thread_id) is context:
                    if previous is None:
                        self._contexts.pop(thread_id, None)
                    else:
                        self._contexts[thread_id] = previous

    @contextmanager
    def activate_thread(self, thread_id: str | None) -> Iterator[None]:
        if not thread_id or current_run_call_context() is not None:
            yield
            return

        with self._guard:
            context = self._contexts.get(thread_id)
        if context is None:
            yield
            return

        reset = _ACTIVE_RUN_CALL.set(context)
        try:
            yield
        finally:
            _ACTIVE_RUN_CALL.reset(reset)


def stream_model_call(runnable: Any, input_value: Any) -> Iterator[Any]:
    """Stream through the runtime bridge during a run, otherwise use sync IO."""
    context = current_run_call_context()
    if context is None or context.supervisor is None:
        return iter(runnable.stream(input_value))
    if context.stop_token.is_set():
        raise RunCallCancelled("run stopped before model call started")
    return context.supervisor.stream(
        runnable,
        input_value,
        cancel_event=context.stop_token.cancel_event,
    )


@dataclass(frozen=True, slots=True)
class _StreamItem:
    value: Any = None
    error: Exception | None = None
    ended: bool = False


class RunCallSupervisor:
    """One lazily started event loop for cancellable model calls."""

    def __init__(
        self,
        *,
        cleanup_grace_s: float = SPEC_CLEANUP_GRACE_MAX_S,
    ) -> None:
        self._cleanup_grace_s = cleanup_grace_s
        self._lock = threading.Lock()
        self._close_guard = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._closed = False
        self._shutdown_complete = False
        self._active: set[_AsyncModelStream] = set()
        self._async_close_future: concurrent.futures.Future[None] | None = None
        self._async_close_error: BaseException | None = None

    @property
    def active_calls(self) -> int:
        with self._lock:
            return len(self._active)

    @property
    def closed(self) -> bool:
        """Whether this supervisor can no longer run a call."""
        with self._lock:
            return self._closed

    def stream(
        self,
        runnable: Any,
        input_value: Any,
        *,
        cancel_event: threading.Event,
    ) -> Iterator[Any]:
        loop = self._ensure_loop()
        queue = self._create_queue(loop)
        stream = _AsyncModelStream(
            supervisor=self,
            loop=loop,
            queue=queue,
            runnable=runnable,
            input_value=input_value,
            cancel_event=cancel_event,
        )
        with self._lock:
            if self._closed:
                raise RunCallSupervisorError("run-call supervisor is closed")
            self._active.add(stream)
            stream.start()
        return stream

    def close(
        self,
        *,
        async_closers: tuple[Callable[[], Awaitable[Any]], ...] = (),
    ) -> None:
        if async_closers:
            self._ensure_loop(allow_closed=True)
        with self._close_guard:
            with self._lock:
                if self._shutdown_complete:
                    return
                self._closed = True
                active = tuple(self._active)
                loop = self._loop
                thread = self._thread

            for stream in active:
                stream.cancel(wait=True)

            if loop is None or thread is None:
                with self._lock:
                    self._shutdown_complete = True
                return

            self._close_async_clients(loop, async_closers)
            self._settle_loop_tasks(loop)
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=self._cleanup_grace_s)
            if thread.is_alive():
                raise RunCallCleanupTimeout(
                    "run-call event loop did not stop during shutdown"
                )
            with self._lock:
                self._shutdown_complete = True
            if self._async_close_error is not None:
                raise RunCallSupervisorError(
                    "asynchronous model client failed to close"
                ) from self._async_close_error

    def _close_async_clients(
        self,
        loop: asyncio.AbstractEventLoop,
        closers: tuple[Callable[[], Awaitable[Any]], ...],
    ) -> None:
        if not closers:
            return
        with self._lock:
            future = self._async_close_future
            if future is None:
                future = asyncio.run_coroutine_threadsafe(
                    self._run_async_closers(closers), loop
                )
                self._async_close_future = future
        try:
            future.result(timeout=self._cleanup_grace_s)
        except concurrent.futures.TimeoutError as exc:
            raise RunCallCleanupTimeout(
                "asynchronous model client did not close during shutdown"
            ) from exc
        except Exception as exc:
            self._async_close_error = exc

    async def _run_async_closers(
        self, closers: tuple[Callable[[], Awaitable[Any]], ...]
    ) -> None:
        first_error: Exception | None = None
        for close in closers:
            try:
                result = close()
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error

    def _settle_loop_tasks(self, loop: asyncio.AbstractEventLoop) -> None:
        future = asyncio.run_coroutine_threadsafe(
            self._cancel_remaining_tasks(), loop
        )
        try:
            future.result(timeout=self._cleanup_grace_s)
        except concurrent.futures.TimeoutError as exc:
            raise RunCallCleanupTimeout(
                "runtime event-loop tasks did not settle during shutdown"
            ) from exc

    async def _cancel_remaining_tasks(self) -> None:
        current = asyncio.current_task()
        pending = tuple(
            task for task in asyncio.all_tasks() if task is not current
        )
        for task in pending:
            task.cancel()
        if not pending:
            return
        _, unsettled = await asyncio.wait(
            pending, timeout=self._cleanup_grace_s
        )
        if unsettled:
            raise RunCallCleanupTimeout(
                "runtime event-loop tasks did not settle during shutdown"
            )

    def _ensure_loop(self, *, allow_closed: bool = False) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._closed and not allow_closed:
                raise RunCallSupervisorError("run-call supervisor is closed")
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run_loop,
                    name="fin-run-call-loop",
                    daemon=True,
                )
                self._thread.start()
            thread = self._thread
        if not self._ready.wait(timeout=self._cleanup_grace_s):
            raise RunCallSupervisorError(
                "run-call event loop did not start within the cleanup grace"
            )
        if not thread.is_alive():
            raise RunCallSupervisorError("run-call event loop is not running")
        with self._lock:
            loop = self._loop
        if loop is None:
            raise RunCallSupervisorError("run-call event loop is unavailable")
        return loop

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        with self._lock:
            self._loop = loop
        self._ready.set()
        loop.run_forever()
        loop.close()

    @staticmethod
    def _create_queue(
        loop: asyncio.AbstractEventLoop,
    ) -> asyncio.Queue[_StreamItem]:
        async def create() -> asyncio.Queue[_StreamItem]:
            return asyncio.Queue(maxsize=1)

        future = asyncio.run_coroutine_threadsafe(create(), loop)
        try:
            return future.result(timeout=SPEC_CLEANUP_GRACE_MAX_S)
        except concurrent.futures.TimeoutError as exc:
            future.cancel()
            raise RunCallSupervisorError(
                "run-call stream queue did not initialize"
            ) from exc

    def _released(self, stream: _AsyncModelStream) -> None:
        with self._lock:
            self._active.discard(stream)


class _AsyncModelStream(Iterator[Any]):
    def __init__(
        self,
        *,
        supervisor: RunCallSupervisor,
        loop: asyncio.AbstractEventLoop,
        queue: asyncio.Queue[_StreamItem],
        runnable: Any,
        input_value: Any,
        cancel_event: threading.Event,
    ) -> None:
        self._supervisor = supervisor
        self._loop = loop
        self._queue = queue
        self._runnable = runnable
        self._input_value = input_value
        self._cancel_event = cancel_event
        self._shutdown = threading.Event()
        self._producer_settled = threading.Event()
        self._producer_task: asyncio.Task[None] | None = None
        self._completion_error: BaseException | None = None
        self._closed = False

    def __iter__(self) -> _AsyncModelStream:
        return self

    def __next__(self) -> Any:
        if self._closed:
            raise StopIteration
        if self._cancel_requested():
            self.cancel(wait=True)
            raise RunCallCancelled("run stopped during model call")

        getter = asyncio.run_coroutine_threadsafe(
            self._queue.get(), self._loop
        )
        while True:
            try:
                item = getter.result(timeout=0.025)
            except concurrent.futures.TimeoutError:
                if self._cancel_requested():
                    getter.cancel()
                    self.cancel(wait=True)
                    raise RunCallCancelled("run stopped during model call")
                continue
            except concurrent.futures.CancelledError as exc:
                if self._cancel_requested():
                    self.cancel(wait=True)
                    raise RunCallCancelled("run stopped during model call") from exc
                raise RunCallSupervisorError(
                    "run-call stream receiver was cancelled"
                ) from exc

            if self._cancel_requested():
                self.cancel(wait=True)
                raise RunCallCancelled("run stopped during model call")
            if item.ended:
                self._closed = True
                return self._end_stream()
            if item.error is not None:
                self._closed = True
                self._await_completion()
                raise item.error
            return item.value

    def close(self) -> None:
        if not self._closed:
            self.cancel(wait=True)

    def cancel(self, *, wait: bool) -> None:
        self._shutdown.set()
        self._loop.call_soon_threadsafe(self._cancel_producer)
        if wait:
            self._await_completion()
        self._closed = True

    def start(self) -> None:
        self._loop.call_soon_threadsafe(self._start_producer)

    def _start_producer(self) -> None:
        task = self._loop.create_task(self._produce())
        self._producer_task = task
        task.add_done_callback(self._producer_finished)
        if self._shutdown.is_set():
            task.cancel()

    def _cancel_producer(self) -> None:
        task = self._producer_task
        if task is not None and not task.done():
            task.cancel()

    async def _produce(self) -> None:
        stream: Any = None
        closures = _NativeStreamClosures()
        reset = _NATIVE_STREAM_CLOSURES.set(closures)
        try:
            stream = self._runnable.astream(self._input_value)
            try:
                async for chunk in stream:
                    await self._queue.put(_StreamItem(value=chunk))
                await self._queue.put(_StreamItem(ended=True))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._queue.put(_StreamItem(error=exc))
        finally:
            try:
                close = _stream_aclose(stream)
                if close is not None:
                    await close()
            finally:
                try:
                    await closures.close()
                finally:
                    _NATIVE_STREAM_CLOSURES.reset(reset)

    def _producer_finished(self, task: asyncio.Task[None]) -> None:
        if not task.cancelled():
            self._completion_error = task.exception()
        self._supervisor._released(self)
        self._producer_settled.set()

    def _cancel_requested(self) -> bool:
        return self._cancel_event.is_set() or self._shutdown.is_set()

    def _await_completion(self) -> None:
        if not self._producer_settled.wait(
            timeout=self._supervisor._cleanup_grace_s
        ):
            raise RunCallCleanupTimeout(
                "model call did not settle during the cleanup grace"
            )
        if self._completion_error is not None:
            raise RunCallSupervisorError(
                "model stream failed during cleanup"
            ) from self._completion_error

    def _end_stream(self) -> Any:
        self._await_completion()
        raise StopIteration


__all__ = [
    "RunCallCancelled",
    "RunCallCleanupTimeout",
    "RunCallSupervisor",
    "RunCallSupervisorError",
    "current_run_call_context",
    "register_native_async_stream",
    "run_call_context",
    "stream_model_call",
]
