"""In-process contract fake for the push path (plan T14, SC18 / SC20, §B4;
spec `operator-console-host-contract` T1).

The fake stands in for the retrieval service so the default suite can
assert the §B4 push contract without a service process, a network hop
or a large corpus. It is a **contract** fake, not a stub: it enforces
the service-side half of the contract itself, so a future real
transport cannot smuggle an envelope past a check that only lived in
the client.

What the fake guarantees (spec `operator-console-host-contract`, T1)
--------------------------------------------------------------------

* **wire field names are the new envelope** — envelopes are stored and
  recalled with ``source_ref`` / ``body`` / ``chunks[]`` / ``metadata``
  / ``valid_from`` / ``valid_until``;
* **idempotency** — the store is keyed on the envelope's
  ``(source_ref, body content identity)`` pair; a repeat push returns
  the stored block ids and writes nothing;
* **envelope-shape invariants** — ``source_ref`` non-empty, body
  non-empty, ``chunks`` non-empty with monotonic ``index`` from 0,
  every chunk content a contiguous substring of the body;
* **no re-splitting** (§B4「不二次切分」) — the body and chunks are
  stored exactly as pushed and recalled byte-identical, never
  re-normalized, trimmed or re-chunked;
* **named overflow** (SC44) — a batch above the per-call cap raises
  ``PushBatchOverflowError`` and stores nothing, never a truncated
  prefix;
* **named refusal of invalid input** — an empty batch raises
  ``PushEmptyBatchError`` (§B2 第 5 条) instead of being answered with a
  zero-write success;
* **named refusal** — an envelope whose SC20 metadata is incomplete
  raises ``PushMetadataIncompleteError`` and the whole batch is
  refused, so a refused push leaves the block total untouched and is
  counted in :meth:`rejected_batches` rather than passing as an empty
  success.

Invalidation semantics (plan T18, SC24, §B4「内容失效语义」)
--------------------------------------------------------

The fake also stands in for the service's invalidation action, since
「关闭时间有效区间、不物理删除、幂等」is retrieval-side semantics that fin's
SC24 assertions need something to stand on:

* every stored chunk carries a **validity interval**
  ``[valid_from, valid_to)`` over a logical instant (an integer — no
  clock enters, so the fake stays deterministic), open-ended while
  valid;
* :meth:`invalidate` **closes** the interval (``valid_to =
  valid_from``, i.e. empty), so the chunk is hit at no query time
  whatsoever — :meth:`is_hittable_at_any_time` answers that claim
  directly rather than by sampling;
* the row itself stays: :meth:`physically_present` and :meth:`recall`
  keep answering byte-identically (**不物理删除**);
* re-issuing the same invalidation closes nothing twice and reports the
  same closing instants (**幂等**);
* a target the store never accepted, or an empty request, is refused by
  name **before any interval is closed**, so a refused request leaves no
  half-done state;
* :meth:`invalidation_records` is the service side's own queryable
  closure record.
"""

from __future__ import annotations

import hashlib
import json
import threading
from typing import Any, Final, Mapping, Sequence

from fin.retrieve.invalidation_port import (
    InvalidationError,
    InvalidationUnknownTargetError,
    validate_targets_not_empty,
)
from fin.retrieve.input_validation import InputValidationLedger
from fin.retrieve.push_port import (
    MAX_PUSH_BATCH_RECORDS,
    PushBatchOverflowError,
    PushError,
    UnknownPushedBlockError,
    validate_batch_not_empty,
    validate_envelope_chunks,
    validate_wire_record,
    validate_wire_record_text,
)

# The logical instant every pushed chunk becomes valid at. Closing an
# interval sets ``valid_to`` to this same value, making the half-open
# interval empty — no query instant can fall inside it.
VALID_FROM: Final[int] = 0

_ID_SEPARATOR: Final[str] = "\x1f"


