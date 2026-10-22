"""Durable catalog publication and recovery over the ingest audit (T6).

The frozen contract (``specs/qa-chain-corpus-retrieval.md`` section
"Durable catalog publication and recovery") makes a catalog upsert a
RECOVERABLE multi-step effect, persisted only in the existing
``fin_ingest_audit.metadata`` JSONB — no new job columns, no second
publication table, no thread-local warning transport.

The five exact event ``action`` names and their payload fields:

=============================  ========================================
``upload_metadata_accepted``   ``version``, ``title``, ``doc_type``,
                               optional ``company`` / ``period``
``push_succeeded``             ``version``, ``content_identities``
``catalog_publication_pending`` ``version``, ``publication_id``,
                               ``attempt``, ``catalog_row``
``catalog_publication_failed``  ``version``, ``publication_id``,
                               ``attempt``, ``failure_code``,
                               ``failure_stage``
``catalog_publication_succeeded`` ``version``, ``publication_id``,
                               ``attempt``
=============================  ========================================

Every payload carries ``schema_version: 1`` (an integer — a boolean is
not an integer here) and no raw exception text, local path, credential
or DSN ever enters one.

The order is: accepted metadata → push → ``push_succeeded`` →
``catalog_publication_pending`` (with the final row) → the idempotent
``(document_id, version)`` upsert → ``catalog_publication_succeeded`` →
the ``cataloging -> ready`` transition. The two "before" writes are the
redo log: if the accepted-metadata or pending write fails, the worker
does not start (or upsert), because recovery input would not be durable.
If the upsert itself fails, the typed failure is appended, the job stays
``cataloging`` and the warning becomes visible through polling. If a
TERMINAL append fails after a durable pending row, the job also stays
``cataloging`` and recovery redoes the idempotent upsert from the
already-durable pending row.

This module is the typed boundary for all of it: the payload builders
(the schema, in one place), the failure mapping
(``CatalogRowError`` / ``CatalogCorruptError`` / ``OSError`` to their
``failure_code`` / ``failure_stage`` pairs), and the pure recovery
decision that reads an audit trail and answers "retry the upsert, or
first the push?" without touching a store.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

#: Integer schema version of every event payload. Bumped when a payload
#: field's meaning changes; readers refuse payloads from a schema they
#: do not know rather than guessing.
CATALOG_PUBLICATION_SCHEMA_VERSION: Final[int] = 1

#: The five exact audit ``action`` names of this contract.
UPLOAD_METADATA_ACCEPTED: Final[str] = "upload_metadata_accepted"
PUSH_SUCCEEDED: Final[str] = "push_succeeded"
CATALOG_PUBLICATION_PENDING: Final[str] = "catalog_publication_pending"
CATALOG_PUBLICATION_FAILED: Final[str] = "catalog_publication_failed"
CATALOG_PUBLICATION_SUCCEEDED: Final[str] = "catalog_publication_succeeded"

#: The typed failure codes of ``catalog_publication_failed``, and the
#: stages they may carry. ``validate`` only ever rides
#: ``catalog_row_invalid``; ``read`` rides ``catalog_corrupt`` or
#: ``catalog_unavailable``; ``lock`` / ``write`` ride
#: ``catalog_unavailable``.
FAILURE_CODES: Final[tuple[str, ...]] = (
    "catalog_row_invalid",
    "catalog_corrupt",
    "catalog_unavailable",
)
FAILURE_STAGES: Final[tuple[str, ...]] = ("validate", "read", "lock", "write")

PublicationFailureCode = Literal[
    "catalog_row_invalid", "catalog_corrupt", "catalog_unavailable"
]
PublicationFailureStage = Literal["validate", "read", "lock", "write"]


class CatalogPublicationAuditError(RuntimeError):
    """An audit event could not be durably appended.

    Carries the event's ``action`` so a caller can tell a failed
    accepted-metadata write (no work may start) from a failed terminal
    write (the pending row is already durable and recovery owns the
    redo) without string matching.
    """

    def __init__(self, message: str, *, action: str) -> None:
        super().__init__(message)
        self.action = action


def accepted_metadata_payload(
    *,
    version: str,
    title: str,
    doc_type: str,
    company: str | None = None,
    period: str | None = None,
) -> dict[str, Any]:
    """The ``upload_metadata_accepted`` metadata object.

    Optional fields are omitted, never written as ``""`` — a reader of
    the audit JSON must be able to tell "the uploader said nothing"
    from "the uploader said the empty string".
    """
    payload: dict[str, Any] = {
        "schema_version": CATALOG_PUBLICATION_SCHEMA_VERSION,
        "version": version,
        "title": title,
        "doc_type": doc_type,
    }
    if company:
        payload["company"] = company
    if period:
        payload["period"] = period
    return payload


def push_succeeded_payload(
    *, version: str, content_identities: Mapping[str, str]
) -> dict[str, Any]:
    """The ``push_succeeded`` metadata object.

    ``content_identities`` maps every non-empty ``source_ref`` to its
    non-empty upstream identity string — the durable half of the
    convergent-resend story: with this row, recovery retries ONLY the
    catalog upsert and never re-pushes.
    """
    return {
        "schema_version": CATALOG_PUBLICATION_SCHEMA_VERSION,
        "version": version,
        "content_identities": {
            source_ref: identity
            for source_ref, identity in content_identities.items()
            if source_ref and identity
        },
    }


def publication_id(document_id: str, version: str) -> str:
    """``catalog-v1:<document_id>:<version>`` — the spec's literal shape."""
    return f"catalog-v1:{document_id}:{version}"


