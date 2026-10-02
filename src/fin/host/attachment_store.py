"""Durable storage for thread-owned attachments (ADRs 0083/0085/0088).

An attachment is a file an operator drops into one research thread. Fin
owns three layers of it, all bound to the owning ``thread_id``:

* the raw bytes, addressed by an :class:`~fin.corpus.object_store.ObjectStore`
  key — the model never sees a path;
* the parsed frozen units, one text row per ``(attachment_id, version, unit)``;
* the page region boxes, one row per ``(attachment_id, version, page, ordinal)``
  — the PDF-point geometry a crop inspection is taken from;
* the derived search index, one posting row per ``(attachment_id, version, term)``;
* the versioned visual observations, one envelope row per
  ``(attachment_id, version, unit, page, observation_version)`` — see
  :mod:`fin.host.attachment_observations` for what an envelope promises.

Every layer is removed with the thread, and no attachment row survives a
delete: the deletion is the arbitration point for a preparation that is
still running, which loses the race with :class:`AttachmentThreadGoneError`
instead of resurrecting its derived data.

A voided attachment leaves a **tombstone**: one row recording that its raw
bytes are still owed a delete. The tombstone is written before any byte or
derivative cleanup runs, so access stays blocked while cleanup is retried,
and it is dropped only once the cleanup is confirmed. An obligation is
thread-scoped and lives no longer than the thread that owes it: the
Postgres rows cascade with ``fin_thread``, and the in-memory store runs the
same retirement against the thread store it already asks for liveness.

Two implementations satisfy :class:`AttachmentStore`:

* :class:`InMemoryAttachmentStore` — dicts behind one lock, checked against a
  :class:`~fin.host.thread_store.ThreadStore` for thread liveness. The default
  test and no-Postgres boot.
* :class:`PostgresAttachmentStore` — the same invariants through foreign keys
  to ``fin_thread`` with ``ON DELETE CASCADE``.

``version`` is the parsed-content version: it is minted by a successful
preparation and stays put across restarts, so a rebuilt index addresses the
same text the operator attached.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from typing import Literal, Mapping, Protocol, Sequence

from fin.corpus.parsers._pages import REGION_KINDS
from fin.host.attachment_observations import (
    CoveredDetail,
    DetailKey,
    ObservationAttempt,
    ObservationEnvelope,
)
from fin.host.attachment_vision import VisionCrop, VisionUsage, VisionCost


# The layout kinds a stored box may name. Read from the extraction core
# rather than restated, so a row can never hold a kind the core would
# refuse to produce and this store would then have to interpret.
PAGE_REGION_KINDS: tuple[str, ...] = REGION_KINDS


# ----- typed errors -----


class AttachmentStoreError(Exception):
    """Base class for attachment-store typed errors."""


class AttachmentThreadGoneError(AttachmentStoreError):
    """The attachment row or its thread row is gone.

    Raised by every write that would land derived data on a deleted
    attachment. The preparation worker treats it as "lose the race": the
    derived text and index it produced are discarded rather than written
    back, and the attachment is never reported ``ready``.
    """


# ----- records -----


AttachmentStatus = Literal["uploaded", "parsing", "ready", "failed"]

_STATUSES: tuple[AttachmentStatus, ...] = (
    "uploaded",
    "parsing",
    "ready",
    "failed",
)


@dataclass(frozen=True)
class ParsedAttachmentUnit:
    """One frozen unit of parsed attachment text for one version."""

    unit: int
    text: str


@dataclass(frozen=True, slots=True)
class StoredPageRegion:
    """One layout region of one page, as this store keeps it.

    Four PDF-point edges, top-left origin, the same space
    :class:`~fin.corpus.parsers._pages.Region` publishes — so a box survives
    preparation without being re-derived, and the crop lane converts it
    against the page's own media box rather than inventing normalized bounds.

    Only the geometry is kept. Whether a box is *reliable enough* to be
    cropped is a rule the crop lane owns
    (:func:`~fin.corpus.parsers._pages.crop_is_legible`), because legibility
    is stated in the render scale of the page being cut; storing a verdict
    here would freeze one build's render scale into the durable row.

    An inverted or degenerate box is refused at construction, which is the
    same rule the extraction core applies to a layout's own boxes: a row that
    cannot be cropped must not be persisted in the first place.
    """

    page: int
    kind: str
    left_pt: float
    top_pt: float
    right_pt: float
    bottom_pt: float

    def __post_init__(self) -> None:
        _require_version(self.page)
        kind = _require_text("region kind", self.kind)
        if kind not in PAGE_REGION_KINDS:
            raise AttachmentStoreError(
                f"StoredPageRegion.kind must be one of "
                f"{', '.join(PAGE_REGION_KINDS)}, got {self.kind!r}"
            )
        for name in ("left_pt", "top_pt", "right_pt", "bottom_pt"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise AttachmentStoreError(
                    f"StoredPageRegion.{name} must be a number, got {value!r}"
                )
        if self.right_pt <= self.left_pt or self.bottom_pt <= self.top_pt:
            raise AttachmentStoreError(
                f"StoredPageRegion bounds must increase, got "
                f"[{self.left_pt}, {self.top_pt}, {self.right_pt}, {self.bottom_pt}]"
            )


@dataclass(frozen=True)
class AttachmentRecord:
    """One thread-owned attachment's durable metadata row."""

    attachment_id: str
    thread_id: str
    filename: str
    size_bytes: int
    sha256: str
    object_key: str
    status: AttachmentStatus
    version: int
    unit_count: int
    indexed: bool
    failure_reason: str | None
    created_at: datetime
    updated_at: datetime

    @property
    def searchable(self) -> bool:
        """``ready`` with a usable derived index for the current version."""
        return self.status == "ready" and self.indexed


def _validate_status(value: str) -> AttachmentStatus:
    if value in _STATUSES:
        return value  # type: ignore[return-value]
    raise AttachmentStoreError(
        f"AttachmentRecord.status must be one of {_STATUSES}, got {value!r}"
    )


# ----- protocol -----


