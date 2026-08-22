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
                                          ``text/event-stream``. The
                                          terminal events keep the
                                          T1 fixed order ``status``
                                          -> ``answer`` ->
                                          ``citations`` (citations
                                          is terminal per spec line
                                          30). T3 adds the
                                          streaming frames before
                                          them: ``thinking`` /
                                          ``tool_call`` /
                                          ``tool_result`` so the SPA
                                          renders the tools region
                                          while the run is live.
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
T3 keeps ``graph.invoke`` as the entry (the per-token deltas are
consumed inside ``model_node``, spec line 37); the streaming
frames are synthesized from the final state. The "decide
contention before headers" invariant is preserved.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any, Iterator, Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from fastapi.responses import JSONResponse, Response, StreamingResponse
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    ToolMessage,
)
from pydantic import BaseModel, Field

from fin.corpus.catalog import list_filings
from fin.host.citation_gate import strip_hidden_bounce_marker
from fin.host.graph import ConcurrentRunError
from fin.host.run_payload import RunResponsePayload, _run_to_response_payload
from fin.host.stream_map import format_tool_summary, strip_thinking_from_answer
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
    # T3 (spec line 27): operator's "Think step by step" toggle.
    # ``True`` (or omitted, the default) routes to ``adaptive`` thinking;
    # ``False`` flips the model to ``disabled`` for this run. The route
    # mutates the compiled ``ChatOpenAI.model_kwargs`` for the duration
    # of the invoke and restores the original value after the run so
    # concurrent / follow-up sends on other threads see the default.
    # The proxy keeps ``reasoning_split=True`` regardless — only the
    # thinking TYPE toggles; reasoning_details round-trip remains on.
    thinking: bool = True


class CitationPayload(BaseModel):
    document_id: str
    version: str
    page: int
    quote: str


class ToolSummaryPayload(BaseModel):
    """One human-readable tool-call row for the tools region.

    ``summary`` is the formatted one-liner (``"n filings"``,
    ``"query' -> 8 hits -> AAPL-2022Q3 p.4"``, ``"<doc> p.N"``);
    raw ToolMessage JSON never reaches the wire (spec line 32).
    """

    name: str
    summary: str
    tool_call_id: str


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
    # T3 (spec line 32): one dict per tool call observed this run;
    # keys are exactly ``name`` / ``summary`` / ``tool_call_id``.
    tool_summaries: list[ToolSummaryPayload] = Field(default_factory=list)
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
        # T3 shape: one {name, summary, tool_call_id} dict per
        # tool call; deep-copied so a caller cannot mutate the
        # record's list through the serialized view.
        "tool_summaries": [dict(entry) for entry in run.tool_summaries],
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


# ----- SSE frame builders (T3) -----


def _thinking_prose_for_message(message: BaseMessage) -> str:
    """Extract the thinking prose for one ``AIMessage``.

    Sources (in priority order):
    1. ``additional_kwargs.reasoning_details`` list (the
       ``reasoning_split=true`` shape the MiniMax proxy emits -
       ADR 0022). Each dict's ``text`` field is concatenated.
    2. Inline ``<thinking>...</thinking>`` block in
       ``message.content`` (the proxy's fallback shape).

    Returns an empty string if neither channel carries prose so
    the caller can skip the SSE frame entirely.
    """
    if not isinstance(message, AIMessage):
        return ""
    pieces: list[str] = []
    details = message.additional_kwargs.get("reasoning_details")
    if isinstance(details, list):
        for entry in details:
            if isinstance(entry, dict):
                text = entry.get("text")
                if isinstance(text, str) and text:
                    pieces.append(text)
    content = message.content
    if isinstance(content, str):
        match_iter = re.finditer(
            r"<thinking\b[^>]*>(.*?)</thinking\s*>",
            content,
            re.IGNORECASE | re.DOTALL,
        )
        for match in match_iter:
            captured = match.group(1).strip()
            if captured:
                pieces.append(captured)
    raw = "\n".join(pieces)
    # T3 fix (code-review #2): the live SSE ``thinking`` frame
    # previously echoed the gate's hidden template verbatim
    # because the GET path (``run_payload``) strips the marker
    # but this synthesis site did not. ADR 0021 forbids the marker
    # on every user surface (SSE / GET / SPA); the strip here
    # closes the asymmetry.
    return strip_hidden_bounce_marker(raw)