def pending_payload(
    *,
    version: str,
    document_id: str,
    attempt: int,
    catalog_row: Mapping[str, Any],
) -> dict[str, Any]:
    """The ``catalog_publication_pending`` metadata object.

    ``catalog_row`` is the COMPLETE physical row to upsert — the redo
    input. Recovery never rebuilds a row from an id or a digest: the
    row that was accepted is the row that gets written, so
    ``company`` / ``period`` can never be re-invented from a
    ``document_id``.
    """
    return {
        "schema_version": CATALOG_PUBLICATION_SCHEMA_VERSION,
        "version": version,
        "publication_id": publication_id(document_id, version),
        "attempt": attempt,
        "catalog_row": dict(catalog_row),
    }


def failed_payload(
    *,
    version: str,
    document_id: str,
    attempt: int,
    failure_code: PublicationFailureCode,
    failure_stage: PublicationFailureStage,
) -> dict[str, Any]:
    """The ``catalog_publication_failed`` metadata object (typed only)."""
    return {
        "schema_version": CATALOG_PUBLICATION_SCHEMA_VERSION,
        "version": version,
        "publication_id": publication_id(document_id, version),
        "attempt": attempt,
        "failure_code": failure_code,
        "failure_stage": failure_stage,
    }


def succeeded_payload(
    *, version: str, document_id: str, attempt: int
) -> dict[str, Any]:
    """The ``catalog_publication_succeeded`` metadata object."""
    return {
        "schema_version": CATALOG_PUBLICATION_SCHEMA_VERSION,
        "version": version,
        "publication_id": publication_id(document_id, version),
        "attempt": attempt,
    }


def classify_catalog_failure(exc: BaseException) -> (
    tuple[PublicationFailureCode, PublicationFailureStage]
):
    """Map one catalog exception to its typed ``(code, stage)`` pair.

    ``CatalogRowError`` → ``catalog_row_invalid`` / ``validate``;
    ``CatalogCorruptError`` → ``catalog_corrupt`` / ``read``; an
    ``OSError`` is the filesystem failing under one of the three
    operations, named by which: ``read``, ``lock`` (the flock) or
    ``write`` (the temp-file replace). Anything else is a programming
    error and propagates — it is never swallowed into a warning.
    """
    from fin.corpus.errors import CatalogCorruptError, CatalogRowError

    if isinstance(exc, CatalogRowError):
        return "catalog_row_invalid", "validate"
    if isinstance(exc, CatalogCorruptError):
        return "catalog_corrupt", "read"
    if isinstance(exc, OSError):
        return "catalog_unavailable", _oserror_stage(exc)
    raise exc


def _oserror_stage(exc: OSError) -> PublicationFailureStage:
    """Which catalog operation an ``OSError`` interrupted.

    The stage is read from the failing file's name — the code's own
    fingerprints: the sibling lock file (``*.json.lock``) means the
    lock, the catalog's temp file (``.catalog-*.tmp``) means the
    atomic replace, and anything else is the read side. A mislabeled
    stage is worse than a truthful coarse one, so unknown names err
    toward ``read``, the first operation an upsert performs.
    """
    filename = str(exc.filename or "")
    if filename.endswith(".lock"):
        return "lock"
    if ".catalog-" in filename and filename.endswith(".tmp"):
        return "write"
    return "read"