def _envelope_body_content_id(envelope: Mapping[str, Any]) -> str:
    """The wire-level idempotency body identity.

    Deterministic sha256 over the body text + the SC20 rule versions,
    joined by ``\\x1f`` so a field value cannot impersonate a field
    boundary (mirrors :func:`fin.corpus.push.content_identity`).
    """
    body = envelope.get("content")
    if body is None:
        body = envelope.get("body")
    metadata = envelope.get("metadata") or {}
    parts = (
        str(body),
        str(metadata.get("normalization_rule_version", "")),
        str(metadata.get("parse_rule_version", "")),
        str(metadata.get("params_table_version", "")),
    )
    return hashlib.sha256(_ID_SEPARATOR.join(parts).encode("utf-8")).hexdigest()


def _fake_chunk_id(source_ref: str, chunk: Mapping[str, Any]) -> str:
    """A deterministic per-chunk id the contract fake mints on push.

    The receiving schema hands back chunk ids on a real push; the
    fake stands in for that side, so it mints an id from the slice
    text + envelope key. Deterministic so a replay returns the same
    block ids — the wire-level idempotency key alone is not enough;
    the chunk ids a replay returns must match.
    """
    payload = {
        "source_ref": str(source_ref),
        "index": int(chunk["index"]),
        "content": str(chunk["content"]),
    }
    return "fake-" + hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _fake_chunk_content_id(
    source_ref: str,
    chunk: Mapping[str, Any],
    metadata: Mapping[str, Any],
) -> str:
    """A deterministic per-chunk content identity the fake derives.

    Mirrors :func:`fin.corpus.push.content_identity`: sha256 over
    ``(chunk text, normalization rule version, parse rule version,
    params table version)``. The wire envelope does not carry this
    value (it only carries the body-level identity); the fake
    derives it on push so the per-child invalidation key
    (``(source_ref, content_id)``) still resolves, even when a test
    pushes a hand-rolled envelope that omits the per-chunk key.
    """
    parts = (
        str(chunk["content"]),
        str(metadata.get("normalization_rule_version", "")),
        str(metadata.get("parse_rule_version", "")),
        str(metadata.get("params_table_version", "")),
    )
    return hashlib.sha256(_ID_SEPARATOR.join(parts).encode("utf-8")).hexdigest()


