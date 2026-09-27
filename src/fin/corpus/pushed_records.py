"""Durable per-document pushed-record store — the previous_records provider.

Replacement closure needs the PushRecords of an earlier upload after the
process that pushed them is gone. Nothing durable stored per-document
pushed records, so this store keeps one append-only JSONL event per
ingestion generation, replayed on construction.

The reader wired into the endpoint is a zero-argument callable over
EVERY document's records (``all_records``); plan_replacement_invalidations
filters by document_id itself and skips the incoming version, so a
same-version re-ingest plans no targets. There is deliberately no
per-document provider closure: the endpoint seam cannot receive the
document, so the all-records view plus planner-side filtering is the
single production path.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from fin.corpus.push import PushRecord
from fin.retrieve._ledger.jsonl import append_jsonl_object, replay_jsonl_objects

EVENT_RECORDED = "recorded"

_PUSHED_RECORDS_FILE = "pushed_records.jsonl"


class PushedRecordsUnwritableError(Exception):
    code = "pushed_records_unwritable"


class PushedRecordsUnreadableError(Exception):
    code = "pushed_records_unreadable"


class PushedRecordsCorruptError(Exception):
    code = "pushed_records_corrupt"


def pushed_records_path_for_catalog(catalog_path: Path) -> Path:
    """The journal that sits beside a catalog, so one data dir holds both."""
    return Path(catalog_path).parent / _PUSHED_RECORDS_FILE


def _record_to_payload(record: PushRecord) -> dict[str, Any]:
    return {
        "block_id": record.block_id,
        "source_ref": record.source_ref,
        "content_id": record.content_id,
        "text": record.text,
        "parent_block_id": record.parent_block_id,
        "parent_text": record.parent_text,
        "metadata": dict(record.metadata),
    }


def _payload_to_record(payload: object, path: Path, number: int) -> PushRecord:
    if not isinstance(payload, dict):
        raise PushedRecordsCorruptError(
            f"pushed records {path} line {number} is not an object"
        )
    missing = {
        "block_id",
        "source_ref",
        "content_id",
        "text",
        "parent_block_id",
        "parent_text",
    } - set(payload)
    if missing:
        raise PushedRecordsCorruptError(
            f"pushed records {path} line {number} lacks {sorted(missing)}"
        )
    metadata = payload.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise PushedRecordsCorruptError(
            f"pushed records {path} line {number} metadata is not an object"
        )
    return PushRecord(
        block_id=str(payload["block_id"]),
        source_ref=str(payload["source_ref"]),
        content_id=str(payload["content_id"]),
        text=str(payload["text"]),
        parent_block_id=str(payload["parent_block_id"]),
        parent_text=str(payload["parent_text"]),
        metadata=dict(metadata),
    )


class PushedRecordsStore:
    """Append-only log of pushed records, keyed (document_id, version)."""

    def __init__(self, *, path: Path | None = None) -> None:
        self._path = path
        # (document_id, version) -> {block_id: record}
        self._groups: dict[tuple[str, str], dict[str, PushRecord]] = {}
        if path is not None and path.exists():
            self._replay(path)

    @property
    def path(self) -> Path | None:
        return self._path

    # ----- recording -----

    def replace(
        self,
        document_id: str,
        version: str,
        records: Iterable[PushRecord],
    ) -> None:
        """Persist one ingestion generation; re-recording replaces it.

        The append is the idempotency unit: replaying the same key twice
        leaves only the last write, so a re-ingest never duplicates blocks.
        """
        if not document_id or not version:
            raise ValueError("document_id and version are required")
        by_block = {record.block_id: record for record in records}
        event = {
            "event": EVENT_RECORDED,
            "document_id": document_id,
            "version": version,
            "records": [_record_to_payload(record) for record in by_block.values()],
        }
        append_jsonl_object(
            self._path,
            event,
            unwritable=PushedRecordsUnwritableError,
            message=f"cannot append pushed records at {self._path}",
        )
        self._groups[(document_id, version)] = by_block

    # ----- reading -----

    def all_records(
        self,
        *,
        exclude_generation: tuple[str, str] | None = None,
    ) -> list[PushRecord]:
        """Every stored record, minus one excluded generation.

        The service-level previous_records shape: a zero-argument call
        returns the stale candidates; the planner filters by document.
        ``exclude_generation`` is an exact ``(document_id, version)``
        pair — never a bare version — so two documents that share a
        version string cannot mutually exclude each other's records.
        """
        if exclude_generation is not None and (
            len(exclude_generation) != 2 or not all(exclude_generation)
        ):
            raise ValueError(
                "exclude_generation must be a non-empty "
                f"(document_id, version) pair, got {exclude_generation!r}"
            )
        return [
            record
            for (document_id, version), by_block in self._groups.items()
            if (document_id, version) != exclude_generation
            for record in by_block.values()
        ]

    def records_for(self, document_id: str) -> list[PushRecord]:
        """All records of one document, every stored version."""
        return [
            record
            for (stored_id, _version), by_block in self._groups.items()
            if stored_id == document_id
            for record in by_block.values()
        ]

    def versions(self, document_id: str) -> list[str]:
        """Stored versions of one document, first-recorded first."""
        return [
            version
            for (stored_id, version) in self._groups
            if stored_id == document_id
        ]

    def __len__(self) -> int:
        return sum(len(by_block) for by_block in self._groups.values())

    # ----- persistence -----

    def _replay(self, path: Path) -> None:
        replay_jsonl_objects(
            path,
            lambda event, number: self._apply_or_corrupt(event, number, path),
            on_read_oserror=lambda exc: PushedRecordsUnreadableError(
                f"cannot read pushed records at {path}: "
                f"{type(exc).__name__}: {exc}"
            ),
            on_unicode=lambda exc: PushedRecordsCorruptError(
                f"pushed records at {path} is not valid UTF-8: {exc}"
            ),
            on_json=lambda exc, number: PushedRecordsCorruptError(
                f"pushed records {path} line {number} is not valid JSON: {exc}"
            ),
        )

    def _apply_or_corrupt(
        self, event: object, number: int, path: Path
    ) -> None:
        try:
            self._apply_replayed_event(event, number, path)
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise PushedRecordsCorruptError(
                f"pushed records {path} line {number} is not a "
                f"record event: {exc}"
            ) from exc

    def _apply_replayed_event(
        self, event: object, number: int, path: Path
    ) -> None:
        if not isinstance(event, dict):
            raise TypeError(f"not an object: {type(event).__name__}")
        if event.get("event") != EVENT_RECORDED:
            raise ValueError(f"unknown event {event.get('event')!r}")
        document_id = str(event["document_id"])
        version = str(event["version"])
        rows = event["records"]
        if not isinstance(rows, list):
            raise TypeError("records is not a list")
        # Replaying a later event for the same key replaces the earlier
        # one wholesale — dropped blocks stay dropped.
        group: dict[str, PushRecord] = {}
        for row in rows:
            record = _payload_to_record(row, path, number)
            group[record.block_id] = record
        self._groups[(document_id, version)] = group