class AttachmentStore(Protocol):
    """The thread-bound durability contract for attachments."""

    def create(
        self,
        *,
        thread_id: str,
        attachment_id: str,
        filename: str,
        size_bytes: int,
        sha256: str,
        object_key: str,
    ) -> AttachmentRecord:
        """Write the metadata row for an accepted upload in ``uploaded``.

        ``version`` starts at 0 with no derived data. Raises
        :class:`AttachmentThreadGoneError` when ``thread_id`` is not a live
        thread, so an upload racing a delete cannot leave an orphan row
        behind its thread.

        Raises :class:`AttachmentStoreError` while ``attachment_id`` still
        owes a byte cleanup. One record can carry one debt, so an identity is
        not reusable until its bytes are confirmed gone: accepting the pairing
        instead would strand the older key, because the void that later claims
        the new row records no second debt.
        """
        ...

    def get(self, attachment_id: str) -> AttachmentRecord | None:
        """Look up one attachment; a miss returns ``None``, not an error."""
        ...

    def list_for_thread(self, thread_id: str) -> list[AttachmentRecord]:
        """Every attachment of one thread, oldest first."""
        ...

    def record_units(
        self,
        *,
        attachment_id: str,
        version: int,
        units: Sequence[ParsedAttachmentUnit],
    ) -> int:
        """Replace the parsed frozen units of ``version``; return the count.

        Raises :class:`AttachmentThreadGoneError` when the attachment or its
        thread was deleted while the parse was running.
        """
        ...

    def units(
        self, attachment_id: str, version: int
    ) -> tuple[ParsedAttachmentUnit, ...]:
        """The frozen units of one version, ordered by ``unit``."""
        ...

    def clear_units(self, attachment_id: str) -> None:
        """Drop every parsed unit of an attachment (re-preparation, delete)."""
        ...

    def record_regions(
        self,
        *,
        attachment_id: str,
        version: int,
        regions: Sequence[StoredPageRegion],
    ) -> int:
        """Replace the page region boxes of ``version``; return the count.

        Region boxes are additive metadata (spec item 10: table and figure
        regions stay anchored under their page unit), so an empty sequence is
        a recorded *absence* — this lane located nothing — and not an error.
        Re-recording replaces that version's boxes rather than accumulating
        them, so a re-run layout pass can never leave a superseded box
        croppable.

        Raises :class:`AttachmentThreadGoneError` when the attachment or its
        thread was deleted while the parse was running.
        """
        ...

    def regions(
        self, attachment_id: str, version: int, *, page: int | None = None
    ) -> tuple[StoredPageRegion, ...]:
        """The stored boxes of one version, by page order; empty when none.

        ``page`` narrows the read to one physical page, which is the shape a
        crop takes: a box is only ever cut from the page the run named.
        """
        ...

    def put_index(
        self,
        *,
        attachment_id: str,
        version: int,
        postings: Mapping[str, Sequence[int]],
    ) -> None:
        """Replace the derived index of ``version`` and mark it usable."""
        ...

    def index_postings(
        self, attachment_id: str, version: int
    ) -> dict[str, tuple[int, ...]]:
        """The stored postings of one version; empty when it has none."""
        ...

    def indexed_versions(self, attachment_id: str) -> tuple[int, ...]:
        """Versions that currently hold derived index rows.

        At most one version of an attachment may be active at a time, so
        concurrent re-preparation is observable here.
        """
        ...

    def clear_index(self, attachment_id: str) -> None:
        """Drop every posting of an attachment and mark it not indexed."""
        ...

    def record_observation(
        self, *, attachment_id: str, observation: ObservationEnvelope
    ) -> ObservationEnvelope:
        """Persist one versioned visual observation of a page of ``version``.

        Re-recording an observation version already on record for the same
        content version, unit, and page replaces it; a lower one is refused
        as stale, so observation versions stay monotonic per source and a
        late answer from a superseded attempt cannot erase newer coverage.

        Raises :class:`AttachmentThreadGoneError` when the attachment or its
        thread was deleted while the inspection was running, and
        :class:`AttachmentStoreError` when the envelope addresses a
        different attachment than the row it is being written to.
        """
        ...

    def observations(
        self, attachment_id: str, version: int, unit: int, page: int
    ) -> tuple[ObservationEnvelope, ...]:
        """The stored observations of one page of one version, oldest
        observation version first; empty when there are none or the
        attachment is gone."""
        ...

    def mark_ready(
        self, *, attachment_id: str, version: int, unit_count: int
    ) -> AttachmentRecord:
        """Publish ``version`` as ``ready``.

        Refuses a version whose derived data is not usable — the store
        checks parsed units and index postings for THAT version before the
        status flips, so a restart can never report a stale ``ready``.
        """
        ...

    def mark_failed(
        self, *, attachment_id: str, version: int, reason: str
    ) -> AttachmentRecord:
        """Record a named preparation failure."""
        ...

    def mark_parsing(
        self,
        *,
        attachment_id: str,
        from_statuses: Sequence[AttachmentStatus] = ("uploaded",),
    ) -> AttachmentRecord | None:
        """Move a still-guarded row into ``parsing``.

        The interim label a job door writes once preparation work is
        enqueued: a first ask moves ``uploaded -> parsing``, an operator
        retry also moves its own ``failed -> parsing`` and clears the
        retired reason, so a retried row never reads as still failed over
        a freshly queued job.

        ``from_statuses`` is a conditional guard checked against the
        stored row at write time, so a racing second request writes
        nothing. A missing, tombstoned, or gone row answers ``None``
        rather than an error: the enqueued job — not this label — is the
        promise of the work, and the row's version is untouched because
        ``parsing`` claims no parsed-content version.
        """
        ...

    def incomplete(self) -> list[AttachmentRecord]:
        """Attachments that must be resumed or rebuilt before they are usable.

        Covers ``parsing`` rows and any ``ready`` row whose parsed units or
        index postings for the current version are missing. ``uploaded``
        rows are deliberately NOT here: they hold durable bytes nobody has
        asked to prepare, and a restart must not start work on them.
        """
        ...

    def delete_for_thread(self, thread_id: str) -> list[str]:
        """Void an entire thread's attachments and record the byte cleanup.

        Every attachment row goes with its units and postings, each one
        through the same void :meth:`tombstone` performs, and the returned
        keys are what those voids recorded: the caller must still delete
        exactly them, and :meth:`pending_cleanup` re-reads the same values
        until the cleanup is confirmed. A row whose thread row went first
        without ever being voided is named here too, on its way out of the
        store: no byte leaves this store unnameable by the door that removes
        it. Idempotent — an unknown or already-voided thread yields an empty
        list and removes nothing.
        """
        ...

    def tombstone(self, *, attachment_id: str, thread_id: str) -> str | None:
        """Void one attachment's access and record its byte cleanup obligation.

        Removes the metadata row with its units and postings, and returns the
        object key the caller must still delete. Repeating the call while the
        bytes are still owed returns that same key, which is what makes a
        failed cleanup retryable through the public door: one record per id is
        the reason :meth:`create` refuses an id that still owes one.

        Returns ``None`` — and writes nothing — when the attachment is
        unknown, already fully deleted, owned by another thread, or owed by a
        thread that no longer exists. Those cases share one answer so a delete
        cannot probe what a different thread holds. Raises
        :class:`AttachmentThreadGoneError` if the owning thread disappears
        mid-transaction.
        """
        ...

    def pending_cleanup(self, thread_id: str) -> list[str]:
        """Object keys still owed a byte delete for one thread.

        An obligation exists only while its thread does, and the records are
        retired with it rather than filtered out of sight: the Postgres
        foreign key cascades with ``fin_thread``, and the in-memory store
        sweeps against the same thread store every other read uses. So a
        thread that no longer exists leaves no cleanup debt behind, and no
        later probe can revive one.
        """
        ...

    def confirm_cleanup(self, *, attachment_id: str) -> None:
        """Drop one tombstone once its bytes are known to be gone.

        Confirming an obligation that is already gone is a no-op: the second
        successful cleanup of an id says the same thing as the first.
        """
        ...

    def confirm_thread_cleanup(self, thread_id: str) -> None:
        """Drop every tombstone of one thread after a confirmed cleanup."""
        ...


def _attachment(
    record: AttachmentRecord, **changes: object
) -> AttachmentRecord:
    """Return a copy of ``record`` with ``changes`` applied.

    Shared by both implementations so the status seam validates the closed
    enum on every transition instead of trusting the caller.
    """
    if "status" in changes:
        changes["status"] = _validate_status(str(changes["status"]))
    return replace(record, **changes)


# ----- shared helpers for the two implementations below -----
#
# Imported with the implementations so the contract header above stays free
# of driver- and clock-shaped names. ``psycopg`` is already a hard dependency
# of the thread store these tables hang off.

import psycopg
from datetime import timezone
from typing import Any

from fin.host.thread_store import ThreadStore


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _require_text(name: str, value: object) -> str:
    """Reject a blank identity before it can address an unreadable row."""
    if not isinstance(value, str) or not value.strip():
        raise AttachmentStoreError(f"{name} must be a non-empty string, got {value!r}")
    return value


def _require_version(version: object) -> int:
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise AttachmentStoreError(
            f"attachment version must be a non-negative int, got {version!r}"
        )
    return version


def _new_record(
    *,
    thread_id: str,
    attachment_id: str,
    filename: str,
    size_bytes: int,
    sha256: str,
    object_key: str,
) -> AttachmentRecord:
    """Build the ``uploaded`` baseline row both implementations persist.

    WHY one builder: every derived write is only safe if the row it addresses
    started from the same closed baseline (no version, no units, no index),
    and an attachment id that is blank or whitespace cannot be read back.
    """
    for name, value in (
        ("thread_id", thread_id),
        ("attachment_id", attachment_id),
        ("filename", filename),
        ("sha256", sha256),
        ("object_key", object_key),
    ):
        _require_text(name, value)
    if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes < 0:
        raise AttachmentStoreError(
            f"size_bytes must be a non-negative int, got {size_bytes!r}"
        )
    stamp = _now()
    return AttachmentRecord(
        attachment_id=attachment_id,
        thread_id=thread_id,
        filename=filename,
        size_bytes=size_bytes,
        sha256=sha256,
        object_key=object_key,
        status="uploaded",
        version=0,
        unit_count=0,
        indexed=False,
        failure_reason=None,
        created_at=stamp,
        updated_at=stamp,
    )


def _normalized_units(
    units: Sequence[ParsedAttachmentUnit],
) -> dict[int, str]:
    """Coerce a unit batch to ``ordinal -> text``.

    WHY a mapping: ``unit`` is a one-based locator ordinal, so a batch that
    repeats an ordinal addresses the same slot and the later text wins — the
    same last-write rule the Postgres upsert applies.
    """
    rows: dict[int, str] = {}
    for item in units:
        if not isinstance(item, ParsedAttachmentUnit):
            raise AttachmentStoreError(
                f"record_units expects ParsedAttachmentUnit values, got {item!r}"
            )
        if item.unit < 1:
            raise AttachmentStoreError(
                f"parsed unit ordinal must be >= 1, got {item.unit}"
            )
        rows[int(item.unit)] = item.text
    return rows


def _normalized_regions(
    regions: Sequence[StoredPageRegion],
) -> dict[int, StoredPageRegion]:
    """Coerce a region batch to ``ordinal -> box``, in the order given.

    WHY an ordinal rather than the box's own edges: several boxes of one
    page may be identical in geometry, and a crop lane needs them
    addressable and stably ordered the way the layout reported them. The
    validation is the value type's own, so a box that could not be cropped
    was refused before it reached this function.
    """
    rows: dict[int, StoredPageRegion] = {}
    for item in regions:
        if not isinstance(item, StoredPageRegion):
            raise AttachmentStoreError(
                f"record_regions expects StoredPageRegion values, got {item!r}"
            )
        rows[len(rows)] = item
    return rows


def _normalized_postings(
    postings: Mapping[str, Sequence[int]],
) -> dict[str, tuple[int, ...]]:
    """Coerce a posting table to ``term -> ordered unit ordinals``.

    WHY here and not downstream: a term with no unit is not a posting and an
    ordinal outside the locator space can never be cited, so both stores have
    to refuse the same shapes before either writes a row.
    """
    normalized: dict[str, tuple[int, ...]] = {}
    for raw_term, raw_units in postings.items():
        term = _require_text("index term", raw_term)
        try:
            ordered = tuple(int(unit) for unit in raw_units)
        except (TypeError, ValueError) as exc:
            raise AttachmentStoreError(
                f"posting {term!r} must map integer unit ordinals, got {raw_units!r}"
            ) from exc
        if not ordered:
            continue  # EXIT: an empty posting carries no locatable unit.
        if any(unit < 1 for unit in ordered):
            raise AttachmentStoreError(
                f"posting {term!r} must use one-based unit ordinals, got {ordered}"
            )
        normalized[term] = ordered
    return normalized


def _observation_source(
    observation: ObservationEnvelope,
) -> tuple[int, int, int]:
    """``(content version, unit, page)`` — the sequence an observation
    version must be monotonic within."""
    return (
        observation.attachment_version,
        observation.source_unit,
        observation.page,
    )


