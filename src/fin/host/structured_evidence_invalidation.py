"""EP9 scrub-before-ack and EP10 checkpoint recovery for structured evidence.

The backend retires a reference by publishing a content-free invalidation
event and Fin's job is to make its own copy of that payload disappear before
it says so. The ordering is the whole contract, and it is three steps in this
order:

1. **scrub** the stored snapshot payloads the events name, in one storage
   transaction that also advances the consumer's applied watermark;
2. **confirm** — that same transaction reads the column back, and a leftover
   matching entry is ``evidence_unavailable``, never an acknowledgement;
3. **acknowledge** — only now is ``next_checkpoint`` sent to the backend, and
   only a returned acknowledgement moves the durable resume cursor.

WHY two watermarks: a crash between steps 1 and 3 must REDELIVER, not skip.
The cursor fin RESUMES from (``acked_checkpoint``) therefore trails the cursor
whose payloads are already gone (``applied_checkpoint``) by exactly the window
the spec puts between "storage confirms the scrub" and "the acknowledgement
lands". One column cannot represent both, because it would either lose an
event or be unable to say that a scrub already happened.

Refusals are typed and distinct, never merged:

* ``malformed_evidence_contract`` — a malformed action/field: an unknown key,
  a missing ``event_id``, an unknown ``reason``, a batch past the documented
  ``limit``.
* ``unsupported_evidence_contract`` — a recognised shape at a version this
  build does not implement.
* ``retrieve_unavailable`` — the stream cannot be continued from this
  consumer's position: the backend reported ``gap``, it answered for a
  cursor other than the one requested, or the per-consumer sequence skipped a
  window. A stale position must never advance acknowledgement, so the drain
  refuses; it never resets to the start and never silently skips.
* ``evidence_unavailable`` — storage could not confirm the payloads are gone,
  so the acknowledgement would be a claim fin cannot back.

The event shape, the ``seq`` ordering, the opaque-checkpoint rule and the
two-phase watermark come from
``docs/adr/0097-ep8-invalidation-event-contract.md``: five closed fields
(``event_id`` / ``seq`` / ``evidence_ref`` / ``reason`` / ``occurred_at``), no
optional set and no free text, so a content field refuses instead of being
dropped. No ``schema_version`` travels on an event: ``invalidations`` is an
action of the existing ``iknow.evidence_result`` envelope, and that envelope
already carries the version (ADR 0097 §2.1). The batch keys are therefore the
envelope's own — the ``schema`` token and ``schema_version``, plus the
``invalidations`` success fields (``events`` / ``next_checkpoint`` / ``gap``).
``action`` is optional because no shipped IK renderer echoes it; when it is
echoed it must name this shape. An optional echoed ``checkpoint`` is accepted
only to re-check contiguity locally and must equal the cursor fin requested.

**One responsibility was added here, and it is deliberately small.** A drain
that cannot continue from its stored position closes T33's recovery gate
(``set_recovery_withheld``) before re-raising. That is the whole addition: the
decision "this consumer is mid-recovery" belongs at the one place the gap is
decided, and leaving it to a caller to remember is how a gap gets swept by a
process that already forgot it. The gate is two columns and no watermark, and
the sweep that opens it lives in
:mod:`fin.host.structured_evidence_recovery`; nothing here scrubs, revalidates,
or re-implements the two-phase machine.

This module consumes an EP8 batch and calls a transport. It names two symbols
from the retrieve seam (:data:`~fin.retrieve.evidence_route.EVIDENCE_RESULT_SCHEMA`
and its bounded-token rule) because both readers of this envelope must agree
on them and the dependency only closes in that direction; it owns no other
``fin.retrieve`` behaviour, and the fin.host <-> fin.retrieve cycle stays shut.

:func:`drain_invalidation_batch` has no production caller yet: its caller is
the IK producer, a separate repo, so the transport arrives with P3. Until then
the ``fetch`` / ``acknowledge`` callables are the seam that caller binds to.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final

from fin.host.evidence_contract import (
    EVIDENCE_SCHEMA_VERSION,
    EVIDENCE_UNAVAILABLE,
    MALFORMED_EVIDENCE_CONTRACT,
    RETRIEVE_UNAVAILABLE,
    UNSUPPORTED_EVIDENCE_CONTRACT,
    _is_integer_value,
)
from fin.host.structured_evidence_validation import _is_whole_second_timestamp
from fin.host.thread_store import (
    PersistenceUnavailableError,
    ThreadStore,
)
# The response token and the bounded-token rule are NAMED here rather than
# restated.  Both already live in the retrieve seam, and both readers of this
# envelope must agree on them; the dependency runs this way because it is the
# only one that closes — ``fin.host`` reaches ``fin.retrieve.evidence_route``,
# but the reverse is a circular import.
from fin.retrieve.evidence_route import (
    EVIDENCE_RESULT_SCHEMA,
    MAX_BINDING_TOKEN_CHARS,
    _is_bounded_token,
)

#: ADR 0097 §2.1: the closed five-field event. An additional key is a
#: contract violation, which is how the content prohibition is enforced on
#: this side. ``consumer_id`` is deliberately absent — it identifies the
#: stream, not the event, so the backend stores it and never sends it.
INVALIDATION_EVENT_FIELDS: Final[frozenset[str]] = frozenset(
    {"event_id", "seq", "evidence_ref", "reason", "occurred_at"}
)

#: ADR 0097 §2.1: the ``iknow.evidence_result`` ENVELOPE version the
#: ``invalidations`` action rides on, not a field of an event. Named from the
#: single authority rather than declared again here: the version is a property
#: of the envelope, and a second literal beside it is a number that can answer
#: this drain and the retrieve ingress differently.
INVALIDATION_EVENT_SCHEMA_VERSION: Final[int] = EVIDENCE_SCHEMA_VERSION

#: The spec's documented ``limit`` for ``invalidations`` is 1..20, so a batch
#: of 21 events is already outside the contract fin asked for.
MAX_INVALIDATION_EVENTS: Final[int] = 20

#: ADR 0097 §2.1 bounds ``evidence_ref`` at 128 characters.
MAX_EVIDENCE_REF_CHARS: Final[int] = 128

#: Bounded-token guard on the identity strings fin stores or forwards
#: (consumer id and opaque checkpoint). These are opaque and short; an
#: unbounded one is a contract violation, not a longer name. Aliases the rule
#: the retrieve seam applies to the same label rather than declaring its own,
#: so a length accepted on one side cannot be refused on the other.
MAX_INVALIDATION_TOKEN_CHARS: Final[int] = MAX_BINDING_TOKEN_CHARS

#: The two reasons ADR 0097 §2.6 names. No expiry member exists on purpose: an
#: event can never be read as an age-based collection signal.
INVALIDATION_REASONS: Final[frozenset[str]] = frozenset({"access_revoked", "purged"})

#: The action this drain implements, an action of the ``iknow.evidence_result``
#: envelope (spec ``structured-corpus-evidence.md:222``). IK does not echo it
#: on every renderer today, so it is accepted as absent; when it IS echoed it
#: must name this shape, and register §1.3 is where it starts being sent.
INVALIDATION_ACTION: Final[str] = "invalidations"

#: The closed batch key set, split the way the spec splits it: the response
#: token and its version sibling are what makes the payload the envelope fin
#: asked for (``specs/structured-corpus-evidence.md:218-219``), and ``events``
#: / ``next_checkpoint`` / ``gap`` are the ``invalidations`` success fields
#: ``:222`` lists. ``action`` is optional for the reason above and
#: ``checkpoint`` is fin's own optional echo, re-checked for contiguity.
_BATCH_REQUIRED_FIELDS: Final[frozenset[str]] = frozenset(
    {"schema", "schema_version", "events", "next_checkpoint", "gap"}
)
_BATCH_OPTIONAL_FIELDS: Final[frozenset[str]] = frozenset({"action", "checkpoint"})


class StructuredInvalidationRefusal(Exception):
    """One typed refusal from the invalidation drain.

    ``code`` is drawn from
    :data:`fin.host.evidence_contract.CONTRACT_REFUSAL_CODES`; it is raised
    rather than returned, because a refused drain has no success-shaped
    outcome to return and acknowledging an empty batch would be a lie.
    """

    __slots__ = ("code", "reason")

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


def _refuse(code: str, reason: str) -> StructuredInvalidationRefusal:
    return StructuredInvalidationRefusal(code, reason)


@dataclass(frozen=True)
class InvalidationDrainOutcome:
    """What one drain did, after the acknowledgement was accepted."""

    consumer_id: str
    events: int
    scrubbed_entries: int
    scrubbed_runs: int
    checkpoint: str
    last_seq: int


def _validated_event_id(event: Mapping[str, Any], index: int) -> None:
    """Validate the event's only identity: an opaque UUID v4.

    ``event_id`` is an idempotency key and is never ordered or compared
    (§2.2), so nothing else about it is checked beyond being one.
    """
    event_id = event["event_id"]
    if not isinstance(event_id, str):
        raise _refuse(
            MALFORMED_EVIDENCE_CONTRACT, f"invalidations[{index}]: event_id must be a string"
        )
    try:
        parsed_id = uuid.UUID(event_id)
    except ValueError as exc:
        raise _refuse(
            MALFORMED_EVIDENCE_CONTRACT,
            f"invalidations[{index}]: event_id is not a UUID",
        ) from exc
    if parsed_id.version != 4:
        raise _refuse(
            MALFORMED_EVIDENCE_CONTRACT,
            f"invalidations[{index}]: event_id is not a UUID v4",
        )


def _validated_event(event: Any, index: int) -> tuple[str, int]:
    """Validate one event; return the ``evidence_ref`` and its ``seq``."""
    if not isinstance(event, Mapping):
        raise _refuse(
            MALFORMED_EVIDENCE_CONTRACT, f"invalidations[{index}]: event is not an object"
        )
    if frozenset(event) != INVALIDATION_EVENT_FIELDS:
        raise _refuse(
            MALFORMED_EVIDENCE_CONTRACT,
            f"invalidations[{index}]: event fields must be "
            f"{sorted(INVALIDATION_EVENT_FIELDS)}, got {sorted(event)}",
        )
    _validated_event_id(event, index)
    evidence_ref = event["evidence_ref"]
    if (
        not isinstance(evidence_ref, str)
        or not evidence_ref
        or len(evidence_ref) > MAX_EVIDENCE_REF_CHARS
        or evidence_ref.strip() != evidence_ref
    ):
        raise _refuse(
            MALFORMED_EVIDENCE_CONTRACT,
            f"invalidations[{index}]: evidence_ref must be a non-empty token of at "
            f"most {MAX_EVIDENCE_REF_CHARS} characters",
        )
    if event["reason"] not in INVALIDATION_REASONS:
        raise _refuse(
            MALFORMED_EVIDENCE_CONTRACT,
            f"invalidations[{index}]: reason must be one of {sorted(INVALIDATION_REASONS)}",
        )
    if not _is_whole_second_timestamp(event["occurred_at"]):
        # Informational per D1 §2.1, but a fractional/offset instant is still
        # a field outside its declared type, and fin does not coerce.
        raise _refuse(
            MALFORMED_EVIDENCE_CONTRACT,
            f"invalidations[{index}]: occurred_at must be a whole-second Z timestamp",
        )
    seq = event["seq"]
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
        raise _refuse(
            MALFORMED_EVIDENCE_CONTRACT,
            f"invalidations[{index}]: seq must be an integer >= 1",
        )
    return evidence_ref, seq


@dataclass(frozen=True)
class _BatchEnvelope:
    """The batch's own shape, with no ordering decision taken on it yet."""

    events: list[Any]
    next_checkpoint: str
    gap: bool
    echoed: str | None


