"""Contract tests for the ``structured_result`` lane.

The lane is the fourth evidence shape and the only one that is NOT
quotable, so most of these cases are about what it refuses: an unknown
key, a wrong version, a receipt bound to a different query, a row whose
support set is truncated without a continuation. The two properties worth
stating positively are that a count of ZERO is a real receipt, and that a
claim verifies only against a value the payload actually carried.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from fin.host.evidence_contract import (
    CONTRACT_REFUSAL_CODES,
    EVIDENCE_TOO_LARGE,
    MALFORMED_EVIDENCE_CONTRACT,
    UNSUPPORTED_EVIDENCE_CONTRACT,
)
from fin.host.structured_evidence import (
    MAX_STRUCTURED_ROWS,
    STORED_EVIDENCE_FIELDS,
    StructuredRefusal,
    StructuredSelector,
    build_snapshot,
    build_stored_record,
    project_stored_structured_evidence,
    select_snapshot,
    snapshot_digests,
    snapshot_refusal,
    structured_snapshots_of,
    validate_structured_result,
    verify_structured_claims,
)
from fin.host.structured_snapshot import (
    STRUCTURED_SNAPSHOT_FIELDS,
    structured_snapshot_digest,
)

BASE_CONTEXT = {
    "schema": "iknow.structured_result",
    "schema_version": 1,
    "subject": "entity",
    "operation": "list",
    "query_digest": "sha256:0f1e2d3c",
    "scope_binding": "scope-fin-tenant-7",
    "read_revision": "rev-118",
    "queried_at": "2026-10-03T00:00:00Z",
    "maintenance": {
        "maintenance_revision": "maintenance-rev-12",
        "sources_in_scope": 1,
        "published_sources": 1,
        "desired_index": {"pending": 0, "running": 0, "ready": 1, "failed": 0, "stale": 0},
        "conflict_checks": {"pending": 0, "running": 0, "ready": 1, "failed": 0, "stale": 0},
        "ruleset_revision": "structural-alternatives:1",
        "coverage": "structural_candidates_only",
    },
}

ROW = {
    "id": "00000000-0000-4000-8000-000000000001",
    "canonical_name": "Northwind Logistics",
    "type": "organization",
    "display_support_id": "00000000-0000-4000-8000-000000000101",
    "display_disagreement": False,
    "evidence_ref": "evref-0001",
    "evidence_expires_at": "2027-10-03T00:00:00Z",
    "provenance": [
        {
            "support_id": "00000000-0000-4000-8000-000000000201",
            "source_ref": "company-source-1",
            "source_version": "source-version-1",
            "location": {
                "document_id": "example-document",
                "version": "frozen-unit-version",
                "unit": 1,
            },
            "span": {"start": 0, "end": 42},
            "extraction_ref": "extraction-revision-1",
        }
    ],
    "provenance_complete": True,
    "provenance_next_ref": None,
}


def _list_sibling(**over: Any) -> dict[str, Any]:
    payload = {**BASE_CONTEXT, "rows": [copy.deepcopy(ROW)], "next_cursor": None}
    payload.update(over)
    return payload


def _support(support_id: str, **over: Any) -> dict[str, Any]:
    return {
        "support_id": support_id,
        "source_ref": "company-source-1",
        "source_version": "source-version-1",
        "location": {
            "document_id": "example-document",
            "version": "frozen-unit-version",
            "unit": 1,
        },
        "span": {"start": 0, "end": 42},
        "extraction_ref": "extraction-revision-1",
        **over,
    }


def _count_sibling(**over: Any) -> dict[str, Any]:
    count = over.get("count", 0)
    payload: dict[str, Any] = {
        **BASE_CONTEXT,
        "operation": "count",
        "count": count,
        "count_receipt": _count_receipt(count=count),
    }
    payload.pop("rows", None)
    payload.pop("next_cursor", None)
    for key, value in over.items():
        if key == "count_receipt" and isinstance(value, dict):
            # Replace, never merge: a case that means "this key is wrong"
            # must not silently keep the other defaults.
            payload["count_receipt"] = value
        else:
            payload[key] = value
    return payload


def _count_receipt(**over: Any) -> dict[str, Any]:
    return {
        "count": 1,
        "subject": "entity",
        "scope_binding": BASE_CONTEXT["scope_binding"],
        "queried_at": BASE_CONTEXT["queried_at"],
        "coverage": "stored_structured_subjects",
        "maintenance_revision": BASE_CONTEXT["maintenance"]["maintenance_revision"],
        "maintenance": copy.deepcopy(BASE_CONTEXT["maintenance"]),
        "evidence_ref": "evref-count-1",
        "evidence_expires_at": "2027-10-03T00:00:00Z",
        "query_digest": BASE_CONTEXT["query_digest"],
        "read_revision": BASE_CONTEXT["read_revision"],
        **over,
    }


def _stored_list_record() -> dict[str, Any]:
    """A frozen record fixture independent of producer admission behavior."""
    return build_stored_record(build_snapshot(ROW, BASE_CONTEXT))


def _code(refusal: StructuredRefusal | None) -> str | None:
    return None if refusal is None else refusal.code


# ----- admission -----


def test_a_well_formed_list_sibling_admits() -> None:
    assert validate_structured_result(_list_sibling()) is None


def test_a_query_spec_entity_id_is_the_persisted_and_selectable_result_identity() -> None:
    sibling = _list_sibling()
    assert validate_structured_result(sibling) is None
    snapshots = structured_snapshots_of(sibling)
    assert snapshots[0]["value"] == ROW

    record = build_stored_record(snapshots[0])
    assert record["selector"]["result_id"] == ROW["id"]
    assert project_stored_structured_evidence(record) is not None


def test_an_empty_row_list_admits_and_yields_no_snapshot() -> None:
    assert validate_structured_result(_list_sibling(rows=[])) is None
    assert structured_snapshots_of(_list_sibling(rows=[])) == []


def test_a_count_of_zero_is_a_receipt_not_a_missing_result() -> None:
    assert validate_structured_result(_count_sibling(count=0)) is None
    snapshots = structured_snapshots_of(_count_sibling(count=0))
    assert len(snapshots) == 1
    assert snapshots[0]["value"]["evidence_ref"] == "evref-count-1"


@pytest.mark.parametrize(
    ("over", "expected"),
    [
        ({"schema": "iknow.something_else"}, MALFORMED_EVIDENCE_CONTRACT),
        ({"schema_version": 1.5}, MALFORMED_EVIDENCE_CONTRACT),
        ({"schema_version": "1"}, MALFORMED_EVIDENCE_CONTRACT),
        ({"schema_version": True}, MALFORMED_EVIDENCE_CONTRACT),
        ({"schema_version": 2}, UNSUPPORTED_EVIDENCE_CONTRACT),
        ({"operation": "aggregate"}, UNSUPPORTED_EVIDENCE_CONTRACT),
        ({"queried_at": "2026-10-03T00:00:00.123Z"}, MALFORMED_EVIDENCE_CONTRACT),
        ({"queried_at": "2026-10-03T00:00:00+00:00"}, MALFORMED_EVIDENCE_CONTRACT),
        ({"queried_at": "not-a-time"}, MALFORMED_EVIDENCE_CONTRACT),
        ({"query_digest": ""}, MALFORMED_EVIDENCE_CONTRACT),
        ({"maintenance": "none"}, MALFORMED_EVIDENCE_CONTRACT),
        ({"next_cursor": 123}, MALFORMED_EVIDENCE_CONTRACT),
    ],
    ids=[
        "unknown-token",
        "fractional-version",
        "string-version",
        "bool-version",
        "future-version",
        "unknown-operation",
        "fractional-queried-at",
        "offset-queried-at",
        "unparseable-queried-at",
        "empty-query-digest",
        "maintenance-not-object",
        "non-string-cursor",
    ],
)
def test_envelope_level_refusals(over: dict[str, Any], expected: str) -> None:
    assert _code(validate_structured_result(_list_sibling(**over))) == expected


def test_an_unknown_envelope_key_refuses_as_malformed() -> None:
    assert (
        _code(validate_structured_result(_list_sibling(mystery=1)))
        == MALFORMED_EVIDENCE_CONTRACT
    )


def test_a_missing_envelope_key_refuses_as_malformed() -> None:
    payload = _list_sibling()
    payload.pop("next_cursor")
    assert _code(validate_structured_result(payload)) == MALFORMED_EVIDENCE_CONTRACT


def test_a_list_carrying_count_keys_refuses_rather_than_being_read_partially() -> None:
    assert (
        _code(validate_structured_result(_list_sibling(count=3))) == MALFORMED_EVIDENCE_CONTRACT
    )


@pytest.mark.parametrize(
    ("over", "expected"),
    [
        ({"id": ""}, MALFORMED_EVIDENCE_CONTRACT),
        ({"evidence_ref": None}, MALFORMED_EVIDENCE_CONTRACT),
        ({"evidence_expires_at": "2027-10-03"}, MALFORMED_EVIDENCE_CONTRACT),
        ({"provenance": "none"}, MALFORMED_EVIDENCE_CONTRACT),
        ({"provenance_complete": "yes"}, MALFORMED_EVIDENCE_CONTRACT),
    ],
    ids=["empty-result-id", "null-ref", "date-only-expiry", "provenance-not-list", "flag-not-bool"],
)
def test_row_level_refusals(over: dict[str, Any], expected: str) -> None:
    row = {**ROW, **over}
    assert _code(validate_structured_result(_list_sibling(rows=[row]))) == expected


def test_a_row_missing_its_query_spec_id_refuses() -> None:
    row = copy.deepcopy(ROW)
    row.pop("id")
    assert _code(validate_structured_result(_list_sibling(rows=[row]))) == MALFORMED_EVIDENCE_CONTRACT


def test_a_non_mapping_row_refuses() -> None:
    assert (
        _code(validate_structured_result(_list_sibling(rows=["nope"])))
        == MALFORMED_EVIDENCE_CONTRACT
    )


def test_more_than_eight_inline_supports_is_a_typed_overflow() -> None:
    row = {
        **ROW,
        "provenance": [
            _support(f"00000000-0000-4000-8000-{i:012d}") for i in range(201, 210)
        ],
    }
    assert _code(validate_structured_result(_list_sibling(rows=[row]))) == EVIDENCE_TOO_LARGE


@pytest.mark.parametrize(
    "defect",
    [
        "malformed-support-id",
        "missing-source-ref",
        "empty-source-version",
        "malformed-company-locator",
        "empty-extraction-ref",
        "unknown-support-field",
        "empty-span",
        "reversed-span",
        "boolean-span-offset",
        "unknown-span-field",
    ],
)
def test_provenance_requires_the_closed_support_and_locator_contract(defect: str) -> None:
    support = _support("00000000-0000-4000-8000-000000000201")
    if defect == "malformed-support-id":
        support["support_id"] = "support-201"
    elif defect == "missing-source-ref":
        support.pop("source_ref")
    elif defect == "empty-source-version":
        support["source_version"] = ""
    elif defect == "malformed-company-locator":
        support["location"] = {**support["location"], "unit": 0}
    elif defect == "empty-extraction-ref":
        support["extraction_ref"] = ""
    elif defect == "unknown-support-field":
        support["source_text"] = "must not be smuggled through provenance"
    elif defect == "empty-span":
        support["span"] = {"start": 42, "end": 42}
    elif defect == "reversed-span":
        support["span"] = {"start": 43, "end": 42}
    elif defect == "boolean-span-offset":
        support["span"] = {"start": False, "end": 42}
    else:
        support["span"] = {"start": 0, "end": 42, "byte_offset": 0}

    row = {**ROW, "provenance": [support]}
    assert _code(validate_structured_result(_list_sibling(rows=[row]))) == MALFORMED_EVIDENCE_CONTRACT


def test_provenance_supports_must_be_ordered_by_support_uuid() -> None:
    row = {
        **ROW,
        "provenance": [
            _support("00000000-0000-4000-8000-000000000202"),
            _support("00000000-0000-4000-8000-000000000201"),
        ],
    }
    assert _code(validate_structured_result(_list_sibling(rows=[row]))) == MALFORMED_EVIDENCE_CONTRACT


def test_individually_bounded_snapshots_do_not_allow_an_oversized_result_envelope() -> None:
    rows = []
    for row_index in range(2):
        first_support = 300 + row_index * 3
        supports = [
            _support(
                f"00000000-0000-4000-8000-{first_support + offset:012d}",
                source_ref=f"source-{row_index}-{offset}-" + "x" * 5800,
            )
            for offset in range(3)
        ]
        rows.append(
            {
                **ROW,
                "id": f"00000000-0000-4000-8000-{row_index + 1:012d}",
                "provenance": supports,
            }
        )

    sibling = _list_sibling(rows=rows)
    envelope_size = len(
        json.dumps(sibling, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    assert envelope_size > 32 * 1024
    for row in rows:
        assert snapshot_refusal(build_snapshot(row, sibling)) is None

    refusal = validate_structured_result(sibling)
    assert refusal is not None
    assert refusal.code == EVIDENCE_TOO_LARGE


def test_an_incomplete_row_without_a_continuation_ref_refuses() -> None:
    row = {**ROW, "provenance_complete": False, "provenance_next_ref": None}
    assert _code(validate_structured_result(_list_sibling(rows=[row]))) == MALFORMED_EVIDENCE_CONTRACT


def test_an_incomplete_row_with_a_continuation_ref_admits() -> None:
    row = {**ROW, "provenance_complete": False, "provenance_next_ref": "evref-next-1"}
    assert validate_structured_result(_list_sibling(rows=[row])) is None


def test_too_many_rows_is_a_typed_overflow() -> None:
    rows = [
        {**ROW, "id": f"00000000-0000-4000-8000-{i:012d}"}
        for i in range(MAX_STRUCTURED_ROWS + 1)
    ]
    assert _code(validate_structured_result(_list_sibling(rows=rows))) == EVIDENCE_TOO_LARGE


@pytest.mark.parametrize(
    "over",
    [
        {"count": -1},
        {"count": True},
        {"count": 1.0},
        {"count_receipt": "none"},
        {"count_receipt": {"evidence_ref": "e", "evidence_expires_at": "2027-10-03T00:00:00Z"}},
        {"count_receipt": _count_receipt(count=99)},
        {"count_receipt": _count_receipt(count=-3)},
        {"count_receipt": _count_receipt(count=True)},
        {"count_receipt": _count_receipt(query_digest="sha256:something-else")},
        {"count_receipt": _count_receipt(read_revision="rev-other")},
        {"count_receipt": _count_receipt(evidence_expires_at="2027-10-03")},
    ],
    ids=[
        "negative-count",
        "bool-count",
        "float-count",
        "receipt-not-object",
        "receipt-missing-keys",
        "receipt-count-mismatch",
        "receipt-negative-count",
        "receipt-bool-count",
        "receipt-query-digest-mismatch",
        "receipt-read-revision-mismatch",
        "receipt-expiry-not-whole-second",
    ],
)
def test_count_receipt_refusals(over: dict[str, Any]) -> None:
    assert _code(validate_structured_result(_count_sibling(**{"count": 1, **over}))) == MALFORMED_EVIDENCE_CONTRACT


def test_a_receipt_without_the_count_itself_refuses() -> None:
    """The receipt IS the retained reference; a zero claim needs it."""
    sibling = _count_sibling(count=0)
    sibling["count_receipt"].pop("count")
    assert _code(validate_structured_result(sibling)) == MALFORMED_EVIDENCE_CONTRACT


@pytest.mark.parametrize(
    "missing",
    [
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
    ],
)
def test_a_count_receipt_requires_its_complete_query_context(missing: str) -> None:
    sibling = _count_sibling(count=0)
    sibling["count_receipt"].pop(missing)
    assert _code(validate_structured_result(sibling)) == MALFORMED_EVIDENCE_CONTRACT


@pytest.mark.parametrize(
    ("field", "wrong_value"),
    [
        ("subject", "relation_assertion"),
        ("scope_binding", "scope-other"),
        ("queried_at", "2026-10-02T00:00:00Z"),
        ("coverage", "all_source_statements"),
        ("maintenance_revision", "maintenance-rev-old"),
        ("maintenance", {"maintenance_revision": "maintenance-rev-other"}),
    ],
)
def test_a_count_receipt_cannot_be_replayed_under_other_context(
    field: str, wrong_value: Any
) -> None:
    sibling = _count_sibling(count=0)
    sibling["count_receipt"][field] = wrong_value
    assert _code(validate_structured_result(sibling)) == MALFORMED_EVIDENCE_CONTRACT


def test_a_non_mapping_sibling_refuses_without_crashing() -> None:
    assert _code(validate_structured_result(["not", "a", "sibling"])) == MALFORMED_EVIDENCE_CONTRACT
    assert _code(validate_structured_result(None)) == MALFORMED_EVIDENCE_CONTRACT


# ----- freezing -----


def test_every_snapshot_carries_the_frozen_key_set_and_its_context() -> None:
    snapshots = structured_snapshots_of(_list_sibling())
    assert len(snapshots) == 1
    snapshot = snapshots[0]
    assert set(snapshot) == set(STRUCTURED_SNAPSHOT_FIELDS)
    assert snapshot["kind"] == "structured-result"
    assert snapshot["subject"] == "entity"
    assert snapshot["operation"] == "list"
    assert snapshot["evidence_ref"] == "evref-0001"
    assert snapshot["expires_at"] == "2027-10-03T00:00:00Z"
    assert snapshot["value"] == ROW


def test_snapshot_digests_recompute_and_are_pairwise_distinct() -> None:
    rows = [
        {
            **ROW,
            "id": f"00000000-0000-4000-8000-{i + 1:012d}",
            "canonical_name": f"Vendor {i}",
        }
        for i in range(2)
    ]
    snapshots = structured_snapshots_of(_list_sibling(rows=rows))
    digests = snapshot_digests(snapshots)
    assert digests == [structured_snapshot_digest(s) for s in snapshots]
    assert len(set(digests)) == 2


def test_one_snapshot_per_row_in_envelope_order() -> None:
    row_ids = [f"00000000-0000-4000-8000-{i + 1:012d}" for i in range(3)]
    rows = [{**ROW, "id": row_id} for row_id in row_ids]
    snapshots = structured_snapshots_of(_list_sibling(rows=rows))
    assert [s["value"]["id"] for s in snapshots] == row_ids


def test_a_refused_sibling_yields_no_snapshot_rather_than_a_partial_one() -> None:
    assert structured_snapshots_of(_list_sibling(mystery=1)) == []


def test_an_oversized_snapshot_is_typed_and_never_trimmed() -> None:
    snapshot = structured_snapshots_of(_list_sibling())[0]
    huge = {**snapshot, "value": {**ROW, "blob": "x" * 40000}}
    refusal = snapshot_refusal(huge)
    assert refusal is not None
    assert refusal.code == EVIDENCE_TOO_LARGE
    # The value is intact: the refusal is the contract's answer, not a trim.
    assert huge["value"]["blob"] == "x" * 40000


# ----- verification -----


_COUNT_SELECTOR = StructuredSelector(
    subject="entity", operation="count", evidence_ref="evref-count-1"
)


def _relation_sibling(**over: Any) -> dict[str, Any]:
    relation = {
        "id": "00000000-0000-4000-8000-000000000301",
        "head": {
            "id": "00000000-0000-4000-8000-000000000401",
            "canonical_name": "Northwind Logistics",
            "type": "organization",
            "display_support_id": "00000000-0000-4000-8000-000000000501",
            "display_disagreement": False,
        },
        "relation_norm": "acquired",
        "tail": {
            "id": "00000000-0000-4000-8000-000000000402",
            "canonical_name": "Contoso Freight",
            "type": "organization",
            "display_support_id": "00000000-0000-4000-8000-000000000502",
            "display_disagreement": False,
        },
        "valid_window": {"from": "2020-01-01T00:00:00Z", "until": None},
        "evidence_ref": "evref-relation-1",
        "evidence_expires_at": ROW["evidence_expires_at"],
        "provenance": copy.deepcopy(ROW["provenance"]),
        "provenance_complete": True,
        "provenance_next_ref": None,
    }
    payload = {
        **BASE_CONTEXT,
        "subject": "relation_assertion",
        "rows": [relation],
        "next_cursor": None,
    }
    payload.update(over)
    return payload


def _list_selector(
    result_id: str | None = ROW["id"], evidence_ref: str | None = None
) -> StructuredSelector:
    return StructuredSelector(
        subject="entity",
        operation="list",
        result_id=result_id,
        evidence_ref=evidence_ref,
    )


def _relation_selector(
    result_id: str = "00000000-0000-4000-8000-000000000301",
    evidence_ref: str | None = None,
) -> StructuredSelector:
    return StructuredSelector(
        subject="relation_assertion",
        operation="list",
        result_id=result_id,
        evidence_ref=evidence_ref,
    )


def test_a_claim_verifies_against_exact_fields_the_selected_row_carried() -> None:
    snapshots = structured_snapshots_of(_list_sibling())
    outcome = verify_structured_claims(
        {"canonical_name": "Northwind Logistics", "type": "organization"},
        _list_selector(),
        snapshots,
    )
    assert outcome.accepted
    assert outcome.claims[0].result_id == ROW["id"]
    assert outcome.claims[0].matched_fields == ("canonical_name", "type")


def test_a_nonempty_claim_may_assert_one_exact_field() -> None:
    snapshots = structured_snapshots_of(_list_sibling())
    outcome = verify_structured_claims(
        {"type": "organization"}, _list_selector(), snapshots
    )
    assert outcome.accepted
    assert outcome.claims[0].matched_fields == ("type",)


def test_a_claim_with_a_partially_matching_field_value_refuses() -> None:
    snapshots = structured_snapshots_of(_list_sibling())
    outcome = verify_structured_claims(
        {"canonical_name": "Northwind", "type": "organization"},
        _list_selector(),
        snapshots,
    )
    assert not outcome.accepted
    assert outcome.refusal is not None
    assert outcome.refusal.code == UNSUPPORTED_EVIDENCE_CONTRACT


def test_a_correct_name_does_not_make_a_wrong_type_claim_pass() -> None:
    snapshots = structured_snapshots_of(_list_sibling())
    outcome = verify_structured_claims(
        {"canonical_name": "Northwind Logistics", "type": "person"},
        _list_selector(),
        snapshots,
    )
    assert not outcome.accepted
    assert outcome.refusal is not None
    assert outcome.refusal.code == UNSUPPORTED_EVIDENCE_CONTRACT


def test_fields_from_different_rows_cannot_be_combined_into_one_claim() -> None:
    rows = [
        {
            **ROW,
            "id": "00000000-0000-4000-8000-000000000201",
            "canonical_name": "Northwind Logistics",
            "type": "organization",
        },
        {
            **ROW,
            "id": "00000000-0000-4000-8000-000000000202",
            "canonical_name": "Contoso Freight",
            "type": "person",
        },
    ]
    snapshots = structured_snapshots_of(_list_sibling(rows=rows))
    for result_id, claims in (
        (
            "00000000-0000-4000-8000-000000000201",
            {"canonical_name": "Contoso Freight", "type": "organization"},
        ),
        (
            "00000000-0000-4000-8000-000000000202",
            {"canonical_name": "Northwind Logistics", "type": "person"},
        ),
    ):
        outcome = verify_structured_claims(claims, _list_selector(result_id), snapshots)
        assert not outcome.accepted, result_id
        assert outcome.refusal is not None
        assert outcome.refusal.code == UNSUPPORTED_EVIDENCE_CONTRACT
    grounded = verify_structured_claims(
        {"canonical_name": "Contoso Freight", "type": "person"},
        _list_selector("00000000-0000-4000-8000-000000000202"),
        snapshots,
    )
    assert grounded.accepted
    assert grounded.claims[0].result_id == "00000000-0000-4000-8000-000000000202"


def test_text_containing_one_matching_substring_cannot_verify_a_wrong_claim() -> None:
    """A claim about another entity cannot borrow this row's matching type."""
    rows = [
        {
            **ROW,
            "id": "00000000-0000-4000-8000-000000000201",
            "canonical_name": "Northwind Logistics",
            "type": "organization",
        },
        {
            **ROW,
            "id": "00000000-0000-4000-8000-000000000202",
            "canonical_name": "Contoso Freight",
            "type": "person",
        },
    ]
    snapshots = structured_snapshots_of(_list_sibling(rows=rows))
    outcome = verify_structured_claims(
        "Contoso Freight is an organization.",
        _list_selector("00000000-0000-4000-8000-000000000201"),
        snapshots,
    )
    assert not outcome.accepted
    assert outcome.refusal is not None
    assert outcome.refusal.code == MALFORMED_EVIDENCE_CONTRACT


