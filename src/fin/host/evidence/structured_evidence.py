"""The ``structured_result`` lane: admit, freeze, and verify same-turn evidence.

``specs/structured-corpus-evidence.md`` adds a fourth evidence shape beside
the three on the quote channel. It is deliberately NOT quotable: a
structured value supports a structured claim, it can never fabricate a
quotation (ADR 0046). So ``kind: "structured-result"`` lives INSIDE the
retained snapshot, never in ``QUOTABLE_EVIDENCE_KINDS``, and this module
owns the whole lane rather than adding an arm to the citation channel.

Three responsibilities, in the order a run meets them:

* **admit** the producer's ``structured_result`` sibling and validate its
  exact key set, so an unknown key or a wrong version is
  ``malformed_evidence_contract`` / ``unsupported_evidence_contract`` and
  never a half-read result;
* **freeze** one bounded snapshot per cited row or count receipt, with the
  type-preserving digest from :mod:`fin.host.evidence.structured_snapshot` — the
  whole snapshot, so a later replay can recompute the same identity;
* **verify** an answer claim against the fields received THIS turn, by
  subject, operation and result ID (or the count receipt's query identity).

The refusal vocabulary is the spec's, and each code is registered in
:data:`fin.host.evidence.evidence_contract.CONTRACT_REFUSAL_CODES` so there is still
exactly one registry. An unavailable reference is never a successful empty
result: a count of zero is a real receipt, and a missing receipt is a
refusal, not zero.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any, Final, Iterable, Mapping

from fin.host.evidence.evidence_contract import (
    EVIDENCE_TOO_LARGE,
    MALFORMED_EVIDENCE_CONTRACT,
    UNSUPPORTED_EVIDENCE_CONTRACT,
)
from fin.host.evidence.structured_snapshot import (
    STRUCTURED_SNAPSHOT_MAX_BYTES,
    StructuredSnapshotError,
    is_structured_snapshot_digest,
    structured_snapshot_digest,
)

from fin.host.evidence.structured_evidence_validation import (
    MAX_INLINE_SUPPORTS,
    MAX_STRUCTURED_ROWS,
    STRUCTURED_RESULT_COVERAGE,
    STRUCTURED_RESULT_SCHEMA,
    STRUCTURED_RESULT_SCHEMA_VERSION,
    StructuredRefusal,
    _COUNT_OPERATION,
    _LIST_OPERATION,
    _is_whole_second_timestamp,
    _non_empty_str,
    _nonnegative_safe_integer,
    _refuse,
    build_snapshot,
    snapshot_digests,
    snapshot_refusal,
    structured_snapshots_of,
    validate_structured_result,
)


@dataclass(frozen=True)
class StructuredClaim:
    """One verified same-turn claim and the snapshot that supports it."""

    snapshot: dict[str, Any]
    digest: str
    result_id: str | None
    matched_fields: tuple[str, ...]


@dataclass(frozen=True)
class StructuredClaimOutcome:
    accepted: bool
    claims: tuple[StructuredClaim, ...] = ()
    refusal: StructuredRefusal | None = None


@dataclass(frozen=True)
class StructuredSelector:
    """What a claim is about, before any text is matched.

    The verifier needs this because grounding a sentence against "some
    payload received this turn" is not enough: two rows can both arrive in
    one envelope, and only the row the sentence is ABOUT may support it.
    ``result_id`` identifies a list row; ``evidence_ref`` identifies a count
    receipt, which has no result id of its own.
    """

    subject: str
    operation: str
    result_id: str | None = None
    evidence_ref: str | None = None


def select_snapshot(
    selector: StructuredSelector,
    snapshots: Iterable[Mapping[str, Any]],
) -> dict[str, Any] | None:
    """The one snapshot the selector names, or ``None``.

    Subject, operation and the result identity must all agree. A snapshot
    that matches on the first two but names a different result is a
    different claim, and returning it would let row A's value verify a
    sentence about row B.
    """
    if not _valid_selector(selector):
        # EXIT: omitted identity can never select the first available snapshot.
        return None
    matches = [snapshot for snapshot in snapshots if _selector_matches(selector, snapshot)]
    # EXIT: an ambiguous identity requires an exact reference, never first-match selection.
    return dict(matches[0]) if len(matches) == 1 else None


def _valid_selector(selector: StructuredSelector) -> bool:
    if not isinstance(selector, StructuredSelector):
        return False
    if selector.subject not in ("entity", "relation_assertion"):
        return False
    if selector.evidence_ref is not None and not _non_empty_str(selector.evidence_ref):
        return False
    if selector.operation == _LIST_OPERATION:
        return _non_empty_str(selector.result_id)
    if selector.operation == _COUNT_OPERATION:
        return selector.result_id is None and _non_empty_str(selector.evidence_ref)
    return False


def _selector_matches(selector: StructuredSelector, snapshot: Mapping[str, Any]) -> bool:
    if not isinstance(snapshot, Mapping):
        return False
    if (snapshot.get("subject"), snapshot.get("operation")) != (selector.subject, selector.operation):
        return False
    if selector.evidence_ref is not None and snapshot.get("evidence_ref") != selector.evidence_ref:
        return False
    if selector.operation == _LIST_OPERATION:
        value = snapshot.get("value")
        return isinstance(value, Mapping) and value.get("id") == selector.result_id
    return True


_ENTITY_CLAIM_FIELDS: Final[frozenset[str]] = frozenset({"canonical_name", "type"})
_RELATION_CLAIM_FIELDS: Final[frozenset[str]] = frozenset({
    "head.canonical_name", "head.type", "relation_norm", "tail.canonical_name",
    "tail.type", "valid_window.from", "valid_window.until",
})
_WINDOW_CLAIM_FIELDS: Final[frozenset[str]] = frozenset({"valid_window.from", "valid_window.until"})
_MISSING_CLAIM_VALUE: Final[object] = object()


def _claim_fields(selector: StructuredSelector) -> frozenset[str]:
    if selector.operation == _COUNT_OPERATION:
        return frozenset({"count"})
    return _ENTITY_CLAIM_FIELDS if selector.subject == "entity" else _RELATION_CLAIM_FIELDS


def _claim_scalar_valid(path: str, value: Any) -> bool:
    if path == "count":
        return _nonnegative_safe_integer(value)
    if path in _WINDOW_CLAIM_FIELDS:
        return value is None or _is_whole_second_timestamp(value)
    return _non_empty_str(value)


def _claim_shape_refusal(claims: object, selector: StructuredSelector) -> StructuredRefusal | None:
    if not isinstance(claims, Mapping) or not claims:
        # EXIT: arbitrary prose cannot stand in for explicit returned field claims.
        return _refuse(MALFORMED_EVIDENCE_CONTRACT, "structured claims must be a nonempty field mapping")
    allowed = _claim_fields(selector)
    for path, value in claims.items():
        if not isinstance(path, str) or path not in allowed or not _claim_scalar_valid(path, value):
            # EXIT: selectors/provenance and unknown or ill-typed values are not domain claims.
            return _refuse(MALFORMED_EVIDENCE_CONTRACT, "structured claim carries an unknown or ill-typed field")
    try:
        encoded = json.dumps(dict(claims), ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        # EXIT: values that cannot form bounded JSON claims are malformed.
        return _refuse(MALFORMED_EVIDENCE_CONTRACT, "structured claim is not valid JSON")
    if len(encoded) > STRUCTURED_SNAPSHOT_MAX_BYTES:
        # EXIT: refuse the complete input, without trimming a claimed value.
        return _refuse(EVIDENCE_TOO_LARGE, "structured claim exceeds the snapshot byte bound")
    return None


def _claimed_value(value: Any, path: str) -> Any:
    for member in path.split("."):
        if not isinstance(value, Mapping) or member not in value:
            return _MISSING_CLAIM_VALUE
        value = value[member]
    return value


def _claim_values_match(claims: Mapping[str, Any], value: Any) -> bool:
    for path, expected in claims.items():
        actual = _claimed_value(value, path)
        if type(actual) is not type(expected) or actual != expected:
            return False
    return True


def verify_structured_claims(
    claims: object,
    selector: StructuredSelector,
    snapshots: Iterable[Mapping[str, Any]],
) -> StructuredClaimOutcome:
    """Verify explicit field-path/value claims against one received result.

    Every claimed scalar must equal its selected field, including JSON type.
    This verifies those atomic fields, not arbitrary prose or semantic truth.
    Quotation authorization remains on the source-read channel.
    """
    if not isinstance(selector, StructuredSelector):
        return StructuredClaimOutcome(False, refusal=_refuse(MALFORMED_EVIDENCE_CONTRACT, "structured claim requires a selector"))
    shape_refusal = _claim_shape_refusal(claims, selector)
    if shape_refusal is not None:
        return StructuredClaimOutcome(False, refusal=shape_refusal)
    try:
        candidates = list(snapshots)
    except TypeError:
        # EXIT: an absent/non-iterable current-turn collection is malformed, not empty evidence.
        return StructuredClaimOutcome(False, refusal=_refuse(MALFORMED_EVIDENCE_CONTRACT, "structured claim requires received snapshots"))
    for snapshot in candidates:
        size_refusal = snapshot_refusal(snapshot)
        if size_refusal is not None:
            return StructuredClaimOutcome(False, refusal=size_refusal)
    selected = select_snapshot(selector, candidates)
    if selected is None:
        return StructuredClaimOutcome(
            False,
            refusal=_refuse(
                MALFORMED_EVIDENCE_CONTRACT,
                f"no received snapshot matches subject={selector.subject!r} "
                f"operation={selector.operation!r} result_id={selector.result_id!r}",
            ),
        )
    assert isinstance(claims, Mapping)  # Validated before snapshot selection.
    if not _claim_values_match(claims, selected.get("value")):
        return StructuredClaimOutcome(
            False,
            refusal=_refuse(
                UNSUPPORTED_EVIDENCE_CONTRACT,
                "a claimed value is absent from the selected structured field",
            ),
        )
    return StructuredClaimOutcome(
        True,
        claims=(
            StructuredClaim(
                snapshot=selected,
                digest=structured_snapshot_digest(selected),
                result_id=selector.result_id,
                matched_fields=tuple(sorted(claims)),
            ),
        ),
    )


# The persisted shape: the exact snapshot, its recomputable digest, and the
# selector that names which claim it supports. Storing the snapshot verbatim
# is what lets a replay return the original retained value instead of
# re-resolving it against current knowledge.
STORED_EVIDENCE_FIELDS: Final[frozenset[str]] = frozenset(
    {"snapshot", "digest", "selector"}
)


def build_stored_record(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """One persisted record: the snapshot, its digest, and its selector.

    The selector is derived from the snapshot rather than supplied, so a
    stored record can never claim a result the snapshot does not carry.
    ``deepcopy`` because the stored JSONB is read back and re-projected by
    callers that must not be able to reach into this record's input.
    """
    value = snapshot.get("value")
    selector: dict[str, Any] = {
        "subject": snapshot["subject"],
        "operation": snapshot["operation"],
        "evidence_ref": snapshot["evidence_ref"],
    }
    if isinstance(value, Mapping) and isinstance(value.get("id"), str):
        selector["result_id"] = value["id"]
    return {
        "snapshot": copy.deepcopy(dict(snapshot)),
        "digest": structured_snapshot_digest(snapshot),
        "selector": selector,
    }


def _stored_selector_matches(selector: Any, snapshot: Mapping[str, Any]) -> bool:
    if not isinstance(selector, Mapping) or not selector:
        return False
    for name in ("subject", "operation", "evidence_ref"):
        if selector.get(name) != snapshot.get(name):
            return False
    value = snapshot.get("value")
    if not isinstance(value, Mapping):
        return False
    result_id = value.get("id") if snapshot.get("operation") == _LIST_OPERATION else None
    if not isinstance(result_id, str):
        result_id = None
    if selector.get("result_id") != result_id:
        return False
    expected_fields = {"subject", "operation", "evidence_ref"}
    if result_id is not None:
        expected_fields.add("result_id")
    return frozenset(selector) == expected_fields


def project_stored_structured_evidence(row: Any) -> dict[str, Any] | None:
    """Match one untrusted stored row against the frozen stored shape.

    The digest is RECOMPUTED, not trusted: a stored row that no longer
    hashes to its own digest is corruption, and a replay that served it
    would publish an identity the producer never issued. The selector is
    checked back against the snapshot on every field, INCLUDING
    ``result_id`` — a selector that names a row the snapshot does not
    contain is the one tamper a digest cannot catch, because the selector
    sits outside the hashed region.

    ``None`` means the caller keeps the row verbatim and refuses it
    per-record.
    """
    if not isinstance(row, Mapping) or frozenset(row) != STORED_EVIDENCE_FIELDS:
        return None
    snapshot = row.get("snapshot")
    if not isinstance(snapshot, Mapping):
        return None
    snapshot_refusal_result = snapshot_refusal(snapshot)
    if snapshot_refusal_result is not None:
        return None
    if not is_structured_snapshot_digest(row.get("digest")):
        return None
    try:
        recomputed = structured_snapshot_digest(snapshot)
    except StructuredSnapshotError:
        return None
    if recomputed != row["digest"]:
        return None
    if not _stored_selector_matches(row.get("selector"), snapshot):
        return None
    return dict(row)


__all__ = [
    "EVIDENCE_TOO_LARGE",
    "MALFORMED_EVIDENCE_CONTRACT",
    "MAX_INLINE_SUPPORTS",
    "MAX_STRUCTURED_ROWS",
    "STORED_EVIDENCE_FIELDS",
    "STRUCTURED_RESULT_SCHEMA",
    "STRUCTURED_RESULT_SCHEMA_VERSION",
    "UNSUPPORTED_EVIDENCE_CONTRACT",
    "StructuredClaim",
    "StructuredClaimOutcome",
    "StructuredRefusal",
    "StructuredSelector",
    "build_snapshot",
    "build_stored_record",
    "project_stored_structured_evidence",
    "select_snapshot",
    "snapshot_digests",
    "snapshot_refusal",
    "structured_snapshots_of",
    "validate_structured_result",
    "verify_structured_claims",
]
