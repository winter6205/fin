"""Structural validator for the E1 enterprise evidence contract (issue #334, E3).

Single home for the shape validation frozen by
``specs/enterprise-evidence-contract.md``: the retrieve-ingress
``database_rows`` envelope, the citation channel (document kinds and the
``database-row`` egress snapshot), visual claims, business-field
observations, and comparison frames — their required/optional keys, types,
and spec §3 bounds. Every refusal is a typed result carrying the exact §6
code as a structured field; the validator never raises on bad input, never
truncates, and never defaults.

Shape only. Semantic verification (citation span repair, crop coverage,
XBRL numeric gate, ``record_key`` claim checks, producer authority) stays
with the existing lanes (ADR 0091). Context-dependent admission facts
(trusted thread, resolvable locators, authorized views, received texts)
arrive as pure data in :class:`AdmissionContext`; the validator maps a
failing lookup to its named code, it does not compute the fact.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Final, Mapping, Sequence

from fin.host.citation_gate import (
    COMPANY_KIND,
    DATABASE_ROW_DISPLAY,
    DATABASE_ROW_KIND,
    QUOTABLE_EVIDENCE_KINDS,
)
from fin.host.tools.compare_facts import COMPARISON_SCHEMA, MAX_CELLS
from fin.host.visual_evidence import (
    VISUAL_CLAIM_FIELDS,
    VISUAL_EVIDENCE_KIND,
    VISUAL_STALE,
)
from fin.retrieve.port import (
    DATABASE_ROWS_KEY,
    DATABASE_ROWS_RESULT_COUNT_FIELD,
    DATABASE_ROWS_ROWS_FIELD,
    DATABASE_ROWS_SCHEMA,
    DATABASE_ROWS_SCHEMA_FIELD,
)
from fin.retrieve.source_read import ATTACHMENT_KIND
from fin.retrieve.tiers import (
    REAL_DB_NOT_WIRED,
    SQL_NO_MATCHING_ROWS,
    TIER_NOT_OPEN,
)

# ----- spec §6 refusal codes and delegated lane exits -----

MALFORMED_EVIDENCE_CONTRACT: Final[str] = "malformed_evidence_contract"
UNSUPPORTED_EVIDENCE_CONTRACT: Final[str] = "unsupported_evidence_contract"
CITATION_OVERFLOW: Final[str] = "citation_overflow"
VISUAL_CLAIM_OVERFLOW: Final[str] = "visual_claim_overflow"
DATABASE_ROW_OVERFLOW: Final[str] = "database_row_overflow"
BUSINESS_FIELD_OVERFLOW: Final[str] = "business_field_overflow"
COMPARISON_OVERFLOW: Final[str] = "comparison_overflow"
INSUFFICIENT_EVIDENCE: Final[str] = "insufficient_evidence"
SQL_QUERY_DENIED: Final[str] = "sql_query_denied"
CITATION_TRUST_DENIED: Final[str] = "citation_trust_denied"
VISUAL_CLAIM_TRUST_DENIED: Final[str] = "visual_claim_trust_denied"
BUSINESS_FIELD_TRUST_DENIED: Final[str] = "business_field_trust_denied"
COMPARISON_SOURCE_UNVERIFIED: Final[str] = "comparison_source_unverified"
RETRIEVE_UNAVAILABLE: Final[str] = "retrieve_unavailable"

# Delegated verifier exits (spec §6 note): named by the citation gate,
# surfaced here because ingress routes them, not because this module
# re-implements the verifier.
UNKNOWN_LOCATOR: Final[str] = "unknown_locator"
CITATION_REPAIR_CAP: Final[str] = "citation_repair_cap"

CONTRACT_REFUSAL_CODES: Final[frozenset[str]] = frozenset(
    {
        MALFORMED_EVIDENCE_CONTRACT,
        UNSUPPORTED_EVIDENCE_CONTRACT,
        CITATION_OVERFLOW,
        VISUAL_CLAIM_OVERFLOW,
        DATABASE_ROW_OVERFLOW,
        BUSINESS_FIELD_OVERFLOW,
        COMPARISON_OVERFLOW,
        INSUFFICIENT_EVIDENCE,
        SQL_QUERY_DENIED,
        SQL_NO_MATCHING_ROWS,
        CITATION_TRUST_DENIED,
        VISUAL_CLAIM_TRUST_DENIED,
        BUSINESS_FIELD_TRUST_DENIED,
        COMPARISON_SOURCE_UNVERIFIED,
        VISUAL_STALE,
        REAL_DB_NOT_WIRED,
        TIER_NOT_OPEN,
        RETRIEVE_UNAVAILABLE,
        UNKNOWN_LOCATOR,
        CITATION_REPAIR_CAP,
    }
)

# ----- schema tokens (spec §2.1) -----
#
# The token is the STABLE NAME of the shape and carries no version
# suffix; the version is the explicit integer sibling field
# ``schema_version`` below. Naming a shape here and a stage there is
# what the suffix did wrong, and ADR 0093 retired it: a rollout marker
# encoded in a token reads as "the version that added field X", which
# stops being true the moment the next shape lands.

CITATION_CHANNEL_SCHEMA: Final[str] = "fin.evidence.citation"
DATABASE_ROW_SNAPSHOT_SCHEMA: Final[str] = "fin.evidence.database_row_snapshot"
VISUAL_CLAIM_SCHEMA: Final[str] = "fin.evidence.visual_claim"
BUSINESS_FIELD_SCHEMA: Final[str] = "fin.evidence.business_field"
BUSINESS_FIELD_KIND: Final[str] = "business_field"

# Spec §2.1 rule 1: every object that carries ``schema`` also carries
# this integer sibling, and a recognized token carrying any other
# integer is ``unsupported_evidence_contract`` (§6) — never the
# nearest older parser, never a silent suffix strip.
EVIDENCE_SCHEMA_VERSION: Final[int] = 1
EVIDENCE_SCHEMA_VERSION_FIELD: Final[str] = "schema_version"

# ----- spec §3 bounds -----

MAX_CITATIONS_PER_RUN: Final[int] = 32
QUOTE_MIN_CODEPOINTS: Final[int] = 32
QUOTE_MAX_CODEPOINTS: Final[int] = 2000
SNAPSHOT_MAX_BYTES: Final[int] = 32 * 1024
MAX_ROWS_PER_RESULT: Final[int] = 20
RECORD_KEY_MAX_PAIRS: Final[int] = 8
KEY_MAX_CHARS: Final[int] = 64
RECORD_KEY_VALUE_MAX_CHARS: Final[int] = 128
FIELDS_MAX_ENTRIES: Final[int] = 16
FIELD_VALUE_MAX_CHARS: Final[int] = 512
SUMMARY_MAX_CODEPOINTS: Final[int] = 1000
SOURCE_MAX_CHARS: Final[int] = 128
MAX_VISUAL_CLAIMS_PER_RUN: Final[int] = 32
VISUAL_DETAIL_MAX_CODEPOINTS: Final[int] = 4000
MAX_BUSINESS_FIELDS_PER_RUN: Final[int] = 32
MAX_BUSINESS_FIELDS_PER_SUBJECT: Final[int] = 24
BUSINESS_TEXT_MAX_CHARS: Final[int] = 512

# Spec §2.5: whole seconds, ``Z`` only. WHY fin does not normalize a
# fractional / offset / space-separated value into this form even though
# the fix would be one regex: the evidence contract forbids silent
# coercion (§2.4), and truncating here would make a producer that emits
# ``2026-09-30T12:22:14.741Z`` look identical to one that emitted the
# whole second — the same instant would then enter the snapshot digest
# under two spellings, and a stored ``queried_at`` would no longer be
# the text that was actually observed. Truncation to whole seconds is
# the PRODUCER's obligation, upstream of the wire; fin names the
# offending row ``malformed_evidence_contract`` and stops.
_TIMESTAMP_RE: Final[re.Pattern[str]] = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})Z$"
)
_SHA256_PREFIXED_RE: Final[re.Pattern[str]] = re.compile(r"^sha256:[0-9a-f]{64}$")
_BARE_SHA256_HEX_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")

_CITATION_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "kind",
        "document_id",
        "attachment_id",
        "version",
        "unit",
        "display",
        "filename",
        "quote",
    }
)
_SNAPSHOT_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "schema",
        EVIDENCE_SCHEMA_VERSION_FIELD,
        "source",
        "record_key",
        "queried_at",
        "fields",
        "summary",
    }
)
# Spec §4.4 ingress: the ENVELOPE names the format of the list it
# carries, so a row inside ``rows[]`` does NOT re-declare a per-row
# ``schema`` (nor its version). The row's keys are exactly the five
# value keys; one carrying ``schema`` is an unknown key and is
# ``malformed_evidence_contract`` under §2.4 like any other.
_INGRESS_ROW_FIELDS: Final[frozenset[str]] = frozenset(
    {"source", "record_key", "queried_at", "fields", "summary"}
)
_VISUAL_CLAIM_ELEMENT_FIELDS: Final[frozenset[str]] = frozenset(VISUAL_CLAIM_FIELDS) | {
    "schema",
    EVIDENCE_SCHEMA_VERSION_FIELD,
}
_BUSINESS_FIELD_ELEMENT_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "schema",
        EVIDENCE_SCHEMA_VERSION_FIELD,
        "kind",
        "subject_ref",
        "source_locator",
        "original_value",
        "normalized_text",
        "status",
    }
)
_BUSINESS_FIELD_STATUSES: Final[frozenset[str]] = frozenset(
    {"extracted", "missing", "ambiguous", "conflicting", "unverified"}
)
# The egress sibling key name is frozen by spec §4.4; the ingress key is the
# plural ``database_rows`` from fin.retrieve.port.
CITATION_SNAPSHOT_KEY: Final[str] = "database_row"
_COMPARISON_ENVELOPE_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "schema",
        EVIDENCE_SCHEMA_VERSION_FIELD,
        "kind",
        "axis",
        "concept",
        "unit",
        "scale",
        "columns",
        "rows",
        "deltas",
        "missing",
        "hit_count",
        "cell_count",
        "locators",
    }
)
_COMPARISON_CELL_FIELDS: Final[frozenset[str]] = frozenset(
    {"row", "column", "status", "value", "display", "unit", "scale", "locator"}
)
_DOCUMENT_LOCATOR_FIELDS: Final[frozenset[str]] = frozenset(
    {"document_id", "version", "unit"}
)
_PRODUCER_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {REAL_DB_NOT_WIRED, TIER_NOT_OPEN, RETRIEVE_UNAVAILABLE}
)

LANE_CITATION: Final[str] = "citation"
LANE_VISUAL_CLAIM: Final[str] = "visual_claim"
LANE_DATABASE_ROW: Final[str] = "database_row"
LANE_BUSINESS_FIELD: Final[str] = "business_field"
LANE_COMPARISON: Final[str] = "comparison"
LANE_CONTRACT: Final[str] = "evidence_contract"


@dataclass(frozen=True, slots=True)
class EvidenceRefusal:
    """One named contract failure: exact §6 code plus a short traceable detail."""

    code: str
    lane: str
    message: str
    item_index: int | None = None


@dataclass(frozen=True, slots=True)
class ContractValidation:
    """The typed outcome of one structural validation: accepted, or refused."""

    refusal: EvidenceRefusal | None = None

    @property
    def accepted(self) -> bool:
        return self.refusal is None


_ACCEPTED: Final[ContractValidation] = ContractValidation()


@dataclass(frozen=True)
class AdmissionContext:
    """The context half the harness supplies for context-dependent classes.

    Pure data about the run — no I/O. An absent lookup means "no fact
    available", which keeps the corresponding check from asserting a
    refusal it cannot ground.
    """

    trusted_attachment_ids: frozenset[str] = frozenset()
    resolvable_locators: frozenset[tuple[str, str]] = frozenset()
    received_texts: Mapping[tuple[str, str, int], str] = field(default_factory=dict)
    current_observation_versions: Mapping[tuple[str, str, int], int] = field(
        default_factory=dict
    )
    authorized_database_views: frozenset[str] = frozenset()
    database_source_names: frozenset[str] = frozenset()
    readable_subject_refs: frozenset[str] = frozenset()
    received_source_versions: frozenset[tuple[str, str]] = frozenset()


# ----- small type predicates -----


def _is_str(value: Any) -> bool:
    return isinstance(value, str)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_integer_value(value: Any) -> bool:
    """Whether ``value`` is a finite integer BY NUMERIC VALUE (operator ruling).

    This is a SEPARATE predicate from :func:`_is_int` on purpose. ``_is_int``
    is the general integer the contract validates (record-key values, ``unit``,
    counts, ``outbound_calls``); loosening it to accept floats would widen
    every one of those. The version check is the single place where the rule is
    over the numeric value rather than the Python runtime type, so it gets its
    own predicate.

    WHY the version rule is over the value and not the type: ``json.loads``
    decodes the JSON text ``1.0`` to the Python ``float`` ``1.0``, so a
    producer that wrote ``1.0`` is typed differently here than the same text is
    in a JavaScript decoder, where ``JSON.parse('1.0')`` yields the integer
    ``1``. Refusing that payload on the Python side purely because of the
    runtime type would give one JSON request two different verdicts at two
    boundaries. The rule is therefore over the value — ``1`` and ``1.0`` are the
    same version — which is what makes both boundaries agree.

    ``bool`` is excluded EXPLICITLY: ``bool`` is a subclass of ``int`` in
    Python, so ``True`` would otherwise pass as version ``1`` and a boolean
    would be accepted as a version declaration.
    """
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    if isinstance(value, float):
        # A non-finite float (nan, inf, -inf) has no integer value.
        return value.is_integer()
    return False


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _strict_timestamp(value: Any) -> bool:
    """Spec §2.5: fixed ``Z`` form, real calendar values, no leap second."""
    if not _is_str(value):
        return False
    match = _TIMESTAMP_RE.match(value)
    if match is None:
        return False
    year, month, day, hour, minute, second = (int(g) for g in match.groups())
    if hour > 23 or minute > 59 or second > 59:
        return False
    try:
        date(year, month, day)
    except ValueError:
        return False
    return True


def _refusal(lane: str, code: str, message: str, index: int | None = None) -> ContractValidation:
    if code not in CONTRACT_REFUSAL_CODES:
        # Programming invariant, not a data path: an explicit raise
        # keeps it enforced under ``python -O`` where ``assert`` vanishes.
        raise AssertionError(f"refusal code outside the §6 set: {code}")
    return ContractValidation(EvidenceRefusal(code, lane, message, index))


def _check_keys(
    lane: str,
    obj: Any,
    required: frozenset[str],
    *,
    optional: frozenset[str] = frozenset(),
    index: int | None = None,
    what: str = "object",
) -> ContractValidation | None:
    if not isinstance(obj, Mapping):
        return _refusal(lane, MALFORMED_EVIDENCE_CONTRACT, f"{what} is not an object", index)
    keys = frozenset(obj.keys())
    missing = required - keys
    extra = keys - required - optional
    if missing or extra:
        parts = []
        if missing:
            parts.append(f"missing {sorted(missing)}")
        if extra:
            parts.append(f"unknown {sorted(extra)}")
        return _refusal(
            lane, MALFORMED_EVIDENCE_CONTRACT, f"{what}: " + "; ".join(parts), index
        )
    return None


def _check_schema_token(
    lane: str,
    value: Any,
    expected: str,
    index: int | None = None,
    *,
    name: str = "schema",
) -> ContractValidation | None:
    """One home for the frozen-token read: is string → equals the frozen
    value → otherwise the named ``unsupported`` refusal (§2.3: never the
    nearest older parser). All six lanes share this check; a divergent
    copy is how a lane starts accepting a token the others refuse."""
    if not _is_str(value):
        return _refusal(lane, MALFORMED_EVIDENCE_CONTRACT, f"{name} is not a string", index)
    if value != expected:
        return _refusal(lane, UNSUPPORTED_EVIDENCE_CONTRACT, f"unknown {name} {value!r}", index)
    return None


def _check_schema_version(
    lane: str,
    value: Any,
    index: int | None = None,
    *,
    name: str = EVIDENCE_SCHEMA_VERSION_FIELD,
) -> ContractValidation | None:
    """The version sibling of a recognized token (spec §2.1 rule 3).

    WHY the split from the token check: a token names a SHAPE and a
    version numbers it, so the two fail under different §6 classes. A
    ``schema_version`` that is not a finite integer by numeric value is
    a malformed payload; a finite integer that is not the current
    version is an unsupported CONTRACT — never the nearest older parser
    and never a silent default to 1, which is the whole refusal this
    pair exists to name.

    WHY :func:`_is_integer_value` and not :func:`_is_int` here: the
    operator's ruling is that BOTH ends validate the version as a
    finite integer VALUE. ``json.loads`` decodes the JSON text ``1.0``
    to the Python float ``1.0``, while a JavaScript decoder turns the
    same text into the integer ``1``; testing the Python runtime type
    would therefore hand one JSON request two different verdicts. The
    value rule makes ``1`` and ``1.0`` the same version ``1`` here, and
    so the same verdict as on the JS side. ``1.5``, ``nan``, ``inf``,
    ``-inf``, a string, a bool, ``None``, a list and an object have no
    integer value and stay malformed. ``_is_int`` itself is untouched:
    it remains the strict type test every OTHER integer in the contract
    (record keys, ``unit``, counts, ``outbound_calls``) depends on.
    """
    if not _is_integer_value(value):
        return _refusal(
            lane,
            MALFORMED_EVIDENCE_CONTRACT,
            f"{name} is not a finite integer",
            index,
        )
    if value != EVIDENCE_SCHEMA_VERSION:
        return _refusal(
            lane, UNSUPPORTED_EVIDENCE_CONTRACT, f"unsupported {name} {value!r}", index
        )
    return None


def seed_context_from_rows(
    citation_rows: Sequence[Any] = (),
    visual_claim_rows: Sequence[Any] = (),
    database_row_values: Sequence[Any] = (),
) -> AdmissionContext:
    """The single home of the self-seed posture every replay boundary uses.

    Thread trust, resolvable locators, and view authorization are
    admission-time facts (E3): by the time the thread store, the SSE/
    history wire, or the retrieve ingress re-reads a row, those lanes
    have already run and the boundary holds no independent view of the
    thread. Seeding the context from the rows' OWN identities keeps
    those context checks inert at the boundary while every shape, type,
    and bound judgment still runs — and an actual violation is still
    named by its §6 code instead of a fabricated denial or (worse) a
    silent pass.

    WHY the str filter is the rule, not a courtesy: every seeded value
    lands in a frozenset membership test, which hashes it — a producer
    dict or list would raise ``TypeError`` right through this module's
    never-raise contract and crash the run at the boundary. A value that
    is not a string simply stays absent; the shape checks name its
    refusal from the payload itself.
    """
    trusted: set[str] = set()
    resolvable: set[tuple[str, str]] = set()
    for row in citation_rows:
        if not isinstance(row, Mapping):
            continue
        version = row.get("version")
        if not isinstance(version, str) or not version:
            continue
        kind = row.get("kind")
        document_id = row.get("document_id")
        attachment_id = row.get("attachment_id")
        if kind == COMPANY_KIND and isinstance(document_id, str) and document_id:
            resolvable.add((document_id, version))
        elif kind == ATTACHMENT_KIND and isinstance(attachment_id, str) and attachment_id:
            trusted.add(attachment_id)
            resolvable.add((attachment_id, version))
    for row in visual_claim_rows:
        if (
            isinstance(row, Mapping)
            and isinstance(row.get("attachment_id"), str)
            and row["attachment_id"]
        ):
            trusted.add(row["attachment_id"])
    views: set[str] = set()
    for row in database_row_values:
        if isinstance(row, Mapping) and isinstance(row.get("source"), str):
            views.add(row["source"])
    return AdmissionContext(
        trusted_attachment_ids=frozenset(trusted),
        resolvable_locators=frozenset(resolvable),
        authorized_database_views=frozenset(views),
    )


# ----- snapshot (spec §4.4) -----


def render_value_text(value: Any) -> str:
    """Spec §4.4 text rule: scalars render to canonical text before compare
    or digest; booleans are lowercase JSON and ``null`` renders empty."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    raise TypeError(f"non-scalar value cannot render to contract text: {type(value).__name__}")


