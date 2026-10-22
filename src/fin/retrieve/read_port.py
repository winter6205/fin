"""Company-locator validation and the unit-text lookup behind ``read_source``.

ADR 0046 / ADR 0047 (spec ``corpus-serve-ingest``): the serve path must
not keep a second on-disk extracted tree as the company-corpus SSOT.
The bounded ``read_source`` legs (ADR 0088, spec
``thread-attachment-source-read``) are the only company read seam:

* :func:`validate_company_source_ref` — the one place the company
  triple's shape and size are judged; both read legs resolve their
  locator through it.
* :func:`unit_text_from_pages` — the in-memory ``{document_id: pages}``
  lookup of one frozen unit's text that the fake serve path
  (:class:`fin.retrieve.fake_port.FakeRetrievePort`) reads its
  ``pages_by_doc`` manifest through. An identifier that is an ALIAS
  resolves to its canonical id at the lookup, so both identifier forms
  of one document read the same text.

The legacy ``read_file`` seam — the whole-page ``ReadPort.read``
pagination contract over a generic ``page`` ordinal — was removed with
the ``page`` locator contract; there is no compatibility path.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from fin.corpus.document_identity import (
    DocumentIdentityStore,
    canonical_document_id,
)
from fin.corpus.extracted import ExtractedPage
from fin.corpus.parsers._common import UnitLocator
from fin.corpus.push import serialize_source_ref
from fin.retrieve.source_read import (
    INVALID_LOCATOR,
    SOURCE_READ_MAX_LOCATOR_CODE_POINTS,
    source_error,
)


# The closed key set of a company locator triple (ADR 0088): an extra
# ``kind`` / ``page`` / path key would let the same frozen unit acquire
# a second identity, so the shape is checked against the set, not
# against a subset.
_COMPANY_LOCATOR_KEYS = frozenset({"document_id", "version", "unit"})


def validate_company_source_ref(
    source_ref: Any,
) -> tuple[str, str, int] | dict[str, Any]:
    """Resolve one company triple, or name why it is not one.

    The company locator is exactly ``{document_id, version, unit}`` with
    non-empty string ``document_id`` / ``version`` and a one-based
    integer ``unit`` — the same field rules
    :func:`fin.corpus.push.deserialize_source_ref` applies to the wire
    string, so fin never accepts a ref it could not send.

    Returns ``(document_id, version, unit)`` on success, otherwise the
    typed ``invalid_locator`` failure (a malformed shape or a canonical
    serialization over :data:`SOURCE_READ_MAX_LOCATOR_CODE_POINTS`,
    whose ``reason`` reuses the backend's own
    ``request_locator_oversized`` token so one oversized identity has
    one name on both legs). Whether the triple addresses a unit that
    exists is the caller's lookup, and answers ``unknown_locator``.

    Never raises.
    """
    if not isinstance(source_ref, Mapping):
        # EXIT: anything that is not a record (a bare serialized string,
        # a list, None) carries no typed identity to resolve.
        return source_error(INVALID_LOCATOR, reason="locator_not_a_record")
    if frozenset(source_ref) != _COMPANY_LOCATOR_KEYS:
        return source_error(INVALID_LOCATOR, reason="locator_keys_malformed")

    document_id = source_ref["document_id"]
    version = source_ref["version"]
    unit = source_ref["unit"]
    if (
        not isinstance(document_id, str)
        or not document_id
        or not isinstance(version, str)
        or not version
        # ``bool`` is an ``int`` subclass: ``True`` is a malformed
        # ordinal, never a spelling of unit 1.
        or isinstance(unit, bool)
        or not isinstance(unit, int)
        or unit < 1
    ):
        return source_error(INVALID_LOCATOR, reason="locator_fields_malformed")

    if (
        len(serialize_source_ref(UnitLocator(document_id, version, unit)))
        > SOURCE_READ_MAX_LOCATOR_CODE_POINTS
    ):
        # EXIT: an identity this long leaves the bounded result no room
        # for any text, so it is refused before a round trip rather than
        # answered with a shrunken or oversized slice.
        return source_error(INVALID_LOCATOR, reason="request_locator_oversized")
    return document_id, version, unit


def unit_text_from_pages(
    pages_by_doc: Mapping[str, Sequence[ExtractedPage]],
    document_id: str,
    version: str,
    unit: int,
    *,
    identity_store: DocumentIdentityStore | None = None,
) -> str | None:
    """The text of one frozen unit of one document, or ``None``.

    This is the shared lookup of every pages-backed company read (the
    fake manifest and an in-memory extracted tree have the same shape).
    ``None`` names every miss: unknown ``document_id``, a version that
    does not match the table's own rows, ``unit < 1``, and a unit past
    the table. Never raises.

    A document id that is an ALIAS resolves to its canonical id BEFORE the
    lookup, so a reader holding either identifier finds the same text. The
    locator stays exactly ``{document_id, version, unit}``: the resolution
    happens at the lookup, never by widening a locator.

    The mapping is ADDITIVE and OPTIONAL, so an identity database that is
    down never fails this read: the identifier stays unresolved — an alias
    then misses, the canonical id still answers — and the outage reaches
    the log.
    """
    resolved_id = canonical_document_id(document_id, store=identity_store)
    pages = pages_by_doc.get(resolved_id)
    if not pages or unit < 1:
        return None

    # The rows' locator ``version`` is the ingested sha256; comparing
    # the caller's ``version`` against the first page rejects any
    # drift without a separate manifest read.
    if version != pages[0].locator.version:
        return None
    if unit > len(pages):
        return None
    return pages[unit - 1].text


__all__ = [
    "unit_text_from_pages",
    "validate_company_source_ref",
]
