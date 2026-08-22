"""Workspace HTTP routes for the T1 surface (#28).

Routes
------
* ``GET    /api/threads``               - list threads (sidebar).
* ``POST   /api/threads``               - create thread (title from
                                          first_message first line,
                                          50-char cap + ellipsis).
* ``GET    /api/threads/{id}``          - thread metadata + turn
                                          table.
* ``DELETE /api/threads/{id}``          - drop metadata row + call
                                          checkpointer.delete_thread.
* ``POST   /api/threads/{id}/messages`` - sync invoke wrapped in a
                                          ``text/event-stream`` with
                                          three terminal events in a
                                          fixed order: ``status`` ->
                                          ``answer`` -> ``citations``
                                          (citations is terminal per
                                          spec line 30). T1 is sync,
                                          not token-by-token (T3 will
                                          stream).
* ``GET    /api/corpus``                - real ``list_filings()`` over
                                          the frozen manifest.

Why one module, not per-route modules? Each route is small, they
share the request schema (``thread_id`` resolution, empty-question
no-op), and the alternative - one module per route - would explode
the file count for a feature this small (spec forbids new top-level
directories).

Why the same app as ``/ask``? Spec / ADR 0013 / ADR 0024: one
FastAPI process per v1 surface. The routes are added to the
existing ``fin.host.http.app`` so a single ``uvicorn`` serves the
v1 ask route AND the workspace routes AND the SPA.

Concurrent-run ordering
-----------------------
``POST /api/threads/{id}/messages`` is wrapped in
``StreamingResponse``. To satisfy the spec rule "409 decided BEFORE
SSE headers", the handler runs ``graph.invoke`` **synchronously**
**before** returning the StreamingResponse. A
``ConcurrentRunError`` raised here propagates to the FastAPI
exception handler as a plain 409 (no headers, no body chunk).
T3 will replace the body of ``graph.invoke`` with a streaming
``stream()`` call but the "decide contention before headers"
invariant is preserved.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Iterator, Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from fastapi.responses import JSONResponse, Response, StreamingResponse
from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field

from fin.corpus.catalog import list_filings
from fin.host.run_payload import _run_to_response_payload
from fin.host.state import EVIDENCE_RESET, reset_input_state
from fin.host.thread_store import (
    RunRecord,
    ThreadNotFoundError,
    ThreadRecord,
    ThreadStore,
)


logger = logging.getLogger(__name__)


# Maximum characters of the first message used as the thread title.
# Spec: "max 50 chars + ellipsis on overflow."
_TITLE_MAX_CHARS: int = 50

# Default title when the caller creates a thread without a first
# message (the empty corpus / empty thread copy).
_DEFAULT_TITLE: str = "New thread"


# ----- request / response schemas -----


class CreateThreadRequest(BaseModel):
    first_message: str | None = None


class CreateThreadResponse(BaseModel):
    id: str
    title: str
    created_at: str  # ISO-8601 from datetime.isoformat()


class SendMessageRequest(BaseModel):
    question: str


class CitationPayload(BaseModel):
    document_id: str
    version: str
    page: int
    quote: str


class TurnRow(BaseModel):
    """One row in the turn table - one user send + assistant reply.

    ``assistant_message`` is the ordinary-person copy the SPA
    renders when ``status`` is ``refuse`` or ``step_cap`` (T2 /
    spec SC67). Empty on ``done``. Mirrored on the
    ``RunRecord`` and on the SSE ``status`` frame so reload and
    live surfaces agree.
    """

    run_id: str
    question: str
    status: Literal["done", "refuse", "step_cap"]
    status_reason: str = ""
    answer: str = ""
    citations: list[CitationPayload] = Field(default_factory=list)
    thinking_text: str = ""
    tool_summaries: list[str] = Field(default_factory=list)
    assistant_message: str = ""
    created_at: str


class ThreadDetailResponse(BaseModel):
    id: str
    title: str
    created_at: str
    turns: list[TurnRow]


# ----- helpers -----


def _derive_title(first_message: str | None) -> str:
    """Build the thread title from a first message.

    Spec: "first line, stripped, max 50 chars + ellipsis on
    overflow". When the caller passes ``None`` or an empty string
    we fall back to ``_DEFAULT_TITLE``.
    """
    if not first_message:
        return _DEFAULT_TITLE
    first_line = first_message.splitlines()[0].strip()
    if not first_line:
        return _DEFAULT_TITLE
    if len(first_line) <= _TITLE_MAX_CHARS:
        return first_line
    return first_line[:_TITLE_MAX_CHARS] + "…"


def _serialize_thread(thread: ThreadRecord) -> dict[str, str]:
    return {
        "id": thread.id,
        "title": thread.title,
        "created_at": thread.created_at.isoformat(),
    }


def _serialize_run(run: RunRecord) -> dict[str, Any]:
    """Translate a ``RunRecord`` into the turn-table JSON shape."""
    return {
        "run_id": run.id,
        "question": run.question,
        "status": run.status,
        "status_reason": run.status_reason,
        "answer": run.answer,
        "citations": list(run.citations),
        "thinking_text": run.thinking_text,
        "tool_summaries": list(run.tool_summaries),
        "assistant_message": run.assistant_message,
        "created_at": run.created_at.isoformat(),
    }


def _format_sse(event: str, data: Any) -> str:
    """Format a single ``text/event-stream`` frame.

    Frames are terminated by a blank line. JSON values that
    contain newlines would split across multiple ``data:`` lines
    per the SSE spec; our payloads (``status`` / ``citations`` /
    ``answer``) are newline-free by construction (no embedded
    tool JSON yet — T3 will add streaming deltas).
    """
    encoded = json.dumps(data, default=str, ensure_ascii=False)
    return f"event: {event}\ndata: {encoded}\n\n"


# ----- route factory -----


def build_router(*, get_graph, get_thread_store) -> APIRouter:
    """Build the workspace APIRouter.

    The two dependencies are passed in so the router can be wired
    against any FastAPI app (the prod app in
    ``fin.host.http``, and any test app).
    """
    router = APIRouter(prefix="/api")

    @router.get("/threads")
    def list_threads(store: ThreadStore = Depends(get_thread_store)) -> list[dict]:
        return [_serialize_thread(t) for t in store.list_threads()]

    @router.post("/threads", response_model=CreateThreadResponse, status_code=200)
    def create_thread(
        body: CreateThreadRequest,
        store: ThreadStore = Depends(get_thread_store),
    ) -> dict:
        # WHY the route does NOT mint the id: the store owns id
        # allocation (uuid4 inside ``create_thread``), matching the
        # checkpointer's own auto-id pattern and keeping the HTTP
        # layer free of persistence concerns. ``record.id`` is
        # exposed on the response so the client can navigate to
        # the new thread.
        title = _derive_title(body.first_message)
        record = store.create_thread(title=title)
        return _serialize_thread(record)

    @router.get("/threads/{thread_id}", response_model=ThreadDetailResponse)
    def get_thread(
        thread_id: str,
        store: ThreadStore = Depends(get_thread_store),
    ) -> dict:
        thread = store.get_thread(thread_id)
        if thread is None:
            return JSONResponse(
                status_code=404, content={"error": "thread_not_found"}
            )
        runs = store.list_runs(thread_id)
        return {
            **_serialize_thread(thread),
            "turns": [_serialize_run(r) for r in runs],
        }

    @router.delete("/threads/{thread_id}")
    def delete_thread(
        thread_id: str,
        graph: Any = Depends(get_graph),
        store: ThreadStore = Depends(get_thread_store),
    ) -> dict:
        existing = store.get_thread(thread_id)
        if existing is None:
            return JSONResponse(
                status_code=404, content={"error": "thread_not_found"}
            )
        # Metadata first, then checkpointer. If the checkpointer
        # delete raises we keep the metadata gone - that's the
        # user-visible intent, and the next ``GET`` will not
        # resurrect the turn table.
        store.delete_thread(thread_id)

        # WHY the narrow excepts: the previous ``except Exception:
        # pass`` swallowed EVERY failure mode — including a saver
        # outage that leaves orphaned checkpoint tuples behind —
        # with no log trail. Now:
        #
        # * ``AttributeError`` = the graph proxy has neither a
        #   ``delete_thread`` seam nor a reachable checkpointer.
        #   That is a wiring bug, not a runtime failure; warn and
        #   still return 200 (metadata row is already gone, which
        #   is what the user asked for).
        # * any OTHER exception (saver outage, DB down) means the
        #   checkpoint tuples may STILL EXIST while metadata is
        #   gone — an inconsistent state the operator must see.
        #   Log with traceback AND surface 500 so a silent orphan
        #   cannot hide behind a green UI.
        try:
            graph.delete_thread(thread_id)
        except AttributeError:
            logger.warning(
                "delete_thread: no checkpointer on graph for thread_id=%s",
                thread_id,
            )
        except Exception:
            logger.exception(
                "delete_thread: saver failed for thread_id=%s", thread_id
            )
            raise HTTPException(status_code=500, detail="delete failed") from None
        return {"ok": True}

    @router.post("/threads/{thread_id}/messages")
    def send_message(
        thread_id: str,
        body: SendMessageRequest,
        graph: Any = Depends(get_graph),
        store: ThreadStore = Depends(get_thread_store),
    ) -> Response:
        """Run one v1 host-graph ``invoke`` on the thread.

        T1 contract: sync ``invoke`` wrapped in a small SSE
        generator that emits three terminal events in the fixed
        order ``status`` -> ``answer`` -> ``citations``
        (citations terminal per spec line 30). T3 will replace
        the body of this generator with ``model.stream()``
        deltas.

        Concurrency: ``graph.invoke`` runs SYNCHRONOUSLY before
        ``StreamingResponse`` is returned, so a
        ``ConcurrentRunError`` becomes a plain 409 BEFORE any
        ``text/event-stream`` headers are flushed.
        """
        # Empty question is a no-op per spec (composer guard):
        # 204 with no body, no graph invoke, no SSE.
        if not body.question or not body.question.strip():
            return Response(status_code=204)

        # Unknown thread -> 404 BEFORE any graph invoke (the graph
        # would happily create state for a new thread_id).
        thread = store.get_thread(thread_id)
        if thread is None:
            return JSONResponse(
                status_code=404, content={"error": "thread_not_found"}
            )

        # T2: title update on the first real send. The T1 path
        # set the title from ``first_message`` at create time;
        # when the user created the thread empty (corpus-card
        # click) the title was the default placeholder. We
        # rewrite it from the question here, using the SAME
        # first-line / 50-char / ellipsis rule ``create_thread``
        # uses. Only when the current title is the default
        # placeholder — a real first_message would have set a
        # non-default title and we never overwrite user intent.
        #
        # Done BEFORE the graph invoke: if the run raises a
        # ``ConcurrentRunError`` (409) the title still updates
        # — the user's question is real intent, the 409 just
        # means another run is in flight.
        if thread.title == _DEFAULT_TITLE:
            derived = _derive_title(body.question)
            if derived != _DEFAULT_TITLE:
                try:
                    thread = store.update_thread_title(
                        thread_id, title=derived
                    )
                except ThreadNotFoundError:
                    # Race: thread was deleted between the
                    # 404-check above and this update. Surface
                    # 404 - same shape as the unknown-thread
                    # guard above. Sidebar stays clean (the
                    # deleted thread was never on it).
                    return JSONResponse(
                        status_code=404,
                        content={"error": "thread_not_found"},
                    )

        # Mint the run_id up front so the SSE ``status`` event can
        # carry it before any graph work happens. The same id is
        # persisted on the RunRecord, so the optimistic turn the
        # client renders matches the stored turn row exactly.
        run_id = str(uuid.uuid4())

        # Sync invoke. Any ``ConcurrentRunError`` raised here
        # propagates to the FastAPI exception handler as a 409 -
        # NO StreamingResponse headers are sent yet, so the 409
        # body's content-type is ``application/json`` and contains
        # no event-stream frames.
        #
        # T2: the input state ALWAYS passes ``EVIDENCE_RESET`` +
        # ``repair_count=0`` + ``step_count=0``. The reducer
        # clears ``evidence`` (impossible with ``operator.add +
        # []``); the explicit zero counts override any prior
        # run's checkpointed values so a follow-up post starts
        # a fresh run, not a continuation. ``messages`` keeps
        # accumulating via ``add_messages`` because we DO NOT
        # reset that key; the helper clears the six run-scoped
        # fields per ADR 0016 / spec line 34.
        result = graph.invoke(
            reset_input_state(body.question),
            config={"configurable": {"thread_id": thread_id}},
        )
        payload = _run_to_response_payload(result)

        # Persist the run record so the GET turn table surfaces
        # it without re-invoking the graph. T1 only fills the
        # basics; T3 will populate thinking_text / tool_summaries.
        try:
            store.append_run(
                RunRecord(
                    id=run_id,
                    thread_id=thread_id,
                    question=body.question,
                    status=payload["status"],
                    status_reason=payload["reason"],
                    answer=payload["answer"],
                    citations=payload["citations"],
                    thinking_text="",
                    tool_summaries=[],
                    assistant_message=payload["assistant_message"],
                )
            )
        except ThreadNotFoundError:
            # The thread was deleted between the 404-check above
            # and this append. The user-visible state (deleted
            # thread) wins; do NOT resurrect a metadata row by
            # failing loudly after the fact.
            return JSONResponse(
                status_code=404, content={"error": "thread_not_found"}
            )

        # Pre-compute the SSE frames. Wire order per spec line 30:
        # ``status`` -> ``answer`` -> ``citations`` (citations is
        # TERMINAL so the client can close its reader once it sees
        # them). Both done and refuse/step_cap emit all three
        # frames; refuse/step_cap carry empty answer + [].
        #
        # T2: the ``status`` frame also carries ``assistant_message``
        # (ordinary-person copy on refuse/step_cap, empty on done)
        # so the SPA can render the copy before the answer bubble
        # appears. The same string is persisted on the RunRecord
        # above so reload stays consistent.
        frames: list[str] = [
            _format_sse(
                "status",
                {
                    "run_id": run_id,
                    "status": payload["status"],
                    "reason": payload["reason"] or None,
                    "assistant_message": payload["assistant_message"],
                },
            ),
            _format_sse("answer", {"text": payload["answer"]}),
            _format_sse("citations", payload["citations"]),
        ]

        def event_stream() -> Iterator[str]:
            yield from frames

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
        )

    @router.get("/corpus")
    def corpus() -> dict:
        """List the frozen 12 filings from the real catalog."""
        return list_filings()

    return router


# ----- mount helper -----


def mount(app: FastAPI, *, get_graph, get_thread_store) -> None:
    """Mount the workspace routes onto an existing FastAPI app."""
    app.include_router(
        build_router(get_graph=get_graph, get_thread_store=get_thread_store)
    )


__all__ = [
    "build_router",
    "mount",
]