def _validated_envelope_head(payload: Mapping[str, Any]) -> None:
    """Judge the three keys that say WHICH envelope this is, not what it carries.

    Split from :func:`_batch_envelope` because that function owns the success
    fields and the cursor tokens; folding the envelope identity into it put the
    three head judgments on top of a body this module had already split once for
    being over the complexity gate, and re-merging them is how the split was lost.
    """
    schema = payload["schema"]
    if not isinstance(schema, str):
        raise _refuse(MALFORMED_EVIDENCE_CONTRACT, "invalidations: schema must be a string")
    if schema != EVIDENCE_RESULT_SCHEMA:
        # WHY the same split as ``_check_schema_token``: a token names a SHAPE,
        # so an unrecognized one is a contract this build does not implement —
        # never the nearest older parser.
        raise _refuse(
            UNSUPPORTED_EVIDENCE_CONTRACT,
            f"invalidations: unknown envelope token {schema!r}",
        )
    if "action" in payload and payload["action"] != INVALIDATION_ACTION:
        # WHY malformed and not unsupported: the action names the shape THIS
        # drain implements, so a batch for another action is a malformed
        # response to this request, not a newer version of it (ADR 0097 §2.7).
        raise _refuse(
            MALFORMED_EVIDENCE_CONTRACT,
            f"invalidations: action must be {INVALIDATION_ACTION!r}, "
            f"got {payload['action']!r}",
        )
    schema_version = payload["schema_version"]
    # WHY the same split as ``_check_schema_version``: a version NUMBERS a shape,
    # so the two fail under different classes. A value with no integer form is a
    # malformed payload; a finite integer this build does not implement is an
    # unsupported contract — never the nearest older parser and never a silent
    # default to 1.
    if not _is_integer_value(schema_version):
        raise _refuse(
            MALFORMED_EVIDENCE_CONTRACT,
            "invalidations: schema_version is not a finite integer",
        )
    if schema_version != INVALIDATION_EVENT_SCHEMA_VERSION:
        raise _refuse(
            UNSUPPORTED_EVIDENCE_CONTRACT,
            f"invalidations: unsupported schema_version {schema_version!r}",
        )


