"""Hand-written spans over the execution seams (ADR 0102).

WHY a module of its own rather than a block inside each seam: the set of
spans is closed by contract, so the correlation, the attribute allowlist
and the emission path are ONE definition all five seams share. A seam
that assembled its own span could drift from the contract without
anything noticing.

WHERE ``segment_seq`` comes from: it is a durable receipt column that
advances in the same transaction as the accepted reply that resumes a
clarification. A graph node holds a run config and checkpointed state,
not the receipt row, so the owner of a segment PUBLISHES the identity
here for the duration of that segment (:func:`publish_segment`) and the
seams read it from the call they are already in. Until it is published
a seam emits NOTHING: a span stamped with a guessed segment number
would sit in the wrong segment forever, which is worse than absent
evidence.

The published segment also owns the segment ROOT span — the parent every
seam span hangs off, and the sibling of the receipt projection. The root
is exported as a span so that no span in the trace references a parent
that was never sent.

ONE SPAN PER SEAM PER SEGMENT: a seam id derives from (thread_id,
turn_id, segment_seq) and the seam's own name, with no invocation
counter — so a second entry into the same seam inside one segment would
mint a SECOND span carrying the FIRST one's id. The segment therefore
keeps ONE span per seam, merging the later entry into it, and exports
them all once when the segment closes. The receipt layer stays the
accounting authority; a span is a best-effort observation, not a count.

NO EXPORTER, NO WORK: a span is only built when the process configured a
span processor to receive it. With none — the default, which selects no
exporter — a seam does no I/O and builds nothing.

App lifetimes may bind a projector-owned sink; standalone callers can
still use the configured global provider.

Export is diagnostics. A span that cannot be built or handed over is
dropped; nothing here may raise into a run.
"""

from __future__ import annotations

import contextvars
import functools
import inspect
import threading
from collections.abc import Generator, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Final, TypeVar, cast

from opentelemetry import trace as trace_api
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.trace import SpanKind

from fin.host.receipt_export import (
    SERVICE_NAME,
    _time_ns,
    build_seam_span,
    segment_contexts,
)
from fin.host.turn_receipt_store import TOKEN_USAGE_KEYS

#: The name of the segment ROOT span. It is not one of the five
#: instrumented seams — it is the parent every one of them hangs off.
SEGMENT_ROOT_SPAN_NAME: Final[str] = "fin.turn.segment"

#: The attribute names a SEAM span may carry, beyond the correlation
#: triple every span carries.
#:
#: A closed literal rather than a filter over whatever a node happens to
#: hold: a name the contract does not list is refused here until the
#: contract admits it, which is what keeps prompts, answers, source
#: labels and ids, raw tool arguments and raw exception text out.
#:
#: Absent from it on purpose: the receipt-projection fields
#: (``fin.receipt.*``, ``fin.metrics.*``, ``fin.metrics.<stage>.*``),
#: which describe the receipt rather than one seam's observation and are
#: exported by the ``fin.turn.receipt`` projection; and the reserved
#: retrieval rerank row, which emits nothing because the receipt carries
#: no rerank fields.
SEAM_SPAN_ATTRIBUTES: Final[frozenset[str]] = frozenset(
    {
        "fin.route.source",
        "fin.route.intent",
        "fin.grounded.decision",
        "fin.grounded.decision_parse",
        "fin.not_ready.cause",
        "fin.not_ready.stage",
        "fin.citation.verified",
        "fin.failure.kind",
        "fin.cancellation.reason",
        "fin.tokens.input",
        "fin.tokens.output",
        "fin.tokens.total",
    }
)

_TOKEN_ATTRIBUTES: Final[dict[str, str]] = dict(
    zip(
        TOKEN_USAGE_KEYS,
        ("fin.tokens.input", "fin.tokens.output", "fin.tokens.total"),
    )
)

_F = TypeVar("_F", bound=Callable[..., Any])
_T = TypeVar("_T")
_R = TypeVar("_R")


@dataclass(frozen=True, slots=True)
class SegmentIdentity:
    """The three inputs every seam span id derives from."""

    thread_id: str
    turn_id: str
    segment_seq: int = 0


