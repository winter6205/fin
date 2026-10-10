"""Pure validation for Query v1 structured evidence and frozen snapshots."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final, Mapping, Sequence, cast
from uuid import UUID

from fin.host.evidence_contract import (
    EVIDENCE_TOO_LARGE,
    MALFORMED_EVIDENCE_CONTRACT,
    UNSUPPORTED_EVIDENCE_CONTRACT,
)
from fin.host.structured_snapshot import (
    SAFE_INTEGER_MAX,
    STRUCTURED_SNAPSHOT_MAX_BYTES,
    STRUCTURED_SNAPSHOT_SCHEMA,
    STRUCTURED_SNAPSHOT_FIELDS,
    STRUCTURED_SNAPSHOT_SCHEMA_VERSION,
    STRUCTURED_RESULT_KIND,
    STRUCTURED_SNAPSHOT_SCHEMA_VERSION_FIELD,
    StructuredSnapshotError,
    canonical_bytes,
    structured_snapshot_bytes,
    structured_snapshot_digest,
)
from fin.retrieve.read_port import validate_company_source_ref

STRUCTURED_RESULT_SCHEMA: Final[str] = "iknow.structured_result"

STRUCTURED_RESULT_SCHEMA_VERSION: Final[int] = 1

STRUCTURED_RESULT_COVERAGE: Final[str] = "stored_structured_subjects"

_ENVELOPE_CONTEXT_FIELDS: Final[tuple[str, ...]] = (
    "subject",
    "operation",
    "query_digest",
    "scope_binding",
    "read_revision",
    "queried_at",
)

_LIST_OPERATION: Final[str] = "list"

_COUNT_OPERATION: Final[str] = "count"

_STRUCTURED_SUBJECTS: Final[frozenset[str]] = frozenset(
    {"entity", "relation_assertion"}
)

_STRUCTURED_OPERATIONS: Final[frozenset[str]] = frozenset(
    {_LIST_OPERATION, _COUNT_OPERATION}
)

_ENTITY_TYPES: Final[frozenset[str]] = frozenset(
    {"person", "organization", "place", "event"}
)

_MAINTENANCE_STATES: Final[frozenset[str]] = frozenset(
    {"pending", "running", "ready", "failed", "stale"}
)

_LIST_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "schema",
        "schema_version",
        *_ENVELOPE_CONTEXT_FIELDS,
        "maintenance",
        "rows",
        "next_cursor",
    }
)

_COUNT_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "schema",
        "schema_version",
        *_ENVELOPE_CONTEXT_FIELDS,
        "maintenance",
        "count",
        "count_receipt",
    }
)

_ROW_EVIDENCE_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "evidence_ref",
        "evidence_expires_at",
        "provenance",
        "provenance_complete",
        "provenance_next_ref",
    }
)

_ENTITY_ROW_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "id",
        "canonical_name",
        "type",
        "display_support_id",
        "display_disagreement",
        *_ROW_EVIDENCE_FIELDS,
    }
)

_RELATION_ROW_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "id",
        "head",
        "relation_norm",
        "tail",
        "valid_window",
        *_ROW_EVIDENCE_FIELDS,
    }
)

_ENDPOINT_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "id",
        "canonical_name",
        "type",
        "display_support_id",
        "display_disagreement",
    }
)

_COUNT_RECEIPT_REQUIRED_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "subject",
        "count",
        "query_digest",
        "scope_binding",
        "read_revision",
        "queried_at",
        "coverage",
        "maintenance_revision",
        "maintenance",
        "evidence_ref",
        "evidence_expires_at",
    }
)

_SUPPORT_REQUIRED_FIELDS: Final[frozenset[str]] = frozenset(
    {"support_id", "source_ref", "source_version", "location", "extraction_ref"}
)

_SUPPORT_SPAN_FIELDS: Final[frozenset[str]] = frozenset({"start", "end"})

_MAINTENANCE_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "maintenance_revision",
        "sources_in_scope",
        "published_sources",
        "desired_index",
        "conflict_checks",
        "ruleset_revision",
        "coverage",
    }
)

MAX_STRUCTURED_ROWS: Final[int] = 20

MAX_INLINE_SUPPORTS: Final[int] = 8

_TIMESTAMP_RE: Final[re.Pattern[str]] = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"
)

@dataclass(frozen=True)
class StructuredRefusal:
    """One typed refusal. ``code`` is the spec's §6 vocabulary."""

    code: str
    reason: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "reason": self.reason}

