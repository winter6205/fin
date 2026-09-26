"""Preparation lifecycle for thread-owned attachments (ADR 0086).

The HTTP door accepts bytes and gets back an ``uploaded`` record; this
service owns what happens after that, once preparation has been asked
for — freezing the parsed text into versioned units, building the
thread-private derived index over the same text, and publishing
``ready`` only when both are usable. A restart resumes or rebuilds
whatever is missing instead of trusting a stored status, and deleting
the thread removes every layer this service created.

``uploaded`` is a deliberate resting state: durable bytes nobody has
asked to prepare. Recovery repairs rows whose preparation is in flight,
never an accepted upload, so a restart cannot start work that was never
requested.

Layered recovery is what makes ``ready`` mean something: raw bytes in the
object store, parsed units in the attachment store, derived postings on
top of those units. Recovery repairs from the layer below, so a crash
between any two of them is rebuilt rather than reported as a stale
``ready`` or as an empty successful search.
"""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass, replace
from typing import Sequence

from fin.corpus.object_store import ObjectStore
from fin.host.attachment_index import build_postings, is_usable
from fin.host.attachment_store import (
    AttachmentRecord,
    AttachmentStore,
    AttachmentStoreError,
    AttachmentThreadGoneError,
    ParsedAttachmentUnit,
)

logger = logging.getLogger(__name__)

_EMPTY_TEXT = "attachment_empty_text"
_NOT_TEXT = "attachment_not_text"
_UNAVAILABLE = "source_unavailable"
_INDEX_UNAVAILABLE = "attachment_index_unavailable"


# ----- typed door errors -----


class AttachmentError(Exception):
    """Base class for attachment-door typed errors."""


class AttachmentNotFoundError(AttachmentError):
    """Unknown attachment id, or an attachment owned by another thread.

    Both cases share this error so a caller on one thread cannot probe
    whether another thread holds a given attachment.
    """


class AttachmentNotReadyError(AttachmentError):
    """The named attachment has no usable parsed text and index yet."""


class AttachmentCleanupError(AttachmentError):
    """Raw bytes survived a thread delete, so the thread is NOT dropped.

    The route reports it as a named failure and the operator can retry:
    object deletion is idempotent, and nothing durable was removed while
    this error was raised.
    """

    def __init__(self, thread_id: str, keys: Sequence[str]) -> None:
        super().__init__(
            f"attachment bytes survived delete for thread_id={thread_id!r}: "
            f"{len(keys)} key(s) could not be removed"
        )
        self.thread_id = thread_id
        self.keys = tuple(keys)


@dataclass(frozen=True)
class ResolvedAttachment:
    """One ready attachment as a turn sees it."""

    attachment_id: str
    filename: str
    text: str


def _safe_display_filename(raw: str) -> str:
    """Strip path and control characters; the model gets a name, never a path."""
    name = os.path.basename(raw or "").strip()
    name = "".join(char for char in name if char.isprintable())
    return name[:200] or "attachment.txt"


def text_rejection_reason(payload: bytes) -> str | None:
    """Name why these bytes cannot be a text attachment, or ``None``.

    Preparation owns this call: the door stores raw bytes without
    classifying them, so the rejection is what a stored-but-unusable
    upload is reported as when its preparation runs.
    """
    try:
        payload.decode("utf-8")
    except UnicodeDecodeError:
        return _NOT_TEXT
    return None


# ----- the service -----