def _batch_envelope(payload: Any) -> _BatchEnvelope:
    """Validate the envelope: keys, envelope head, array, cursor tokens.

    Shape only. Whether this consumer can CONTINUE the batch from its stored
    position is an ordering question, decided by the caller.
    """
    if not isinstance(payload, Mapping):
        raise _refuse(
            MALFORMED_EVIDENCE_CONTRACT, "invalidations: the result payload is not an object"
        )
    unknown = frozenset(payload) - _BATCH_REQUIRED_FIELDS - _BATCH_OPTIONAL_FIELDS
    missing = _BATCH_REQUIRED_FIELDS - frozenset(payload)
    if missing or unknown:
        raise _refuse(
            MALFORMED_EVIDENCE_CONTRACT,
            f"invalidations: payload keys must be {sorted(_BATCH_REQUIRED_FIELDS)} "
            f"plus optional {sorted(_BATCH_OPTIONAL_FIELDS)}, got {sorted(payload)}",
        )
    _validated_envelope_head(payload)
    events = payload["events"]
    if not isinstance(events, list):
        raise _refuse(MALFORMED_EVIDENCE_CONTRACT, "invalidations: events must be an array")
    if len(events) > MAX_INVALIDATION_EVENTS:
        raise _refuse(
            MALFORMED_EVIDENCE_CONTRACT,
            f"invalidations: batch carries {len(events)} events, bound is "
            f"{MAX_INVALIDATION_EVENTS}",
        )
    gap = payload["gap"]
    if not isinstance(gap, bool):
        raise _refuse(MALFORMED_EVIDENCE_CONTRACT, "invalidations: gap must be a boolean")
    next_checkpoint = payload["next_checkpoint"]
    if not _is_bounded_token(next_checkpoint):
        raise _refuse(
            MALFORMED_EVIDENCE_CONTRACT,
            "invalidations: next_checkpoint must be a non-empty bounded token",
        )
    echoed = payload.get("checkpoint")
    if echoed is not None and not _is_bounded_token(echoed):
        raise _refuse(
            MALFORMED_EVIDENCE_CONTRACT,
            "invalidations: checkpoint must be a non-empty bounded token when present",
        )
    return _BatchEnvelope(
        events=events, next_checkpoint=next_checkpoint, gap=gap, echoed=echoed
    )