def _refuse(code: str, reason: str) -> StructuredRefusal:
    return StructuredRefusal(code=code, reason=reason)

def _is_whole_second_timestamp(value: Any) -> bool:
    """True only for the contract's whole-second ``Z`` form.

    A producer that emits a fractional or offset timestamp is refused
    rather than normalized: coercing it here would put the same instant
    into the snapshot digest under a spelling the producer never wrote.
    """
    if not isinstance(value, str) or _TIMESTAMP_RE.match(value) is None:
        return False
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return False
    return True

def _non_empty_str(value: Any) -> bool:
    return isinstance(value, str) and value != ""

def _canonical_uuid(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False

def _nonnegative_safe_integer(value: Any) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 0 <= value <= SAFE_INTEGER_MAX
    )

def _expected_evidence_expiry(queried_at: str) -> str | None:
    try:
        expiry = datetime.strptime(queried_at, "%Y-%m-%dT%H:%M:%SZ") + timedelta(days=365)
    except (ValueError, OverflowError):
        return None
    return expiry.strftime("%Y-%m-%dT%H:%M:%SZ")

def _check_maintenance_states(
    name: str, states: Any, source_count: int
) -> StructuredRefusal | None:
    if not isinstance(states, Mapping) or frozenset(states) != _MAINTENANCE_STATES:
        return _refuse(
            MALFORMED_EVIDENCE_CONTRACT,
            f"maintenance.{name} has the wrong shape",
        )
    if any(not _nonnegative_safe_integer(count) for count in states.values()):
        return _refuse(
            MALFORMED_EVIDENCE_CONTRACT,
            f"maintenance.{name} contains an invalid count",
        )
    if sum(states.values()) != source_count:
        return _refuse(
            MALFORMED_EVIDENCE_CONTRACT,
            f"maintenance.{name} does not cover sources_in_scope",
        )
    return None


def _check_maintenance_counts(value: Mapping[str, Any]) -> StructuredRefusal | None:
    source_count = value.get("sources_in_scope")
    published_count = value.get("published_sources")
    if not _nonnegative_safe_integer(source_count) or not _nonnegative_safe_integer(
        published_count
    ):
        return _refuse(MALFORMED_EVIDENCE_CONTRACT, "maintenance source counts are invalid")
    source_count = cast(int, source_count)
    published_count = cast(int, published_count)
    if published_count > source_count:
        return _refuse(
            MALFORMED_EVIDENCE_CONTRACT,
            "maintenance.published_sources exceeds sources_in_scope",
        )
    for name in ("desired_index", "conflict_checks"):
        refusal = _check_maintenance_states(name, value.get(name), source_count)
        if refusal is not None:
            return refusal
    return None


def _check_maintenance(value: Any) -> StructuredRefusal | None:
    if not isinstance(value, Mapping) or frozenset(value) != _MAINTENANCE_FIELDS:
        return _refuse(MALFORMED_EVIDENCE_CONTRACT, "maintenance has the wrong shape")
    for name in ("maintenance_revision", "ruleset_revision"):
        if not _non_empty_str(value.get(name)):
            return _refuse(
                MALFORMED_EVIDENCE_CONTRACT,
                f"maintenance.{name} is not a non-empty string",
            )
    if value.get("coverage") != "structural_candidates_only":
        return _refuse(MALFORMED_EVIDENCE_CONTRACT, "maintenance.coverage is unsupported")
    return _check_maintenance_counts(value)

def _check_evidence_reference(value: Mapping[str, Any], queried_at: str) -> StructuredRefusal | None:
    if not _non_empty_str(value.get("evidence_ref")):
        return _refuse(MALFORMED_EVIDENCE_CONTRACT, "evidence_ref is not a non-empty string")
    expiry = value.get("evidence_expires_at")
    if not _is_whole_second_timestamp(expiry):
        return _refuse(
            MALFORMED_EVIDENCE_CONTRACT,
            "evidence_expires_at is not a whole-second Z timestamp",
        )
    expected_expiry = _expected_evidence_expiry(queried_at)
    if expected_expiry is None or expiry != expected_expiry:
        return _refuse(
            MALFORMED_EVIDENCE_CONTRACT,
            "evidence_expires_at is not 365 days after queried_at",
        )
    return None

