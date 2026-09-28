"""The attachment leg of the bounded ``read_source`` boundary (ADR 0087/0088).

Spec ``thread-attachment-source-read`` (Boundaries: "Ready attachment hits
carry ``source_ref={kind:"attachment",attachment_id,version,unit}``";
"Validate attachment identity, active-thread ownership, and parsed-content
version before a read... can succeed"): one model-facing read resolves
exactly one frozen unit of one parsed-content version of one attachment of
the HOST-trusted thread and hands back at most the shared slice bounds
(see :mod:`fin.retrieve.source_read`).

Two placement facts shape this module:

* ``thread_id`` is constructor input the host supplies from the live run,
  never a locator field — a model-supplied thread id cannot widen scope,
  and a locator carrying one fails the exact-key-set check.
* Cross-thread, unknown, tombstoned, and stale-version lookups share one
  ``unknown_locator`` answer with no differentiating field, so the read
  cannot be used to probe what a different thread holds (the same
  code the preparation door publishes, see
  :mod:`fin.host.attachment_prepare`).

The canonical locator serialization mirrors
:func:`fin.corpus.push.serialize_source_ref`: sorted keys, compact
separators, UTF-8, with ``version`` as a STRING like the company wire, so
one dispatching reader cannot fork two spelling rules for the same hit.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from fin.corpus.determinism import canonical_json_bytes
from fin.host.attachment_store import (
    AttachmentStore,
    AttachmentThreadGoneError,
)
from fin.retrieve.source_read import (
    ATTACHMENT_KIND,
    INVALID_LOCATOR,
    SOURCE_READ_MAX_LOCATOR_CODE_POINTS,
    SOURCE_UNAVAILABLE,
    UNKNOWN_LOCATOR,
    bounded_slice,
    source_error,
)

logger = logging.getLogger(__name__)

_LOCATOR_KEYS: frozenset[str] = frozenset({"kind", "attachment_id", "version", "unit"})


class AttachmentLocatorError(ValueError):
    """One malformed locator shape.

    ``reason`` names only the shape of the model's own input (which key,
    which type, oversize) — never anything about the stored attachment,
    so an invalid_locator answer cannot reveal whether an id exists.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class AttachmentSourceLocator:
    """One attachment hit's identity: ``{attachment_id, version, unit}``.

    ``version`` is the parsed-content version (non-negative int once
    parsed), ``unit`` the 1-based frozen-unit ordinal. ``position`` is
    NOT part of the locator — it is a separate zero-based read offset
    (spec: "read ``position`` is a separate zero-based offset within
    that unit").
    """

    attachment_id: str
    version: int
    unit: int

    def to_wire_mapping(self) -> dict[str, Any]:
        """The locator as the model sees it: ``kind`` tag plus the triple.

        ``version`` rides as a string, the company-wire convention
        (:class:`fin.corpus.parsers._common.UnitLocator`), so both legs
        serialize a version identically.
        """
        return {
            "kind": ATTACHMENT_KIND,
            "attachment_id": self.attachment_id,
            "version": str(self.version),
            "unit": self.unit,
        }

    def serialize(self) -> str:
        """The canonical serialization: sorted keys, compact separators.

        A pure function of the triple, so the same frozen unit cannot
        acquire multiple reference strings (the invariant
        :func:`fin.corpus.push.serialize_source_ref` fixes for the
        company leg).
        """
        return canonical_json_bytes(self.to_wire_mapping()).decode("utf-8")


def parse_attachment_source_locator(
    source_ref: Mapping[str, Any],
) -> AttachmentSourceLocator:
    """Validate one model-supplied mapping, or raise the shape's name.

    Accepts both wire spellings of ``version``: the canonical string from
    :meth:`AttachmentSourceLocator.serialize`, and the integer a host-side
    hit freshly built from ``AttachmentRecord.version`` carries. Every
    rejection names only the offending input shape, and the canonical
    serialization must fit :data:`SOURCE_READ_MAX_LOCATOR_CODE_POINTS` so
    an oversized identity is refused before any store lookup.
    """
    if not isinstance(source_ref, Mapping):
        raise AttachmentLocatorError("locator_not_a_mapping")
    if source_ref.get("kind") != ATTACHMENT_KIND:
        raise AttachmentLocatorError("locator_kind")
    if set(source_ref) != _LOCATOR_KEYS:
        # EXIT: extra keys (a model-supplied thread_id would be one) and
        # missing keys share this answer; the locator has exactly four.
        raise AttachmentLocatorError("locator_keys")

    attachment_id = source_ref["attachment_id"]
    if not isinstance(attachment_id, str) or not attachment_id.strip():
        raise AttachmentLocatorError("locator_attachment_id")
    version = _parse_version(source_ref["version"])
    unit = source_ref["unit"]
    if isinstance(unit, bool) or not isinstance(unit, int) or unit < 1:
        raise AttachmentLocatorError("locator_unit")

    locator = AttachmentSourceLocator(
        attachment_id=attachment_id, version=version, unit=unit
    )
    if len(locator.serialize()) > SOURCE_READ_MAX_LOCATOR_CODE_POINTS:
        # EXIT: an oversized identity would make the result envelope
        # unbounded; typed here so no store lookup is ever probed with it.
        raise AttachmentLocatorError("locator_identity_oversized")
    return locator


