"""Cooperative stop signals for in-flight workspace runs (T4, the tracked change).

Spec Focus 3: "Stop cancels the in-flight **run** on that thread
(composer re-enabled); 409 concurrent send remains."

Why cooperative rather than a kill: the run executes inside
``graph.stream`` on the request thread that owns the per-thread lock
(``fin.host.graph._LockedGraph``). Nothing outside that thread may
touch the graph — a forced cancellation would abandon the checkpoint
mid-write and leave the lock held forever, which is exactly the state
"composer re-enabled after Stop" forbids. Instead the stop route flips
a flag; the SSE generator checks it between frames, closes the stream
(releasing the lock in ``_LockedGraph.stream``'s ``finally``), and
writes a terminal turn row.

Why the registry never acquires the graph lock: a stop is only
meaningful while a run holds that lock, so a stop that waited for it
could never fire.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field


@dataclass(frozen=True)
class StopToken:
    """The stop flag for one ``(thread_id, run_id)`` pair.

    Handed to the SSE generator at run start. ``is_set`` is the only
    thing the generator asks; ``stop`` is what the route calls.
    """

    thread_id: str
    run_id: str
    _event: threading.Event = field(default_factory=threading.Event, repr=False)

    def stop(self) -> None:
        self._event.set()

    def is_set(self) -> bool:
        return self._event.is_set()


class RunControl:
    """At most one stoppable run per thread, mirroring the graph lock.

    One instance per mounted router (not a module global) so two test
    apps — or two mounts in one process — cannot signal each other's
    runs.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tokens: dict[str, StopToken] = {}

    def begin(self, thread_id: str, run_id: str) -> StopToken:
        """Register a run as stoppable and return its token."""
        token = StopToken(thread_id=thread_id, run_id=run_id)
        with self._lock:
            self._tokens[thread_id] = token
        return token

    def request_stop(self, thread_id: str) -> str | None:
        """Flip the flag for ``thread_id``; return the stopped run id.

        ``None`` means no run is registered — the route answers 204
        (nothing to stop) rather than inventing a failure.
        """
        with self._lock:
            token = self._tokens.get(thread_id)
        if token is None:
            return None
        token.stop()
        return token.run_id

    def end(self, token: StopToken) -> None:
        """Deregister ``token``'s run once its stream is finished.

        Identity-checked: a late ``end`` from a previous run must not
        deregister the run that replaced it, or the operator would
        press Stop on a live run and nothing would happen.
        """
        with self._lock:
            current = self._tokens.get(token.thread_id)
            if current is token:
                del self._tokens[token.thread_id]

    def active_run_id(self, thread_id: str) -> str | None:
        with self._lock:
            token = self._tokens.get(thread_id)
        return None if token is None else token.run_id


__all__ = ["RunControl", "StopToken"]