def _require_continuable(envelope: _BatchEnvelope, requested_cursor: str | None) -> None:
    """Refuse a batch this consumer's stored position cannot continue from."""
    if envelope.gap or (
        envelope.echoed is not None and envelope.echoed != requested_cursor
    ):
        # WHY refuse instead of reset-and-continue: the window between two
        # cursors is no longer recoverable, so acknowledging would retire
        # events fin never scrubbed, and skipping would drop them silently.
        # The drain stops and leaves the stored position where it was; the
        # caller withholds historical evidence until a revalidation sweep has
        # run.
        #
        # WHY an absent echo is not a mismatch: fin asks for the cursor it
        # holds, so the spec's ``gap`` flag is the channel's own contiguity
        # attestation. A batch that DOES echo a cursor is re-checked here,
        # because a stated cursor other than the requested one is a
        # cross-consumer or out-of-band checkpoint (D1 §2.3 requires those
        # to refuse).
        raise _refuse(
            RETRIEVE_UNAVAILABLE,
            "invalidations: the stream cannot be continued from the stored "
            f"position (gap={envelope.gap}, echoed={envelope.echoed!r}, "
            f"requested={requested_cursor!r})",
        )


def _contiguous_refs(events: list[Any], last_applied_seq: int) -> tuple[list[str], int]:
    """Return the batch's distinct refs and its last seq, contiguity-checked."""
    refs: list[str] = []
    first_seq: int | None = None
    previous_seq: int | None = None
    for index, event in enumerate(events):
        evidence_ref, seq = _validated_event(event, index)
        if previous_seq is not None and seq != previous_seq + 1:
            # ADR 0097 §2.3: a batch is acknowledged only if it is contiguous.
            raise _refuse(
                RETRIEVE_UNAVAILABLE,
                f"invalidations[{index}]: seq {seq} does not continue {previous_seq}",
            )
        if first_seq is None:
            first_seq = seq
        previous_seq = seq
        if evidence_ref not in refs:
            refs.append(evidence_ref)

    if first_seq is not None and first_seq > last_applied_seq + 1:
        # ADR 0097 §2.7: fin's own independent check. A first sequence beyond
        # the applied watermark means the backend skipped a window fin never
        # saw, whatever it said about ``gap``.
        raise _refuse(
            RETRIEVE_UNAVAILABLE,
            f"invalidations: batch starts at seq {first_seq}, past the applied "
            f"watermark {last_applied_seq}",
        )
    return refs, previous_seq if previous_seq is not None else last_applied_seq