class FakePushPort:
    """An idempotent in-memory store satisfying ``PushPort``.

    The store is keyed on the wire envelope's ``(source_ref, body
    content identity)`` pair and stored per-chunk: every chunk in a
    pushed envelope gets its own block id (deterministic from
    ``source_ref`` + ``index`` + ``content``) and its own validity
    interval. ``block_total()`` and ``block_ids()`` therefore report
    per-chunk counts — the unit the receiving service hands block ids
    back in, and the unit the citation gate keys on.

    Counters (:meth:`written_total`, :meth:`rejected_batches`) exist so
    tests can assert that a refused batch left a trace and did not
    contribute to the idempotency numbers.

    ``max_batch_records`` is the receiving side's per-call cap (SC44).
    Like the SC20 metadata check it lives here rather than in the
    client, so a future real transport cannot smuggle an over-sized
    batch past a limit that only existed client-side; a test can lower
    it to exercise the overflow path cheaply.
    """

    def __init__(
        self,
        *,
        max_batch_records: int = MAX_PUSH_BATCH_RECORDS,
        input_validation_ledger: InputValidationLedger | None = None,
    ) -> None:
        # Per-envelope store: the envelope row + its chunks, indexed
        # by the wire-level idempotency key (source_ref, body_content_id).
        self._by_key: dict[tuple[str, str], dict[str, Any]] = {}
        # Per-chunk index: every chunk in the store (across envelopes),
        # keyed by its deterministic block id. ``block_total`` and
        # ``block_ids`` answer off this index.
        self._by_block_id: dict[str, dict[str, Any]] = {}
        # Per-chunk invalidation index: the invalidation path emits
        # one ``InvalidationTarget`` per previously-pushed child
        # block, with the per-child ``content_id`` (the per-chunk
        # content identity). The wire envelope no longer carries a
        # per-child content identity, so the fake builds the index
        # itself from the envelope metadata when the chunk carries
        # ``content_id`` (via the wire ``chunk.content_id``) or, as
        # a stand-in for the spec pair, derives it from the slice
        # text + the same rule versions the body identity uses.
        self._by_chunk_invalidation_key: dict[
            tuple[str, str], tuple[dict[str, Any], dict[str, Any]]
        ] = {}
        self._idempotency_lock = threading.Lock()
        self._rejected_batches: int = 0
        self._invalidations: list[dict[str, Any]] = []
        self._refused_invalidations: int = 0
        self.max_batch_records = max_batch_records
        self._input_validation_ledger = (
            input_validation_ledger
            if input_validation_ledger is not None
            else InputValidationLedger()
        )

    @property
    def input_validation_failures(self) -> InputValidationLedger:
        """The「输入校验失败」count for refusals taken on this side (T21).

        A store built without one still counts: the default is an
        in-memory ledger created on first access, so an empty batch or an
        empty document is a number here even when no log is configured.
        """
        return self._input_validation_ledger

    # ----- push -----

    def push(self, envelopes: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """Store ``envelopes`` idempotently and report the outcome.

        Validation of the whole batch happens first: an empty,
        over-sized or incomplete batch is refused before a single write,
        so the store can never hold half of a refused push and never a
        truncated prefix of one (SC44). An empty batch is invalid input
        (§B2 第 5 条) and is refused here as well as client-side, so a
        future transport cannot pass one off as an empty result; an
        envelope with no body is the same kind of thing (T21) and is
        counted on :attr:`input_validation_failures`.

        Wire-shape invariants checked here (one envelope):

        * ``source_ref`` is a non-empty string;
        * ``content`` / ``body`` is a non-empty string;
        * ``chunks`` is a non-empty list of ``{content, index}`` mappings
          with monotonic ``index`` starting at 0;
        * every chunk ``content`` is a contiguous substring of the body
          (SC11 / SC16 「连续子串」);
        * the SC20 metadata filter fields are present and well-typed.
        """
        ledger = self.input_validation_failures
        try:
            validate_batch_not_empty(envelopes, ledger=ledger)
            if len(envelopes) > self.max_batch_records:
                raise PushBatchOverflowError(
                    f"push batch of {len(envelopes)} envelopes exceeds the "
                    f"per-call cap of {self.max_batch_records}"
                )
            for envelope in envelopes:
                validate_wire_record(envelope)
                validate_wire_record_text(envelope, ledger=ledger)
                validate_envelope_chunks(envelope)
        except PushError:
            self._rejected_batches += 1
            raise

        # Idempotency on the envelope: wire-level
        # ``(source_ref, body content identity)``. The fake reconstructs
        # the body content identity from the envelope metadata + body
        # text (one canonical hash over the body + the three rule
        # versions) so the contract fake matches what the adapter
        # computes.
        block_ids: list[str] = []
        written = 0
        duplicates = 0
        for envelope in envelopes:
            source_ref = str(envelope["source_ref"])
            body_key = _envelope_body_content_id(envelope)
            key = (source_ref, body_key)
            with self._idempotency_lock:
                stored = self._by_key.get(key)
                if stored is None:
                    # Deep-enough copy: the metadata mapping is copied
                    # so a caller mutating its wire dict afterwards
                    # cannot alter what the "service" holds. The chunks
                    # list is rebuilt as a fresh list of fresh dicts so
                    # the same isolation covers the slices.
                    stored = dict(envelope)
                    stored["metadata"] = dict(envelope["metadata"])
                    stored["chunks"] = [
                        {
                            "index": chunk["index"],
                            "content": chunk["content"],
                            # Preserve the wire-side ``id`` /
                            # ``content_id`` when the producer
                            # provided them (the per-child
                            # ``PushRecord`` shim pins both, so the
                            # catalog side's pre-T1 block ids stay
                            # round-trippable).
                            **(
                                {"id": chunk["id"]}
                                if "id" in chunk
                                else {}
                            ),
                            **(
                                {"content_id": chunk["content_id"]}
                                if "content_id" in chunk
                                else {}
                            ),
                        }
                        for chunk in envelope["chunks"]
                    ]
                    stored["valid_from"] = VALID_FROM
                    stored["valid_to"] = None
                    # Per-chunk validity: every slice starts open; an
                    # invalidation closes one slice without affecting
                    # siblings.
                    for chunk in stored["chunks"]:
                        chunk["valid_from"] = VALID_FROM
                        chunk["valid_to"] = None
                    self._by_key[key] = stored
                    # Index every chunk by its deterministic block id
                    # so callers (citation gate, retrieve side) can
                    # still recall by block id, and also by its
                    # per-chunk content identity so the invalidation
                    # path (which is keyed per child) can resolve the
                    # target back to the right slice.
                    chunk_block_ids: list[str] = []
                    for chunk in stored["chunks"]:
                        chunk_block_id = chunk.get("id")
                        if chunk_block_id is None:
                            chunk_block_id = _fake_chunk_id(
                                source_ref, chunk
                            )
                            chunk["id"] = chunk_block_id
                        self._by_block_id[chunk_block_id] = {
                            "envelope": stored,
                            "chunk": chunk,
                        }
                        chunk_content_id = chunk.get("content_id")
                        if chunk_content_id is None:
                            chunk_content_id = _fake_chunk_content_id(
                                source_ref, chunk, stored["metadata"]
                            )
                            chunk["content_id"] = chunk_content_id
                        self._by_chunk_invalidation_key[
                            (source_ref, chunk_content_id)
                        ] = (stored, chunk)
                        chunk_block_ids.append(chunk_block_id)
                    # Counted per chunk (the response shape after T1
                    # is one block id per chunk, with ``written`` /
                    # ``duplicates`` summing to the chunk total).
                    written += len(stored["chunks"])
                    block_ids.extend(chunk_block_ids)
                else:
                    duplicates += len(stored["chunks"])
                    block_ids.extend(
                        chunk.get("id") or _fake_chunk_id(source_ref, chunk)
                        for chunk in stored["chunks"]
                    )
        return {
            "block_ids": block_ids,
            "written": written,
            "duplicates": duplicates,
        }

    # ----- recall -----

    def recall(self, block_id: str) -> dict[str, Any]:
        """Return the stored envelope for ``block_id``, byte-identical to
        what was pushed.

        A `source_ref` is not block identity (§B4), so passing one here
        raises :class:`UnknownPushedBlockError` instead of returning an
        empty row.
        """
        entry = self._by_block_id.get(block_id)
        if entry is None:
            raise UnknownPushedBlockError(
                f"no pushed block with id {block_id!r}"
            )
        stored = entry["envelope"]
        row = dict(stored)
        row["metadata"] = dict(stored["metadata"])
        row["chunks"] = [
            {
                "index": chunk["index"],
                "content": chunk["content"],
                "id": chunk["id"],
            }
            for chunk in stored["chunks"]
        ]
        return row

    # ----- invalidation (plan T18, §B4) -----

    def invalidate(
        self, targets: Sequence[Mapping[str, Any]]
    ) -> dict[str, Any]:
        """Close the validity interval of every target, idempotently.

        The whole request is resolved before a single interval is closed:
        an empty request or a target whose ``(source_ref, content_id)``
        key the store never accepted is refused by name with nothing
        closed, so a refused invalidation cannot half-land.

        ``content_id`` here is the per-child content identity the
        invalidation plan emits (one per child block the corpus side
        pushed before), which the fake stores alongside the chunk it
        corresponds to so the per-child key still resolves to the
        right envelope + slice.

        A target already closed is reported under ``already_closed`` with
        its original closing instant, which is what makes a repeated
        request return the same answer.
        """
        try:
            validate_targets_not_empty(targets)
            resolved = [self._resolve_target(target) for target in targets]
        except InvalidationError:
            self._refused_invalidations += 1
            raise

        closed_at: dict[str, int] = {}
        block_ids: list[str] = []
        closed = 0
        already_closed = 0
        for target, stored, chunk in resolved:
            block_id = str(chunk["id"])
            if chunk.get("valid_to") is None:
                # 关闭时间有效区间: [valid_from, valid_from) is empty, so no
                # query instant can hit it. The row is NOT deleted.
                chunk["valid_to"] = chunk["valid_from"]
                # The envelope-level ``valid_to`` is the AND of its
                # chunks — it flips to the first closing instant and
                # never recovers.
                if stored["valid_to"] is None:
                    stored["valid_to"] = chunk["valid_from"]
                closed += 1
                self._invalidations.append(
                    {
                        "block_id": block_id,
                        "source_ref": str(stored["source_ref"]),
                        "content_id": str(target.get("content_id", "")),
                        "reason": str(target.get("reason", "")),
                        "closed_at": chunk["valid_to"],
                    }
                )
            else:
                already_closed += 1
            block_ids.append(block_id)
            closed_at[block_id] = chunk["valid_to"]
        return {
            "block_ids": block_ids,
            "closed": closed,
            "already_closed": already_closed,
            "closed_at": closed_at,
        }

    def _resolve_target(
        self, target: Mapping[str, Any]
    ) -> tuple[Mapping[str, Any], dict[str, Any], dict[str, Any]]:
        key = (str(target.get("source_ref")), str(target.get("content_id")))
        resolved = self._by_chunk_invalidation_key.get(key)
        if resolved is None:
            raise InvalidationUnknownTargetError(
                f"no pushed chunk with idempotency key {key!r}; refusing "
                "the whole invalidation request"
            )
        stored, chunk = resolved
        return (target, stored, chunk)

    def hits(self, *, at: int) -> tuple[dict[str, Any], ...]:
        """Every stored envelope with at least one chunk hit by ``at``."""
        # Deduplicate envelopes that have multiple hittable chunks.
        seen: set[int] = set()
        out: list[dict[str, Any]] = []
        for entry in self._by_block_id.values():
            stored = entry["envelope"]
            chunk = entry["chunk"]
            if id(stored) in seen:
                continue
            if self._is_valid_at(chunk, at):
                seen.add(id(stored))
                out.append(dict(stored))
        return tuple(out)

    def count_hits(self, block_id: str, *, at: int) -> int:
        """How often ``block_id`` is hit by a query at instant ``at``
        (SC24 asserts 0 for an invalidated block)."""
        entry = self._by_block_id.get(block_id)
        if entry is None:
            return 0
        chunk = entry["chunk"]
        return 1 if self._is_valid_at(chunk, at) else 0

    def is_hittable_at_any_time(self, block_id: str) -> bool:
        """Whether *some* query instant hits ``block_id``.

        The universal SC24 claim「以任意查询时刻检索该旧块出现次数 = 0」,
        answered by interval emptiness rather than by sampling instants.
        """
        entry = self._by_block_id.get(block_id)
        if entry is None:
            raise UnknownPushedBlockError(
                f"no pushed block with id {block_id!r}"
            )
        chunk = entry["chunk"]
        valid_to = chunk["valid_to"]
        return valid_to is None or valid_to > chunk["valid_from"]

    @staticmethod
    def _is_valid_at(chunk: Mapping[str, Any], at: int) -> bool:
        if at < chunk["valid_from"]:
            return False
        valid_to = chunk["valid_to"]
        return valid_to is None or at < valid_to

    def physically_present(self, block_id: str) -> bool:
        """§B4「不物理删除」: the row survives its invalidation."""
        return block_id in self._by_block_id

    def invalidation_state(self) -> dict[str, int]:
        """``block_id -> closing instant`` for every closed interval."""
        return {
            block_id: entry["chunk"]["valid_to"]
            for block_id, entry in self._by_block_id.items()
            if entry["chunk"]["valid_to"] is not None
        }

    def invalidation_records(self) -> tuple[dict[str, Any], ...]:
        """The service side's own queryable closure record (§B4)."""
        return tuple(dict(record) for record in self._invalidations)

    def refused_invalidations(self) -> int:
        """Invalidation requests refused with a named failure."""
        return self._refused_invalidations

    # ----- assertions surface -----

    def block_total(self) -> int:
        """The number of stored chunks (SC18's 块总数, counted per
        block id — the unit the citation gate keys on)."""
        return len(self._by_block_id)

    def block_ids(self) -> set[str]:
        """The stored block id set (SC18's 块标识集合)."""
        return set(self._by_block_id)

    def written_total(self) -> int:
        """First-time envelopes accepted over the store's lifetime."""
        return len(self._by_key)

    def rejected_batches(self) -> int:
        """Batches refused with a named failure — the trace that keeps a
        failed push out of the idempotency numbers without silence."""
        return self._rejected_batches


__all__ = ["VALID_FROM", "FakePushPort"]