# ---------- audit-trail reading (the recovery input) ----------


@dataclass(frozen=True)
class AcceptedMetadata:
    """What the durable ``upload_metadata_accepted`` row recorded."""

    version: str
    title: str
    doc_type: str
    company: str | None
    period: str | None


@dataclass(frozen=True)
class PendingPublication:
    """The durable redo input of the latest pending upsert attempt."""

    version: str
    attempt: int
    catalog_row: dict[str, Any]


@dataclass(frozen=True)
class PublicationWarning:
    """The unresolved publication state a poller reports.

    ``code`` is exactly ``catalog_publication_failed`` or
    ``catalog_publication_pending``; ``attempts`` is the LATEST 1-based
    attempt number, never a count of attempts.
    """

    code: Literal[
        "catalog_publication_failed", "catalog_publication_pending"
    ]
    failure_code: str | None
    failure_stage: str | None
    stage: str | None
    attempts: int


def read_accepted_metadata(
    rows: Sequence[Any],
) -> AcceptedMetadata | None:
    """The accepted metadata of a document's audit trail, or ``None``.

    Reads ONLY audit JSON. ``company`` / ``period`` come from the row
    or not at all — reconstruction from ``document_id`` is the one
    thing recovery must never do, because a guessed company makes a
    document searchable under a name that does not own it.
    """
    for row in reversed(rows):
        if row.action != UPLOAD_METADATA_ACCEPTED:
            continue
        metadata = row.metadata
        if not isinstance(metadata, Mapping):
            return None
        version = _required_str(metadata, "version")
        title = _required_str(metadata, "title")
        doc_type = _required_str(metadata, "doc_type")
        if version is None or title is None or doc_type is None:
            return None
        return AcceptedMetadata(
            version=version,
            title=title,
            doc_type=doc_type,
            company=_optional(metadata, "company"),
            period=_optional(metadata, "period"),
        )
    return None


def read_push_succeeded(
    rows: Sequence[Any], *, version: str
) -> bool:
    """Whether ``push_succeeded`` is durable for this version.

    The one question recovery asks before deciding to re-push: a
    durable push-success means the ONLY remaining work is the catalog
    upsert, and re-pushing would be a second delivery of a completed
    one.
    """
    for row in reversed(rows):
        if row.action != PUSH_SUCCEEDED:
            continue
        metadata = row.metadata
        if not isinstance(metadata, Mapping):
            return False
        return metadata.get("version") == version
    return False


def read_push_identities(
    rows: Sequence[Any], *, version: str
) -> dict[str, str]:
    """The ``content_identities`` map ``push_succeeded`` recorded.

    Recovery that skips its re-push still has to land the job with the
    pair map the completing push persisted — the index-job handoff
    reads them off the ready edge — so it reads them back from the
    ``push_succeeded`` row rather than recomputing anything. An absent
    or unusable row yields ``{}``; a recovery without the map skips the
    same handoff a run without identities skips, and never invents a
    pair.
    """
    for row in reversed(rows):
        if row.action != PUSH_SUCCEEDED:
            continue
        metadata = row.metadata
        if not isinstance(metadata, Mapping):
            return {}
        if metadata.get("version") != version:
            return {}
        recorded = metadata.get("content_identities")
        if not isinstance(recorded, Mapping):
            return {}
        return {
            source_ref: identity
            for source_ref, identity in recorded.items()
            if (
                isinstance(source_ref, str)
                and source_ref
                and isinstance(identity, str)
                and identity
            )
        }
    return {}