def _frames_for_message(message: BaseMessage) -> Iterator[str]:
    """Yield per-message SSE frames during graph.stream.

    T3 fix (code-review #1 + #2): emit per-message frames as the
    stream produces them, so the SPA sees ``thinking`` /
    ``tool_call`` / ``tool_result`` events live rather than after
    the run completes. Hidden bounce marker is stripped at the
    synthesis site (ADR 0021) so the wire shape matches the GET
    surface.
    """
    if isinstance(message, AIMessage):
        # Thinking frame (SC60 binding: only emitted if the
        # message carries reasoning prose).
        thinking = _thinking_prose_for_message(message)
        if thinking:
            yield _format_sse("thinking", {"text": thinking})
        # Tool-call frames (one per parsed tool_call).
        for call in message.tool_calls or ():
            args = call.get("args") or {}
            if isinstance(args, str):
                # LangChain sometimes leaves args as a JSON string
                # when the model streamed them; parse defensively.
                try:
                    args = json.loads(args)
                except (TypeError, ValueError):
                    args = {}
            yield _format_sse(
                "tool_call",
                {
                    "name": call.get("name", ""),
                    "args": args,
                    "tool_call_id": call.get("id", ""),
                },
            )
        return
    if isinstance(message, ToolMessage):
        tool_name = getattr(message, "name", "") or ""
        tool_call_id = getattr(message, "tool_call_id", "") or ""
        raw_content = message.content
        parsed: Any = raw_content
        if isinstance(raw_content, str):
            try:
                parsed = json.loads(raw_content)
            except (TypeError, ValueError):
                parsed = raw_content
        # Strip the marker from the human-readable summary so a
        # bounce template embedded in tool output never escapes
        # to the SPA (ADR 0021).
        yield _format_sse(
            "tool_result",
            {
                "name": tool_name,
                "summary": strip_hidden_bounce_marker(
                    format_tool_summary(tool_name, result=parsed)
                ),
                "tool_call_id": tool_call_id,
            },
        )
        return
    # HumanMessage / SystemMessage / others: no per-message frame
    # emitted (the user side already has its own UI; SystemMessages
    # are the gate's hidden bounce templates and MUST NOT appear
    # on the wire per ADR 0021).