def _check_schema(payload: Mapping[str, Any]) -> StructuredRefusal | None:
    if payload.get("schema") != STRUCTURED_RESULT_SCHEMA:
        return _refuse(MALFORMED_EVIDENCE_CONTRACT, "unknown structured_result schema token")
    raw_version = payload.get("schema_version")
    if isinstance(raw_version, bool) or not isinstance(raw_version, (int, float)):
        return _refuse(MALFORMED_EVIDENCE_CONTRACT, "schema_version is not a number")
    if not isinstance(raw_version, int) and not float(raw_version).is_integer():
        return _refuse(MALFORMED_EVIDENCE_CONTRACT, "schema_version is not an integer")
    if int(raw_version) != STRUCTURED_RESULT_SCHEMA_VERSION:
        return _refuse(
            UNSUPPORTED_EVIDENCE_CONTRACT,
            f"structured_result schema_version {int(raw_version)} is not supported",
        )
    return None

def _check_context(payload: Mapping[str, Any]) -> StructuredRefusal | None:
    for name in _ENVELOPE_CONTEXT_FIELDS:
        value = payload.get(name)
        if name == "queried_at":
            if not _is_whole_second_timestamp(value):
                return _refuse(
                    MALFORMED_EVIDENCE_CONTRACT,
                    "queried_at is not a whole-second Z timestamp",
                )
            continue
        if not _non_empty_str(value):
            return _refuse(MALFORMED_EVIDENCE_CONTRACT, f"{name} is not a non-empty string")
    if payload.get("subject") not in _STRUCTURED_SUBJECTS:
        return _refuse(MALFORMED_EVIDENCE_CONTRACT, "subject is outside the Query v1 vocabulary")
    if payload.get("operation") not in _STRUCTURED_OPERATIONS:
        return _refuse(
            MALFORMED_EVIDENCE_CONTRACT,
            "operation is outside the Query v1 vocabulary",
        )
    return _check_maintenance(payload.get("maintenance"))

def _check_endpoint(endpoint: Any, label: str) -> StructuredRefusal | None:
    if not isinstance(endpoint, Mapping) or frozenset(endpoint) != _ENDPOINT_FIELDS:
        return _refuse(MALFORMED_EVIDENCE_CONTRACT, f"{label} has the wrong shape")
    if not _canonical_uuid(endpoint.get("id")):
        return _refuse(MALFORMED_EVIDENCE_CONTRACT, f"{label}.id is not a canonical UUID")
    if not _non_empty_str(endpoint.get("canonical_name")):
        return _refuse(
            MALFORMED_EVIDENCE_CONTRACT,
            f"{label}.canonical_name is not a non-empty string",
        )
    if endpoint.get("type") not in _ENTITY_TYPES:
        return _refuse(MALFORMED_EVIDENCE_CONTRACT, f"{label}.type is unsupported")
    if not _canonical_uuid(endpoint.get("display_support_id")):
        return _refuse(
            MALFORMED_EVIDENCE_CONTRACT,
            f"{label}.display_support_id is not a canonical UUID",
        )
    if not isinstance(endpoint.get("display_disagreement"), bool):
        return _refuse(
            MALFORMED_EVIDENCE_CONTRACT,
            f"{label}.display_disagreement is not a boolean",
        )
    return None

def _check_valid_window(value: Any) -> StructuredRefusal | None:
    if not isinstance(value, Mapping) or frozenset(value) != {"from", "until"}:
        return _refuse(MALFORMED_EVIDENCE_CONTRACT, "valid_window has the wrong shape")
    start, end = value.get("from"), value.get("until")
    for name, endpoint in (("from", start), ("until", end)):
        if endpoint is not None and not _is_whole_second_timestamp(endpoint):
            return _refuse(
                MALFORMED_EVIDENCE_CONTRACT,
                f"valid_window.{name} is not a whole-second Z timestamp or null",
            )
    if start is not None and end is not None and start >= end:
        return _refuse(MALFORMED_EVIDENCE_CONTRACT, "valid_window is empty or reversed")
    return None