def _check_observation_binding(
    attachment_id: str, observation: ObservationEnvelope
) -> None:
    """Refuse an envelope that addresses a different attachment row.

    WHY here and not in the envelope: the envelope carries its own identity
    for reading back provenance, and the store call carries the row being
    written; only the door where they meet can say whether they agree.
    """
    _require_text("attachment_id", attachment_id)
    if observation.attachment_id != attachment_id:
        raise AttachmentStoreError(
            f"observation addresses attachment {observation.attachment_id!r}, "
            f"not {attachment_id!r}"
        )


class _MutableAttachment:
    """Internal cell: one record plus the derived rows of every version."""

    __slots__ = ("observations", "postings", "record", "regions", "units")

    def __init__(self, record: AttachmentRecord) -> None:
        self.record = record
        self.units: dict[tuple[int, int], str] = {}
        self.postings: dict[int, dict[str, tuple[int, ...]]] = {}
        self.observations: dict[tuple[int, int, int, int], ObservationEnvelope] = {}
        # (version, page, ordinal) -> box, so a page's boxes keep both their
        # layout order and their identity under re-recording.
        self.regions: dict[tuple[int, int, int], StoredPageRegion] = {}


@dataclass(frozen=True)
class _CleanupObligation:
    """A voided attachment whose raw bytes are still owed a delete."""

    thread_id: str
    object_key: str