def read_pending_publication(
    rows: Sequence[Any],
) -> PendingPublication | None:
    """The LATEST pending publication row, or ``None``.

    ``version`` / ``attempt`` / ``catalog_row`` come from the row's own
    fields; a row with a missing or non-mapping ``catalog_row`` is not
    usable redo input and yields ``None`` — recovery refuses to
    reconstruct what was never written.
    """
    for row in reversed(rows):
        if row.action != CATALOG_PUBLICATION_PENDING:
            continue
        metadata = row.metadata
        if not isinstance(metadata, Mapping):
            return None
        version = metadata.get("version")
        attempt = metadata.get("attempt")
        catalog_row = metadata.get("catalog_row")
        if not isinstance(version, str) or not version:
            return None
        if isinstance(attempt, bool) or not isinstance(attempt, int):
            return None
        if not isinstance(catalog_row, Mapping):
            return None
        return PendingPublication(
            version=version,
            attempt=attempt,
            catalog_row=dict(catalog_row),
        )
    return None


def publication_warning(rows: Sequence[Any]) -> PublicationWarning | None:
    """The unresolved warning state of one document's audit trail.

    Walking BACKWARD, the newest publication event IS the current
    state — the trail is append-only and ordered, so the first
    ``catalog_publication_*`` row from the end is the last thing that
    happened:

    * ``catalog_publication_succeeded`` → no warning (success clears);
    * ``catalog_publication_failed`` → ``{code:
      catalog_publication_failed, failure_code, failure_stage,
      attempts=that attempt}``;
    * ``catalog_publication_pending`` with no terminal event after it →
      ``{code: catalog_publication_pending, stage: "recovery",
      attempts}`` — this is also the shape after a terminal append
      itself failed, which is exactly why the pending row is durable,
      and after a recovery retry re-opened the attempt: the retry's
      pending row supersedes the older failure it responds to.

    ``attempts`` is the 1-based attempt number of that newest event,
    not a tally: a document retried five times reports
    ``attempts: 5`` once, and ``catalog_warning_count`` counts
    DOCUMENTS, so counting attempts here would double-count.
    """
    for row in reversed(rows):
        if row.action == CATALOG_PUBLICATION_SUCCEEDED:
            return None
        if row.action == CATALOG_PUBLICATION_FAILED:
            metadata = row.metadata
            if not isinstance(metadata, Mapping):
                continue
            attempt = _attempt_of(metadata)
            if attempt < 1:
                continue
            return PublicationWarning(
                code="catalog_publication_failed",
                failure_code=(
                    str(metadata.get("failure_code"))
                    if metadata.get("failure_code")
                    else None
                ),
                failure_stage=(
                    str(metadata.get("failure_stage"))
                    if metadata.get("failure_stage")
                    else None
                ),
                stage=None,
                attempts=attempt,
            )
        if row.action == CATALOG_PUBLICATION_PENDING:
            metadata = row.metadata
            if not isinstance(metadata, Mapping):
                continue
            attempt = _attempt_of(metadata)
            if attempt < 1:
                continue
            return PublicationWarning(
                code="catalog_publication_pending",
                failure_code=None,
                failure_stage=None,
                stage="recovery",
                attempts=attempt,
            )
    return None


def _attempt_of(metadata: Mapping[str, Any] | None) -> int:
    if metadata is None:
        return -1
    attempt = metadata.get("attempt")
    if isinstance(attempt, bool) or not isinstance(attempt, int):
        return -1
    return attempt


def _optional(metadata: Mapping[str, Any], key: str) -> str | None:
    value = metadata.get(key)
    return value if isinstance(value, str) and value else None


def _required_str(metadata: Mapping[str, Any], key: str) -> str | None:
    """A payload field that must be a non-empty string, or ``None``."""
    value = metadata.get(key)
    return value if isinstance(value, str) and value else None


__all__ = [
    "CATALOG_PUBLICATION_FAILED",
    "CATALOG_PUBLICATION_PENDING",
    "CATALOG_PUBLICATION_SCHEMA_VERSION",
    "CATALOG_PUBLICATION_SUCCEEDED",
    "FAILURE_CODES",
    "FAILURE_STAGES",
    "PUSH_SUCCEEDED",
    "UPLOAD_METADATA_ACCEPTED",
    "AcceptedMetadata",
    "CatalogPublicationAuditError",
    "PendingPublication",
    "PublicationFailureCode",
    "PublicationFailureStage",
    "PublicationWarning",
    "accepted_metadata_payload",
    "classify_catalog_failure",
    "failed_payload",
    "pending_payload",
    "publication_id",
    "publication_warning",
    "push_succeeded_payload",
    "read_accepted_metadata",
    "read_pending_publication",
    "read_push_identities",
    "read_push_succeeded",
    "succeeded_payload",
]