def test_a_list_claim_requires_a_result_id_even_when_one_row_was_received() -> None:
    snapshots = structured_snapshots_of(_list_sibling())
    outcome = verify_structured_claims(
        {"canonical_name": "Northwind Logistics"}, _list_selector(None), snapshots
    )
    assert not outcome.accepted
    assert outcome.refusal is not None
    assert outcome.refusal.code == MALFORMED_EVIDENCE_CONTRACT


def test_a_selector_naming_an_absent_result_refuses() -> None:
    snapshots = structured_snapshots_of(_list_sibling())
    outcome = verify_structured_claims(
        {"canonical_name": "Northwind Logistics"}, _list_selector("row-Z"), snapshots
    )
    assert not outcome.accepted
    assert outcome.refusal is not None


def test_a_selector_naming_the_wrong_subject_or_operation_refuses() -> None:
    snapshots = structured_snapshots_of(_list_sibling())
    for selector in (
        StructuredSelector(
            subject="relation_assertion", operation="list", result_id=ROW["id"]
        ),
        StructuredSelector(subject="entity", operation="count", result_id=ROW["id"]),
    ):
        outcome = verify_structured_claims(
            {"canonical_name": "Northwind Logistics"}, selector, snapshots
        )
        assert not outcome.accepted
        assert outcome.refusal is not None
        assert outcome.refusal.code == MALFORMED_EVIDENCE_CONTRACT