def _check_support_span(span: Any, label: str) -> StructuredRefusal | None:
    if not isinstance(span, Mapping) or frozenset(span) != _SUPPORT_SPAN_FIELDS:
        return _refuse(MALFORMED_EVIDENCE_CONTRACT, f"{label}.span has the wrong shape")
    start, end = span.get("start"), span.get("end")
    if not _nonnegative_safe_integer(start) or not _nonnegative_safe_integer(end):
        return _refuse(
            MALFORMED_EVIDENCE_CONTRACT,
            f"{label}.span is not a non-empty half-open code-point interval",
        )
    if cast(int, start) >= cast(int, end):
        return _refuse(
            MALFORMED_EVIDENCE_CONTRACT,
            f"{label}.span is not a non-empty half-open code-point interval",
        )
    return None

def _check_support(support: Any, label: str) -> StructuredRefusal | None:
    if not isinstance(support, Mapping):
        return _refuse(MALFORMED_EVIDENCE_CONTRACT, f"{label} is not an object")
    fields = frozenset(support)
    if fields not in (_SUPPORT_REQUIRED_FIELDS, _SUPPORT_REQUIRED_FIELDS | {"span"}):
        return _refuse(MALFORMED_EVIDENCE_CONTRACT, f"{label} has the wrong field set")
    if not _canonical_uuid(support.get("support_id")):
        return _refuse(MALFORMED_EVIDENCE_CONTRACT, f"{label}.support_id is not a canonical UUID")
    for name in ("source_ref", "source_version", "extraction_ref"):
        if not _non_empty_str(support.get(name)):
            return _refuse(MALFORMED_EVIDENCE_CONTRACT, f"{label}.{name} is not a non-empty string")
    if not isinstance(validate_company_source_ref(support.get("location")), tuple):
        return _refuse(MALFORMED_EVIDENCE_CONTRACT, f"{label}.location is not a valid company locator")
    if "span" in support:
        return _check_support_span(support["span"], label)
    return None

def _check_support_order(provenance: list[Any], row_index: int) -> StructuredRefusal | None:
    previous: str | None = None
    for support_index, support in enumerate(provenance):
        label = f"rows[{row_index}].provenance[{support_index}]"
        refusal = _check_support(support, label)
        if refusal is not None:
            return refusal
        support_id = cast(str, support["support_id"])
        if previous is not None and support_id <= previous:
            return _refuse(
                MALFORMED_EVIDENCE_CONTRACT,
                f"rows[{row_index}].provenance is not ordered by support UUID",
            )
        previous = support_id
    return None

def _check_row_provenance(row: Mapping[str, Any], index: int) -> StructuredRefusal | None:
    provenance = row["provenance"]
    if not isinstance(provenance, list):
        return _refuse(MALFORMED_EVIDENCE_CONTRACT, f"rows[{index}].provenance is not a list")
    if len(provenance) > MAX_INLINE_SUPPORTS:
        return _refuse(
            EVIDENCE_TOO_LARGE,
            f"rows[{index}].provenance exceeds {MAX_INLINE_SUPPORTS} inline supports",
        )
    support_refusal = _check_support_order(provenance, index)
    if support_refusal is not None:
        return support_refusal
    if not isinstance(row["provenance_complete"], bool):
        return _refuse(
            MALFORMED_EVIDENCE_CONTRACT, f"rows[{index}].provenance_complete is not a boolean"
        )
    next_ref = row["provenance_next_ref"]
    if next_ref is not None and not _non_empty_str(next_ref):
        return _refuse(
            MALFORMED_EVIDENCE_CONTRACT,
            f"rows[{index}].provenance_next_ref is not a non-empty string or null",
        )
    if row["provenance_complete"] is False and not _non_empty_str(next_ref):
        return _refuse(
            MALFORMED_EVIDENCE_CONTRACT,
            f"rows[{index}] is incomplete without a provenance_next_ref",
        )
    return None

def _check_entity_row(row: Mapping[str, Any], index: int) -> StructuredRefusal | None:
    endpoint = {name: row[name] for name in _ENDPOINT_FIELDS}
    return _check_endpoint(endpoint, f"rows[{index}]")