@dataclass(slots=True)
class SeamSpan:
    """One seam's open span; it has no parent until the segment closes."""

    seam: str
    identity: SegmentIdentity | None
    started_at: datetime
    attributes: dict[str, str | bool | int] = field(default_factory=dict)

    def record(self, attributes: Mapping[str, Any]) -> None:
        """Keep the allowlisted attributes; drop nulls and the rest."""
        for name, value in attributes.items():
            if value is not None and name in SEAM_SPAN_ATTRIBUTES:
                self.attributes[name] = cast("str | bool | int", value)

    def merge(self, later: "SeamSpan") -> None:
        """Fold a later entry of the same seam into this one.

        A segment that entered the same seam twice contributes ONE span,
        because the seam's span id has no invocation counter. The span
        then covers the whole segment: it starts at the FIRST entry, the
        later entry's state wins, and the token counters add up because a
        segment that called the model twice spent the sum.
        """
        for name, value in later.attributes.items():
            if name in _TOKEN_ATTRIBUTES.values():
                previous = self.attributes.get(name, 0)
                if isinstance(previous, int) and isinstance(value, int):
                    self.attributes[name] = previous + value
                continue
            self.attributes[name] = value


@dataclass(slots=True)
class _SegmentScope:
    """What one published segment accumulates while it is open."""

    identity: SegmentIdentity
    started_at: datetime
    key: tuple[str, str, int, int | None]
    span_sink: Callable[[Sequence[ReadableSpan]], object] | None = None
    app_scoped: bool = False
    #: The one span of each seam, keyed by the seam's name.
    spans: dict[str, SeamSpan] = field(default_factory=dict)
    #: How many publishers currently hold this segment open.
    depth: int = 0


_ACTIVE_SCOPE: contextvars.ContextVar[_SegmentScope | None] = contextvars.ContextVar(
    "fin_active_segment", default=None
)
_ACTIVE_SEAM: contextvars.ContextVar[SeamSpan | None] = contextvars.ContextVar(
    "fin_active_seam_span", default=None
)

#: One in-flight scope per segment and app-owned sink.
_SEGMENTS: dict[tuple[str, str, int, int | None], _SegmentScope] = {}
_SEGMENTS_LOCK = threading.Lock()


def _open_segment(
    identity: SegmentIdentity,
    span_sink: Callable[[Sequence[ReadableSpan]], object] | None,
    app_scoped: bool,
) -> _SegmentScope:
    active = _ACTIVE_SCOPE.get()
    if active is not None and active.identity == identity:
        scope = active
        with _SEGMENTS_LOCK:
            if scope.span_sink is None:
                scope.span_sink = span_sink
            scope.app_scoped = scope.app_scoped or app_scoped
            scope.depth += 1
    else:
        sink_id = id(span_sink) if span_sink is not None else None
        if app_scoped and sink_id is None:
            sink_id = -1
        key = (
            identity.thread_id,
            identity.turn_id,
            identity.segment_seq,
            sink_id,
        )
        with _SEGMENTS_LOCK:
            scope = _SEGMENTS.get(key)
            if scope is None:
                scope = _SegmentScope(
                    identity=identity,
                    started_at=datetime.now(timezone.utc),
                    key=key,
                    span_sink=span_sink,
                    app_scoped=app_scoped,
                )
                _SEGMENTS[key] = scope
            scope.depth += 1
    return scope


@contextmanager
def _activate_segment(scope: _SegmentScope) -> Iterator[SegmentIdentity]:
    reset = _ACTIVE_SCOPE.set(scope)
    try:
        yield scope.identity
    finally:
        _ACTIVE_SCOPE.reset(reset)


def _close_segment(scope: _SegmentScope) -> None:
    with _SEGMENTS_LOCK:
        scope.depth -= 1
        closed = scope.depth <= 0
        if closed:
            _SEGMENTS.pop(scope.key, None)
    if closed:
        _export_segment(scope)


@contextmanager
def publish_segment(
    identity: SegmentIdentity,
    *,
    span_sink: Callable[[Sequence[ReadableSpan]], object] | None = None,
    app_scoped: bool = False,
) -> Iterator[SegmentIdentity]:
    """Own one execution segment: publish its identity and its root span.

    The root span covers the segment and carries no parent; the receipt
    projection is its sibling in the same trace. Every seam span inside
    the segment hangs off this root, so no span in the trace references
    a parent that was never exported.

    Publishing the SAME identity twice — a nested publisher, or two
    publishers that disagree about who owns the run — shares ONE scope:
    the root is exported once and each seam contributes one span.
    """
    scope = _open_segment(identity, span_sink, app_scoped)
    try:
        with _activate_segment(scope) as active_identity:
            yield active_identity
    finally:
        _close_segment(scope)