def test_a_count_claim_requires_its_exact_evidence_ref() -> None:
    snapshots = structured_snapshots_of(_count_sibling(count=0))
    for selector in (
        StructuredSelector(subject="entity", operation="count"),
        StructuredSelector(subject="entity", operation="count", evidence_ref="wrong-ref"),
    ):
        outcome = verify_structured_claims({"count": 0}, selector, snapshots)
        assert not outcome.accepted
        assert outcome.refusal is not None
        assert outcome.refusal.code == MALFORMED_EVIDENCE_CONTRACT
    correct = verify_structured_claims({"count": 0}, _COUNT_SELECTOR, snapshots)
    assert correct.accepted


def test_a_count_selector_cannot_borrow_a_rows_evidence_ref() -> None:
    snapshots = structured_snapshots_of(_list_sibling())
    outcome = verify_structured_claims({"count": 0}, _COUNT_SELECTOR, snapshots)
    assert not outcome.accepted


def test_relation_claim_verifies_the_exact_returned_tuple_fields() -> None:
    snapshots = structured_snapshots_of(_relation_sibling())
    claims = {
        "head.canonical_name": "Northwind Logistics",
        "relation_norm": "acquired",
        "tail.canonical_name": "Contoso Freight",
        "valid_window.from": "2020-01-01T00:00:00Z",
        "valid_window.until": None,
    }
    outcome = verify_structured_claims(claims, _relation_selector(), snapshots)
    assert outcome.accepted
    assert outcome.claims[0].matched_fields == tuple(sorted(claims))