def _check_relation_row(row: Mapping[str, Any], index: int) -> StructuredRefusal | None:
    for name in ("head", "tail"):
        refusal = _check_endpoint(row[name], f"rows[{index}].{name}")
        if refusal is not None:
            return refusal
    if not _non_empty_str(row.get("relation_norm")):
        return _refuse(
            MALFORMED_EVIDENCE_CONTRACT,
            f"rows[{index}].relation_norm is not a non-empty string",
        )
    return _check_valid_window(row.get("valid_window"))

def _check_row(
    row: Any,
    index: int,
    subject: str,
    queried_at: str,
) -> StructuredRefusal | None:
    if not isinstance(row, Mapping):
        return _refuse(MALFORMED_EVIDENCE_CONTRACT, f"rows[{index}] is not an object")
    expected_fields = _ENTITY_ROW_FIELDS if subject == "entity" else _RELATION_ROW_FIELDS
    if frozenset(row) != expected_fields:
        return _refuse(
            MALFORMED_EVIDENCE_CONTRACT,
            f"rows[{index}] has the wrong Query v1 field set",
        )
    if not _canonical_uuid(row.get("id")):
        return _refuse(
            MALFORMED_EVIDENCE_CONTRACT, f"rows[{index}].id is not a canonical UUID"
        )
    evidence_refusal = _check_evidence_reference(row, queried_at)
    if evidence_refusal is not None:
        return evidence_refusal
    provenance_refusal = _check_row_provenance(row, index)
    if provenance_refusal is not None:
        return provenance_refusal
    if subject == "entity":
        return _check_entity_row(row, index)
    return _check_relation_row(row, index)

def _check_count_receipt_fields(
    receipt: Mapping[str, Any], expected_count: Any
) -> StructuredRefusal | None:
    if frozenset(receipt) != _COUNT_RECEIPT_REQUIRED_FIELDS:
        return _refuse(
            MALFORMED_EVIDENCE_CONTRACT,
            "count_receipt does not carry the complete Query v1 field set",
        )
    for name in (
        "subject",
        "query_digest",
        "scope_binding",
        "read_revision",
        "maintenance_revision",
        "evidence_ref",
    ):
        if not _non_empty_str(receipt.get(name)):
            return _refuse(
                MALFORMED_EVIDENCE_CONTRACT, f"count_receipt.{name} is not a non-empty string"
            )
    if receipt["subject"] not in _STRUCTURED_SUBJECTS:
        return _refuse(MALFORMED_EVIDENCE_CONTRACT, "count_receipt.subject is unsupported")
    if not _is_whole_second_timestamp(receipt.get("queried_at")):
        return _refuse(
            MALFORMED_EVIDENCE_CONTRACT,
            "count_receipt.queried_at is not a whole-second Z timestamp",
        )
    receipt_count = receipt.get("count")
    if not _nonnegative_safe_integer(receipt_count):
        return _refuse(
            MALFORMED_EVIDENCE_CONTRACT,
            "count_receipt.count is not a non-negative integer",
        )
    if expected_count is not None and receipt_count != expected_count:
        return _refuse(
            MALFORMED_EVIDENCE_CONTRACT,
            "count_receipt.count does not match the envelope count",
        )
    return None

def _check_count_receipt_context(
    receipt: Mapping[str, Any], context: Mapping[str, Any]
) -> StructuredRefusal | None:
    for name in ("subject", "query_digest", "scope_binding", "read_revision", "queried_at"):
        if receipt[name] != context.get(name):
            return _refuse(
                MALFORMED_EVIDENCE_CONTRACT,
                f"count_receipt.{name} does not match its context",
            )
    if receipt.get("coverage") != STRUCTURED_RESULT_COVERAGE:
        return _refuse(MALFORMED_EVIDENCE_CONTRACT, "count_receipt.coverage is unsupported")
    maintenance = context.get("maintenance")
    if not isinstance(maintenance, Mapping):
        return _refuse(MALFORMED_EVIDENCE_CONTRACT, "maintenance is not an object")
    if receipt["maintenance_revision"] != maintenance.get("maintenance_revision"):
        return _refuse(
            MALFORMED_EVIDENCE_CONTRACT,
            "count_receipt.maintenance_revision does not match maintenance",
        )
    if receipt.get("maintenance") != maintenance:
        return _refuse(
            MALFORMED_EVIDENCE_CONTRACT,
            "count_receipt.maintenance does not match its context",
        )
    return _check_evidence_reference(receipt, cast(str, context.get("queried_at")))