def _parse_version(raw: Any) -> int:
    """The parsed-content version from either accepted wire spelling."""
    if isinstance(raw, bool):
        raise AttachmentLocatorError("locator_version")
    if isinstance(raw, int):
        if raw < 0:
            raise AttachmentLocatorError("locator_version")
        return raw
    if isinstance(raw, str):
        try:
            value = int(raw)
        except ValueError:
            raise AttachmentLocatorError("locator_version") from None
        # WHY the re-stringify guard: "01"/"+3" would serialize to a
        # second spelling of the same version, forking locator identity.
        if value < 0 or str(value) != raw:
            raise AttachmentLocatorError("locator_version")
        return value
    raise AttachmentLocatorError("locator_version")


class AttachmentSourceReader:
    """The attachment leg of the shared :class:`SourceReader` contract.

    Constructed with the store and the HOST-trusted active ``thread_id``;
    the model cannot supply or override it. Each call resolves exactly one
    frozen unit at one parsed-content version and answers either the
    bounded slice or a typed error — never an exception, never prose.
    """

    def __init__(self, store: AttachmentStore, thread_id: str) -> None:
        if not isinstance(thread_id, str) or not thread_id.strip():
            raise ValueError("thread_id must be a non-empty string")
        self._store = store
        self._thread_id = thread_id

    def read_slice(
        self,
        source_ref: Mapping[str, Any],
        position: int = 0,
    ) -> dict[str, Any]:
        """Read one bounded slice of one attachment frozen unit.

        The attachment leg of :class:`fin.retrieve.source_read.SourceReader`
        (ADR 0087 / ADR 0088): exactly one frozen unit of one
        parsed-content version of one attachment of the host-trusted
        thread resolves at ``position`` and answers
        ``{source_ref, text, next_position}`` or one typed failure —
        ``invalid_locator`` (malformed shape), ``unknown_locator``
        (missing, foreign-thread, tombstoned, stale, unready, or an
        off-range unit), ``invalid_position`` (beyond the unit), or
        ``source_unavailable`` (store fault). It never raises at the
        model and never carries text on a failure.
        """
        try:
            locator = parse_attachment_source_locator(source_ref)
        except AttachmentLocatorError as exc:
            # EXIT: malformed or oversized locator shape; no store lookup,
            # so the answer reveals nothing about what exists.
            return source_error(INVALID_LOCATOR, reason=exc.reason)
        try:
            record = self._store.get(locator.attachment_id)
            if (
                record is None
                or record.thread_id != self._thread_id
                or record.version != locator.version
                or not record.searchable
            ):
                # EXIT: missing, foreign-thread, tombstoned, stale-version,
                # or not-ready attachments share one reveal-nothing answer.
                return source_error(UNKNOWN_LOCATOR)
            units = self._store.units(locator.attachment_id, locator.version)
        except AttachmentThreadGoneError:
            # EXIT: the thread or attachment went away mid-resolution —
            # the same identity loss the pre-read gate answers.
            return source_error(UNKNOWN_LOCATOR)
        except Exception as exc:
            # EXIT: a genuine store/transport fault is source_unavailable
            # — this leg never raises at the model, so an unexpected
            # exception is named in the log (programming errors and
            # outages stay visible) while the answer stays the one outage
            # code with no locator-derived text either way.
            logger.debug("attachment read store fault: %s", exc)
            return source_error(SOURCE_UNAVAILABLE)

        text = next((unit.text for unit in units if unit.unit == locator.unit), None)
        if text is None:
            # EXIT: the ordinal addresses no frozen row of this version
            # (out of range, or derived data lost); identity stays unknown.
            return source_error(UNKNOWN_LOCATOR)
        # WHY echo the request mapping: the caller keeps one locator
        # spelling across the continuation walk, and ``bounded_slice``
        # types the position classes (invalid_position) plus the
        # 8000-code-point / 32-KiB bounds from the shared contract.
        return bounded_slice(dict(source_ref), text, position)


__all__ = [
    "ATTACHMENT_KIND",
    "AttachmentLocatorError",
    "AttachmentSourceLocator",
    "AttachmentSourceReader",
    "parse_attachment_source_locator",
]