def test_a_relation_claim_with_one_wrong_tuple_field_refuses() -> None:
    snapshots = structured_snapshots_of(_relation_sibling())
    claims = {
        "head.canonical_name": "Northwind Logistics",
        "relation_norm": "acquired",
        "tail.canonical_name": "Different Company",
        "valid_window.from": "2020-01-01T00:00:00Z",
        "valid_window.until": None,
    }
    outcome = verify_structured_claims(claims, _relation_selector(), snapshots)
    assert not outcome.accepted
    assert outcome.refusal is not None
    assert outcome.refusal.code == UNSUPPORTED_EVIDENCE_CONTRACT


def test_evidence_ref_repeating_alone_does_not_ground_a_claim() -> None:
    snapshots = structured_snapshots_of(_list_sibling())
    outcome = verify_structured_claims("See evref-0001.", _list_selector(), snapshots)
    assert not outcome.accepted


def test_a_zero_count_claim_verifies_against_its_receipt() -> None:
    snapshots = structured_snapshots_of(_count_sibling(count=0))
    assert len(snapshots) == 1
    outcome = verify_structured_claims({"count": 0}, _COUNT_SELECTOR, snapshots)
    assert outcome.accepted


def test_no_payload_at_all_refuses_rather_than_verifying() -> None:
    outcome = verify_structured_claims(
        {"canonical_name": "Northwind Logistics"}, _list_selector(), []
    )
    assert not outcome.accepted
    assert outcome.refusal is not None