def current_segment() -> SegmentIdentity | None:
    """The segment this call is inside, or ``None`` outside one."""
    scope = _ACTIVE_SCOPE.get()
    return scope.identity if scope is not None else None


def segment_seq_of(receipts: Any, thread_id: str, turn_id: str) -> int:
    """The turn's durable segment number, or ``0`` for a turn with none.

    The receipt is the durable authority on which segment a turn is in:
    its ``segment_seq`` advances in the same transaction as the accepted
    reply that resumes a clarification, and nothing inside a segment
    changes it. A turn whose receipt does not exist yet is in its FIRST
    segment, which is segment ``0``.

    The read is best-effort: a store that cannot be read leaves the
    segment at ``0`` rather than failing the run — a span in the wrong
    segment is diagnostics, never an answer-path dependency.
    """
    reader = getattr(receipts, "read", None)
    if not callable(reader):
        return 0
    try:
        receipt = reader(thread_id, turn_id)
    except Exception:  # noqa: BLE001 - a store fault must not fail the run
        return 0
    segment_seq = getattr(receipt, "segment_seq", None)
    if isinstance(segment_seq, int) and not isinstance(segment_seq, bool):
        return max(segment_seq, 0)
    return 0


@contextmanager
def publish_turn_segment(
    receipts: Any, thread_id: str, turn_id: str
) -> Iterator[SegmentIdentity]:
    """Own the segment the named turn is in, for the duration of a run.

    The one call a run's owner needs: it reads the turn's durable
    segment number from the receipt and publishes the identity the
    seams derive their span ids from.
    """
    span_sink = getattr(receipts, "finished_span_sink", None)
    with publish_segment(
        SegmentIdentity(
            thread_id=thread_id,
            turn_id=turn_id,
            segment_seq=segment_seq_of(receipts, thread_id, turn_id),
        ),
        span_sink=span_sink,
        app_scoped=hasattr(receipts, "finished_span_sink"),
    ) as identity:
        yield identity


def stream_in_turn_segment(
    receipts: Any,
    thread_id: str,
    turn_id: str,
    events: Generator[_T, None, _R],
) -> Generator[_T, None, _R]:
    """Advance inside one segment without holding context across yields."""
    identity = SegmentIdentity(
        thread_id=thread_id,
        turn_id=turn_id,
        segment_seq=segment_seq_of(receipts, thread_id, turn_id),
    )
    scope = _open_segment(
        identity,
        getattr(receipts, "finished_span_sink", None),
        hasattr(receipts, "finished_span_sink"),
    )
    iterator = iter(events)
    try:
        while True:
            try:
                with _activate_segment(scope):
                    event = next(iterator)
            except StopIteration as stopped:
                return cast("_R", stopped.value)
            yield event
    finally:
        try:
            close = getattr(iterator, "close", None)
            if callable(close):
                with _activate_segment(scope):
                    close()
        finally:
            _close_segment(scope)


def record_seam_attributes(attributes: Mapping[str, Any]) -> None:
    """Record on the seam span this call is inside, if there is one."""
    span = _ACTIVE_SEAM.get()
    if span is not None:
        span.record(attributes)


def model_token_attributes(message: Any) -> dict[str, int]:
    """Only the provider's own token counts, and only what it supplied."""
    usage = getattr(message, "usage_metadata", None)
    if not isinstance(usage, Mapping):
        return {}
    attributes: dict[str, int] = {}
    for key, name in _TOKEN_ATTRIBUTES.items():
        value = usage.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            attributes[name] = value
    return attributes


@contextmanager
def seam_span(seam: str, identity: SegmentIdentity | None) -> Iterator[SeamSpan]:
    """One execution-seam span, ended however the body leaves it."""
    span = SeamSpan(
        seam=seam, identity=identity, started_at=datetime.now(timezone.utc)
    )
    scope = _ACTIVE_SCOPE.get()
    reset = _ACTIVE_SEAM.set(span)
    try:
        yield span
    finally:
        _ACTIVE_SEAM.reset(reset)
        if scope is not None and identity == scope.identity:
            # ONE span per seam per segment: the seam's span id carries no
            # invocation counter, so a second entry would mint a second
            # span under the first one's id. The entries merge instead.
            existing = scope.spans.get(seam)
            if existing is None:
                scope.spans[seam] = span
            else:
                existing.merge(span)