def _identity_input(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """The ``m`` object the digest is computed over (spec §4.4, ADR 0092)."""
    return {
        "source": snapshot["source"],
        "record_key": {k: render_value_text(v) for k, v in snapshot["record_key"].items()},
        "queried_at": snapshot["queried_at"],
        "fields": {k: render_value_text(v) for k, v in snapshot["fields"].items()},
        "summary": snapshot["summary"],
    }


def snapshot_canonical_json(snapshot: Mapping[str, Any]) -> str:
    return json.dumps(
        _identity_input(snapshot),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def compute_snapshot_digest(snapshot: Mapping[str, Any]) -> str:
    payload = snapshot_canonical_json(snapshot).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _check_scalar_mapping(
    mapping: Any,
    *,
    lane: str,
    name: str,
    max_entries: int,
    value_max_chars: int,
    index: int | None,
    structural_only: bool,
) -> ContractValidation | None:
    if not isinstance(mapping, Mapping):
        return _refusal(lane, MALFORMED_EVIDENCE_CONTRACT, f"{name} is not an object", index)
    if not structural_only and len(mapping) > max_entries:
        return _refusal(
            lane, DATABASE_ROW_OVERFLOW, f"{name} exceeds {max_entries} entries", index
        )
    for key, value in mapping.items():
        if not _is_str(key):
            return _refusal(lane, MALFORMED_EVIDENCE_CONTRACT, f"{name} key is not a string", index)
        if not _is_scalar(value):
            return _refusal(lane, MALFORMED_EVIDENCE_CONTRACT, f"{name}.{key} is not a JSON scalar", index)
        if structural_only:
            continue
        if len(key) > KEY_MAX_CHARS:
            return _refusal(lane, DATABASE_ROW_OVERFLOW, f"{name} key exceeds {KEY_MAX_CHARS} chars", index)
        if len(render_value_text(value)) > value_max_chars:
            return _refusal(
                lane,
                DATABASE_ROW_OVERFLOW,
                f"{name}.{key} text exceeds {value_max_chars} chars",
                index,
            )
    return None


def _validate_row_values(
    row: Mapping[str, Any], *, index: int | None = None
) -> ContractValidation | None:
    """Validate the value and bounds shared by ingress rows and snapshots.

    Callers establish their distinct exact-key and schema contracts first.
    This common projection preserves the same refusal order at both wire
    boundaries, including the canonical whole-snapshot byte check.
    """
    lane = LANE_DATABASE_ROW
    if not _is_str(row["source"]) or not _is_str(row["summary"]):
        return _refusal(lane, MALFORMED_EVIDENCE_CONTRACT, "source/summary must be strings", index)
    if not _strict_timestamp(row["queried_at"]):
        return _refusal(lane, MALFORMED_EVIDENCE_CONTRACT, "queried_at is not a strict timestamp", index)

    for mapping in (
        ("record_key", RECORD_KEY_MAX_PAIRS, RECORD_KEY_VALUE_MAX_CHARS),
        ("fields", FIELDS_MAX_ENTRIES, FIELD_VALUE_MAX_CHARS),
    ):
        name, max_entries, value_max_chars = mapping
        structural = _check_scalar_mapping(
            row[name], lane=lane, name=name, max_entries=max_entries,
            value_max_chars=value_max_chars, index=index, structural_only=True,
        )
        if structural is not None:
            return structural

    # Spec §3 precedence: evaluate the canonical whole-row byte bound before
    # the per-entry bounds. The projection is also the digest input.
    if len(snapshot_canonical_json(row).encode("utf-8")) > SNAPSHOT_MAX_BYTES:
        return _refusal(lane, DATABASE_ROW_OVERFLOW, "serialized snapshot exceeds 32 KiB", index)

    for mapping in (
        ("record_key", RECORD_KEY_MAX_PAIRS, RECORD_KEY_VALUE_MAX_CHARS),
        ("fields", FIELDS_MAX_ENTRIES, FIELD_VALUE_MAX_CHARS),
    ):
        name, max_entries, value_max_chars = mapping
        bounded = _check_scalar_mapping(
            row[name], lane=lane, name=name, max_entries=max_entries,
            value_max_chars=value_max_chars, index=index, structural_only=False,
        )
        if bounded is not None:
            return bounded

    if len(row["source"]) > SOURCE_MAX_CHARS:
        return _refusal(lane, DATABASE_ROW_OVERFLOW, "source exceeds 128 chars", index)
    if len(row["summary"]) > SUMMARY_MAX_CODEPOINTS:
        return _refusal(lane, DATABASE_ROW_OVERFLOW, "summary exceeds 1,000 code points", index)
    return None


def _validate_snapshot(
    snapshot: Any, *, index: int | None = None
) -> ContractValidation | None:
    """The EGRESS snapshot (spec §4.4): a ``database-row`` citation's
    retained snapshot, which travels ALONE rather than inside a list and
    therefore names its own shape. Its per-object ``schema`` plus the
    ``schema_version`` sibling is the E5 cutover shape; the ingress row
    that carries the same five value keys is judged by
    :func:`_validate_ingress_row`, not by this one."""
    lane = LANE_DATABASE_ROW
    problem = _check_keys(lane, snapshot, _SNAPSHOT_FIELDS, index=index, what="database_row snapshot")
    if problem is not None:
        return problem
    schema_problem = _check_schema_token(
        lane, snapshot[DATABASE_ROWS_SCHEMA_FIELD], DATABASE_ROW_SNAPSHOT_SCHEMA,
        index=index, name="snapshot schema",
    )
    if schema_problem is not None:
        return schema_problem
    version_problem = _check_schema_version(
        lane, snapshot[EVIDENCE_SCHEMA_VERSION_FIELD], index=index,
    )
    if version_problem is not None:
        return version_problem
    return _validate_row_values(snapshot, index=index)


def _validate_ingress_row(
    row: Any, *, index: int | None = None
) -> ContractValidation | None:
    """One row inside the retrieve-ingress ``database_rows.rows`` list
    (spec §4.4 ingress).

    WHY this is NOT :func:`_validate_snapshot`: the two objects carry
    the same five value keys but they are not the same object. The
    snapshot is the EGRESS object, which travels alone and therefore
    names its own ``schema`` plus version; the ingress row is an
    element of a list whose ENVELOPE already names the format, so a
    per-row ``schema`` would restate what the envelope fixed and would
    let one list's rows disagree about their own format. The row is
    held to exactly the five keys — a row carrying ``schema`` is an
    unknown key and is refused as malformed (§2.4), never tolerated
    as an optional extra.

    Its exact key set remains distinct from the standalone snapshot, while
    both boundaries use the same row-value validation after their shape
    checks pass.
    """
    lane = LANE_DATABASE_ROW
    problem = _check_keys(
        lane, row, _INGRESS_ROW_FIELDS, index=index, what="database_row ingress row"
    )
    if problem is not None:
        return problem
    return _validate_row_values(row, index=index)


def validate_snapshot_identity(
    carrier: Any, snapshot: Any
) -> ContractValidation:
    """Check a ``(source, record_key, snapshot_digest)`` carrier against the
    stored snapshot it claims to identify (spec §4.4, ADR 0092)."""
    lane = LANE_DATABASE_ROW
    problem = _check_keys(
        lane, carrier,
        frozenset({"source", "record_key", "snapshot_digest"}),
        what="snapshot identity carrier",
    )
    if problem is not None:
        return problem
    if not _is_str(carrier["snapshot_digest"]) or not _SHA256_PREFIXED_RE.match(
        carrier["snapshot_digest"]
    ):
        return _refusal(lane, MALFORMED_EVIDENCE_CONTRACT, "snapshot_digest is not sha256:<64 hex>")
    validated = _validate_snapshot(snapshot)
    if validated is not None:
        return validated
    if carrier["source"] != snapshot["source"] or carrier["record_key"] != snapshot["record_key"]:
        return _refusal(lane, MALFORMED_EVIDENCE_CONTRACT, "carrier identity does not match the snapshot")
    if compute_snapshot_digest(snapshot) != carrier["snapshot_digest"]:
        return _refusal(lane, MALFORMED_EVIDENCE_CONTRACT, "snapshot_digest mismatch on recomputation")
    return _ACCEPTED


# ----- citation channel (spec §2.2, §4.1, §4.4 egress) -----


def _validate_citation_channel(payload: Mapping[str, Any], ctx: AdmissionContext) -> ContractValidation:
    """The channel carrier on a response / terminal SSE frame (spec §2.2
    rule 2): the ``citations[]`` elements themselves carry no ``schema``
    — their eight slots plus the optional ``database_row`` are frozen
    and versioned by additive siblings — so the frame declares the
    channel ONCE, here, and the declared version is the version of
    every row in it."""
    problem = _check_keys(
        LANE_CITATION, payload,
        frozenset({"evidence_schema", EVIDENCE_SCHEMA_VERSION_FIELD, "citations"}),
        what="citation channel",
    )
    if problem is not None:
        return problem
    schema_problem = _check_schema_token(
        LANE_CITATION, payload["evidence_schema"], CITATION_CHANNEL_SCHEMA,
        name="evidence_schema",
    )
    if schema_problem is not None:
        return schema_problem
    version_problem = _check_schema_version(
        LANE_CITATION, payload[EVIDENCE_SCHEMA_VERSION_FIELD],
    )
    if version_problem is not None:
        return version_problem
    citations = payload["citations"]
    if not isinstance(citations, list):
        return _refusal(LANE_CITATION, MALFORMED_EVIDENCE_CONTRACT, "citations is not a list")
    if len(citations) > MAX_CITATIONS_PER_RUN:
        return _refusal(LANE_CITATION, CITATION_OVERFLOW, f"more than {MAX_CITATIONS_PER_RUN} citations")
    for index, item in enumerate(citations):
        refused = _validate_citation_item(item, index, ctx)
        if refused is not None:
            return refused
    return _ACCEPTED


def _validate_citation_item(
    item: Any, index: int, ctx: AdmissionContext
) -> ContractValidation | None:
    """Key set and shared types first, then exactly one lane: the ledger
    row (snapshot mirror rules) or a document citation (locator, trust,
    and quote rules). WHY split: the two lanes share only the eight
    slot names, and one ~70-branch function made a lane's rule read as
    if it applied to the other."""
    problem = _check_keys(
        LANE_CITATION, item, _CITATION_FIELDS,
        optional=frozenset({CITATION_SNAPSHOT_KEY}), index=index, what="citation",
    )
    if problem is not None:
        return problem
    kind = item["kind"]
    if not _is_str(kind):
        return _refusal(LANE_CITATION, MALFORMED_EVIDENCE_CONTRACT, "kind is not a string", index)
    if kind not in QUOTABLE_EVIDENCE_KINDS:
        return _refusal(LANE_CITATION, UNSUPPORTED_EVIDENCE_CONTRACT, f"kind {kind!r} is not quotable here", index)
    row_kind = kind == DATABASE_ROW_KIND
    lane = LANE_DATABASE_ROW if row_kind else LANE_CITATION
    if CITATION_SNAPSHOT_KEY in item and not row_kind:
        return _refusal(
            LANE_DATABASE_ROW, MALFORMED_EVIDENCE_CONTRACT,
            "a document-kind citation cannot carry database_row", index,
        )
    if row_kind and CITATION_SNAPSHOT_KEY not in item:
        return _refusal(lane, MALFORMED_EVIDENCE_CONTRACT, "database-row citation lacks its snapshot", index)
    for name in ("document_id", "attachment_id", "version", "display", "filename", "quote"):
        if not _is_str(item[name]):
            return _refusal(lane, MALFORMED_EVIDENCE_CONTRACT, f"{name} is not a string", index)
    if not _is_int(item["unit"]) or item["unit"] < 1:
        return _refusal(lane, MALFORMED_EVIDENCE_CONTRACT, "unit must be an integer >= 1", index)
    if not item["version"]:
        return _refusal(lane, MALFORMED_EVIDENCE_CONTRACT, "version must not be empty", index)
    if row_kind:
        return _validate_database_row_citation(item, index)
    return _validate_document_citation(item, index, ctx)


def _validate_database_row_citation(
    item: Mapping[str, Any], index: int
) -> ContractValidation | None:
    """The egress snapshot pair (spec §4.4): frozen placeholders, a
    shape-legal snapshot, and the flat-slot mirror of its identity."""
    lane = LANE_DATABASE_ROW
    if item["display"] != DATABASE_ROW_DISPLAY or item["unit"] != 1:
        return _refusal(lane, MALFORMED_EVIDENCE_CONTRACT, "database-row placeholders (display=record, unit=1) broken", index)
    if item["document_id"] or item["attachment_id"]:
        return _refusal(lane, MALFORMED_EVIDENCE_CONTRACT, "database-row placeholders: document_id/attachment_id must be empty", index)
    snapshot = item[CITATION_SNAPSHOT_KEY]
    snapshot_problem = _validate_snapshot(snapshot, index=index)
    if snapshot_problem is not None:
        return snapshot_problem
    if item["version"] != snapshot["queried_at"]:
        return _refusal(lane, MALFORMED_EVIDENCE_CONTRACT, "mirror mismatch: version != queried_at", index)
    if item["quote"] != snapshot["summary"]:
        return _refusal(lane, MALFORMED_EVIDENCE_CONTRACT, "mirror mismatch: quote != summary", index)
    return None


def _validate_document_citation(
    item: Mapping[str, Any], index: int, ctx: AdmissionContext
) -> ContractValidation | None:
    """Company/attachment leg: identity exclusivity, quote bounds, then
    the context-grounded trust/locator checks (spec §4.1)."""
    lane = LANE_CITATION
    kind = item["kind"]
    if item["display"] not in ("page", "unit"):
        return _refusal(lane, MALFORMED_EVIDENCE_CONTRACT, "display must be page or unit", index)
    if kind == COMPANY_KIND:
        if not item["document_id"] or item["attachment_id"]:
            return _refusal(lane, MALFORMED_EVIDENCE_CONTRACT, "company locator identity broken", index)
        locator_id = item["document_id"]
    else:
        if not item["attachment_id"] or item["document_id"]:
            return _refusal(lane, MALFORMED_EVIDENCE_CONTRACT, "attachment locator identity broken", index)
        locator_id = item["attachment_id"]
    quote = item["quote"]
    if len(quote) < QUOTE_MIN_CODEPOINTS:
        return _refusal(lane, MALFORMED_EVIDENCE_CONTRACT, "quote below the 32-code-point floor", index)
    if len(quote) > QUOTE_MAX_CODEPOINTS:
        return _refusal(LANE_CITATION, CITATION_OVERFLOW, "quote above 2,000 code points", index)
    if kind == ATTACHMENT_KIND and locator_id not in ctx.trusted_attachment_ids:
        return _refusal(lane, CITATION_TRUST_DENIED, "attachment is not trusted on this thread", index)
    if (locator_id, item["version"]) not in ctx.resolvable_locators:
        return _refusal(lane, UNKNOWN_LOCATOR, "locator does not resolve this run", index)
    received = ctx.received_texts.get((locator_id, item["version"], item["unit"]))
    if received is not None and quote not in received:
        return _refusal(lane, CITATION_REPAIR_CAP, "quote not found in the text this run received", index)
    return None


# ----- visual claim lane (spec §4.2) -----


def _validate_visual_claims_channel(payload: Mapping[str, Any], ctx: AdmissionContext) -> ContractValidation:
    problem = _check_keys(LANE_VISUAL_CLAIM, payload, frozenset({"visual_claims"}), what="visual lane")
    if problem is not None:
        return problem
    claims = payload["visual_claims"]
    if not isinstance(claims, list):
        return _refusal(LANE_VISUAL_CLAIM, MALFORMED_EVIDENCE_CONTRACT, "visual_claims is not a list")
    if len(claims) > MAX_VISUAL_CLAIMS_PER_RUN:
        return _refusal(LANE_VISUAL_CLAIM, VISUAL_CLAIM_OVERFLOW, f"more than {MAX_VISUAL_CLAIMS_PER_RUN} visual claims")
    for index, claim in enumerate(claims):
        refused = _validate_visual_claim(claim, index, ctx)
        if refused is not None:
            return refused
    return _ACCEPTED


def _validate_visual_claim(claim: Any, index: int, ctx: AdmissionContext) -> ContractValidation | None:
    lane = LANE_VISUAL_CLAIM
    problem = _check_keys(lane, claim, _VISUAL_CLAIM_ELEMENT_FIELDS, index=index, what="visual claim")
    if problem is not None:
        return problem
    schema_problem = _check_schema_token(lane, claim[DATABASE_ROWS_SCHEMA_FIELD], VISUAL_CLAIM_SCHEMA, index=index)
    if schema_problem is not None:
        return schema_problem
    version_problem = _check_schema_version(lane, claim[EVIDENCE_SCHEMA_VERSION_FIELD], index=index)
    if version_problem is not None:
        return version_problem
    kind = claim["kind"]
    if not _is_str(kind):
        return _refusal(lane, MALFORMED_EVIDENCE_CONTRACT, "kind is not a string", index)
    if kind != VISUAL_EVIDENCE_KIND:
        return _refusal(lane, UNSUPPORTED_EVIDENCE_CONTRACT, f"kind {kind!r} is not the visual lane", index)
    for name in ("attachment_id", "version", "provider", "model_version",
                 "page_image_sha256", "detail", "filename"):
        if not _is_str(claim[name]):
            return _refusal(lane, MALFORMED_EVIDENCE_CONTRACT, f"{name} is not a string", index)
    if not claim["version"]:
        return _refusal(lane, MALFORMED_EVIDENCE_CONTRACT, "version must not be empty", index)
    for name in ("unit", "page", "observation_version"):
        if not _is_int(claim[name]) or claim[name] < 1:
            return _refusal(lane, MALFORMED_EVIDENCE_CONTRACT, f"{name} must be an integer >= 1", index)
    if not _is_int(claim["outbound_calls"]) or claim["outbound_calls"] < 0:
        return _refusal(lane, MALFORMED_EVIDENCE_CONTRACT, "outbound_calls must be an integer >= 0", index)
    if not isinstance(claim["reused"], bool):
        return _refusal(lane, MALFORMED_EVIDENCE_CONTRACT, "reused must be a boolean", index)
    if not _BARE_SHA256_HEX_RE.match(claim["page_image_sha256"]):
        return _refusal(lane, MALFORMED_EVIDENCE_CONTRACT, "page_image_sha256 is not bare lowercase 64-hex", index)
    scope = claim["scope"]
    if scope is not None:
        if not isinstance(scope, list) or len(scope) != 4 or not all(_is_number(v) for v in scope):
            return _refusal(lane, MALFORMED_EVIDENCE_CONTRACT, "scope must be null or four normalized numbers", index)
        x0, y0, x1, y1 = scope
        if not (0.0 <= x0 < x1 <= 1.0 and 0.0 <= y0 < y1 <= 1.0):
            return _refusal(lane, MALFORMED_EVIDENCE_CONTRACT, "scope crop bounds out of range or inverted", index)
    if len(claim["detail"]) > VISUAL_DETAIL_MAX_CODEPOINTS:
        return _refusal(lane, VISUAL_CLAIM_OVERFLOW, "detail above 4,000 code points", index)
    if claim["attachment_id"] not in ctx.trusted_attachment_ids:
        return _refusal(lane, VISUAL_CLAIM_TRUST_DENIED, "attachment is not trusted on this thread", index)
    current = ctx.current_observation_versions.get((claim["attachment_id"], claim["version"], claim["unit"]))
    if current is not None and claim["observation_version"] < current:
        return _refusal(lane, VISUAL_STALE, "observation is older than this run's stored version", index)
    return None


# ----- retrieve-ingress database_rows envelope (spec §4.4 ingress) -----


def _validate_database_rows_channel(payload: Mapping[str, Any], ctx: AdmissionContext) -> ContractValidation:
    lane = LANE_DATABASE_ROW
    if "error" in payload:
        if frozenset(payload) != frozenset({"error"}) or not _is_str(payload["error"]):
            return _refusal(lane, MALFORMED_EVIDENCE_CONTRACT, "producer error frame must carry one named error string")
        code = payload["error"]
        if code not in _PRODUCER_ERROR_CODES:
            return _refusal(lane, MALFORMED_EVIDENCE_CONTRACT, f"unknown producer error {code!r}")
        return _refusal(lane, code, "producer reported a named capability/transport failure")

    chunks = payload.get("chunks")
    if chunks is not None:
        if not isinstance(chunks, list):
            return _refusal(lane, MALFORMED_EVIDENCE_CONTRACT, "chunks is not a list")
        for index, chunk in enumerate(chunks):
            if not isinstance(chunk, Mapping):
                continue
            source_ref = chunk.get("source_ref")
            if not isinstance(source_ref, Mapping):
                continue
            document_id = source_ref.get("document_id")
            # WHY the str guard before membership: a frozenset test
            # hashes its argument, and a producer dict/list would raise
            # straight through the never-raise contract. A non-string
            # names no ledger source, so the disguise check simply
            # declines to ground a denial.
            if _is_str(document_id) and document_id in ctx.database_source_names:
                return _refusal(
                    lane, MALFORMED_EVIDENCE_CONTRACT,
                    "a database row cannot travel as a document chunk", index,
                )

    if DATABASE_ROWS_KEY not in payload:
        return _ACCEPTED
    envelope = payload[DATABASE_ROWS_KEY]
    problem = _check_keys(
        lane, envelope,
        frozenset({
            DATABASE_ROWS_SCHEMA_FIELD,
            EVIDENCE_SCHEMA_VERSION_FIELD,
            DATABASE_ROWS_RESULT_COUNT_FIELD,
            DATABASE_ROWS_ROWS_FIELD,
        }),
        what="retrieve_rows envelope",
    )
    if problem is not None:
        return problem
    schema_problem = _check_schema_token(
        lane, envelope[DATABASE_ROWS_SCHEMA_FIELD], DATABASE_ROWS_SCHEMA,
        name="retrieve_rows schema",
    )
    if schema_problem is not None:
        return schema_problem
    version_problem = _check_schema_version(
        lane, envelope[EVIDENCE_SCHEMA_VERSION_FIELD], name="retrieve_rows schema_version",
    )
    if version_problem is not None:
        return version_problem
    result_count = envelope[DATABASE_ROWS_RESULT_COUNT_FIELD]
    rows = envelope[DATABASE_ROWS_ROWS_FIELD]
    if not _is_int(result_count) or result_count < 0:
        return _refusal(lane, MALFORMED_EVIDENCE_CONTRACT, "result_count must be an integer >= 0")
    if not isinstance(rows, list):
        return _refusal(lane, MALFORMED_EVIDENCE_CONTRACT, "rows is not a list")
    if len(rows) > MAX_ROWS_PER_RESULT:
        return _refusal(lane, DATABASE_ROW_OVERFLOW, f"more than {MAX_ROWS_PER_RESULT} rows")
    if result_count != len(rows):
        return _refusal(lane, MALFORMED_EVIDENCE_CONTRACT, "result_count does not match the rows list length")
    if not rows:
        return _refusal(lane, SQL_NO_MATCHING_ROWS, "well-formed result with zero rows")
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            return _refusal(lane, MALFORMED_EVIDENCE_CONTRACT, "row is not an object", index)
        row_source = row.get("source")
        # WHY the str guard before membership: see the chunks check — a
        # non-string source is not an unauthorized *name*, it is a
        # broken type, and the snapshot shape check below owns naming
        # that refusal (malformed), so authorization stays silent here.
        if _is_str(row_source) and row_source not in ctx.authorized_database_views:
            return _refusal(lane, SQL_QUERY_DENIED, "row names a view not authorized this run", index)
        snapshot_problem = _validate_ingress_row(row, index=index)
        if snapshot_problem is not None:
            return snapshot_problem
    return _ACCEPTED


# ----- business field lane (spec §4.5) -----


def _validate_business_fields_channel(payload: Mapping[str, Any], ctx: AdmissionContext) -> ContractValidation:
    lane = LANE_BUSINESS_FIELD
    problem = _check_keys(lane, payload, frozenset({"business_fields"}), what="business field lane")
    if problem is not None:
        return problem
    elements = payload["business_fields"]
    if not isinstance(elements, list):
        return _refusal(lane, MALFORMED_EVIDENCE_CONTRACT, "business_fields is not a list")
    if len(elements) > MAX_BUSINESS_FIELDS_PER_RUN:
        return _refusal(lane, BUSINESS_FIELD_OVERFLOW, f"more than {MAX_BUSINESS_FIELDS_PER_RUN} fields per run")
    for index, element in enumerate(elements):
        refused = _validate_business_field(element, index)
        if refused is not None:
            return refused
    per_subject: dict[str, int] = {}
    for element in elements:
        subject = element["subject_ref"]
        per_subject[subject] = per_subject.get(subject, 0) + 1
    if any(count > MAX_BUSINESS_FIELDS_PER_SUBJECT for count in per_subject.values()):
        return _refusal(lane, BUSINESS_FIELD_OVERFLOW, f"more than {MAX_BUSINESS_FIELDS_PER_SUBJECT} fields for one subject")
    for index, element in enumerate(elements):
        subject = element["subject_ref"]
        if subject not in ctx.readable_subject_refs:
            return _refusal(lane, BUSINESS_FIELD_TRUST_DENIED, "subject is not readable by this caller", index)
        if element["status"] == "missing":
            return _refusal(lane, INSUFFICIENT_EVIDENCE, "field status is missing", index)
    return _ACCEPTED


def _validate_source_locator(locator: Any, index: int) -> ContractValidation | None:
    lane = LANE_BUSINESS_FIELD
    if not isinstance(locator, Mapping):
        return _refusal(lane, MALFORMED_EVIDENCE_CONTRACT, "source_locator is not an object", index)
    keys = frozenset(locator.keys())
    if keys == _DOCUMENT_LOCATOR_FIELDS:
        if not _is_str(locator["document_id"]) or not locator["document_id"]:
            return _refusal(lane, MALFORMED_EVIDENCE_CONTRACT, "document locator document_id must be a non-empty string", index)
        if not _is_str(locator["version"]) or not locator["version"]:
            return _refusal(lane, MALFORMED_EVIDENCE_CONTRACT, "document locator version must be a non-empty string", index)
        if not _is_int(locator["unit"]) or locator["unit"] < 1:
            return _refusal(lane, MALFORMED_EVIDENCE_CONTRACT, "document locator unit must be an integer >= 1", index)
        return None
    if keys == frozenset({"kind", "attachment_id", "version", "unit"}):
        if locator["kind"] != ATTACHMENT_KIND:
            return _refusal(lane, MALFORMED_EVIDENCE_CONTRACT, "attachment locator kind must be 'attachment'", index)
        for name in ("attachment_id", "version"):
            if not _is_str(locator[name]) or not locator[name]:
                return _refusal(lane, MALFORMED_EVIDENCE_CONTRACT, f"attachment locator {name} must be a non-empty string", index)
        if not _is_int(locator["unit"]) or locator["unit"] < 1:
            return _refusal(lane, MALFORMED_EVIDENCE_CONTRACT, "attachment locator unit must be an integer >= 1", index)
        return None
    if keys == frozenset({"source", "record_key", "snapshot_digest"}):
        if not _is_str(locator["source"]) or not locator["source"]:
            return _refusal(lane, MALFORMED_EVIDENCE_CONTRACT, "database locator source must be a non-empty string", index)
        record_key_problem = _check_scalar_mapping(
            locator["record_key"], lane=lane, name="record_key",
            max_entries=RECORD_KEY_MAX_PAIRS, value_max_chars=RECORD_KEY_VALUE_MAX_CHARS,
            index=index, structural_only=True,
        )
        if record_key_problem is not None:
            return record_key_problem
        if not _is_str(locator["snapshot_digest"]) or not _SHA256_PREFIXED_RE.match(locator["snapshot_digest"]):
            return _refusal(lane, MALFORMED_EVIDENCE_CONTRACT, "snapshot_digest is not sha256:<64 hex>", index)
        return None
    return _refusal(lane, MALFORMED_EVIDENCE_CONTRACT, "source_locator matches none of the frozen locator shapes", index)


def _validate_business_field(element: Any, index: int) -> ContractValidation | None:
    lane = LANE_BUSINESS_FIELD
    problem = _check_keys(lane, element, _BUSINESS_FIELD_ELEMENT_FIELDS, index=index, what="business field")
    if problem is not None:
        return problem
    schema_problem = _check_schema_token(lane, element[DATABASE_ROWS_SCHEMA_FIELD], BUSINESS_FIELD_SCHEMA, index=index)
    if schema_problem is not None:
        return schema_problem
    version_problem = _check_schema_version(lane, element[EVIDENCE_SCHEMA_VERSION_FIELD], index=index)
    if version_problem is not None:
        return version_problem
    kind = element["kind"]
    if not _is_str(kind):
        return _refusal(lane, MALFORMED_EVIDENCE_CONTRACT, "kind is not a string", index)
    if kind != BUSINESS_FIELD_KIND:
        return _refusal(lane, UNSUPPORTED_EVIDENCE_CONTRACT, f"kind {kind!r} is not the business field lane", index)
    if not _is_str(element["subject_ref"]) or not element["subject_ref"]:
        return _refusal(lane, MALFORMED_EVIDENCE_CONTRACT, "subject_ref must be a non-empty string", index)
    for name in ("original_value", "normalized_text"):
        if not _is_str(element[name]):
            return _refusal(lane, MALFORMED_EVIDENCE_CONTRACT, f"{name} must be text", index)
        if len(element[name]) > BUSINESS_TEXT_MAX_CHARS:
            return _refusal(lane, BUSINESS_FIELD_OVERFLOW, f"{name} above {BUSINESS_TEXT_MAX_CHARS} chars", index)
    locator_problem = _validate_source_locator(element["source_locator"], index)
    if locator_problem is not None:
        return locator_problem
    status = element["status"]
    if not _is_str(status) or status not in _BUSINESS_FIELD_STATUSES:
        return _refusal(lane, MALFORMED_EVIDENCE_CONTRACT, "status outside the closed set", index)
    return None


# ----- comparison frame (spec §4.3) -----


def _validate_document_locator(locator: Any, lane: str, index: int | None) -> ContractValidation | None:
    problem = _check_keys(lane, locator, _DOCUMENT_LOCATOR_FIELDS, index=index, what="document locator")
    if problem is not None:
        return problem
    if not _is_str(locator["document_id"]) or not locator["document_id"]:
        return _refusal(lane, MALFORMED_EVIDENCE_CONTRACT, "document_id must be a non-empty string", index)
    if not _is_str(locator["version"]) or not locator["version"]:
        return _refusal(lane, MALFORMED_EVIDENCE_CONTRACT, "locator version must be a non-empty string", index)
    if not _is_int(locator["unit"]) or locator["unit"] < 1:
        return _refusal(lane, MALFORMED_EVIDENCE_CONTRACT, "locator unit must be an integer >= 1", index)
    return None


def _validate_comparison_cell(cell: Any, index: int) -> ContractValidation | None:
    lane = LANE_COMPARISON
    problem = _check_keys(lane, cell, _COMPARISON_CELL_FIELDS, index=index, what="comparison cell")
    if problem is not None:
        return problem
    for name in ("row", "column", "display", "unit", "scale"):
        if not _is_str(cell[name]):
            return _refusal(lane, MALFORMED_EVIDENCE_CONTRACT, f"cell {name} is not a string", index)
    status = cell["status"]
    if status == "hit":
        if not _is_number(cell["value"]):
            return _refusal(lane, MALFORMED_EVIDENCE_CONTRACT, "hit cell value must be a number", index)
        return _validate_document_locator(cell["locator"], lane, index)
    if status == "missing":
        if cell["value"] is not None or cell["locator"] is not None:
            return _refusal(lane, MALFORMED_EVIDENCE_CONTRACT, "missing cell must carry null value and locator", index)
        return None
    return _refusal(lane, MALFORMED_EVIDENCE_CONTRACT, "cell status outside hit/missing", index)


def _validate_comparison_header(lane: str, envelope: Mapping[str, Any]) -> ContractValidation | None:
    """The frozen token, its version, and the five descriptive slots
    (spec §4.3)."""
    schema_problem = _check_schema_token(
        lane, envelope[DATABASE_ROWS_SCHEMA_FIELD], COMPARISON_SCHEMA,
        name="comparison schema",
    )
    if schema_problem is not None:
        return schema_problem
    version_problem = _check_schema_version(
        lane, envelope[EVIDENCE_SCHEMA_VERSION_FIELD], name="comparison schema_version",
    )
    if version_problem is not None:
        return version_problem
    for name in ("kind", "axis", "concept", "unit", "scale"):
        if not _is_str(envelope[name]):
            return _refusal(lane, MALFORMED_EVIDENCE_CONTRACT, f"{name} is not a string")
    return None


def _walk_comparison_grid(
    lane: str, envelope: Mapping[str, Any]
) -> tuple[list[Mapping[str, Any]], int, ContractValidation | None]:
    """The columns/rows/cells structure, returning the walked cells and
    the grid size, or the first refusal. WHY the tuple: the count and
    overflow checks below re-read the same walk, and re-walking an
    untrusted grid twice only risks two different answers."""
    columns = envelope["columns"]
    if not isinstance(columns, list):
        return [], 0, _refusal(lane, MALFORMED_EVIDENCE_CONTRACT, "columns is not a list")
    for column in columns:
        problem = _check_keys(lane, column, frozenset({"key", "label"}), what="comparison column")
        if problem is not None:
            return [], 0, problem
        if not _is_str(column["key"]) or not _is_str(column["label"]):
            return [], 0, _refusal(lane, MALFORMED_EVIDENCE_CONTRACT, "column key/label must be strings")
    rows = envelope["rows"]
    if not isinstance(rows, list):
        return [], 0, _refusal(lane, MALFORMED_EVIDENCE_CONTRACT, "rows is not a list")
    cells: list[Mapping[str, Any]] = []
    for row in rows:
        problem = _check_keys(
            lane, row, frozenset({"key", "label", "kind", "cells"}), what="comparison row"
        )
        if problem is not None:
            return [], 0, problem
        if not all(_is_str(row[name]) for name in ("key", "label", "kind")):
            return [], 0, _refusal(lane, MALFORMED_EVIDENCE_CONTRACT, "row key/label/kind must be strings")
        if not isinstance(row["cells"], list):
            return [], 0, _refusal(lane, MALFORMED_EVIDENCE_CONTRACT, "row cells is not a list")
        cells.extend(row["cells"])
    for index, cell in enumerate(cells):
        cell_problem = _validate_comparison_cell(cell, index)
        if cell_problem is not None:
            return [], 0, cell_problem
    return cells, len(columns) * len(rows), None


def _validate_comparison_counts(
    lane: str, envelope: Mapping[str, Any], cells: list[Mapping[str, Any]], grid_size: int
) -> ContractValidation | None:
    hit_count = envelope["hit_count"]
    cell_count = envelope["cell_count"]
    if not _is_int(hit_count) or not _is_int(cell_count):
        return _refusal(lane, MALFORMED_EVIDENCE_CONTRACT, "hit_count/cell_count must be integers")
    if cell_count != len(cells) or hit_count != sum(1 for c in cells if c["status"] == "hit"):
        return _refusal(lane, MALFORMED_EVIDENCE_CONTRACT, "count fields do not match the walked grid")
    if grid_size > MAX_CELLS:
        return _refusal(lane, COMPARISON_OVERFLOW, f"grid exceeds {MAX_CELLS} cells")
    return None


def _validate_comparison_locators(
    lane: str, envelope: Mapping[str, Any], cells: list[Mapping[str, Any]], ctx: AdmissionContext
) -> ContractValidation | None:
    locators = envelope["locators"]
    if not isinstance(locators, list):
        return _refusal(lane, MALFORMED_EVIDENCE_CONTRACT, "locators is not a list")
    for index, locator in enumerate(locators):
        locator_problem = _validate_document_locator(locator, lane, index)
        if locator_problem is not None:
            return locator_problem
    # Provenance of the whole frame precedes intra-frame cross-checks: a
    # version this run never received fails the binding before any
    # cell-level comparison is meaningful (spec §4.3).
    for locator in locators:
        if (locator["document_id"], locator["version"]) not in ctx.received_source_versions:
            return _refusal(lane, COMPARISON_SOURCE_UNVERIFIED, "frame names a source version this run did not receive")
    envelope_locator_set = [dict(locator) for locator in locators]
    for cell in cells:
        if cell["status"] != "hit":
            continue
        if dict(cell["locator"]) not in envelope_locator_set:
            return _refusal(lane, MALFORMED_EVIDENCE_CONTRACT, "cell locator is not in the envelope locators")
    return None


def _validate_comparison_deltas(lane: str, envelope: Mapping[str, Any]) -> ContractValidation | None:
    for delta in envelope["deltas"]:
        problem = _check_keys(
            lane, delta,
            frozenset({"from", "to", "absolute", "percent", "direction", "display"}),
            what="comparison delta",
        )
        if problem is not None:
            return problem
        for side in ("from", "to"):
            side_problem = _check_keys(lane, delta[side], frozenset({"row", "column"}), what="delta side")
            if side_problem is not None:
                return side_problem
            if not _is_str(delta[side]["row"]) or not _is_str(delta[side]["column"]):
                return _refusal(lane, MALFORMED_EVIDENCE_CONTRACT, "delta side row/column must be strings")
        if not _is_number(delta["absolute"]) or not _is_number(delta["percent"]):
            return _refusal(lane, MALFORMED_EVIDENCE_CONTRACT, "delta absolute/percent must be numbers")
        if not _is_str(delta["direction"]) or not _is_str(delta["display"]):
            return _refusal(lane, MALFORMED_EVIDENCE_CONTRACT, "delta direction/display must be strings")
    return None


def _validate_comparison_missing(lane: str, envelope: Mapping[str, Any]) -> ContractValidation | None:
    for missing_entry in envelope["missing"]:
        problem = _check_keys(
            lane, missing_entry, frozenset({"row", "column", "reason"}), what="comparison missing entry"
        )
        if problem is not None:
            return problem
        if not all(_is_str(missing_entry[name]) for name in ("row", "column", "reason")):
            return _refusal(lane, MALFORMED_EVIDENCE_CONTRACT, "missing entry fields must be strings")
    return None


def _validate_comparison_channel(payload: Mapping[str, Any], ctx: AdmissionContext) -> ContractValidation:
    """Frame checks run in spec §4.3 order; each section is a helper of
    its own so one refusal path stays readable (~40 branches in one
    function is where a divergent precedence hides)."""
    lane = LANE_COMPARISON
    problem = _check_keys(lane, payload, frozenset({"comparison"}), what="comparison frame")
    if problem is not None:
        return problem
    envelope = payload["comparison"]
    problem = _check_keys(lane, envelope, _COMPARISON_ENVELOPE_FIELDS, what="comparison envelope")
    if problem is not None:
        return problem
    header = _validate_comparison_header(lane, envelope)
    if header is not None:
        return header
    cells, grid_size, problem = _walk_comparison_grid(lane, envelope)
    if problem is not None:
        return problem
    counts = _validate_comparison_counts(lane, envelope, cells, grid_size)
    if counts is not None:
        return counts
    locators = _validate_comparison_locators(lane, envelope, cells, ctx)
    if locators is not None:
        return locators
    deltas = _validate_comparison_deltas(lane, envelope)
    if deltas is not None:
        return deltas
    missing = _validate_comparison_missing(lane, envelope)
    if missing is not None:
        return missing
    return _ACCEPTED


# ----- public entry -----


def validate_evidence(payload: Any, ctx: AdmissionContext) -> ContractValidation:
    """Structurally validate one untrusted producer payload by its root shape.

    Returns a typed outcome for every input class; a payload whose root names
    no frozen channel is a named malformed refusal, never a crash.
    """
    if not isinstance(payload, Mapping):
        return _refusal(LANE_CONTRACT, MALFORMED_EVIDENCE_CONTRACT, "payload is not an object")
    keys = frozenset(payload.keys())
    if "citations" in keys or "evidence_schema" in keys:
        return _validate_citation_channel(payload, ctx)
    if "visual_claims" in keys:
        return _validate_visual_claims_channel(payload, ctx)
    if "business_fields" in keys:
        return _validate_business_fields_channel(payload, ctx)
    if "comparison" in keys:
        return _validate_comparison_channel(payload, ctx)
    if keys & {DATABASE_ROWS_KEY, "chunks", "error"}:
        return _validate_database_rows_channel(payload, ctx)
    return _refusal(LANE_CONTRACT, MALFORMED_EVIDENCE_CONTRACT, "payload root names no frozen evidence channel")


__all__: Final[tuple[str, ...]] = (
    "AdmissionContext",
    "BUSINESS_FIELD_OVERFLOW",
    "BUSINESS_FIELD_TRUST_DENIED",
    "CITATION_OVERFLOW",
    "CITATION_REPAIR_CAP",
    "CITATION_TRUST_DENIED",
    "COMPARISON_OVERFLOW",
    "COMPARISON_SOURCE_UNVERIFIED",
    "CONTRACT_REFUSAL_CODES",
    "ContractValidation",
    "DATABASE_ROW_OVERFLOW",
    "EVIDENCE_SCHEMA_VERSION",
    "EVIDENCE_SCHEMA_VERSION_FIELD",
    "EvidenceRefusal",
    "INSUFFICIENT_EVIDENCE",
    "MALFORMED_EVIDENCE_CONTRACT",
    "SQL_QUERY_DENIED",
    "UNSUPPORTED_EVIDENCE_CONTRACT",
    "UNKNOWN_LOCATOR",
    "VISUAL_CLAIM_OVERFLOW",
    "VISUAL_CLAIM_TRUST_DENIED",
    "compute_snapshot_digest",
    "render_value_text",
    "seed_context_from_rows",
    "snapshot_canonical_json",
    "validate_evidence",
    "validate_snapshot_identity",
)