class InMemoryAttachmentStore:
    """Dict-backed attachment store guarded by a single ``threading.Lock``.

    The default boot without Postgres and the reference the Postgres
    implementation is measured against. Thread liveness is delegated to a
    :class:`~fin.host.thread_store.ThreadStore`, so "the thread is gone" has
    one authority in both implementations instead of two dictionaries that
    can disagree. That authority decides two things, each in one place: a
    record whose thread row is gone is invisible to every read and write
    (``_owned``), and it is removed only by the cleanup doors
    (``_retire_gone_locked``) — the retirement the ``ON DELETE CASCADE``
    foreign keys perform for the durable store.
    """

    def __init__(self, threads: ThreadStore) -> None:
        self._threads = threads
        self._lock = threading.Lock()
        self._attachments: dict[str, _MutableAttachment] = {}
        self._tombstones: dict[str, _CleanupObligation] = {}

    # -- race arbitration --

    def _owned(self, attachment_id: str) -> _MutableAttachment | None:
        """The cell ``attachment_id`` may be read or written, else ``None``.

        THE one liveness question of this store, asked by every read and every
        derived write: a record whose thread row is gone answers as if it were
        gone, which is what the ``ON DELETE CASCADE`` foreign keys guarantee
        the durable store. It is a hide, not a removal — only
        :meth:`_retire_gone_locked` removes, so an invisible row is still
        there for a cleanup door to name on its way out.
        """
        cell = self._attachments.get(attachment_id)
        if cell is None:
            return None  # EXIT: voided, or never created.
        if self._threads.get_thread(cell.record.thread_id) is None:
            return None  # EXIT: its thread row went first; retirement's turn.
        return cell

    def _live_cell(self, attachment_id: str) -> _MutableAttachment:
        """The cell a derived write may land on, or a typed loss of the race.

        Callers hold ``self._lock``, which is what makes the liveness decision
        and the following mutation one atomic step.
        """
        cell = self._owned(attachment_id)
        if cell is None:
            raise AttachmentThreadGoneError(
                f"attachment {attachment_id!r} is gone; discarding derived write"
            )
        return cell

    # -- metadata rows --

    def create(
        self,
        *,
        thread_id: str,
        attachment_id: str,
        filename: str,
        size_bytes: int,
        sha256: str,
        object_key: str,
    ) -> AttachmentRecord:
        record = _new_record(
            thread_id=thread_id,
            attachment_id=attachment_id,
            filename=filename,
            size_bytes=size_bytes,
            sha256=sha256,
            object_key=object_key,
        )
        # WHY liveness, duplicate check, owed check, and insert share one
        # critical section: releasing the lock between them lets a thread
        # delete land in the gap and leave an attachment row no thread owns
        # anymore, or a second row beside a cleanup nobody has paid.
        with self._lock:
            if self._threads.get_thread(thread_id) is None:
                raise AttachmentThreadGoneError(
                    f"thread {thread_id!r} is not live for attachment "
                    f"{attachment_id!r}; refusing to create an orphan row"
                )
            if attachment_id in self._attachments:
                raise AttachmentStoreError(
                    f"attachment {attachment_id!r} already exists"
                )
            if attachment_id in self._tombstones:
                raise AttachmentStoreError(
                    f"attachment {attachment_id!r} still owes a byte cleanup; "
                    "its identity is not reusable until the bytes are confirmed "
                    "gone"
                )
            self._attachments[attachment_id] = _MutableAttachment(record)
            return _attachment(record)

    def get(self, attachment_id: str) -> AttachmentRecord | None:
        with self._lock:
            cell = self._owned(attachment_id)
            # EXIT: a lookup miss is the documented ``None``, not an error.
            return None if cell is None else _attachment(cell.record)

    def list_for_thread(self, thread_id: str) -> list[AttachmentRecord]:
        # WHY insertion order: attachment ids are minted per upload, so dict
        # order is the arrival order and does not depend on two uploads
        # landing in the same clock tick.
        with self._lock:
            if self._threads.get_thread(thread_id) is None:
                return []  # EXIT: a thread with no live row has no attachments.
            return [
                _attachment(cell.record)
                for cell in self._attachments.values()
                if cell.record.thread_id == thread_id
            ]

    def incomplete(self) -> list[AttachmentRecord]:
        """Rows to resume: ``parsing``, or ``ready`` with derived data lost."""
        with self._lock:
            rows: list[AttachmentRecord] = []
            for attachment_id, cell in self._attachments.items():
                record = cell.record
                # WHY through ``_owned``: a row whose thread row went first is
                # the cascade's to remove, so it is neither a resume candidate
                # nor a listing — and ``_owned`` is where that is decided.
                if self._owned(attachment_id) is None:
                    continue
                if record.status == "parsing":
                    rows.append(_attachment(record))
                elif record.status == "ready" and not self._derived_present(cell):
                    rows.append(_attachment(record))
            return rows

    @staticmethod
    def _derived_present(cell: _MutableAttachment) -> bool:
        version = cell.record.version
        has_units = any(key[0] == version for key in cell.units)
        has_postings = bool(cell.postings.get(version))
        return has_units and has_postings

    # -- parsed frozen units --

    def record_units(
        self,
        *,
        attachment_id: str,
        version: int,
        units: Sequence[ParsedAttachmentUnit],
    ) -> int:
        _require_version(version)
        rows = _normalized_units(units)
        with self._lock:
            cell = self._live_cell(attachment_id)
            cell.units = {
                key: text for key, text in cell.units.items() if key[0] != version
            }
            for ordinal, text in rows.items():
                cell.units[(version, ordinal)] = text
            cell.record = _attachment(cell.record, updated_at=_now())
            return len(rows)

    def units(self, attachment_id: str, version: int) -> tuple[ParsedAttachmentUnit, ...]:
        _require_version(version)
        with self._lock:
            cell = self._owned(attachment_id)
            # EXIT: an attachment no live thread owns reads back as no units,
            # which is what the Postgres cascade guarantees.
            if cell is None:
                return ()
            rows = {
                ordinal: text
                for (stored_version, ordinal), text in cell.units.items()
                if stored_version == version
            }
            return tuple(
                ParsedAttachmentUnit(unit=ordinal, text=rows[ordinal])
                for ordinal in sorted(rows)
            )

    def clear_units(self, attachment_id: str) -> None:
        with self._lock:
            cell = self._owned(attachment_id)
            if cell is None:
                return  # EXIT: nothing to drop; cleanup is idempotent.
            cell.units = {}
            cell.record = _attachment(cell.record, updated_at=_now())

    # -- page region boxes --

    def record_regions(
        self,
        *,
        attachment_id: str,
        version: int,
        regions: Sequence[StoredPageRegion],
    ) -> int:
        _require_version(version)
        rows = _normalized_regions(regions)
        with self._lock:
            cell = self._live_cell(attachment_id)
            cell.regions = {
                key: box for key, box in cell.regions.items() if key[0] != version
            }
            for ordinal, box in rows.items():
                cell.regions[(version, box.page, ordinal)] = box
            cell.record = _attachment(cell.record, updated_at=_now())
            return len(rows)

    def regions(
        self, attachment_id: str, version: int, *, page: int | None = None
    ) -> tuple[StoredPageRegion, ...]:
        _require_version(version)
        if page is not None:
            _require_version(page)
        with self._lock:
            cell = self._owned(attachment_id)
            # EXIT: an attachment no live thread owns reads back as no
            # regions, which is what the Postgres cascade guarantees.
            if cell is None:
                return ()
            return tuple(
                cell.regions[key]
                for key in sorted(cell.regions)
                if key[0] == version and (page is None or key[1] == page)
            )

    # -- derived search index --

    def put_index(
        self,
        *,
        attachment_id: str,
        version: int,
        postings: Mapping[str, Sequence[int]],
    ) -> None:
        _require_version(version)
        rows = _normalized_postings(postings)
        with self._lock:
            cell = self._live_cell(attachment_id)
            if rows:
                cell.postings[version] = rows
            else:
                cell.postings.pop(version, None)
            # WHY ``indexed`` follows the rows: an empty posting set is not a
            # usable index, so it must not advertise searchability.
            cell.record = _attachment(
                cell.record, indexed=bool(rows), updated_at=_now()
            )

    def index_postings(
        self, attachment_id: str, version: int
    ) -> dict[str, tuple[int, ...]]:
        _require_version(version)
        with self._lock:
            cell = self._owned(attachment_id)
            stored = None if cell is None else cell.postings.get(version)
            # EXIT: a version with no postings reads back empty, never an error.
            return {} if not stored else dict(stored)

    def indexed_versions(self, attachment_id: str) -> tuple[int, ...]:
        with self._lock:
            cell = self._owned(attachment_id)
            if cell is None:
                return ()  # EXIT: no live owner, so no active index version.
            return tuple(
                sorted(version for version, rows in cell.postings.items() if rows)
            )

    def clear_index(self, attachment_id: str) -> None:
        with self._lock:
            cell = self._owned(attachment_id)
            if cell is None:
                return  # EXIT: nothing to drop; cleanup is idempotent.
            cell.postings = {}
            cell.record = _attachment(cell.record, indexed=False, updated_at=_now())

    # -- versioned visual observations --

    def record_observation(
        self, *, attachment_id: str, observation: ObservationEnvelope
    ) -> ObservationEnvelope:
        _check_observation_binding(attachment_id, observation)
        source = _observation_source(observation)
        with self._lock:
            cell = self._live_cell(attachment_id)
            current = max(
                (
                    key[3]
                    for key in cell.observations
                    if key[:3] == source
                ),
                default=0,
            )
            if observation.observation_version < current:
                raise AttachmentStoreError(
                    f"observation version {observation.observation_version} is "
                    f"stale for source {source}: version {current} is already "
                    "on record"
                )
            cell.observations[
                (
                    source[0],
                    source[1],
                    source[2],
                    observation.observation_version,
                )
            ] = observation
            cell.record = _attachment(cell.record, updated_at=_now())
            return observation

    def observations(
        self, attachment_id: str, version: int, unit: int, page: int
    ) -> tuple[ObservationEnvelope, ...]:
        _require_version(version)
        _require_version(unit)
        _require_version(page)
        with self._lock:
            cell = self._owned(attachment_id)
            # EXIT: an attachment no live thread owns reads back as no
            # observations, which is what the Postgres cascade guarantees.
            if cell is None:
                return ()
            return tuple(
                cell.observations[key]
                for key in sorted(cell.observations)
                if key[:3] == (version, unit, page)
            )

    # -- publication --

    def mark_ready(
        self, *, attachment_id: str, version: int, unit_count: int
    ) -> AttachmentRecord:
        _require_version(version)
        if isinstance(unit_count, bool) or not isinstance(unit_count, int) or unit_count < 1:
            raise AttachmentStoreError(
                f"mark_ready needs a positive unit_count, got {unit_count!r}"
            )
        with self._lock:
            cell = self._live_cell(attachment_id)
            stored_units = sum(1 for key in cell.units if key[0] == version)
            stored_postings = cell.postings.get(version)
            # WHY the count is compared with the rows instead of trusted:
            # ``ready`` is the promise that the operator's text is citable, so
            # the row may only claim what is actually addressed by units.
            if stored_units != unit_count:
                raise AttachmentStoreError(
                    f"cannot publish version {version} of {attachment_id!r} as "
                    f"ready: {unit_count} units claimed, {stored_units} stored"
                )
            if not stored_postings:
                raise AttachmentStoreError(
                    f"cannot publish version {version} of {attachment_id!r} as "
                    "ready: no index postings stored for that version"
                )
            cell.record = _attachment(
                cell.record,
                status="ready",
                version=version,
                unit_count=unit_count,
                indexed=True,
                failure_reason=None,
                updated_at=_now(),
            )
            return _attachment(cell.record)

    def mark_failed(
        self, *, attachment_id: str, version: int, reason: str
    ) -> AttachmentRecord:
        _require_version(version)
        reason = _require_text("failure reason", reason)
        with self._lock:
            cell = self._live_cell(attachment_id)
            # WHY ``version`` is not stamped: a parsed-content version is
            # minted by a successful preparation, so an abandoned attempt
            # must not take credit for one, and an earlier published version
            # stays addressable while its rows are still on disk.
            cell.record = _attachment(
                cell.record,
                status="failed",
                failure_reason=reason,
                updated_at=_now(),
            )
            return _attachment(cell.record)

    def mark_parsing(
        self,
        *,
        attachment_id: str,
        from_statuses: Sequence[AttachmentStatus] = ("uploaded",),
    ) -> AttachmentRecord | None:
        wanted = tuple(_validate_status(status) for status in from_statuses)
        with self._lock:
            cell = self._owned(attachment_id)
            # EXIT: gone, voided, or a concurrent request already moved
            # it — the guard is checked against the stored row under the
            # same lock that writes the transition, so the loser of that
            # race writes nothing and answers the same ``None``.
            if cell is None or cell.record.status not in wanted:
                return None
            cell.record = _attachment(
                cell.record,
                status="parsing",
                failure_reason=None,
                updated_at=_now(),
            )
            return _attachment(cell.record)

    # -- deletion --

    def _retire_gone_locked(self, *, name_for: str | None = None) -> list[str]:
        """Retire every record whose thread row is gone; return the named keys.

        THE one place this store removes a record of a dead thread, entered by
        every cleanup door, so "gone ⇒ retired" is stated once: the in-memory
        analogue of the ``ON DELETE CASCADE`` the durable store gets from its
        foreign keys. Removal rather than hiding matters for the obligations —
        one its thread can no longer name is never finishable, so it would sit
        in the map until a later probe of that same id collected it. One thread
        row is asked once per sweep, against the injected thread store.

        ``name_for`` is the thread the calling door acts for. Rows of that
        thread which no void ever claimed are handed back as object keys on
        their way out: an obligation cannot outlive the thread that owes it,
        but a door that dropped such a row in silence would strand a byte
        nothing can name again. Every other door passes nothing, because it has
        no channel to hand a key over.
        """
        live: dict[str, bool] = {}

        def gone(thread_id: str) -> bool:
            if thread_id not in live:
                live[thread_id] = self._threads.get_thread(thread_id) is None
            return live[thread_id]

        named: list[str] = []
        for attachment_id, cell in list(self._attachments.items()):
            if not gone(cell.record.thread_id):
                continue
            if name_for is not None and cell.record.thread_id == name_for:
                named.append(cell.record.object_key)
            del self._attachments[attachment_id]
        for attachment_id, obligation in list(self._tombstones.items()):
            if gone(obligation.thread_id):
                del self._tombstones[attachment_id]
        return named

    def _void_locked(self, attachment_id: str, thread_id: str) -> str | None:
        """Void one attachment and return the key its obligation holds.

        THE one tombstone write of this store: both deletion doors route
        through here, so what a void returns and what ``pending_cleanup()``
        reads back cannot drift apart. Callers hold ``self._lock``, which makes
        the liveness decision, the row removal, and the record of the debt one
        atomic step.

        A live row never meets an existing obligation: one record per id
        carries one debt, and :meth:`create` refuses an id whose bytes are
        still owed. So a void of a row records that row's key, and a repeat
        void answers from the obligation already on record — which is what
        keeps a failed cleanup retryable — while another thread's obligation
        is answered as a miss with nothing written, because a door must not
        name bytes a different thread owes.
        """
        cell = self._owned(attachment_id)
        if cell is not None and cell.record.thread_id == thread_id:
            object_key = cell.record.object_key
            del self._attachments[attachment_id]
            self._tombstones[attachment_id] = _CleanupObligation(
                thread_id, object_key
            )
            return object_key
        obligation = self._tombstones.get(attachment_id)
        if obligation is None or obligation.thread_id != thread_id:
            return None  # EXIT: unknown, another thread's, or fully deleted.
        # EXIT: the void already landed and its bytes are still owed, so the
        # retry is handed the same key rather than a fresh miss.
        return obligation.object_key

    def _thread_obligations_locked(
        self, thread_id: str
    ) -> list[tuple[str, _CleanupObligation]]:
        """One thread's owed cleanups, in arrival order. Callers hold the lock."""
        return [
            (attachment_id, obligation)
            for attachment_id, obligation in self._tombstones.items()
            if obligation.thread_id == thread_id
        ]

    def delete_for_thread(self, thread_id: str) -> list[str]:
        # WHY one lock spans the whole thread: units and postings live on the
        # cell, so each void removes every derived layer in the same step, and
        # a preparation that arrives afterwards loses the race in
        # ``_live_cell``.
        with self._lock:
            # The acting thread's un-voided rows are named before they are
            # retired, so no row can leave this door without a key.
            orphaned = self._retire_gone_locked(name_for=thread_id)
            voided = [
                key
                for key in (
                    self._void_locked(attachment_id, thread_id)
                    for attachment_id, cell in list(self._attachments.items())
                    if cell.record.thread_id == thread_id
                )
                if key is not None
            ]
            return orphaned + voided

    def tombstone(self, *, attachment_id: str, thread_id: str) -> str | None:
        _require_text("attachment_id", attachment_id)
        _require_text("thread_id", thread_id)
        with self._lock:
            self._retire_gone_locked()
            return self._void_locked(attachment_id, thread_id)

    def pending_cleanup(self, thread_id: str) -> list[str]:
        _require_text("thread_id", thread_id)
        with self._lock:
            self._retire_gone_locked()
            return [
                obligation.object_key
                for _, obligation in self._thread_obligations_locked(thread_id)
            ]

    def confirm_cleanup(self, *, attachment_id: str) -> None:
        _require_text("attachment_id", attachment_id)
        with self._lock:
            self._retire_gone_locked()
            self._tombstones.pop(attachment_id, None)

    def confirm_thread_cleanup(self, thread_id: str) -> None:
        _require_text("thread_id", thread_id)
        with self._lock:
            self._retire_gone_locked()
            for attachment_id, _ in self._thread_obligations_locked(thread_id):
                del self._tombstones[attachment_id]


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS fin_thread_attachment (
    attachment_id   TEXT PRIMARY KEY,
    thread_id       TEXT NOT NULL REFERENCES fin_thread(id) ON DELETE CASCADE,
    filename        TEXT NOT NULL,
    size_bytes      BIGINT NOT NULL,
    sha256          TEXT NOT NULL,
    object_key      TEXT NOT NULL,
    status          TEXT NOT NULL,
    version         INTEGER NOT NULL DEFAULT 0,
    unit_count      INTEGER NOT NULL DEFAULT 0,
    indexed         BOOLEAN NOT NULL DEFAULT FALSE,
    failure_reason  TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS fin_thread_attachment_thread_order_idx
    ON fin_thread_attachment (thread_id, created_at);

CREATE TABLE IF NOT EXISTS fin_thread_attachment_unit (
    attachment_id   TEXT NOT NULL
        REFERENCES fin_thread_attachment(attachment_id) ON DELETE CASCADE,
    thread_id       TEXT NOT NULL REFERENCES fin_thread(id) ON DELETE CASCADE,
    version         INTEGER NOT NULL,
    unit            INTEGER NOT NULL,
    text            TEXT NOT NULL,
    PRIMARY KEY (attachment_id, version, unit)
);

CREATE TABLE IF NOT EXISTS fin_thread_attachment_index (
    attachment_id   TEXT NOT NULL
        REFERENCES fin_thread_attachment(attachment_id) ON DELETE CASCADE,
    thread_id       TEXT NOT NULL REFERENCES fin_thread(id) ON DELETE CASCADE,
    version         INTEGER NOT NULL,
    term            TEXT NOT NULL,
    units           TEXT NOT NULL,
    PRIMARY KEY (attachment_id, version, term)
);

CREATE INDEX IF NOT EXISTS fin_thread_attachment_index_lookup_idx
    ON fin_thread_attachment_index (attachment_id, version);

-- The page/region boxes of one version. Anchored under the page, not under
-- a generic locator (spec item 10): a table or figure region stays with its
-- page unit, and the crop lane reads exactly one page's boxes.
CREATE TABLE IF NOT EXISTS fin_thread_attachment_region (
    attachment_id   TEXT NOT NULL
        REFERENCES fin_thread_attachment(attachment_id) ON DELETE CASCADE,
    thread_id       TEXT NOT NULL REFERENCES fin_thread(id) ON DELETE CASCADE,
    version         INTEGER NOT NULL,
    page            INTEGER NOT NULL,
    ordinal         INTEGER NOT NULL,
    kind            TEXT NOT NULL,
    left_pt         DOUBLE PRECISION NOT NULL,
    top_pt          DOUBLE PRECISION NOT NULL,
    right_pt        DOUBLE PRECISION NOT NULL,
    bottom_pt       DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (attachment_id, version, page, ordinal)
);

CREATE INDEX IF NOT EXISTS fin_thread_attachment_region_lookup_idx
    ON fin_thread_attachment_region (attachment_id, version, page);

CREATE TABLE IF NOT EXISTS fin_thread_attachment_observation (
    attachment_id   TEXT NOT NULL
        REFERENCES fin_thread_attachment(attachment_id) ON DELETE CASCADE,
    thread_id       TEXT NOT NULL REFERENCES fin_thread(id) ON DELETE CASCADE,
    version         INTEGER NOT NULL,
    unit            INTEGER NOT NULL,
    page            INTEGER NOT NULL,
    observation_version INTEGER NOT NULL,
    coverage_schema_version TEXT NOT NULL,
    page_image_sha256 TEXT NOT NULL,
    provider        TEXT NOT NULL,
    model_version   TEXT NOT NULL,
    answer_text     TEXT NOT NULL DEFAULT '',
    scope           TEXT NOT NULL,
    covered_details TEXT NOT NULL,
    attempts        TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (attachment_id, version, unit, page, observation_version)
);

-- Column migration on existing observation tables: pre-answer rows are
-- provenance-only records, so the column is NULL-safe via the table
-- default '' and fresh installs get it from the CREATE TABLE above.
ALTER TABLE fin_thread_attachment_observation
    ADD COLUMN IF NOT EXISTS answer_text TEXT NOT NULL DEFAULT '';

CREATE TABLE IF NOT EXISTS fin_thread_attachment_tombstone (
    attachment_id   TEXT PRIMARY KEY,
    thread_id       TEXT NOT NULL REFERENCES fin_thread(id) ON DELETE CASCADE,
    object_key      TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS fin_thread_attachment_tombstone_thread_idx
    ON fin_thread_attachment_tombstone (thread_id, attachment_id);
"""


class PostgresAttachmentStore:
    """psycopg-backed attachment store on the shared ``FIN_CHECKPOINT_URL``.

    Four attachment tables plus the tombstone record, each carrying
    ``thread_id`` as an ``ON DELETE CASCADE`` foreign key to ``fin_thread``:
    the cascade is what makes a thread delete atomic across metadata, parsed
    units, and postings, and it is the reason a late derived write can be
    detected instead of silently persisted. The tombstone row is what a
    voided attachment leaves behind while its bytes are still owed, and it
    cascades away with the thread so a deleted thread keeps no cleanup debt —
    the guarantee the in-memory store matches by retiring records against the
    same thread rows. Both deletion doors write that row through ``_void`` and
    answer from it, so a thread-wide void is N single voids, not a second
    statement of the same rule.

    Each public call opens its own short-lived connection, matching
    :class:`~fin.host.thread_store.PostgresThreadStore`: the workspace is a
    local operator server, and this keeps a second pool off the boot path.
    Schema creation is idempotent and runs once per construction.
    """

    _COLUMNS = (
        "attachment_id, thread_id, filename, size_bytes, sha256, object_key, "
        "status, version, unit_count, indexed, failure_reason, created_at, "
        "updated_at"
    )
    # WHY the liveness touch is the first statement of every derived write:
    # it takes the attachment row's lock and drops the row from the
    # transaction's view if ``fin_thread`` no longer has the thread, so a zero
    # rowcount means the delete won and this write must be discarded.
    # ``indexed`` uses COALESCE so a unit-only write cannot revoke a usable
    # index and a term with no unit leaves nothing to mark.
    _TOUCH_SQL = (
        "UPDATE fin_thread_attachment "
        "SET updated_at = now(), indexed = COALESCE(%s, indexed) "
        "WHERE attachment_id = %s "
        "AND thread_id IN (SELECT id FROM fin_thread) "
        "RETURNING thread_id"
    )
    # WHY ``FOR UPDATE``: it puts the void and a preparation's ``_touch`` in one
    # order, so a derived write either lands before the tombstone or loses the
    # race after it — never between the tombstone and the row delete.
    _VOID_TARGET_SQL = (
        "SELECT thread_id, object_key FROM fin_thread_attachment "
        "WHERE attachment_id = %s FOR UPDATE"
    )
    # THE tombstone write, reached only through ``_void``. ``DO NOTHING``
    # because the id is the primary key: a second debt cannot be recorded
    # beside the first, and the cleanup a caller is already retrying must not
    # be dropped for a newer key. ``RETURNING`` lets the void answer with the
    # key it actually recorded instead of enumerating the obligation again.
    # ``create()`` is what keeps a live row off an owed id, so the one record
    # per id never has to choose between two keys.
    _VOID_SQL = (
        "INSERT INTO fin_thread_attachment_tombstone "
        "(attachment_id, thread_id, object_key) "
        "VALUES (%s, %s, %s) "
        "ON CONFLICT (attachment_id) DO NOTHING RETURNING object_key"
    )
    # One obligation, one owner: the thread filter is what keeps a delete from
    # probing what a different thread holds.
    _OWED_KEY_SQL = (
        "SELECT object_key FROM fin_thread_attachment_tombstone "
        "WHERE attachment_id = %s AND thread_id = %s"
    )

    def __init__(self, connection_url: str) -> None:
        self._url = connection_url
        self._setup_lock = threading.Lock()
        with self._setup_lock:
            try:
                with self._connect() as conn:
                    conn.execute(_SCHEMA_SQL)
                    conn.commit()
            except psycopg.errors.UndefinedTable as exc:
                raise AttachmentStoreError(
                    "fin_thread must exist before the attachment tables can "
                    "reference it: construct the Postgres thread store first"
                ) from exc

    def _connect(self) -> Any:
        """A connection that closes when the caller's block ends.

        WHY ``closing``: psycopg's own connection context manager only commits
        or rolls back, so an explicitly unclosed connection would hold a
        server slot until garbage collection — and this store is called once
        per attachment operation for the life of the process.
        """
        import contextlib

        return contextlib.closing(psycopg.connect(self._url))

    @staticmethod
    def _gone(attachment_id: str) -> AttachmentThreadGoneError:
        return AttachmentThreadGoneError(
            f"attachment {attachment_id!r} is gone; discarding derived write"
        )

    def _touch(self, conn: Any, attachment_id: str, *, indexed: bool | None) -> str:
        row = conn.execute(self._TOUCH_SQL, (indexed, attachment_id)).fetchone()
        if row is None:
            raise self._gone(attachment_id)
        return str(row[0])

    @staticmethod
    def _row_to_record(row: Sequence[Any]) -> AttachmentRecord:
        return AttachmentRecord(
            attachment_id=str(row[0]),
            thread_id=str(row[1]),
            filename=str(row[2]),
            size_bytes=int(row[3]),
            sha256=str(row[4]),
            object_key=str(row[5]),
            status=_validate_status(str(row[6])),
            version=int(row[7]),
            unit_count=int(row[8]),
            indexed=bool(row[9]),
            failure_reason=None if row[10] is None else str(row[10]),
            created_at=row[11],
            updated_at=row[12],
        )

    @staticmethod
    def _encode_units(units: Sequence[int]) -> str:
        """Postings store one comma-separated ordinal list per term.

        WHY a text column instead of an array: the read boundary hands out
        plain tuples and the shape has to survive both a driver with and
        without array codecs configured on the connection.
        """
        return ",".join(str(unit) for unit in units)

    @staticmethod
    def _decode_units(value: str) -> tuple[int, ...]:
        return tuple(int(part) for part in value.split(",") if part)

    # -- metadata rows --

    def create(
        self,
        *,
        thread_id: str,
        attachment_id: str,
        filename: str,
        size_bytes: int,
        sha256: str,
        object_key: str,
    ) -> AttachmentRecord:
        record = _new_record(
            thread_id=thread_id,
            attachment_id=attachment_id,
            filename=filename,
            size_bytes=size_bytes,
            sha256=sha256,
            object_key=object_key,
        )
        try:
            with self._connect() as conn:
                # WHY one transaction for the owed check and the insert: an id
                # whose bytes are still owed has one record that cannot carry
                # a second debt, so the pairing is refused here rather than
                # resolved later by a void that could name only one of them.
                owed = conn.execute(
                    "SELECT 1 FROM fin_thread_attachment_tombstone "
                    "WHERE attachment_id = %s",
                    (record.attachment_id,),
                ).fetchone()
                if owed is not None:
                    conn.rollback()
                    raise AttachmentStoreError(
                        f"attachment {attachment_id!r} still owes a byte "
                        "cleanup; its identity is not reusable until the bytes "
                        "are confirmed gone"
                    )
                row = conn.execute(
                    f"INSERT INTO fin_thread_attachment ({self._COLUMNS}) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    f"RETURNING {self._COLUMNS}",
                    (
                        record.attachment_id,
                        record.thread_id,
                        record.filename,
                        record.size_bytes,
                        record.sha256,
                        record.object_key,
                        record.status,
                        record.version,
                        record.unit_count,
                        record.indexed,
                        record.failure_reason,
                        record.created_at,
                        record.updated_at,
                    ),
                ).fetchone()
                conn.commit()
        except psycopg.errors.ForeignKeyViolation as exc:
            raise AttachmentThreadGoneError(
                f"thread {thread_id!r} is not live for attachment "
                f"{attachment_id!r}; refusing to create an orphan row"
            ) from exc
        except psycopg.errors.UniqueViolation as exc:
            raise AttachmentStoreError(
                f"attachment {attachment_id!r} already exists"
            ) from exc
        assert row is not None
        return self._row_to_record(row)

    def get(self, attachment_id: str) -> AttachmentRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT {self._COLUMNS} FROM fin_thread_attachment "
                "WHERE attachment_id = %s",
                (attachment_id,),
            ).fetchone()
            # EXIT: a lookup miss is the documented ``None``, not an error.
            return None if row is None else self._row_to_record(row)

    def list_for_thread(self, thread_id: str) -> list[AttachmentRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT {self._COLUMNS} FROM fin_thread_attachment "
                "WHERE thread_id = %s ORDER BY created_at ASC, attachment_id ASC",
                (thread_id,),
            ).fetchall()
            return [self._row_to_record(row) for row in rows]

    def incomplete(self) -> list[AttachmentRecord]:
        # WHY the predicate is set-based: a row that is ``ready`` without
        # units or postings for ITS OWN version is a lost rebuild, and the
        # two NOT EXISTS clauses answer that in one round trip.
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT {self._COLUMNS} FROM fin_thread_attachment a "
                "WHERE status = 'parsing' "
                "OR (status = 'ready' AND ("
                "NOT EXISTS (SELECT 1 FROM fin_thread_attachment_unit u "
                "WHERE u.attachment_id = a.attachment_id AND u.version = a.version) "
                "OR NOT EXISTS (SELECT 1 FROM fin_thread_attachment_index i "
                "WHERE i.attachment_id = a.attachment_id AND i.version = a.version "
                "AND i.units <> ''))) "
                "ORDER BY created_at ASC, attachment_id ASC"
            ).fetchall()
            return [self._row_to_record(row) for row in rows]

    # -- parsed frozen units --

    def record_units(
        self,
        *,
        attachment_id: str,
        version: int,
        units: Sequence[ParsedAttachmentUnit],
    ) -> int:
        _require_version(version)
        rows = _normalized_units(units)
        with self._connect() as conn:
            try:
                thread_id = self._touch(conn, attachment_id, indexed=None)
                conn.execute(
                    "DELETE FROM fin_thread_attachment_unit "
                    "WHERE attachment_id = %s AND version = %s",
                    (attachment_id, version),
                )
                for ordinal, text in rows.items():
                    conn.execute(
                        "INSERT INTO fin_thread_attachment_unit "
                        "(attachment_id, thread_id, version, unit, text) "
                        "VALUES (%s, %s, %s, %s, %s) "
                        "ON CONFLICT (attachment_id, version, unit) "
                        "DO UPDATE SET text = EXCLUDED.text",
                        (attachment_id, thread_id, version, ordinal, text),
                    )
                conn.commit()
            except psycopg.errors.ForeignKeyViolation as exc:
                raise self._gone(attachment_id) from exc
        return len(rows)

    def units(self, attachment_id: str, version: int) -> tuple[ParsedAttachmentUnit, ...]:
        _require_version(version)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT unit, text FROM fin_thread_attachment_unit "
                "WHERE attachment_id = %s AND version = %s ORDER BY unit ASC",
                (attachment_id, version),
            ).fetchall()
            # EXIT: a version that was never written or already cascaded
            # away reads back as no units.
            return () if not rows else tuple(
                ParsedAttachmentUnit(unit=int(row[0]), text=str(row[1])) for row in rows
            )

    def clear_units(self, attachment_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM fin_thread_attachment_unit WHERE attachment_id = %s",
                (attachment_id,),
            )
            conn.execute(
                "UPDATE fin_thread_attachment SET updated_at = now() "
                "WHERE attachment_id = %s",
                (attachment_id,),
            )
            conn.commit()

    # -- page region boxes --

    # WHY one column list: the INSERT parameters and the SELECT below bind
    # through this one name tuple, so a positional row can never be read
    # against a drifted column layout — the same rule the observation table
    # follows.
    _REGION_COLUMN_NAMES = (
        "attachment_id",
        "thread_id",
        "version",
        "page",
        "ordinal",
        "kind",
        "left_pt",
        "top_pt",
        "right_pt",
        "bottom_pt",
    )
    _REGION_COLUMNS = ", ".join(_REGION_COLUMN_NAMES)

    def record_regions(
        self,
        *,
        attachment_id: str,
        version: int,
        regions: Sequence[StoredPageRegion],
    ) -> int:
        _require_version(version)
        rows = _normalized_regions(regions)
        with self._connect() as conn:
            try:
                # WHY the touch first, exactly like units and postings: it is
                # the arbitration point, so boxes from a parse that raced the
                # delete are discarded rather than persisted.
                thread_id = self._touch(conn, attachment_id, indexed=None)
                conn.execute(
                    "DELETE FROM fin_thread_attachment_region "
                    "WHERE attachment_id = %s AND version = %s",
                    (attachment_id, version),
                )
                for ordinal, box in rows.items():
                    conn.execute(
                        f"INSERT INTO fin_thread_attachment_region "
                        f"({self._REGION_COLUMNS}) "
                        "VALUES (" + ", ".join(["%s"] * len(self._REGION_COLUMN_NAMES))
                        + ") ON CONFLICT (attachment_id, version, page, ordinal) "
                        "DO UPDATE SET kind = EXCLUDED.kind, "
                        "left_pt = EXCLUDED.left_pt, top_pt = EXCLUDED.top_pt, "
                        "right_pt = EXCLUDED.right_pt, bottom_pt = EXCLUDED.bottom_pt",
                        (
                            attachment_id,
                            thread_id,
                            version,
                            box.page,
                            ordinal,
                            box.kind,
                            float(box.left_pt),
                            float(box.top_pt),
                            float(box.right_pt),
                            float(box.bottom_pt),
                        ),
                    )
                conn.commit()
            except psycopg.errors.ForeignKeyViolation as exc:
                raise self._gone(attachment_id) from exc
        return len(rows)

    def regions(
        self, attachment_id: str, version: int, *, page: int | None = None
    ) -> tuple[StoredPageRegion, ...]:
        _require_version(version)
        if page is not None:
            _require_version(page)
        with self._connect() as conn:
            if page is None:
                rows = conn.execute(
                    f"SELECT {self._REGION_COLUMNS} "
                    "FROM fin_thread_attachment_region "
                    "WHERE attachment_id = %s AND version = %s "
                    "ORDER BY page ASC, ordinal ASC",
                    (attachment_id, version),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"SELECT {self._REGION_COLUMNS} "
                    "FROM fin_thread_attachment_region "
                    "WHERE attachment_id = %s AND version = %s AND page = %s "
                    "ORDER BY ordinal ASC",
                    (attachment_id, version, page),
                ).fetchall()
            # EXIT: a version that was never written, or already cascaded
            # away with its thread or attachment, reads back as no boxes.
            return tuple(
                StoredPageRegion(
                    page=int(values["page"]),
                    kind=str(values["kind"]),
                    left_pt=float(values["left_pt"]),
                    top_pt=float(values["top_pt"]),
                    right_pt=float(values["right_pt"]),
                    bottom_pt=float(values["bottom_pt"]),
                )
                for values in (
                    dict(zip(self._REGION_COLUMN_NAMES, row)) for row in rows
                )
            )

    # -- derived search index --

    def put_index(
        self,
        *,
        attachment_id: str,
        version: int,
        postings: Mapping[str, Sequence[int]],
    ) -> None:
        _require_version(version)
        rows = _normalized_postings(postings)
        with self._connect() as conn:
            try:
                thread_id = self._touch(conn, attachment_id, indexed=bool(rows))
                conn.execute(
                    "DELETE FROM fin_thread_attachment_index "
                    "WHERE attachment_id = %s AND version = %s",
                    (attachment_id, version),
                )
                for term, units in rows.items():
                    conn.execute(
                        "INSERT INTO fin_thread_attachment_index "
                        "(attachment_id, thread_id, version, term, units) "
                        "VALUES (%s, %s, %s, %s, %s) "
                        "ON CONFLICT (attachment_id, version, term) "
                        "DO UPDATE SET units = EXCLUDED.units",
                        (
                            attachment_id,
                            thread_id,
                            version,
                            term,
                            self._encode_units(units),
                        ),
                    )
                conn.commit()
            except psycopg.errors.ForeignKeyViolation as exc:
                raise self._gone(attachment_id) from exc

    def index_postings(
        self, attachment_id: str, version: int
    ) -> dict[str, tuple[int, ...]]:
        _require_version(version)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT term, units FROM fin_thread_attachment_index "
                "WHERE attachment_id = %s AND version = %s AND units <> '' "
                "ORDER BY term ASC",
                (attachment_id, version),
            ).fetchall()
            return {str(row[0]): self._decode_units(str(row[1])) for row in rows}

    def indexed_versions(self, attachment_id: str) -> tuple[int, ...]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT version FROM fin_thread_attachment_index "
                "WHERE attachment_id = %s AND units <> '' ORDER BY version ASC",
                (attachment_id,),
            ).fetchall()
            return tuple(int(row[0]) for row in rows)

    def clear_index(self, attachment_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM fin_thread_attachment_index WHERE attachment_id = %s",
                (attachment_id,),
            )
            conn.execute(
                "UPDATE fin_thread_attachment SET indexed = FALSE, updated_at = now() "
                "WHERE attachment_id = %s",
                (attachment_id,),
            )
            conn.commit()

    # -- versioned visual observations --

    # THE one column order for the observation row: the INSERT parameter
    # tuple, the SELECT list, and the decode below all bind through this
    # name list, so a positional row can never be read against a drifted
    # column layout.
    _OBS_COLUMN_NAMES = (
        "attachment_id",
        "thread_id",
        "version",
        "unit",
        "page",
        "observation_version",
        "coverage_schema_version",
        "page_image_sha256",
        "provider",
        "model_version",
        "answer_text",
        "scope",
        "covered_details",
        "attempts",
        "created_at",
    )
    _OBS_COLUMNS = ", ".join(_OBS_COLUMN_NAMES)

    @staticmethod
    def _encode_scope(scope: VisionCrop | None) -> str:
        if scope is None:
            return "null"
        return json.dumps(
            {
                "left": float(scope.left),
                "top": float(scope.top),
                "right": float(scope.right),
                "bottom": float(scope.bottom),
            }
        )

    @staticmethod
    def _decode_scope(raw: Any) -> VisionCrop | None:
        if raw is None:
            return None
        return VisionCrop(
            left=float(raw["left"]),
            top=float(raw["top"]),
            right=float(raw["right"]),
            bottom=float(raw["bottom"]),
        )

    @classmethod
    def _encode_observation_payload(
        cls, observation: ObservationEnvelope
    ) -> tuple[str, str, str]:
        """Envelope value types become the three JSON text columns.

        WHY text: the same shape has to survive the driver with and without
        JSON codecs, and the observation's meaning is owned by
        :mod:`fin.host.attachment_observations`, not by a column type.
        """
        covered = json.dumps(
            [
                {
                    "kind": detail.key.kind,
                    "target_key": detail.key.target_key,
                    "scope": json.loads(cls._encode_scope(detail.scope)),
                }
                for detail in observation.covered_details
            ]
        )
        attempts = json.dumps(
            [
                {
                    "usage": (
                        None
                        if attempt.usage is None
                        else {
                            "input_tokens": attempt.usage.input_tokens,
                            "output_tokens": attempt.usage.output_tokens,
                            "total_tokens": attempt.usage.total_tokens,
                        }
                    ),
                    "cost": (
                        None
                        if attempt.cost is None
                        else {
                            "amount": str(attempt.cost.amount),
                            "currency": attempt.cost.currency,
                        }
                    ),
                }
                for attempt in observation.attempts
            ]
        )
        return cls._encode_scope(observation.scope), covered, attempts

    @classmethod
    def _decode_observation_row(cls, row: Sequence[Any]) -> ObservationEnvelope:
        # WHY zip against ``_OBS_COLUMN_NAMES``: every SELECT of this table
        # lists that constant, so the row arrives in its order and each value
        # is read under its own name — a schema reorder fails loudly on a
        # missing key instead of silently reading the wrong column.
        values = dict(zip(cls._OBS_COLUMN_NAMES, row))
        scope = cls._decode_scope(json.loads(str(values["scope"])))
        covered_details = tuple(
            CoveredDetail(
                key=DetailKey(
                    kind=item["kind"],
                    target_key=item["target_key"],
                ),
                scope=cls._decode_scope(item["scope"]),
            )
            for item in json.loads(str(values["covered_details"]))
        )
        attempts = tuple(
            ObservationAttempt(
                usage=(
                    None
                    if item["usage"] is None
                    else VisionUsage(
                        input_tokens=item["usage"]["input_tokens"],
                        output_tokens=item["usage"]["output_tokens"],
                        total_tokens=item["usage"]["total_tokens"],
                    )
                ),
                cost=(
                    None
                    if item["cost"] is None
                    else VisionCost(
                        amount=Decimal(str(item["cost"]["amount"])),
                        currency=item["cost"]["currency"],
                    )
                ),
            )
            for item in json.loads(str(values["attempts"]))
        )
        return ObservationEnvelope(
            attachment_id=str(values["attachment_id"]),
            coverage_schema_version=str(values["coverage_schema_version"]),
            observation_version=int(values["observation_version"]),
            attachment_version=int(values["version"]),
            source_unit=int(values["unit"]),
            page=int(values["page"]),
            page_image_sha256=str(values["page_image_sha256"]),
            provider=str(values["provider"]),
            model_version=str(values["model_version"]),
            scope=scope,
            covered_details=covered_details,
            attempts=attempts,
            answer_text=str(values["answer_text"]),
            created_at=values["created_at"],
        )

    def record_observation(
        self, *, attachment_id: str, observation: ObservationEnvelope
    ) -> ObservationEnvelope:
        _check_observation_binding(attachment_id, observation)
        scope_json, covered_json, attempts_json = self._encode_observation_payload(
            observation
        )
        with self._connect() as conn:
            try:
                # WHY the touch first, exactly like units and postings: it is
                # the arbitration point, so an observation from an inspection
                # that raced the delete is discarded rather than persisted.
                thread_id = self._touch(conn, attachment_id, indexed=None)
                current = conn.execute(
                    "SELECT max(observation_version) FROM fin_thread_attachment_observation "
                    "WHERE attachment_id = %s AND version = %s AND unit = %s "
                    "AND page = %s",
                    (
                        attachment_id,
                        observation.attachment_version,
                        observation.source_unit,
                        observation.page,
                    ),
                ).fetchone()[0]
                current = 0 if current is None else int(current)
                if observation.observation_version < current:
                    conn.rollback()
                    raise AttachmentStoreError(
                        f"observation version {observation.observation_version} "
                        f"is stale for attachment {attachment_id!r}: version "
                        f"{current} is already on record"
                    )
                conn.execute(
                    "INSERT INTO fin_thread_attachment_observation "
                    f"({self._OBS_COLUMNS}) "
                    "VALUES ("
                    + ", ".join(["%s"] * len(self._OBS_COLUMN_NAMES))
                    + ") "
                    "ON CONFLICT (attachment_id, version, unit, page, "
                    "observation_version) DO UPDATE SET "
                    "coverage_schema_version = EXCLUDED.coverage_schema_version, "
                    "page_image_sha256 = EXCLUDED.page_image_sha256, "
                    "provider = EXCLUDED.provider, "
                    "model_version = EXCLUDED.model_version, "
                    "answer_text = EXCLUDED.answer_text, "
                    "scope = EXCLUDED.scope, "
                    "covered_details = EXCLUDED.covered_details, "
                    "attempts = EXCLUDED.attempts, "
                    "created_at = EXCLUDED.created_at",
                    # WHY the tuple is ordered by ``_OBS_COLUMN_NAMES``:
                    # the column list and the placeholders above are derived
                    # from that one constant, so this is the only place a
                    # value could drift and it is visible per name.
                    (
                        attachment_id,
                        thread_id,
                        observation.attachment_version,
                        observation.source_unit,
                        observation.page,
                        observation.observation_version,
                        observation.coverage_schema_version,
                        observation.page_image_sha256,
                        observation.provider,
                        observation.model_version,
                        observation.answer_text,
                        scope_json,
                        covered_json,
                        attempts_json,
                        observation.created_at,
                    ),
                )
                conn.commit()
            except psycopg.errors.ForeignKeyViolation as exc:
                raise self._gone(attachment_id) from exc
        return observation

    def observations(
        self, attachment_id: str, version: int, unit: int, page: int
    ) -> tuple[ObservationEnvelope, ...]:
        _require_version(version)
        _require_version(unit)
        _require_version(page)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT {self._OBS_COLUMNS} FROM fin_thread_attachment_observation "
                "WHERE attachment_id = %s AND version = %s AND unit = %s "
                "AND page = %s ORDER BY observation_version ASC",
                (attachment_id, version, unit, page),
            ).fetchall()
            # EXIT: a source with no observation, or one already cascaded
            # away with its thread or attachment, reads back empty.
            return tuple(self._decode_observation_row(row) for row in rows)

    # -- publication --

    def mark_ready(
        self, *, attachment_id: str, version: int, unit_count: int
    ) -> AttachmentRecord:
        _require_version(version)
        if isinstance(unit_count, bool) or not isinstance(unit_count, int) or unit_count < 1:
            raise AttachmentStoreError(
                f"mark_ready needs a positive unit_count, got {unit_count!r}"
            )
        with self._connect() as conn:
            try:
                self._touch(conn, attachment_id, indexed=None)
                stored_units = int(
                    conn.execute(
                        "SELECT count(*) FROM fin_thread_attachment_unit "
                        "WHERE attachment_id = %s AND version = %s",
                        (attachment_id, version),
                    ).fetchone()[0]
                )
                stored_postings = int(
                    conn.execute(
                        "SELECT count(*) FROM fin_thread_attachment_index "
                        "WHERE attachment_id = %s AND version = %s AND units <> ''",
                        (attachment_id, version),
                    ).fetchone()[0]
                )
                # WHY the counts come from the rows and not the caller:
                # ``ready`` is the promise that the operator's text is both
                # readable and citable for THIS version, so the row may only
                # claim what the derived tables actually address.
                if stored_units != unit_count or stored_postings == 0:
                    raise AttachmentStoreError(
                        f"cannot publish version {version} of {attachment_id!r} "
                        f"as ready: {stored_units} units and {stored_postings} "
                        "postings stored"
                    )
                row = conn.execute(
                    "UPDATE fin_thread_attachment SET status = 'ready', "
                    "version = %s, unit_count = %s, indexed = TRUE, "
                    "failure_reason = NULL, updated_at = now() "
                    "WHERE attachment_id = %s "
                    "AND thread_id IN (SELECT id FROM fin_thread) "
                    f"RETURNING {self._COLUMNS}",
                    (version, unit_count, attachment_id),
                ).fetchone()
                if row is None:
                    raise self._gone(attachment_id)
                conn.commit()
            except psycopg.errors.ForeignKeyViolation as exc:
                raise self._gone(attachment_id) from exc
        return self._row_to_record(row)

    def mark_failed(
        self, *, attachment_id: str, version: int, reason: str
    ) -> AttachmentRecord:
        _require_version(version)
        reason = _require_text("failure reason", reason)
        with self._connect() as conn:
            try:
                # WHY ``version`` is not written: a parsed-content version is
                # minted by a successful preparation, so an abandoned attempt
                # must not take credit for one, and an earlier published
                # version stays addressable while its rows are still on disk.
                row = conn.execute(
                    "UPDATE fin_thread_attachment SET status = 'failed', "
                    "failure_reason = %s, updated_at = now() "
                    "WHERE attachment_id = %s "
                    "AND thread_id IN (SELECT id FROM fin_thread) "
                    f"RETURNING {self._COLUMNS}",
                    (reason, attachment_id),
                ).fetchone()
                if row is None:
                    raise self._gone(attachment_id)
                conn.commit()
            except psycopg.errors.ForeignKeyViolation as exc:
                raise self._gone(attachment_id) from exc
        return self._row_to_record(row)

    def mark_parsing(
        self,
        *,
        attachment_id: str,
        from_statuses: Sequence[AttachmentStatus] = ("uploaded",),
    ) -> AttachmentRecord | None:
        wanted = [_validate_status(status) for status in from_statuses]
        with self._connect() as conn:
            # WHY one guarded UPDATE carries the whole contract: the
            # status filter is the conditional guard (a row a concurrent
            # request already moved matches nothing), the liveness
            # subquery is the store's own delete-race guard, and
            # ``failure_reason = NULL`` only ever erases the reason of a
            # row that just left ``failed`` — every other guarded status
            # carries none.
            row = conn.execute(
                "UPDATE fin_thread_attachment SET status = 'parsing', "
                "failure_reason = NULL, updated_at = now() "
                "WHERE attachment_id = %s "
                "AND status = ANY(%s) "
                "AND thread_id IN (SELECT id FROM fin_thread) "
                f"RETURNING {self._COLUMNS}",
                (attachment_id, wanted),
            ).fetchone()
            conn.commit()
        # EXIT: a row the guard refused is a silent no-op, not an error:
        # the enqueued job still promises the work.
        return None if row is None else self._row_to_record(row)

    # -- deletion --

    def _owed_key(self, conn: Any, attachment_id: str, thread_id: str) -> str | None:
        """The key one thread still owes for one attachment id."""
        row = conn.execute(self._OWED_KEY_SQL, (attachment_id, thread_id)).fetchone()
        return None if row is None else str(row[0])

    def _void(self, conn: Any, attachment_id: str, thread_id: str) -> str | None:
        """Void one attachment on ``conn`` and return its owed object key.

        THE one tombstone write for this adapter: :meth:`tombstone` performs it
        once and :meth:`delete_for_thread` performs it once per attachment of
        the thread, so a thread-wide delete cannot drift from a
        single-attachment one and the keys a caller is handed are the
        obligations it can read back. Callers own the transaction.

        A row of the acting thread always goes with it: the promise of a thread
        delete is that no attachment row survives it. The answer is the key the
        record holds, because :meth:`create` refuses an owed id and a live row
        therefore meets no older debt.
        """
        row = conn.execute(self._VOID_TARGET_SQL, (attachment_id,)).fetchone()
        if row is None:
            # EXIT: the row is already gone, so this is the retry path — the
            # obligation on record, or nothing.
            return self._owed_key(conn, attachment_id, thread_id)
        if str(row[0]) != thread_id:
            return None  # EXIT: another thread's attachment reveals nothing.
        object_key = str(row[1])
        written = conn.execute(
            self._VOID_SQL, (attachment_id, thread_id, object_key)
        ).fetchone()
        conn.execute(
            "DELETE FROM fin_thread_attachment WHERE attachment_id = %s",
            (attachment_id,),
        )
        # A ``DO NOTHING`` here means the id's one record belongs to a
        # different thread -- the window between ``create()``'s check and a
        # concurrent void. Their debt stays on record and this row's own key
        # goes to the caller, because the row must not outlive its thread.
        return object_key if written is None else str(written[0])

    def delete_for_thread(self, thread_id: str) -> list[str]:
        # WHY the ids are read once and each voided in the same transaction:
        # the caller still has to remove the raw bytes, and the keys it is
        # handed are exactly the obligations those voids recorded.
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT attachment_id FROM fin_thread_attachment "
                "WHERE thread_id = %s ORDER BY created_at ASC, attachment_id ASC",
                (thread_id,),
            ).fetchall()
            keys = [
                key
                for key in (
                    self._void(conn, str(row[0]), thread_id) for row in rows
                )
                if key is not None
            ]
            conn.commit()
            # EXIT: an unknown or already-voided thread has no row to void,
            # which is what makes this call idempotent.
            return keys

    def tombstone(self, *, attachment_id: str, thread_id: str) -> str | None:
        _require_text("attachment_id", attachment_id)
        _require_text("thread_id", thread_id)
        with self._connect() as conn:
            try:
                object_key = self._void(conn, attachment_id, thread_id)
                if object_key is None:
                    # EXIT: an unknown id, another thread's id, an id whose
                    # cleanup is already confirmed, and an id another thread
                    # owes all share one answer, so the door cannot be used to
                    # find out which thread holds an attachment. Nothing is
                    # written: the transaction is rolled back.
                    conn.rollback()
                    return None
                conn.commit()
                return object_key
            except psycopg.errors.ForeignKeyViolation as exc:
                conn.rollback()
                raise AttachmentThreadGoneError(
                    f"thread for attachment {attachment_id!r} is gone; "
                    "nothing left to void"
                ) from exc

    def pending_cleanup(self, thread_id: str) -> list[str]:
        _require_text("thread_id", thread_id)
        with self._connect() as conn:
            # The ``thread_id`` foreign key cascades with ``fin_thread``, so
            # a deleted thread leaves no obligation behind.
            rows = conn.execute(
                "SELECT object_key FROM fin_thread_attachment_tombstone "
                "WHERE thread_id = %s ORDER BY created_at ASC, attachment_id ASC",
                (thread_id,),
            ).fetchall()
            return [str(row[0]) for row in rows]

    def confirm_cleanup(self, *, attachment_id: str) -> None:
        _require_text("attachment_id", attachment_id)
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM fin_thread_attachment_tombstone "
                "WHERE attachment_id = %s",
                (attachment_id,),
            )
            conn.commit()

    def confirm_thread_cleanup(self, thread_id: str) -> None:
        _require_text("thread_id", thread_id)
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM fin_thread_attachment_tombstone WHERE thread_id = %s",
                (thread_id,),
            )
            conn.commit()


__all__ = [
    "AttachmentRecord",
    "AttachmentStore",
    "AttachmentStoreError",
    "AttachmentStatus",
    "AttachmentThreadGoneError",
    "InMemoryAttachmentStore",
    "ParsedAttachmentUnit",
    "PostgresAttachmentStore",
    "StoredPageRegion",
]
