"""Push port: the idempotent push seam and its named failures
(plan T14, SC18 / SC20, §B4).

The port is the seam between fin's corpus side (which owns parsing,
chunking and the record shape — :mod:`fin.corpus.push`) and the
retrieval service (which stores and recalls the pushed text verbatim,
§B4「不二次切分」). This ticket ships the seam plus the contract fake
(:mod:`fin.retrieve.fake_push_port`); the real transport to the
iknow-memory service lands with a later ticket.

Envelope contract (spec `operator-console-host-contract`, T1)
-------------------------------------------------------------

Each ``push`` call carries a batch of **envelopes** (one per 资料 —
one per frozen unit). The envelope shape is verbatim:

* ``source_ref`` — the unit locator triple serialization;
* ``content`` — the unit's full extracted text (the 资料 body);
* ``chunks`` — ``[{content, index}, ...]`` with ``index`` starting at 0
  and increasing monotonically; every ``chunks[i].content`` is a
  contiguous substring of ``content`` (SC11 / SC16);
* ``metadata`` — the SC20 filter surface (see
  :data:`fin.corpus.push.REQUIRED_PUSH_METADATA_FIELDS`);
* ``valid_from`` / ``valid_until`` — the validity interval the service
  attaches to the block.

Idempotency contract (§B4 / SC18)
---------------------------------

The wire-level write identity is the envelope's idempotency key —
the ``(source_ref, body content identity)`` pair. Pushing the same
batch again (the §B1「幂等基准」row: :data:`IDEMPOTENCY_BASELINE_PUSHES`
= 3 consecutive pushes) must leave the block total increment at 0
and the block id set unchanged, and must return the same block ids.

The service keeps its own copy of that second component — it computes
the stored ``content_identity`` and answers with it — and the pair it
later validates at ``submit_index_job`` is *its* value, not fin's hash.
:func:`content_identities_of` is the one place that reads it back out.

Failure contract
----------------

A batch whose envelope misses a SC20 metadata field is refused **as a
whole** with :class:`PushMetadataIncompleteError`: the receiving side
validates every envelope before it writes any, so a refused batch
writes nothing and cannot half-land. Failures are typed and carry a
stable ``code`` — never ``None``, never an empty success, never a
silent drop. A refused batch is not a write and therefore does not
count towards the idempotency assertion (the store's rejection
counter keeps the trace).

SC21 splits the transport's two failure shapes into distinguishable
codes: :class:`PushServiceUnavailableError` (the service was never
reached) and :class:`PushProtocolError` (it answered off-contract).
Neither returns an outcome and neither is retried inside this module
(:data:`PUSH_ATTEMPTS_PER_BATCH`), so a failed push cannot be recorded
as a write and cannot be settled by a local artifact — the replay path
is :mod:`fin.retrieve.push_batching`, whose journal keeps the failure
enumerable.

An empty envelope batch is a third thing: **invalid input** per §B2
第 5 条, refused with :class:`PushEmptyBatchError` rather than answered
with a zero-write success, and — unlike the two capability failures
above — it does not set ``degraded`` (see :class:`PushError`).

T21 folds that third thing into the shared dichotomy: the two
invalid-input failures on this seam (:class:`PushEmptyBatchError` and
:class:`PushEmptyDocumentError`, an envelope with no body) also
inherit :class:`~fin.retrieve.input_validation.InputValidationError`,
so one classification and one「输入校验失败」count cover the push side
and the retrieve side alike.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Final, Mapping, Protocol, Sequence, runtime_checkable

from fin.corpus.push import (
    REQUIRED_PUSH_METADATA_FIELDS,
    PushEnvelope,
    record_to_envelope,
)
from fin.retrieve.input_validation import (
    INPUT_CLASS_EMPTY_BATCH,
    INPUT_CLASS_EMPTY_DOCUMENT,
    InputValidationError,
    InputValidationLedger,
    raise_input_failure,
)

# §B1「幂等基准」: same batch pushed 3 times in a row → block total
# increment = 0, block id set unchanged.
IDEMPOTENCY_BASELINE_PUSHES: Final[int] = 3

# SC44: the number of records one push call accepts. The value itself is
# open headroom (plan T15「单批上限的具体取值全部开放」); what is not open
# is that crossing it is a named overflow rather than a truncation, and
# that the batch planner (:mod:`fin.retrieve.push_batching`) reads the
# same constant so a planned run never trips it.
MAX_PUSH_BATCH_RECORDS: Final[int] = 200

# T16 retry policy: one attempt per batch, no retry inside the push
# path. SC21 forbids a *silent* retry; rather than pick a hidden retry
# count here, a failed batch surfaces as a named failure and is replayed
# deliberately through :mod:`fin.retrieve.push_batching` (whose journal
# keeps the failure enumerable). Tests assert the transport was called
# exactly this many times on a failure.
PUSH_ATTEMPTS_PER_BATCH: Final[int] = 1

# §B3 reason strings: which capability item, and whether it was never
# attempted or attempted and failed.
DEGRADED_REASON_PUSH_ATTEMPT_FAILED: Final[str] = "push:attempt_failed"

# Transport-level exceptions that mean「检索服务不可用」(SC21). Socket
# errors, refused connections, DNS failures and timeouts are all
# ``OSError`` subclasses (``TimeoutError`` included since 3.10).
_TRANSPORT_UNAVAILABLE: Final[tuple[type[BaseException], ...]] = (OSError,)

_RESPONSE_COUNT_FIELDS: Final[tuple[str, ...]] = ("written", "duplicates")


class PushError(RuntimeError):
    """Base class for the push path's named failures.

    Every subclass carries a distinct stable :attr:`code`; callers that
    must branch on the failure read ``exc.code`` instead of matching on
    message text. The push path never signals failure by returning
    ``None`` / an empty result / a sentinel number.

    :attr:`degraded` classifies the failure against §B2 第 5 条 and §B3
    (field names verbatim ``degraded`` / ``degraded_reasons``): invalid
    input from the caller is *not* a capability failure and never sets
    the flag, while a capability that was attempted and failed does.
    This ticket only *declares* the classification — reading the flag
    off a retrieval response envelope and acting on it is T20's scope.
    """

    code: str = "push_error"
    degraded: bool = False
    degraded_reasons: tuple[str, ...] = ()


class PushMetadataIncompleteError(PushError):
    """A pushed record misses one or more SC20 metadata fields.

    The offending block id and the missing field names are in the
    message. The batch is refused as a whole — nothing is written.
    """

    code = "push_metadata_incomplete"


class UnknownPushedBlockError(PushError):
    """A recall asked for a block id the store never accepted.

    Raised rather than answered with ``None`` so that handing the store
    a `source_ref` (which is not block identity, §B4) fails loudly.
    """

    code = "unknown_pushed_block"


class PushBatchOverflowError(PushError):
    """One push call carries more records than the cap allows (SC44).

    Raised instead of storing a prefix and dropping the rest: an
    over-sized batch is a caller error the batch planner exists to
    prevent, so it surfaces as a named failure with nothing written.
    """

    code = "push_batch_overflow"


class PushBatchSizeInvalidError(PushError):
    """A batch size of zero or less was requested.

    A non-positive size cannot split a non-empty record sequence, so it
    would silently produce no batches — refused by name instead.
    """

    code = "push_batch_size_invalid"


class PushBatchFailedError(PushError):
    """A batch failed for a reason that is not itself a :class:`PushError`.

    The background push path (§B4) must leave a named record for every
    failure, so a transport-level exception is wrapped (``raise ... from
    exc``, original preserved as ``__cause__``) rather than escaping
    untyped or being swallowed.
    """

    code = "push_batch_failed"
    degraded = True
    degraded_reasons = (DEGRADED_REASON_PUSH_ATTEMPT_FAILED,)


class PushServiceUnavailableError(PushError):
    """The retrieval service could not be reached (SC21).

    The transport never got an answer, so nothing about the batch is
    known to have landed: the caller must treat the batch as unwritten
    and replay it. Distinct from :class:`PushProtocolError`, where the
    service *did* answer.
    """

    code = "push_service_unavailable"
    degraded = True
    degraded_reasons = (DEGRADED_REASON_PUSH_ATTEMPT_FAILED,)


class PushProtocolError(PushError):
    """The service answered off-contract (SC21).

    The response envelope is missing a field, carries a wrong type, or
    does not account for every pushed record. Since the outcome cannot
    be trusted, it is refused by name rather than coerced into a
    plausible-looking :class:`PushOutcome`.
    """

    code = "push_protocol_error"
    degraded = True
    degraded_reasons = (DEGRADED_REASON_PUSH_ATTEMPT_FAILED,)


class PushEmptyBatchError(PushError, InputValidationError):
    """A push carried no records at all (§B2 第 5 条).

    An empty block batch is **invalid input**, not a legitimate empty
    result: it is refused by name instead of answered with a zero-write
    success, and — being a caller error rather than a capability
    failure — it does not set ``degraded``.
    """

    code = "push_empty_batch"
    input_class = INPUT_CLASS_EMPTY_BATCH


class PushEmptyDocumentError(PushError, InputValidationError):
    """A pushed envelope carries no body (§B2 第 5 条's 空文档 / 空内容).

    A 资料 with nothing in it cannot be recalled verbatim and cannot be
    a citation's evidence, so storing it would create a row that looks
    like content. Refused by name, with the batch unwritten — the same
    class the parse path names as ``empty_source`` / ``no_units``.
    """

    code = "push_empty_document"
    input_class = INPUT_CLASS_EMPTY_DOCUMENT


def frozen_content_identities(
    values: Mapping[str, str],
) -> Mapping[str, str]:
    """One read-only view of a push result's ``source_ref`` → identity map.

    Every push result carries this map, and the run that built it hands
    the same map on to the next result (the batched run's merge feeds the
    ingest result), so a writable one would let any consumer edit what the
    service reported. Wrapping — not copying — is enough: nothing in fin
    keeps a mutable reference to these maps after the result is built.
    """
    return (
        values
        if isinstance(values, MappingProxyType)
        else MappingProxyType(values)
    )


@dataclass(frozen=True)
class PushOutcome:
    """The result of one push batch.

    ``block_ids`` is parallel to the pushed envelopes: an envelope whose
    idempotency key was already stored yields the **stored** block ids,
    so a replay returns the same ids as the original write. ``written``
    counts first-time writes, ``duplicates`` counts envelopes
    recognized as an existing write.

    ``content_identities`` maps each pushed ``source_ref`` to the
    ``content_identity`` the service computed and stored for it — the
    other half of the pair ``submit_index_job`` takes. It is empty for a
    service that reports no identity, and is never filled in by fin. A
    caller may read it and copy it, never write it: it is the record of
    what the service said, and on the batched path the same map is handed
    to every result built from that run.
    """

    block_ids: tuple[str, ...]
    written: int
    duplicates: int
    content_identities: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "content_identities",
            frozen_content_identities(self.content_identities),
        )


@runtime_checkable
class PushPort(Protocol):
    """Structural protocol for push adapters.

    ``push`` takes wire envelopes (``PushEnvelope.to_wire()`` shape) and
    returns ``{"block_ids": [...], "written": int, "duplicates": int}``
    plus, for a service that reports one, the ``content_identity`` /
    ``content_identities`` fields the receiving side computed.
    Implementations raise a :class:`PushError` subclass on failure and
    never report a refused batch as an empty success.
    """

    def push(
        self, envelopes: Sequence[Mapping[str, Any]]
    ) -> Mapping[str, Any]:  # pragma: no cover - protocol
        ...


def missing_metadata_fields(wire: Mapping[str, Any]) -> tuple[str, ...]:
    """The SC20 metadata fields absent or empty on one wire envelope.

    A present-but-empty value counts as missing: an empty format or an
    empty parse rule version is not a filterable value.
    """
    metadata = wire.get("metadata")
    if not isinstance(metadata, Mapping):
        return REQUIRED_PUSH_METADATA_FIELDS
    return tuple(
        field
        for field in REQUIRED_PUSH_METADATA_FIELDS
        if not metadata.get(field)
    )


def validate_wire_record(wire: Mapping[str, Any]) -> None:
    """Raise :class:`PushMetadataIncompleteError` when the envelope's
    SC20 metadata is incomplete (SC20's rejection path)."""
    missing = missing_metadata_fields(wire)
    if missing:
        raise PushMetadataIncompleteError(
            f"push of envelope {wire.get('source_ref')!r} misses metadata "
            f"fields: {', '.join(missing)}"
        )


def validate_batch_not_empty(
    records: Sequence[object],
    *,
    ledger: InputValidationLedger | None = None,
) -> None:
    """Raise :class:`PushEmptyBatchError` for a batch with no envelopes.

    Single authority for §B2 第 5 条's empty-batch rule, applied on both
    sides of the seam: the caller is refused before a pointless round
    trip, and the receiving side refuses independently so a future
    transport cannot smuggle an empty batch past a client-only check.

    ``ledger`` is the「输入校验失败」count (T21). Whichever side refuses
    the batch is the side that counts it, so the number is taken exactly
    once per refusal rather than once per validation call.
    """
    if len(records) == 0:
        failure = PushEmptyBatchError(
            "push batch is empty; an empty envelope batch is invalid "
            "input, not an empty result"
        )
        if ledger is None:
            raise failure
        raise_input_failure(failure, ledger=ledger)


def validate_wire_record_text(
    wire: Mapping[str, Any],
    *,
    ledger: InputValidationLedger | None = None,
) -> None:
    """Raise :class:`PushEmptyDocumentError` for an envelope with no
    body.

    Whitespace-only counts as none: the retrieval service stores the
    body verbatim and the citation gate quotes it, so a blank body is
    not a thinner one, it is an absent one.
    """
    body = wire.get("content") if "content" in wire else wire.get("body")
    if not isinstance(body, str) or not body.strip():
        failure = PushEmptyDocumentError(
            f"push of envelope {wire.get('source_ref')!r} carries no body; "
            "an empty document is invalid input, not an empty result"
        )
        if ledger is None:
            raise failure
        raise_input_failure(failure, ledger=ledger)


def validate_envelope_chunks(
    wire: Mapping[str, Any],
) -> None:
    """Raise :class:`PushProtocolError` when the envelope's ``chunks``
    array violates the one-envelope invariants (T1 acceptance).

    Pinned invariants:

    * ``chunks`` is a non-empty sequence of mappings;
    * each chunk has an integer ``index`` and a non-empty string
      ``content``;
    * indexes are monotonic starting at 0 — the wire shape declares
      ``0, 1, ..., N-1`` in order;
    * each chunk's ``content`` is a contiguous substring of the body —
      SC11 / SC16 (「连续子串」).
    """
    chunks = wire.get("chunks")
    body = wire.get("content") if "content" in wire else wire.get("body")
    if not isinstance(chunks, list) or not chunks:
        raise PushProtocolError(
            f"push of envelope {wire.get('source_ref')!r} carries no chunks; "
            "one envelope must carry at least one slice"
        )
    body_str = body if isinstance(body, str) else ""
    expected = 0
    for chunk in chunks:
        if not isinstance(chunk, Mapping):
            raise PushProtocolError(
                f"push of envelope {wire.get('source_ref')!r} carries a "
                "non-mapping chunk; the chunk shape is {content, index}"
            )
        index = chunk.get("index")
        content = chunk.get("content")
        if not isinstance(index, int) or isinstance(index, bool):
            raise PushProtocolError(
                f"push of envelope {wire.get('source_ref')!r} carries a "
                f"non-integer chunk index {index!r}"
            )
        if not isinstance(content, str) or not content:
            raise PushProtocolError(
                f"push of envelope {wire.get('source_ref')!r} carries a "
                f"non-string chunk content at index {index!r}"
            )
        if index != expected:
            raise PushProtocolError(
                f"push of envelope {wire.get('source_ref')!r} chunks are "
                f"not monotonic from 0; saw index {index} where "
                f"{expected} was expected"
            )
        if body_str and content not in body_str:
            raise PushProtocolError(
                f"push of envelope {wire.get('source_ref')!r} chunk at "
                f"index {index} is not a contiguous substring of the body"
            )
        expected += 1


def _pushed_source_refs(
    source_refs: Sequence[str] | None,
) -> tuple[str, ...]:
    """The 资料 one response's identities belong to: first-seen distinct
    ``source_ref`` values, the order the receiving service answers in.

    ``None`` means the caller has no batch to attribute an identity to,
    which is a legal answer only for a response that carries none.
    """
    if source_refs is None:
        return ()
    seen: dict[str, None] = {}
    for source_ref in source_refs:
        seen.setdefault(source_ref, None)
    return tuple(seen)


def _validated_identity(value: Any, *, where: str) -> str:
    """One identity off the wire, or a named protocol failure.

    The value is checked for shape only, never recomputed: a hash fin
    derived itself would let a wrong pair reach ``submit_index_job`` with
    nothing to contradict it.
    """
    if not isinstance(value, str) or not value:
        raise PushProtocolError(
            f"push response {where} carries {value!r}, expected a "
            "non-empty content identity"
        )
    return value


def _pairs_from_scalar(value: Any, *, refs: tuple[str, ...]) -> dict[str, str]:
    """The one-资料 answer: a scalar identity for exactly one pushed source.

    The scalar carries no name, so it is only attributable when the batch
    held exactly one 资料 — a whole batch answering with one identity
    cannot be spread over the sources without inventing pairs.
    """
    identity = _validated_identity(value, where="content_identity")
    if len(refs) != 1:
        raise PushProtocolError(
            f"push response reports one content_identity for "
            f"{len(refs)} pushed 资料"
        )
    return {refs[0]: identity}


def _pairs_from_keyed(
    identities: Mapping[Any, Any], *, refs: tuple[str, ...]
) -> dict[str, str]:
    """The attributed answer: every key names the `source_ref` it belongs to.

    Attribution by name cannot shift a pair the way a positional read can,
    so this shape answers for exactly the keys it carries — an empty map
    is the same "no identity" answer as an absent field, and a partial map
    simply leaves out the sources the service reported nothing for. What
    it may not do is name a `source_ref` this batch never pushed: that key
    would be an invented pair, since the only thing this response can
    speak for is the push it answers.
    """
    for source_ref in identities:
        if not isinstance(source_ref, str) or not source_ref:
            raise PushProtocolError(
                f"push response content_identities has the "
                f"{source_ref!r} key; expected a non-empty source_ref"
            )
    pushed = set(refs)
    unpushed = tuple(ref for ref in identities if ref not in pushed)
    if unpushed:
        raise PushProtocolError(
            f"push response content_identities names {list(unpushed)}, which "
            "this batch did not push"
        )
    return {
        source_ref: _validated_identity(
            identity, where=f"content_identities[{source_ref!r}]"
        )
        for source_ref, identity in identities.items()
    }


def _pairs_from_sequence(
    identities: Any, *, refs: tuple[str, ...]
) -> dict[str, str]:
    """The batch answer: one identity per pushed 资料, in the order fin sent.

    Position is the only binding here, so the two sides must be the same
    length — a short sequence would silently drop the tail's pairs and a
    long one would attribute an identity to a source that does not exist.
    """
    if isinstance(identities, (str, bytes)) or not isinstance(
        identities, Sequence
    ):
        raise PushProtocolError(
            f"push response content_identities is "
            f"{type(identities).__name__}, expected a sequence of identities"
        )
    if len(identities) != len(refs):
        raise PushProtocolError(
            f"push response carries {len(identities)} content identities "
            f"for {len(refs)} pushed 资料"
        )
    return {
        source_ref: _validated_identity(identity, where="content_identities")
        for source_ref, identity in zip(refs, identities)
    }


def content_identities_of(
    response: Mapping[str, Any],
    *,
    source_refs: Sequence[str] | None = None,
) -> dict[str, str]:
    """Attribute the identities of one push response to their `source_ref`.

    The service owns ``content_identity`` — it computes the value, stores
    it beside the `source_ref`, and validates the pair at
    ``submit_index_job``. So the only authority for the pair fin hands on
    is this response; recomputing it fin-side would be a second,
    unverifiable one.

    Three shapes are on-contract (iknow ``src/tools/push.ts``):

    * ``content_identity`` — one 资料 wrote and its identity is the
      scalar;
    * ``content_identities`` — a sequence, parallel to the batch's 资料
      in the order fin sent them;
    * neither — a build that predates the field. That is still a
      successful write, so it reports *no* identity rather than an
      invented one.

    A ``content_identities`` :class:`~typing.Mapping` is the fourth,
    adapter-side shape: the MCP adapter hands on what it has already
    attributed, so the read is idempotent across the seam. Its keys are
    checked against the batch for the same reason the other shapes are —
    an identity for a source this push did not send is a pair nothing
    holds.

    Anything else — a value that is not a non-empty string, a count that
    does not line up with the pushed 资料, a key outside the batch, both
    wire shapes at once, a sequence with no batch to attribute it to — is
    a :class:`PushProtocolError`: an identity fin cannot pin to exactly
    one source is worse than no identity, because the wrong pair would
    reach the durable index job silently.
    """
    has_scalar = "content_identity" in response
    has_plural = "content_identities" in response
    if has_scalar and has_plural:
        raise PushProtocolError(
            "push response reports both content_identity and "
            "content_identities; the service answers with one shape per batch"
        )
    refs = _pushed_source_refs(source_refs)
    if has_scalar:
        return _pairs_from_scalar(response["content_identity"], refs=refs)
    if not has_plural:
        return {}
    identities = response["content_identities"]
    if isinstance(identities, Mapping):
        return _pairs_from_keyed(identities, refs=refs)
    return _pairs_from_sequence(identities, refs=refs)


def outcome_of_response(
    response: Any,
    *,
    chunk_count: int,
    source_refs: Sequence[str] | None = None,
) -> PushOutcome:
    """Read one push response envelope, or fail by name (SC21).

    After T1 the response carries one ``block_ids`` entry per chunk
    (the slices are what the receiving service hands back ids for;
    envelopes are not chunked again). Every field is checked before
    it is trusted: ``block_ids`` is a sequence of strings of length
    ``chunk_count``, and ``written`` / ``duplicates`` sum to that
    same total. Anything else is a :class:`PushProtocolError` — a
    short envelope is never padded and a stray one is never truncated
    into a plausible outcome.

    ``source_refs`` are the `source_ref` values of the envelopes this
    response answers, in send order; they let an unattributed
    ``content_identity`` / ``content_identities`` field be pinned to the
    资料 it describes (see :func:`content_identities_of`).
    """
    if not isinstance(response, Mapping):
        raise PushProtocolError(
            f"push response is {type(response).__name__}, expected a mapping"
        )
    missing = [
        name
        for name in ("block_ids", *_RESPONSE_COUNT_FIELDS)
        if name not in response
    ]
    if missing:
        raise PushProtocolError(
            f"push response misses fields: {', '.join(missing)}"
        )

    block_ids = response["block_ids"]
    if isinstance(block_ids, (str, bytes)) or not isinstance(
        block_ids, Sequence
    ):
        raise PushProtocolError(
            f"push response block_ids is {type(block_ids).__name__}, "
            "expected a sequence of block ids"
        )
    if len(block_ids) != chunk_count:
        raise PushProtocolError(
            f"push response carries {len(block_ids)} block ids for "
            f"{chunk_count} pushed chunks"
        )
    if not all(isinstance(block_id, str) for block_id in block_ids):
        raise PushProtocolError(
            "push response block_ids contains a non-string block id"
        )

    counts: dict[str, int] = {}
    for name in _RESPONSE_COUNT_FIELDS:
        value = response[name]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise PushProtocolError(
                f"push response {name} is {value!r}, expected a "
                "non-negative integer"
            )
        counts[name] = value
    total = sum(counts.values())
    if total != chunk_count:
        raise PushProtocolError(
            f"push response accounts for {total} of {chunk_count} pushed "
            "chunks"
        )
    return PushOutcome(
        block_ids=tuple(block_ids),
        written=counts["written"],
        duplicates=counts["duplicates"],
        content_identities=content_identities_of(
            response, source_refs=source_refs
        ),
    )


def push_envelopes(
    port: PushPort,
    envelopes: Sequence[PushEnvelope],
    *,
    input_validation_ledger: InputValidationLedger | None = None,
) -> PushOutcome:
    """Push ``envelopes`` (one per 资料) through ``port`` idempotently.

    Metadata completeness is the receiving side's judgement (SC20:
    「缺字段的推送**被拒**」), so this function does not pre-screen the
    batch for it: it hands the wire envelopes over and lets the typed
    :class:`PushMetadataIncompleteError` propagate. A refused batch
    therefore leaves its trace where the refusal happened, and one
    validation authority (:func:`validate_wire_record`) serves both
    sides.

    The two SC21 failure shapes are separated here, because only this
    function sees both the transport and the envelope: an unreachable
    service becomes :class:`PushServiceUnavailableError` (the original
    ``OSError`` kept as ``__cause__``), an off-contract answer becomes
    :class:`PushProtocolError`. Neither is retried
    (:data:`PUSH_ATTEMPTS_PER_BATCH`) and neither returns an outcome, so
    a failed batch is never recorded as a write.

    ``input_validation_ledger`` is where a client-side refusal of an
    empty batch is counted (T21). The envelope-level checks stay on
    the receiving side, which carries its own ledger.
    """
    validate_batch_not_empty(envelopes, ledger=input_validation_ledger)
    chunk_count = sum(len(envelope.chunks) for envelope in envelopes)
    try:
        response = port.push([envelope.to_wire() for envelope in envelopes])
    except PushError:
        # already named by the receiving side (SC20 / SC44) — leave it
        raise
    except _TRANSPORT_UNAVAILABLE as exc:
        raise PushServiceUnavailableError(
            f"push to the retrieval service failed to reach it: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    return outcome_of_response(
        response,
        chunk_count=chunk_count,
        source_refs=[envelope.source_ref for envelope in envelopes],
    )


def push_records(
    port: PushPort,
    records: Sequence[Any],
    *,
    input_validation_ledger: InputValidationLedger | None = None,
) -> PushOutcome:
    """Backward-compat shim: forward wire dicts OR :class:`PushRecord`
    instances to the port as envelopes.

    Most older tests call this with hand-rolled wire dicts or
    :class:`PushRecord` rows (the per-child record shape that the
    invalidation path still keys on, but that no longer rides the
    wire). The production path uses :func:`push_envelopes` with
    :class:`PushEnvelope` instances; this shim exists so a per-child
    record can be flattened into an envelope wire dict on the way in,
    without forcing every test to rewrite its fixtures for the new
    shape.

    Conversion rule: a :class:`PushRecord` becomes
    ``{"source_ref": ..., "body": text, "chunks": [{"content": text,
    "index": 0}], "metadata": ..., "valid_from": ..., "valid_until":
    ...}`` — a single-chunk envelope whose chunk is the record's
    block text. A mapping is forwarded as-is.
    """
    validate_batch_not_empty(records, ledger=input_validation_ledger)
    wire_envelopes = [_record_or_wire_to_envelope(item) for item in records]
    chunk_count = sum(len(item["chunks"]) for item in wire_envelopes)
    try:
        response = port.push(wire_envelopes)
    except PushError:
        raise
    except _TRANSPORT_UNAVAILABLE as exc:
        raise PushServiceUnavailableError(
            f"push to the retrieval service failed to reach it: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    return outcome_of_response(
        response,
        chunk_count=chunk_count,
        source_refs=[item["source_ref"] for item in wire_envelopes],
    )


def _record_or_wire_to_envelope(item: Any) -> Mapping[str, Any]:
    """Forward a wire envelope dict OR project a :class:`PushRecord`
    into the one-envelope-per-资料 wire shape.

    The record projection is the SSOT
    :func:`fin.corpus.push.record_to_envelope`, shared with the
    batching path's ``_coerce_envelope`` — the direct-push wire and
    the batched wire are the same shape by construction.
    """
    if isinstance(item, Mapping):
        return item
    return record_to_envelope(item).to_wire()


__all__ = [
    "DEGRADED_REASON_PUSH_ATTEMPT_FAILED",
    "IDEMPOTENCY_BASELINE_PUSHES",
    "MAX_PUSH_BATCH_RECORDS",
    "PUSH_ATTEMPTS_PER_BATCH",
    "PushBatchFailedError",
    "PushBatchOverflowError",
    "PushBatchSizeInvalidError",
    "PushEmptyBatchError",
    "PushEmptyDocumentError",
    "PushError",
    "PushMetadataIncompleteError",
    "PushOutcome",
    "PushPort",
    "PushProtocolError",
    "PushServiceUnavailableError",
    "UnknownPushedBlockError",
    "content_identities_of",
    "frozen_content_identities",
    "missing_metadata_fields",
    "outcome_of_response",
    "push_envelopes",
    "push_records",
    "validate_batch_not_empty",
    "validate_envelope_chunks",
    "validate_wire_record",
    "validate_wire_record_text",
]