def test_verifier_returns_typed_refusal_for_a_missing_selector() -> None:
    outcome = verify_structured_claims(
        {"canonical_name": "Northwind Logistics"}, None, structured_snapshots_of(_list_sibling())
    )
    assert not outcome.accepted
    assert outcome.refusal is not None
    assert outcome.refusal.code == MALFORMED_EVIDENCE_CONTRACT


def test_verifier_returns_typed_refusal_for_a_missing_snapshot_collection() -> None:
    outcome = verify_structured_claims(
        {"canonical_name": "Northwind Logistics"}, _list_selector(), None
    )
    assert not outcome.accepted
    assert outcome.refusal is not None
    assert outcome.refusal.code == MALFORMED_EVIDENCE_CONTRACT


def test_snapshot_selector_returns_no_match_for_a_missing_selector() -> None:
    assert select_snapshot(None, structured_snapshots_of(_list_sibling())) is None


def test_a_list_selector_uses_evidence_ref_to_disambiguate_same_result_revisions() -> None:
    first = structured_snapshots_of(
        _list_sibling(
            read_revision="rev-118",
            rows=[{**ROW, "canonical_name": "Old Name", "evidence_ref": "evref-old"}],
        )
    )
    second = structured_snapshots_of(
        _list_sibling(
            read_revision="rev-119",
            rows=[{**ROW, "canonical_name": "New Name", "evidence_ref": "evref-new"}],
        )
    )
    snapshots = [*first, *second]
    ambiguous = verify_structured_claims(
        {"canonical_name": "Old Name"}, _list_selector(), snapshots
    )
    assert not ambiguous.accepted
    assert ambiguous.refusal is not None
    assert ambiguous.refusal.code == MALFORMED_EVIDENCE_CONTRACT

    selected = verify_structured_claims(
        {"canonical_name": "New Name"},
        _list_selector(evidence_ref="evref-new"),
        snapshots,
    )
    assert selected.accepted
    assert selected.claims[0].snapshot["read_revision"] == "rev-119"