def _validated_batch(
    payload: Any, requested_cursor: str | None, last_applied_seq: int
) -> tuple[list[str], str, int, int]:
    """Validate the decoded ``invalidations`` body.

    Returns ``(evidence_refs, next_checkpoint, event_count, last_seq)``.

    ``requested_cursor`` is the checkpoint fin asked for, so contiguity can be
    re-checked here instead of trusting the batch to describe its own origin;
    ``last_applied_seq`` is this consumer's applied watermark, which is what
    makes a skipped window decidable without parsing an opaque checkpoint.
    """
    envelope = _batch_envelope(payload)
    _require_continuable(envelope, requested_cursor)
    refs, last_seq = _contiguous_refs(envelope.events, last_applied_seq)
    if last_seq < last_applied_seq:
        # WHY the drain owns this and not the store: ``applied_checkpoint`` is
        # opaque, so no store can order it and neither can refuse a regression
        # on that column. ``last_seq`` IS orderable and is written alongside the
        # applied checkpoint in the same store call, so a batch ending behind
        # the stored watermark is refused here rather than being allowed to
        # walk the applied checkpoint backwards.
        raise _refuse(
            RETRIEVE_UNAVAILABLE,
            f"invalidations: batch ends at seq {last_seq}, behind the stored "
            f"watermark {last_applied_seq}",
        )
    return refs, envelope.next_checkpoint, len(envelope.events), last_seq


