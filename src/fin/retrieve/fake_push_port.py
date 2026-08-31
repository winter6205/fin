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
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from fin.retrieve.push_port import (
    MAX_PUSH_BATCH_RECORDS,
    PushBatchOverflowError,
    PushError,
    UnknownPushedBlockError,
    validate_batch_not_empty,
    validate_wire_record,
)


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

    def __init__(self, *, max_batch_records: int = MAX_PUSH_BATCH_RECORDS) -> None:
        self._by_key: dict[tuple[str, str], dict[str, Any]] = {}
        self._by_block_id: dict[str, dict[str, Any]] = {}
        self._rejected_batches: int = 0
        self.max_batch_records = max_batch_records

    # ----- push -----

    def push(self, records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """Store ``records`` idempotently and report the outcome.

        Validation of the whole batch happens first: an empty,
        over-sized or incomplete batch is refused before a single write,
        so the store can never hold half of a refused push and never a
        truncated prefix of one (SC44). An empty batch is invalid input
        (§B2 第 5 条) and is refused here as well as client-side, so a
        future transport cannot pass one off as an empty result.
        """
        try:
            validate_batch_not_empty(records)
            if len(records) > self.max_batch_records:
                raise PushBatchOverflowError(
                    f"push batch of {len(records)} records exceeds the "
                    f"per-call cap of {self.max_batch_records}"
                )
            for record in records:
                validate_wire_record(record)
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


__all__ = ["FakePushPort"]
