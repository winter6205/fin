"""In-process contract fake for the push path (plan T14, SC18 / SC20).

The fake stands in for the retrieval service so the default suite can
assert the §B4 push contract without a service process, a network hop
or a large corpus. It is a **contract** fake, not a stub: it enforces
the service-side half of the contract itself, so a future real
transport cannot smuggle a record past a check that only lived in the
client.

What the fake guarantees
------------------------

* **wire field names are verbatim §B4** — records are stored and
  recalled under ``source_ref`` / ``block_id`` / ``text`` /
  ``parent_text`` / ``content_id`` / ``metadata``;
* **idempotency** — the store is keyed on the record's
  ``(source_ref, content_id)`` pair; a repeat push returns the stored
  block id and writes nothing;
* **no re-splitting** (§B4「不二次切分」) — the text is stored exactly
  as pushed and recalled byte-identical, never re-normalized, trimmed
  or re-chunked;
* **named overflow** (SC44) — a batch above the per-call cap raises
  ``PushBatchOverflowError`` and stores nothing, never a truncated
  prefix;
* **named refusal of invalid input** — an empty batch raises
  ``PushEmptyBatchError`` (§B2 第 5 条) instead of being answered with a
  zero-write success;
* **named refusal** — an incomplete record raises
  ``PushMetadataIncompleteError`` and the whole batch is refused, so a
  refused push leaves the block total untouched and is counted in
  :meth:`rejected_batches` rather than passing as an empty success.

Invalidation semantics (plan T18, SC24, §B4「内容失效语义」)
--------------------------------------------------------

The fake also stands in for the service's invalidation action, since
「关闭时间有效区间、不物理删除、幂等」is retrieval-side semantics that fin's
SC24 assertions need something to stand on:

* every stored block carries a **validity interval** ``[valid_from,
  valid_to)`` over a logical instant (an integer — no clock enters, so
  the fake stays deterministic), open-ended while valid;
* :meth:`invalidate` **closes** the interval (``valid_to = valid_from``,
  i.e. empty), so the block is hit at no query time whatsoever —
  :meth:`is_hittable_at_any_time` answers that claim directly rather
  than by sampling;
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
    validate_wire_record,
    validate_wire_record_text,
)

# The logical instant every pushed block becomes valid at. Closing an
# interval sets ``valid_to`` to this same value, making the half-open
# interval empty — no query instant can fall inside it.
VALID_FROM: Final[int] = 0


class FakePushPort:
    """An idempotent in-memory store satisfying ``PushPort``.

    The store maps each idempotency key to the accepted wire record.
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
        self._by_key: dict[tuple[str, str], dict[str, Any]] = {}
        self._by_block_id: dict[str, dict[str, Any]] = {}
        self._rejected_batches: int = 0
        self._invalidations: list[dict[str, Any]] = []
        self._refused_invalidations: int = 0
        self.max_batch_records = max_batch_records
        self._input_validation_ledger = input_validation_ledger

    @property
    def input_validation_failures(self) -> InputValidationLedger:
        """The「输入校验失败」count for refusals taken on this side (T21).

        A store built without one still counts: the default is an
        in-memory ledger created on first access, so an empty batch or an
        empty document is a number here even when no log is configured.
        """
        ledger = self._input_validation_ledger
        if ledger is None:
            ledger = InputValidationLedger()
            self._input_validation_ledger = ledger
        return ledger

    # ----- push -----

    def push(self, records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """Store ``records`` idempotently and report the outcome.

        Validation of the whole batch happens first: an empty,
        over-sized or incomplete batch is refused before a single write,
        so the store can never hold half of a refused push and never a
        truncated prefix of one (SC44). An empty batch is invalid input
        (§B2 第 5 条) and is refused here as well as client-side, so a
        future transport cannot pass one off as an empty result; a record
        with no text is the same kind of thing (T21) and is counted on
        :attr:`input_validation_failures`.
        """
        ledger = self.input_validation_failures
        try:
            validate_batch_not_empty(records, ledger=ledger)
            if len(records) > self.max_batch_records:
                raise PushBatchOverflowError(
                    f"push batch of {len(records)} records exceeds the "
                    f"per-call cap of {self.max_batch_records}"
                )
            for record in records:
                validate_wire_record(record)
                validate_wire_record_text(record, ledger=ledger)
        except PushError:
            self._rejected_batches += 1
            raise

        block_ids: list[str] = []
        written = 0
        duplicates = 0
        for record in records:
            key = (str(record["source_ref"]), str(record["content_id"]))
            stored = self._by_key.get(key)
            if stored is None:
                # Deep-enough copy: the metadata mapping is copied so a
                # caller mutating its wire dict afterwards cannot alter
                # what the "service" holds.
                stored = dict(record)
                stored["metadata"] = dict(record["metadata"])
                stored["valid_from"] = VALID_FROM
                stored["valid_to"] = None
                self._by_key[key] = stored
                self._by_block_id[stored["block_id"]] = stored
                written += 1
            else:
                duplicates += 1
            block_ids.append(str(stored["block_id"]))
        return {
            "block_ids": block_ids,
            "written": written,
            "duplicates": duplicates,
        }

    # ----- recall -----

    def recall(self, block_id: str) -> dict[str, Any]:
        """Return the stored record for ``block_id``, byte-identical to
        what was pushed.

        A `source_ref` is not block identity (§B4), so passing one here
        raises :class:`UnknownPushedBlockError` instead of returning an
        empty row.
        """
        stored = self._by_block_id.get(block_id)
        if stored is None:
            raise UnknownPushedBlockError(
                f"no pushed block with id {block_id!r}"
            )
        row = dict(stored)
        row["metadata"] = dict(stored["metadata"])
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

        block_ids: list[str] = []
        closed_at: dict[str, int] = {}
        closed = 0
        already_closed = 0
        for target, stored in resolved:
            block_id = str(stored["block_id"])
            if stored["valid_to"] is None:
                # 关闭时间有效区间: [valid_from, valid_from) is empty, so no
                # query instant can hit it. The row is NOT deleted.
                stored["valid_to"] = stored["valid_from"]
                closed += 1
                self._invalidations.append(
                    {
                        "block_id": block_id,
                        "source_ref": str(stored["source_ref"]),
                        "content_id": str(stored["content_id"]),
                        "reason": str(target.get("reason", "")),
                        "closed_at": stored["valid_to"],
                    }
                )
            else:
                already_closed += 1
            block_ids.append(block_id)
            closed_at[block_id] = stored["valid_to"]
        return {
            "block_ids": block_ids,
            "closed": closed,
            "already_closed": already_closed,
            "closed_at": closed_at,
        }

    def _resolve_target(
        self, target: Mapping[str, Any]
    ) -> tuple[Mapping[str, Any], dict[str, Any]]:
        key = (str(target.get("source_ref")), str(target.get("content_id")))
        stored = self._by_key.get(key)
        if stored is None:
            raise InvalidationUnknownTargetError(
                f"no pushed block with idempotency key {key!r}; refusing "
                "the whole invalidation request"
            )
        return (target, stored)

    def hits(self, *, at: int) -> tuple[dict[str, Any], ...]:
        """Every stored block whose validity interval contains ``at``."""
        return tuple(
            dict(stored)
            for stored in self._by_block_id.values()
            if self._is_valid_at(stored, at)
        )

    def count_hits(self, block_id: str, *, at: int) -> int:
        """How often ``block_id`` is hit by a query at instant ``at``
        (SC24 asserts 0 for an invalidated block)."""
        stored = self._by_block_id.get(block_id)
        if stored is None:
            return 0
        return 1 if self._is_valid_at(stored, at) else 0

    def is_hittable_at_any_time(self, block_id: str) -> bool:
        """Whether *some* query instant hits ``block_id``.

        The universal SC24 claim「以任意查询时刻检索该旧块出现次数 = 0」,
        answered by interval emptiness rather than by sampling instants.
        """
        stored = self._by_block_id.get(block_id)
        if stored is None:
            raise UnknownPushedBlockError(
                f"no pushed block with id {block_id!r}"
            )
        valid_to = stored["valid_to"]
        return valid_to is None or valid_to > stored["valid_from"]

    @staticmethod
    def _is_valid_at(stored: Mapping[str, Any], at: int) -> bool:
        if at < stored["valid_from"]:
            return False
        valid_to = stored["valid_to"]
        return valid_to is None or at < valid_to

    def physically_present(self, block_id: str) -> bool:
        """§B4「不物理删除」: the row survives its invalidation."""
        return block_id in self._by_block_id

    def invalidation_state(self) -> dict[str, int]:
        """``block_id -> closing instant`` for every closed interval."""
        return {
            block_id: stored["valid_to"]
            for block_id, stored in self._by_block_id.items()
            if stored["valid_to"] is not None
        }

    def invalidation_records(self) -> tuple[dict[str, Any], ...]:
        """The service side's own queryable closure record (§B4)."""
        return tuple(dict(record) for record in self._invalidations)

    def refused_invalidations(self) -> int:
        """Invalidation requests refused with a named failure."""
        return self._refused_invalidations

    # ----- assertions surface -----

    def block_total(self) -> int:
        """The number of stored blocks (SC18's 块总数)."""
        return len(self._by_block_id)

    def block_ids(self) -> set[str]:
        """The stored block id set (SC18's 块标识集合)."""
        return set(self._by_block_id)

    def written_total(self) -> int:
        """First-time writes accepted over the store's lifetime."""
        return len(self._by_key)

    def rejected_batches(self) -> int:
        """Batches refused with a named failure — the trace that keeps a
        failed push out of the idempotency numbers without silence."""
        return self._rejected_batches


__all__ = ["VALID_FROM", "FakePushPort"]