@pytest.mark.parametrize(
    ("claims", "selector", "expected"),
    [
        (None, _list_selector(), MALFORMED_EVIDENCE_CONTRACT),
        ("Northwind Logistics", _list_selector(), MALFORMED_EVIDENCE_CONTRACT),
        ([], _list_selector(), MALFORMED_EVIDENCE_CONTRACT),
        ({}, _list_selector(), MALFORMED_EVIDENCE_CONTRACT),
        ({"": "value"}, _list_selector(), MALFORMED_EVIDENCE_CONTRACT),
        ({"unknown.field": "value"}, _list_selector(), MALFORMED_EVIDENCE_CONTRACT),
        (
            {"display_support_id": ROW["display_support_id"]},
            _list_selector(),
            MALFORMED_EVIDENCE_CONTRACT,
        ),
        (
            {"head.canonical_name": "Northwind Logistics"},
            _list_selector(),
            MALFORMED_EVIDENCE_CONTRACT,
        ),
        ({"canonical_name": ""}, _list_selector(), MALFORMED_EVIDENCE_CONTRACT),
        ({"canonical_name": 1}, _list_selector(), MALFORMED_EVIDENCE_CONTRACT),
        (
            {"canonical_name": ["Northwind Logistics"]},
            _list_selector(),
            MALFORMED_EVIDENCE_CONTRACT,
        ),
        ({"count": True}, _COUNT_SELECTOR, MALFORMED_EVIDENCE_CONTRACT),
        ({"count": "0"}, _COUNT_SELECTOR, MALFORMED_EVIDENCE_CONTRACT),
        ({"count": 0.0}, _COUNT_SELECTOR, MALFORMED_EVIDENCE_CONTRACT),
        ({"count": 2**53}, _COUNT_SELECTOR, MALFORMED_EVIDENCE_CONTRACT),
    ],
    ids=[
        "null-claim",
        "bare-prose",
        "array-claim",
        "empty-claim-set",
        "empty-path",
        "unknown-path",
        "non-domain-selector-field",
        "path-absent-for-subject",
        "empty-value",
        "wrong-scalar-type",
        "non-scalar-value",
        "boolean-is-not-count-one",
        "string-is-not-count-zero",
        "float-is-not-count-zero",
        "unsafe-integer-count",
    ],
)
def test_malformed_or_unreceived_claim_fields_refuse(
    claims: Any, selector: StructuredSelector, expected: str
) -> None:
    snapshots = (
        structured_snapshots_of(_count_sibling(count=0))
        if selector.operation == "count"
        else structured_snapshots_of(_list_sibling())
    )
    outcome = verify_structured_claims(claims, selector, snapshots)
    assert not outcome.accepted
    assert outcome.refusal is not None
    assert outcome.refusal.code == expected