def _build_sse_frames(
    *,
    result: dict[str, Any],
    run_id: str,
    payload: RunResponsePayload,
) -> Iterator[str]:
    """Yield the ordered SSE frames for one run.

    Frame order:
        1. ``thinking``     - one frame per AIMessage with thinking.
        2. ``tool_call``    - one frame per AIMessage tool call.
        3. ``tool_result``  - one frame per ToolMessage on the list.
        4. ``status``       - terminal decision (status / reason /
                              assistant_message / run_id).
        5. ``answer``       - terminal answer prose.
        6. ``citations``    - terminal citations list (TERMINAL per
                              spec line 30 so the SPA closes the
                              reader).

    The hidden bounce marker is stripped from every prose field
    so the wire shape never leaks the gate's internal template
    (ADR 0021). ``answer`` is also re-stripped for thinking
    prose here (SC60 binding) even though ``payload["answer"]``
    was already stripped - a second pass is cheap and removes
    any ``<thinking>`` block that landed in the answer field
    after ``_run_to_response_payload`` ran.
    """
    messages: list[BaseMessage] = list(result.get("messages", []) or [])

    # Per-AIMessage streaming frames: thinking first, then
    # tool_call(s). An AIMessage with both reasoning_details and
    # tool_calls emits one ``thinking`` and one ``tool_call`` per
    # call - in that order - so the SPA renders the scratchpad
    # before the model commits to a tool.
    for message in messages:
        if isinstance(message, AIMessage):
            thinking_text = _thinking_prose_for_message(message)
            if thinking_text:
                yield _format_sse(
                    "thinking",
                    {"text": thinking_text},
                )
            for call in message.tool_calls or []:
                args = call.get("args") or {}
                # ``args`` is a dict in langchain-core 1.6 (already
                # parsed from the JSON the model emitted); pass it
                # through unchanged.
                yield _format_sse(
                    "tool_call",
                    {
                        "name": call.get("name", ""),
                        "args": args,
                        "tool_call_id": call.get("id", ""),
                    },
                )

    # Per-ToolMessage result frames in the order they landed on the
    # message list. T3 fix (code-review #3): the wire shape is the
    # same human-readable shape as the ``tool_call`` frame — name,
    # summary, tool_call_id — NOT the parsed raw ToolMessage
    # payload. Spec line 32: "Events carry name + short summary,
    # not raw ToolMessage JSON." The raw ``ToolMessage.content``
    # can carry multi-KB page / chunk texts; routing those over
    # SSE would leak them to the SPA and any LAN-reachable
    # operator.
    for message in messages:
        if not isinstance(message, ToolMessage):
            continue
        tool_name = getattr(message, "name", "") or ""
        tool_call_id = getattr(message, "tool_call_id", "") or ""
        # Parse just enough to feed the formatter; do NOT include
        # the parsed result in the frame payload.
        raw_content = message.content
        parsed_for_summary: Any = raw_content
        if isinstance(raw_content, str):
            try:
                parsed_for_summary = json.loads(raw_content)
            except (TypeError, ValueError):
                parsed_for_summary = raw_content
        summary = format_tool_summary(tool_name, result=parsed_for_summary)
        yield _format_sse(
            "tool_result",
            {
                "name": tool_name,
                "summary": summary,
                "tool_call_id": tool_call_id,
            },
        )

    # Terminal frames. Order is fixed per spec line 30:
    # ``status`` -> ``answer`` -> ``citations``. Both done and
    # refuse/step_cap emit all three frames; refuse/step_cap
    # carry empty answer + [].
    safe_answer = strip_thinking_from_answer(
        payload["answer"] if payload["status"] == "done" else ""
    )
    yield _format_sse(
        "status",
        {
            "run_id": run_id,
            "status": payload["status"],
            "reason": payload["reason"] or None,
            "assistant_message": payload["assistant_message"],
        },
    )
    yield _format_sse("answer", {"text": safe_answer})
    yield _format_sse("citations", payload["citations"])


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

        T3 wire shape: the SSE generator emits an ORDERED stream
        of frames:

        * ``thinking``     - one per ``AIMessage`` whose
          ``additional_kwargs.reasoning_details`` or inline
          ``<thinking>`` block carried thinking prose. ``text``
          is the chunk prose; the SPA collapses / line-clamps.
        * ``tool_call``    - one per tool call observed on any
          ``AIMessage``. Carries ``name`` / ``args`` /
          ``tool_call_id`` so the SPA can render a running
          indicator while the tool runs.
        * ``tool_result``  - one per ``ToolMessage`` on the
          message list. Carries ``tool_call_id`` /
          ``name`` / ``result`` (JSON-decoded).
        * ``status``       - terminal decision (status / reason /
          assistant_message / run_id). Emitted AFTER the in-flight
          thinking / tool frames so the T1 wire order
          ``status -> answer -> citations`` is preserved (and
          ``status`` precedes ``answer``).
        * ``answer``       - terminal answer text (``{"text":
          <full>}``). Empty on refuse / step_cap.
        * ``citations``    - terminal citations list (TERMINAL
          per spec line 30).

        Concurrency: ``graph.invoke`` runs SYNCHRONOUSLY before
        ``StreamingResponse`` is returned, so a
        ``ConcurrentRunError`` becomes a plain 409 BEFORE any
        ``text/event-stream`` headers are flushed. The per-token
        deltas are consumed inside ``model_node`` to populate
        ``thinking_text`` / ``additional_kwargs``; the wire
        shape is what the SPA reads, not what arrives per-byte.
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

        # T3 fix (preserves T1's 409 contract under streaming): the
        # workspace route previously called ``graph.invoke``
        # synchronously BEFORE returning the StreamingResponse, so a
        # second concurrent request hit ``_LockedGraph.invoke``'s
        # lock acquisition and raised ``ConcurrentRunError`` BEFORE
        # any SSE headers went out. With the T3 streaming refactor
        # the actual stream happens INSIDE the StreamingResponse
        # generator; if the lock check ran there, the exception
        # would surface mid-body and FastAPI would emit a 500 once
        # headers were already flushed. Probe the lock here, raise
        # ``ConcurrentRunError`` synchronously, and let the existing
        # exception handler translate to 409 with
        # ``application/json`` (spec line 26).
        if hasattr(graph, "is_locked") and graph.is_locked(thread_id):
            raise ConcurrentRunError(
                f"another run is already in flight on thread_id={thread_id!r}"
            )

        # T3 (H4 part 2): apply the operator's thinking preference to
        # the model for the duration of THIS invoke. Mutating the
        # compiled ``ChatOpenAI.model_kwargs`` is the documented seam
        # for per-call ``extra_body`` overrides; restore on the way
        # out so a follow-up send on the same process sees the
        # default. Only the ``thinking.type`` field toggles —
        # ``reasoning_split`` stays ``True`` for round-trip continuity.
        thinking_type = "adaptive" if body.thinking else "disabled"
        model_obj = graph._graph.builder.nodes.get("model").runnable  # type: ignore[attr-defined]
        # Fallback: most LangGraph compiled graphs expose the bound
        # model via the ``builder``; the proxy is the public handle
        # here. If the layout differs, skip the mutation and rely on
        # the boot default.
        prior_thinking: dict | None = None
        if hasattr(model_obj, "model_kwargs"):
            prior_thinking = model_obj.model_kwargs.get("extra_body", {}).get("thinking")
            model_obj.model_kwargs.setdefault("extra_body", {}).setdefault("thinking", {})["type"] = thinking_type

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
        #
        # T3 fix (code-review #1): use ``graph.stream`` (via the
        # route's ``event_stream`` generator) instead of
        # ``graph.invoke`` so the SSE frames flow as nodes
        # complete (model -> tools -> citation_gate) instead of
        # the route buffering the entire sequence and flushing
        # once. The route mints the run_id and yields the SSE
        # generator; ``event_stream`` drives ``graph.stream``
        # internally, persists the RunRecord after the stream
        # completes, and emits the terminal frames.
        config = {"configurable": {"thread_id": thread_id}}
        thinking_type = "adaptive" if body.thinking else "disabled"
        # Resolve the compiled model's ``model_kwargs`` for the
        # thinking-type swap. ``graph._graph.builder.nodes`` is the
        # LangGraph compiled-graph internal seam; if the layout
        # changes (e.g. ``StateGraph`` schema), the swap is a
        # no-op and the boot default of ``adaptive`` stands.
        model_obj: Any = None
        try:
            model_obj = graph._graph.builder.nodes["model"].runnable  # type: ignore[attr-defined]
        except (AttributeError, KeyError):
            model_obj = None
        prior_thinking: dict | None = None
        if model_obj is not None and hasattr(model_obj, "model_kwargs"):
            prior_thinking = model_obj.model_kwargs.get("extra_body", {}).get("thinking")
            model_obj.model_kwargs.setdefault("extra_body", {}).setdefault("thinking", {})["type"] = thinking_type

        def event_stream() -> Iterator[str]:
            """Yield SSE frames as the graph streams node-by-node.

            T3 fix (code-review #1): the previous implementation
            buffered all frames until the synchronous
            ``graph.invoke`` returned. We now drive
            ``graph.stream(stream_mode="values")`` and yield each
            node's frames as it completes, so the SPA sees the
            tools region fill in while the run is still live.

            Run-id and SSE ``status`` frame consistency: ``run_id``
            is minted in the outer scope; the terminal ``status``
            frame uses it. ``RunRecord`` persistence is best-effort
            here — if the thread was deleted mid-stream the
            persistence is skipped rather than resurrected (a row
            for a deleted thread is never wanted).
            """
            seen_message_ids: set[str] = set()
            final_state: dict[str, Any] | None = None
            try:
                for node_state in graph.stream(
                    reset_input_state(body.question),
                    config=config,
                    stream_mode="values",
                ):
                    final_state = node_state
                    # Emit per-message frames for any message added
                    # by THIS node. Filter to messages we have not
                    # already emitted so a follow-up run does NOT
                    # replay run-1's frames onto run-2's stream
                    # (spec line 34).
                    for message in node_state.get("messages", []):
                        mid = getattr(message, "id", None)
                        if mid and mid in seen_message_ids:
                            continue
                        if mid:
                            seen_message_ids.add(mid)
                        yield from _frames_for_message(message)
            finally:
                # Restore the prior thinking type so the next
                # request starts from the default unless the
                # operator toggles again.
                if prior_thinking is not None and hasattr(model_obj, "model_kwargs"):
                    model_obj.model_kwargs["extra_body"]["thinking"] = prior_thinking

            if final_state is None:
                return
            payload = _run_to_response_payload(final_state)
            # Persist the run record so the GET turn table surfaces
            # it without re-invoking the graph.
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
                        thinking_text=payload["thinking_text"],
                        tool_summaries=payload["tool_summaries"],
                        assistant_message=payload["assistant_message"],
                    )
                )
            except ThreadNotFoundError:
                # The thread was deleted mid-stream; do NOT
                # resurrect metadata. Yield a single error frame
                # so the SPA surfaces the state, then stop.
                yield _format_sse("status", {
                    "run_id": run_id,
                    "status": "refuse",
                    "reason": "thread_not_found",
                    "assistant_message": "Thread was deleted while the run was in flight.",
                })
                return
            # Terminal frames per spec line 30: status -> answer -> citations.
            yield _format_sse(
                "status",
                {
                    "run_id": run_id,
                    "status": payload["status"],
                    "reason": payload["reason"] or None,
                    "assistant_message": payload["assistant_message"],
                    "thinking_type": thinking_type,
                },
            )
            yield _format_sse(
                "answer",
                {"text": strip_thinking_from_answer(
                    payload["answer"] if payload["status"] == "done" else ""
                )},
            )
            citations_payload = []
            for c in payload["citations"]:
                # ``payload["citations"]`` may already be a list of
                # dicts (the run_payload helper's wire shape) — pass
                # through. Older callers / test fixtures may carry
                # ``Citation`` dataclasses; normalise either to the
                # CitationPayload dict shape on the wire.
                if isinstance(c, dict):
                    citations_payload.append(
                        CitationPayload(
                            document_id=c["document_id"],
                            version=c["version"],
                            page=c["page"],
                            quote=c["quote"],
                        ).model_dump()
                    )
                else:
                    citations_payload.append(
                        CitationPayload(
                            document_id=c.document_id,
                            version=c.version,
                            page=c.page,
                            quote=c.quote,
                        ).model_dump()
                    )
            yield _format_sse("citations", citations_payload)

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