def _check_count_receipt(
    receipt: Any,
    context: Mapping[str, Any],
    expected_count: Any = None,
) -> StructuredRefusal | None:
    if not isinstance(receipt, Mapping):
        return _refuse(MALFORMED_EVIDENCE_CONTRACT, "count_receipt is not an object")
    fields_refusal = _check_count_receipt_fields(receipt, expected_count)
    if fields_refusal is not None:
        return fields_refusal
    return _check_count_receipt_context(receipt, context)

def _check_count(payload: Mapping[str, Any]) -> StructuredRefusal | None:
    count = payload.get("count")
    if not _nonnegative_safe_integer(count):
        return _refuse(MALFORMED_EVIDENCE_CONTRACT, "count is not a non-negative integer")
    return _check_count_receipt(payload.get("count_receipt"), payload, count)

def _check_list_result(sibling: Mapping[str, Any]) -> StructuredRefusal | None:
    rows = sibling.get("rows")
    if not isinstance(rows, list):
        return _refuse(MALFORMED_EVIDENCE_CONTRACT, "rows is not a list")
    if len(rows) > MAX_STRUCTURED_ROWS:
        return _refuse(EVIDENCE_TOO_LARGE, f"rows exceeds {MAX_STRUCTURED_ROWS} entries")
    next_cursor = sibling.get("next_cursor")
    if next_cursor is not None and not isinstance(next_cursor, str):
        return _refuse(MALFORMED_EVIDENCE_CONTRACT, "next_cursor is not a string or null")
    for index, row in enumerate(rows):
        refusal = _check_row(row, index, sibling["subject"], sibling["queried_at"])
        if refusal is not None:
            return refusal
        refusal = snapshot_refusal(build_snapshot(row, sibling))
        if refusal is not None:
            return refusal
    return None

def _structured_result_size_refusal(sibling: Mapping[str, Any]) -> StructuredRefusal | None:
    try:
        size = len(canonical_bytes(sibling))
    except StructuredSnapshotError as exc:
        return _refuse(exc.code, exc.reason)
    if size > STRUCTURED_SNAPSHOT_MAX_BYTES:
        return _refuse(
            EVIDENCE_TOO_LARGE,
            f"structured_result is {size} bytes, over the {STRUCTURED_SNAPSHOT_MAX_BYTES} bound",
        )
    return None

def _check_structured_result_payload(
    sibling: Mapping[str, Any], operation: str
) -> StructuredRefusal | None:
    if operation == _COUNT_OPERATION:
        count_refusal = _check_count(sibling)
        if count_refusal is not None:
            return count_refusal
        snapshot_result = snapshot_refusal(build_snapshot(sibling["count_receipt"], sibling))
        if snapshot_result is not None:
            return snapshot_result
    else:
        list_refusal = _check_list_result(sibling)
        if list_refusal is not None:
            return list_refusal
    return _structured_result_size_refusal(sibling)

def validate_structured_result(sibling: Any) -> StructuredRefusal | None:
    """Validate the producer's ``structured_result`` sibling.

    ``None`` means the sibling is admissible. Every other outcome is a
    typed refusal; the sibling is never partially read.
    """
    if not isinstance(sibling, Mapping):
        return _refuse(MALFORMED_EVIDENCE_CONTRACT, "structured_result is not an object")
    schema_refusal = _check_schema(sibling)
    if schema_refusal is not None:
        return schema_refusal
    operation = sibling.get("operation")
    if operation == _LIST_OPERATION:
        expected = _LIST_FIELDS
    elif operation == _COUNT_OPERATION:
        expected = _COUNT_FIELDS
    else:
        return _refuse(
            UNSUPPORTED_EVIDENCE_CONTRACT,
            f"structured_result operation {operation!r} is not supported",
        )
    keys = frozenset(sibling.keys())
    if keys - expected:
        return _refuse(
            MALFORMED_EVIDENCE_CONTRACT,
            f"structured_result carries unknown keys {sorted(keys - expected)}",
        )
    if expected - keys:
        return _refuse(
            MALFORMED_EVIDENCE_CONTRACT,
            f"structured_result is missing {sorted(expected - keys)}",
        )
    context_refusal = _check_context(sibling)
    if context_refusal is not None:
        return context_refusal
    return _check_structured_result_payload(sibling, cast(str, operation))