def test_an_oversized_claim_is_refused_without_truncation() -> None:
    snapshots = structured_snapshots_of(_list_sibling())
    oversized_value = "漢" * 12000  # More than 32 KiB when encoded as UTF-8.
    huge_claims = {"canonical_name": oversized_value}
    outcome = verify_structured_claims(huge_claims, _list_selector(), snapshots)
    assert not outcome.accepted
    assert outcome.refusal is not None
    assert outcome.refusal.code == EVIDENCE_TOO_LARGE
    assert huge_claims["canonical_name"] == oversized_value


# ----- stored record + projection -----

def test_a_stored_record_carries_the_snapshot_its_digest_and_a_selector() -> None:
    record = _stored_list_record()
    snapshot = record["snapshot"]
    assert set(record) == set(STORED_EVIDENCE_FIELDS)
    assert record["snapshot"] == snapshot
    assert record["digest"] == structured_snapshot_digest(snapshot)
    assert record["selector"] == {
        "subject": "entity",
        "operation": "list",
        "evidence_ref": "evref-0001",
        "result_id": ROW["id"],
    }


def test_a_well_formed_stored_record_projects() -> None:
    assert project_stored_structured_evidence(_stored_list_record()) is not None


def test_a_count_record_selects_without_a_result_id() -> None:
    snapshot = structured_snapshots_of(_count_sibling(count=0))[0]
    record = build_stored_record(snapshot)
    assert "result_id" not in record["selector"]
    assert project_stored_structured_evidence(record) is not None