class AttachmentPreparationService:
    """Owns accept → prepare → ready/failed, restart recovery, and deletion."""

    def __init__(self, *, store: AttachmentStore, objects: ObjectStore) -> None:
        self.store = store
        self.objects = objects

    # -- door: accept --

    def accept(
        self, *, thread_id: str, filename: str, payload: bytes
    ) -> AttachmentRecord:
        """Store the raw bytes and record the attachment as ``uploaded``.

        Nothing else happens here: no parse, no OCR, no layout, no vision,
        no index. The bytes land first so an orphan is removable; a store
        rejection (thread gone mid-upload, backing failure) takes those
        bytes with it so the door never leaves bytes no record owns.
        """
        attachment_id = str(uuid.uuid4())
        key = f"thread-attachments/{thread_id}/{attachment_id}"
        try:
            pointer = self.objects.put(
                key, payload, content_type="text/plain"
            )
        except Exception as exc:
            # EXIT: the byte store rejected the upload; there is nothing to
            # record and the door turns this into its named store failure.
            raise AttachmentStoreError(
                f"object store rejected attachment upload: {exc}"
            ) from exc
        try:
            return self.store.create(
                thread_id=thread_id,
                attachment_id=attachment_id,
                filename=_safe_display_filename(filename),
                size_bytes=pointer.size_bytes,
                sha256=pointer.sha256,
                object_key=pointer.key,
            )
        except AttachmentStoreError:
            self._discard_bytes(thread_id, (pointer.key,))
            raise

    # -- lifecycle: prepare --

    def _require(self, attachment_id: str) -> AttachmentRecord:
        record = self.store.get(attachment_id)
        if record is None:
            raise AttachmentNotFoundError(attachment_id)
        return record

    def prepare(self, attachment_id: str) -> AttachmentRecord:
        """Freeze parsed units and the derived index, then publish ready.

        This is also the retry path: a record that never reached ``ready`` is
        driven again from its retained bytes, while a published version is
        left alone because earlier answers cite its locators.
        """
        record = self._require(attachment_id)
        if record.searchable:
            # EXIT: already published and usable — re-preparing would mint a
            # version for identical bytes and race a reader that is done.
            return record
        return self._prepare(record, version=record.version + 1)

    def _prepare(self, record: AttachmentRecord, *, version: int) -> AttachmentRecord:
        attachment_id = record.attachment_id
        try:
            payload = self.objects.get(record.object_key)
        except Exception as exc:
            # EXIT: the byte store is unreachable, so no text exists to
            # freeze. Named failure, never an empty ready attachment.
            logger.warning(
                "attachment bytes unreadable: attachment_id=%s (%s)",
                attachment_id,
                exc,
            )
            return self.store.mark_failed(
                attachment_id=attachment_id, version=record.version, reason=_UNAVAILABLE
            )
        return self._freeze(record, version=version, payload=payload)

    def _freeze(
        self, record: AttachmentRecord, *, version: int, payload: bytes
    ) -> AttachmentRecord:
        attachment_id = record.attachment_id
        units = self._parse(record.filename, payload)
        if isinstance(units, str):
            return self.store.mark_failed(
                attachment_id=attachment_id, version=record.version, reason=units
            )
        try:
            unit_count = self.store.record_units(
                attachment_id=attachment_id, version=version, units=units
            )
            self.store.put_index(
                attachment_id=attachment_id,
                version=version,
                postings=build_postings(units),
            )
            return self.store.mark_ready(
                attachment_id=attachment_id, version=version, unit_count=unit_count
            )
        except AttachmentThreadGoneError:
            # The thread was deleted mid-flight and took the derived data
            # with it; the caller's ``AttachmentThreadGoneError`` is the
            # honest outcome, never a resurrected row.
            raise
        except AttachmentStoreError as exc:
            # EXIT: the parsed text or its index could not be written, so the
            # attachment is not searchable. Named failure, not an empty
            # success and not a silent retry against another source.
            logger.warning(
                "attachment derived data could not be stored: attachment_id=%s (%s)",
                attachment_id,
                exc,
            )
            return self.store.mark_failed(
                attachment_id=attachment_id,
                version=record.version,
                reason=_INDEX_UNAVAILABLE,
            )

    def _parse(
        self, filename: str, payload: bytes
    ) -> tuple[ParsedAttachmentUnit, ...] | str:
        """Freeze the payload into versioned units, or name why it cannot be."""
        # This is where an upload the door could not classify is finally
        # read: until document-type handling lands, only bytes that decode
        # as UTF-8 become text, and the whole file is one unit addressed as
        # unit 1.
        rejected = text_rejection_reason(payload)
        if rejected:
            return rejected
        text = payload.decode("utf-8")
        if not text.strip():
            return _EMPTY_TEXT
        return (ParsedAttachmentUnit(unit=1, text=text),)

    # -- reads --

    def status(self, attachment_id: str) -> AttachmentRecord | None:
        """The stored record, with a published-but-unusable row downgraded.

        ``ready`` is only ever reported when the derived data behind it is
        present for that version, so a torn rebuild reads as ``parsing``
        instead of a search that silently returns nothing.
        """
        record = self.store.get(attachment_id)
        if record is None or record.status != "ready":
            return record
        if self._derived_data_usable(record):
            return record
        logger.warning(
            "attachment %s reads ready without usable derived data; "
            "reporting it as parsing until it is rebuilt",
            attachment_id,
        )
        return replace(record, status="parsing", indexed=False)

    def list_for_thread(self, thread_id: str) -> tuple[AttachmentRecord, ...]:
        """Every attachment of a thread, with the same verification as a read.

        The stored status is not trusted here either: a row that claims
        ``ready`` without its derived data reads as ``parsing``, so the list
        and the single-status door can never disagree about one row.
        """
        verified: list[AttachmentRecord] = []
        for record in self.store.list_for_thread(thread_id):
            current = self.status(record.attachment_id)
            if current is not None:
                verified.append(current)
        return tuple(verified)

    def units(self, attachment_id: str) -> tuple[ParsedAttachmentUnit, ...]:
        record = self.store.get(attachment_id)
        if record is None or not record.version:
            return ()
        return self.store.units(attachment_id, record.version)

    def resolve_for_turn(
        self, *, thread_id: str, attachment_id: str
    ) -> ResolvedAttachment:
        """Resolve one attachment the active thread may put into a run."""
        record = self.status(attachment_id)
        if record is None or record.thread_id != thread_id:
            # EXIT: unknown and other-thread ids share one error so the door
            # does not confirm what another thread holds.
            raise AttachmentNotFoundError(attachment_id)
        if not record.searchable:
            raise AttachmentNotReadyError(attachment_id)
        text = "\n\n".join(unit.text for unit in self.units(attachment_id))
        if not text.strip():
            # EXIT: a ready record whose frozen text is gone is not an empty
            # document; the turn gets the typed exit instead of a content
            # block that names nothing.
            raise AttachmentNotReadyError(attachment_id)
        return ResolvedAttachment(
            attachment_id=record.attachment_id,
            filename=record.filename,
            text=text,
        )

    # -- restart --

    def recover(self) -> int:
        """Resume or rebuild every attachment the stored status overstates.

        Returns how many rows needed work. Recovery never advances the
        parsed version of a row that already had one, so the locator the
        operator attached still addresses the same text afterwards.
        """
        repaired = 0
        for record in self.store.incomplete():
            repaired += 1
            try:
                self._prepare(record, version=record.version or 1)
            except AttachmentThreadGoneError:
                # EXIT: its thread is gone; nothing this row could write
                # would be readable, and the cascade already took the rest.
                continue
            except AttachmentStoreError:
                # EXIT: this row could not be rebuilt, so it keeps reading
                # as ``parsing`` and the next restart tries it again.
                logger.exception(
                    "attachment recovery could not rebuild attachment_id=%s",
                    record.attachment_id,
                )
        return repaired

    def _derived_data_usable(self, record: AttachmentRecord) -> bool:
        if not record.indexed or record.version not in self.store.indexed_versions(
            record.attachment_id
        ):
            return False
        postings = self.store.index_postings(
            record.attachment_id, record.version
        )
        if not is_usable(postings):
            return False
        return bool(self.store.units(record.attachment_id, record.version))

    # -- deletion --

    def delete_thread(self, thread_id: str) -> None:
        """Remove every layer of a thread's attachments, bytes first.

        The bytes are removed before any durable row: a byte store that is
        down must leave the thread and its attachment metadata intact, so
        the failure is visible and the delete is retryable rather than a
        200 over orphaned data.
        """
        failures: list[str] = []
        removed: set[str] = set()
        for record in self.store.list_for_thread(thread_id):
            try:
                self.objects.delete(record.object_key)
            except Exception as exc:
                logger.warning(
                    "attachment bytes not removable: attachment_id=%s (%s)",
                    record.attachment_id,
                    exc,
                )
                failures.append(record.object_key)
            else:
                removed.add(record.object_key)
        if failures:
            raise AttachmentCleanupError(thread_id, failures)
        # WHY the sweep is restricted: the row delete also reports every key it
        # knew about, including one an upload added after the list above. Those
        # bytes have not been touched yet, so only they are swept here, and no
        # key is deleted twice.
        pending = self.store.delete_for_thread(thread_id)
        self._discard_bytes(
            thread_id, [key for key in pending if key not in removed]
        )

    def _discard_bytes(self, thread_id: str, keys: Sequence[str]) -> None:
        """Best-effort byte sweep; a failure here is logged, never hidden.

        The durable rows are already gone when this runs, so the door cannot
        retry the leftover and the named key in the log is the operator's
        handle for it.
        """
        for key in keys:
            try:
                self.objects.delete(key)
            except Exception:
                # EXIT: no record left owns this key to fail on, so the
                # delete still reports success and the key is logged.
                logger.exception(
                    "orphaned attachment bytes left behind for thread_id=%s key=%s",
                    thread_id,
                    key,
                )


__all__ = [
    "AttachmentCleanupError",
    "AttachmentError",
    "AttachmentNotFoundError",
    "AttachmentNotReadyError",
    "AttachmentPreparationService",
    "ResolvedAttachment",
    "text_rejection_reason",
]