def build_snapshot(value: Any, sibling: Mapping[str, Any]) -> dict[str, Any]:
    """One frozen snapshot for a validated row or count receipt.

    The whole validated value is carried verbatim — truncating it would
    cite an incomplete value — so the 32 KiB bound is a refusal, not a
    trim. Every context field comes off the envelope, which is what makes
    "Top-level context must match the backend grant" checkable later.
    """
    row = value
    return {
        "schema": STRUCTURED_SNAPSHOT_SCHEMA,
        STRUCTURED_SNAPSHOT_SCHEMA_VERSION_FIELD: STRUCTURED_SNAPSHOT_SCHEMA_VERSION,
        "kind": STRUCTURED_RESULT_KIND,
        "subject": sibling["subject"],
        "operation": sibling["operation"],
        "query_digest": sibling["query_digest"],
        "scope_binding": sibling["scope_binding"],
        "read_revision": sibling["read_revision"],
        "queried_at": sibling["queried_at"],
        "expires_at": row["evidence_expires_at"],
        "evidence_ref": row["evidence_ref"],
        "value": row,
        "coverage": STRUCTURED_RESULT_COVERAGE,
        "maintenance": dict(sibling["maintenance"]),
    }

def structured_snapshots_of(sibling: Any) -> list[dict[str, Any]]:
    """Every citable snapshot in one admitted sibling, rows first.

    A sibling that does not admit yields nothing; the caller keeps the
    refusal. Order is the envelope's row order, which is the producer's
    stable order, so the digest of the list is reproducible.
    """
    if validate_structured_result(sibling) is not None:
        return []
    if not isinstance(sibling, Mapping):
        # An explicit return, not a bare ``assert``: under ``python -O`` an
        # assert vanishes and the caller would proceed on an unvalidated
        # value.
        return []
    if sibling["operation"] == _COUNT_OPERATION:
        return [build_snapshot(sibling["count_receipt"], sibling)]
    return [build_snapshot(row, sibling) for row in sibling["rows"]]

def _snapshot_size_refusal(snapshot: Mapping[str, Any]) -> StructuredRefusal | None:
    try:
        size = structured_snapshot_bytes(snapshot)
    except StructuredSnapshotError as exc:
        return _refuse(exc.code, exc.reason)
    if size > STRUCTURED_SNAPSHOT_MAX_BYTES:
        return _refuse(
            EVIDENCE_TOO_LARGE,
            f"snapshot is {size} bytes, over the {STRUCTURED_SNAPSHOT_MAX_BYTES} bound",
        )
    return None

def _check_snapshot_identity(snapshot: Mapping[str, Any]) -> StructuredRefusal | None:
    if snapshot.get("schema") != STRUCTURED_SNAPSHOT_SCHEMA:
        return _refuse(MALFORMED_EVIDENCE_CONTRACT, "snapshot has an unknown schema token")
    version = snapshot.get(STRUCTURED_SNAPSHOT_SCHEMA_VERSION_FIELD)
    if isinstance(version, bool) or not isinstance(version, int):
        return _refuse(MALFORMED_EVIDENCE_CONTRACT, "snapshot schema_version is not an integer")
    if version != STRUCTURED_SNAPSHOT_SCHEMA_VERSION:
        return _refuse(
            UNSUPPORTED_EVIDENCE_CONTRACT,
            f"snapshot schema_version {version} is not supported",
        )
    if snapshot.get("kind") != STRUCTURED_RESULT_KIND:
        return _refuse(MALFORMED_EVIDENCE_CONTRACT, "snapshot kind is unsupported")
    subject = snapshot.get("subject")
    if not isinstance(subject, str) or subject not in _STRUCTURED_SUBJECTS:
        return _refuse(MALFORMED_EVIDENCE_CONTRACT, "snapshot subject is unsupported")
    operation = snapshot.get("operation")
    if not isinstance(operation, str) or operation not in _STRUCTURED_OPERATIONS:
        return _refuse(MALFORMED_EVIDENCE_CONTRACT, "snapshot operation is unsupported")
    return None