def drain_invalidation_batch(
    store: ThreadStore,
    *,
    consumer_id: str,
    fetch: Callable[[str | None], object],
    acknowledge: Callable[[str], None],
) -> InvalidationDrainOutcome:
    """Drain one invalidation batch: scrub, confirm, then acknowledge.

    ``fetch`` is called with the cursor this consumer last acknowledged
    (``None`` on a first drain) and returns the decoded ``invalidations``
    body. ``acknowledge`` is called with ``next_checkpoint`` only after the
    scrub committed and storage confirmed it; a failure inside it propagates
    unchanged, leaving the resume cursor where it was so the backend
    redelivers and the retry converges.
    """
    if not _is_bounded_token(consumer_id):
        raise _refuse(
            MALFORMED_EVIDENCE_CONTRACT,
            "drain: consumer_id must be a non-empty bounded token",
        )
    stored = store.read_invalidation_checkpoint(consumer_id)
    requested_cursor = stored.acked_checkpoint or None
    try:
        refs, next_checkpoint, event_count, last_seq = _validated_batch(
            fetch(requested_cursor), requested_cursor, stored.last_seq
        )
    except StructuredInvalidationRefusal as exc:
        # A stream this consumer cannot continue from is exactly the condition
        # T33's sweep exists for, so the gate is closed HERE — at the one place
        # the gap is decided — rather than left to a caller to remember. The
        # refusal still propagates unchanged: the drain's job is to stop, and
        # stopping is not a recovery.
        #
        # WHY the gate close cannot advance anything: it writes only the two
        # recovery columns. The watermark is already where the last
        # acknowledgement put it, and this statement does not name the others.
        if exc.code == RETRIEVE_UNAVAILABLE:
            store.set_recovery_withheld(consumer_id, exc.reason)
        raise

    try:
        scrubbed = store.scrub_structured_evidence_payloads(
            consumer_id=consumer_id,
            evidence_refs=refs,
            next_checkpoint=next_checkpoint,
            last_seq=last_seq,
        )
    except PersistenceUnavailableError as exc:
        raise _refuse(
            RETRIEVE_UNAVAILABLE, f"drain: storage could not confirm the scrub ({exc})"
        ) from exc
    if scrubbed.remaining_entries != 0:
        raise _refuse(
            EVIDENCE_UNAVAILABLE,
            f"drain: storage still holds {scrubbed.remaining_entries} payload(s) "
            "for this batch",
        )

    acknowledge(next_checkpoint)
    store.advance_invalidation_acknowledgement(consumer_id, next_checkpoint)
    return InvalidationDrainOutcome(
        consumer_id=consumer_id,
        events=event_count,
        scrubbed_entries=scrubbed.scrubbed_entries,
        scrubbed_runs=scrubbed.scrubbed_runs,
        checkpoint=next_checkpoint,
        last_seq=last_seq,
    )


__all__ = [
    "INVALIDATION_EVENT_FIELDS",
    "INVALIDATION_EVENT_SCHEMA_VERSION",
    "INVALIDATION_REASONS",
    "MAX_EVIDENCE_REF_CHARS",
    "MAX_INVALIDATION_EVENTS",
    "MAX_INVALIDATION_TOKEN_CHARS",
    "InvalidationDrainOutcome",
    "StructuredInvalidationRefusal",
    "drain_invalidation_batch",
]