def traced_seam(
    seam: str, identity_of: Callable[[Any], SegmentIdentity | None]
) -> Callable[[_F], _F]:
    """Instrument one node or tool as a hand-written seam span.

    A decorator rather than an inline ``with`` inside the body: the seam
    bodies return from several places, and wrapping them without
    reindenting them is what makes the span impossible to leak past an
    early return or an exception. ``functools.wraps`` preserves the
    wrapped function's signature, name and docstring, so LangGraph's node
    introspection and the model-visible tool description are unchanged.
    """

    def decorate(func: _F) -> _F:
        call_signature = inspect.signature(cast("Callable[..., Any]", func))

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            identity = identity_of(_config_argument(call_signature, args, kwargs))
            with seam_span(seam, identity):
                return func(*args, **kwargs)

        return cast("_F", wrapper)

    return decorate


def _config_argument(
    signature: inspect.Signature, args: tuple[Any, ...], kwargs: Mapping[str, Any]
) -> Any:
    """The run config this call was handed, as a positional or a kwarg.

    A node receives it positionally and the ``retrieve`` tool receives it
    as an injected kwarg, so the name is BOUND rather than read out of
    ``kwargs``: a bound call and a named call are the same call.
    """
    try:
        return signature.bind_partial(*args, **kwargs).arguments.get("config")
    except TypeError:
        return None


def _export_segment(scope: _SegmentScope) -> None:
    """Export the segment's root and the one span of each of its seams."""
    if scope.span_sink is None and (
        scope.app_scoped or not active_span_processors()
    ):
        # No exporter selected: nothing to hand a span to, so nothing is
        # built. A segment with no exporter costs one lookup.
        return
    identity = scope.identity
    try:
        spans = [_segment_root_span(identity, scope.started_at)]
        for span in scope.spans.values():
            built = _build_seam_span(span)
            if built is not None:
                spans.append(built)
        if scope.span_sink is not None:
            scope.span_sink(spans)
        else:
            for span in spans:
                _export_span(span)
    except Exception:  # noqa: BLE001 - export is diagnostics, never a run failure
        pass


def _build_seam_span(span: SeamSpan) -> ReadableSpan | None:
    """The finished seam span, parented to its segment root."""
    identity = span.identity
    if identity is None:
        return None
    return build_seam_span(
        segment_contexts(identity.thread_id, identity.turn_id, identity.segment_seq),
        thread_id=identity.thread_id,
        turn_id=identity.turn_id,
        segment_seq=identity.segment_seq,
        seam=span.seam,
        started_at=span.started_at,
        ended_at=datetime.now(timezone.utc),
        attributes=span.attributes,
    )


def _segment_root_span(identity: SegmentIdentity, started_at: datetime) -> ReadableSpan:
    """The segment's root span: no parent, and the seams' parent."""
    contexts = segment_contexts(
        identity.thread_id, identity.turn_id, identity.segment_seq
    )
    return ReadableSpan(
        name=SEGMENT_ROOT_SPAN_NAME,
        context=contexts.segment_root,
        parent=None,
        resource=Resource.create({"service.name": SERVICE_NAME}),
        attributes={
            "fin.thread_id": identity.thread_id,
            "fin.turn_id": identity.turn_id,
            "fin.segment_seq": identity.segment_seq,
        },
        kind=SpanKind.INTERNAL,
        start_time=_time_ns(started_at),
        end_time=_time_ns(datetime.now(timezone.utc)),
    )


def _export_span(span: ReadableSpan) -> None:
    """Hand the finished span to the processors this process configured."""
    for processor in active_span_processors():
        processor.on_end(span)


def active_span_processors() -> tuple[Any, ...]:
    """The span processors of the trace provider this process configured.

    WHY the provider's processors rather than ``tracer.start_span``: the
    SDK mints a random span id, and a seam span id derives from
    (thread_id, turn_id, segment_seq) — so the finished span is handed to
    the configured processors directly, the way the SDK hands its own.

    With no provider configured, or a provider carrying no processors (the
    default, which selects no exporter), this is empty and a seam costs
    one attribute lookup and no I/O.
    """
    active = getattr(trace_api.get_tracer_provider(), "_active_span_processor", None)
    return tuple(getattr(active, "_span_processors", ()))


__all__ = [
    "SEAM_SPAN_ATTRIBUTES",
    "SEGMENT_ROOT_SPAN_NAME",
    "SeamSpan",
    "SegmentIdentity",
    "active_span_processors",
    "current_segment",
    "model_token_attributes",
    "publish_segment",
    "publish_turn_segment",
    "record_seam_attributes",
    "seam_span",
    "segment_seq_of",
    "stream_in_turn_segment",
    "traced_seam",
]