def _check_snapshot_context(snapshot: Mapping[str, Any]) -> StructuredRefusal | None:
    for name in ("query_digest", "scope_binding", "read_revision", "evidence_ref"):
        if not _non_empty_str(snapshot.get(name)):
            return _refuse(
                MALFORMED_EVIDENCE_CONTRACT,
                f"snapshot.{name} is not a non-empty string",
            )
    queried_at = snapshot.get("queried_at")
    if not _is_whole_second_timestamp(queried_at):
        return _refuse(
            MALFORMED_EVIDENCE_CONTRACT,
            "snapshot.queried_at is not a whole-second Z timestamp",
        )
    queried_at = cast(str, queried_at)
    if snapshot.get("coverage") != STRUCTURED_RESULT_COVERAGE:
        return _refuse(MALFORMED_EVIDENCE_CONTRACT, "snapshot coverage is unsupported")
    maintenance_refusal = _check_maintenance(snapshot.get("maintenance"))
    if maintenance_refusal is not None:
        return maintenance_refusal
    expected_expiry = _expected_evidence_expiry(queried_at)
    if snapshot.get("expires_at") != expected_expiry:
        return _refuse(
            MALFORMED_EVIDENCE_CONTRACT,
            "snapshot.expires_at is not 365 days after queried_at",
        )
    return None


def _check_snapshot_fields(snapshot: Mapping[str, Any]) -> StructuredRefusal | None:
    if frozenset(snapshot) != STRUCTURED_SNAPSHOT_FIELDS:
        return _refuse(MALFORMED_EVIDENCE_CONTRACT, "snapshot has the wrong field set")
    identity_refusal = _check_snapshot_identity(snapshot)
    if identity_refusal is not None:
        return identity_refusal
    return _check_snapshot_context(snapshot)

def _check_snapshot_value(
    snapshot: Mapping[str, Any], subject: str, operation: str, queried_at: str
) -> StructuredRefusal | None:
    value = snapshot.get("value")
    if not isinstance(value, Mapping):
        return _refuse(MALFORMED_EVIDENCE_CONTRACT, "snapshot value is not an object")
    if operation == _LIST_OPERATION:
        row_refusal = _check_row(value, 0, subject, queried_at)
        if row_refusal is not None:
            return row_refusal
    else:
        receipt_refusal = _check_count_receipt(value, snapshot)
        if receipt_refusal is not None:
            return receipt_refusal
    if value.get("evidence_ref") != snapshot["evidence_ref"]:
        return _refuse(
            MALFORMED_EVIDENCE_CONTRACT,
            "snapshot evidence_ref does not match its value",
        )
    if value.get("evidence_expires_at") != snapshot["expires_at"]:
        return _refuse(
            MALFORMED_EVIDENCE_CONTRACT,
            "snapshot expires_at does not match its value",
        )
    return None

def _snapshot_contract_refusal(snapshot: Any) -> StructuredRefusal | None:
    """Revalidate an untrusted frozen value against the complete v1 contract."""
    if not isinstance(snapshot, Mapping):
        return _refuse(MALFORMED_EVIDENCE_CONTRACT, "snapshot is not an object")
    size_refusal = _snapshot_size_refusal(snapshot)
    if size_refusal is not None:
        return size_refusal
    fields_refusal = _check_snapshot_fields(snapshot)
    if fields_refusal is not None:
        return fields_refusal
    return _check_snapshot_value(
        snapshot,
        cast(str, snapshot["subject"]),
        cast(str, snapshot["operation"]),
        cast(str, snapshot["queried_at"]),
    )

def snapshot_refusal(snapshot: Mapping[str, Any]) -> StructuredRefusal | None:
    """Check both the v1 snapshot contract and its serialized UTF-8 bound."""
    return _snapshot_contract_refusal(snapshot)

def snapshot_digests(snapshots: Sequence[Mapping[str, Any]]) -> list[str]:
    """One recomputable digest per snapshot, in the same order."""
    return [structured_snapshot_digest(snapshot) for snapshot in snapshots]