def test_a_tampered_snapshot_fails_digest_recomputation() -> None:
    record = _stored_list_record()
    record["snapshot"]["value"] = {**ROW, "canonical_name": "Someone Else"}
    assert project_stored_structured_evidence(record) is None


def test_a_stored_digest_that_is_not_bare_hex_refuses() -> None:
    record = _stored_list_record()
    record["digest"] = "sha256:" + record["digest"]
    assert project_stored_structured_evidence(record) is None


@pytest.mark.parametrize(
    "over",
    [
        {"snapshot": "not-an-object"},
        {"digest": None},
        {"selector": {}},
        {"selector": {"subject": "assertion"}},
    ],
    ids=["snapshot-not-object", "digest-none", "empty-selector", "selector-disagrees"],
)
def test_off_shape_stored_rows_refuse_rather_than_defaulting(over: dict[str, Any]) -> None:
    record = _stored_list_record()
    record.update(over)
    assert project_stored_structured_evidence(record) is None


def test_a_stored_row_with_an_extra_key_refuses() -> None:
    record = _stored_list_record()
    record["smuggled"] = True
    assert project_stored_structured_evidence(record) is None


@pytest.mark.parametrize(
    "defect",
    [
        "schema-version",
        "subject-vocabulary",
        "operation-vocabulary",
        "coverage-vocabulary",
        "row-reference-disagrees",
        "row-expiry-disagrees",
        "over-32-kib",
    ],
)
def test_projection_refuses_well_hashed_snapshots_that_violate_the_frozen_contract(
    defect: str,
) -> None:
    record = _stored_list_record()
    snapshot = record["snapshot"]
    if defect == "schema-version":
        snapshot["schema_version"] = 2
    elif defect == "subject-vocabulary":
        snapshot["subject"] = "company"
        record["selector"]["subject"] = "company"
    elif defect == "operation-vocabulary":
        snapshot["operation"] = "search"
        record["selector"]["operation"] = "search"
    elif defect == "coverage-vocabulary":
        snapshot["coverage"] = "all_source_statements"
    elif defect == "row-reference-disagrees":
        snapshot["value"]["evidence_ref"] = "evref-other"
    elif defect == "row-expiry-disagrees":
        snapshot["value"]["evidence_expires_at"] = "2027-10-02T00:00:00Z"
    else:
        snapshot["value"]["oversized_payload"] = "x" * (32 * 1024)

    # The producer or database row could corrupt both data and its digest.
    # Recompute it here so this only exercises semantic revalidation and the
    # size bound, not the already-covered stale-digest check.
    record["digest"] = structured_snapshot_digest(snapshot)
    assert project_stored_structured_evidence(record) is None


def test_a_non_mapping_stored_row_refuses_without_crashing() -> None:
    assert project_stored_structured_evidence(["nope"]) is None
    assert project_stored_structured_evidence(None) is None
    assert project_stored_structured_evidence({}) is None


def test_a_non_string_cursor_refuses_even_on_an_empty_page() -> None:
    """The type check used to be skipped when ``rows`` was empty, so a
    non-string cursor only surfaced once a page happened to carry rows."""
    assert (
        _code(validate_structured_result(_list_sibling(rows=[], next_cursor=123)))
        == MALFORMED_EVIDENCE_CONTRACT
    )
    assert validate_structured_result(_list_sibling(rows=[], next_cursor=None)) is None
    assert validate_structured_result(_list_sibling(rows=[], next_cursor="c-1")) is None


def test_a_tampered_selector_result_id_fails_projection() -> None:
    """The selector sits OUTSIDE the hashed region, so the digest cannot
    catch a selector that names a row the snapshot does not carry."""
    record = _stored_list_record()
    snapshot = record["snapshot"]
    record["selector"]["result_id"] = "00000000-0000-4000-8000-000000000202"
    assert project_stored_structured_evidence(record) is None
    # The untouched record still projects, so the refusal above is the
    # selector check and not a blanket rejection.
    assert project_stored_structured_evidence(_stored_list_record()) is not None


# ----- registry -----


def test_every_new_exit_is_registered_in_the_one_contract_registry() -> None:
    for code in (
        EVIDENCE_TOO_LARGE,
        "evidence_expired",
        "evidence_unavailable",
        "access_denied",
        "unsupported_source_binding",
    ):
        assert code in CONTRACT_REFUSAL_CODES, code
