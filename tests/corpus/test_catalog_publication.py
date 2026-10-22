"""The durable catalog publication contract (T6, spec section
"Durable catalog publication and recovery").

The five audit actions are the ONLY durability: payload builders produce
the exact frozen payload fields, failures are typed (never raw exception
text or paths), and the recovery decisions read the audit trail without
touching a store. These are the in-memory-path tests; the Postgres-backed
durable cases run only with ``FIN_TEST_DSN`` and are reported separately.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from fin.corpus.catalog_publication import (
    CATALOG_PUBLICATION_FAILED,
    CATALOG_PUBLICATION_PENDING,
    CATALOG_PUBLICATION_SUCCEEDED,
    PUSH_SUCCEEDED,
    UPLOAD_METADATA_ACCEPTED,
    AcceptedMetadata,
    PublicationWarning,
    accepted_metadata_payload,
    classify_catalog_failure,
    failed_payload,
    pending_payload,
    publication_id,
    publication_warning,
    push_succeeded_payload,
    read_accepted_metadata,
    read_pending_publication,
    read_push_succeeded,
    succeeded_payload,
)
from fin.corpus.catalog import CatalogCorruptError, CatalogRowError
from fin.corpus.ingest_state_model import IngestAuditRow
from datetime import datetime, timezone

_NOW = datetime(2026, 10, 17, 12, 0, 0, tzinfo=timezone.utc)


def _row(action: str, metadata: dict[str, Any]) -> IngestAuditRow:
    return IngestAuditRow(
        document_id="doc_x",
        action=action,
        actor="tester",
        occurred_at=_NOW,
        metadata=metadata,
    )


def _accepted(**overrides: Any) -> dict[str, Any]:
    metadata = {
        "schema_version": 1,
        "version": "a" * 64,
        "title": "Team handbook",
        "doc_type": "narrative",
    }
    metadata.update(overrides)
    return {k: v for k, v in metadata.items() if v is not None}


# ---------- payload builders ----------


def test_accepted_metadata_payload_has_the_frozen_fields() -> None:
    payload = accepted_metadata_payload(
        version="a" * 64, title="T", doc_type="narrative"
    )

    assert payload == {
        "schema_version": 1,
        "version": "a" * 64,
        "title": "T",
        "doc_type": "narrative",
    }


def test_accepted_metadata_payload_omits_blank_company_and_period() -> None:
    """Omitted, never ``""`` — the reader must tell "said nothing" from
    "said the empty string"."""
    payload = accepted_metadata_payload(
        version="a" * 64,
        title="T",
        doc_type="narrative",
        company="",
        period=None,
    )

    assert "company" not in payload
    assert "period" not in payload


def test_accepted_metadata_payload_carries_company_and_period_when_given() -> None:
    payload = accepted_metadata_payload(
        version="a" * 64,
        title="T",
        doc_type="narrative",
        company="Acme",
        period="2024Q1",
    )

    assert payload["company"] == "Acme"
    assert payload["period"] == "2024Q1"


def test_push_succeeded_payload_carries_content_identities() -> None:
    payload = push_succeeded_payload(
        version="a" * 64,
        content_identities={"src-1": "cid-1", "": "dropped", "src-2": ""},
    )

    assert payload == {
        "schema_version": 1,
        "version": "a" * 64,
        "content_identities": {"src-1": "cid-1"},
    }


def test_every_payload_shape_has_schema_version_and_no_extra_fields() -> None:
    pid = publication_id("doc_x", "a" * 64)
    assert pid == "catalog-v1:doc_x:" + "a" * 64

    assert pending_payload(
        version="a" * 64, document_id="doc_x", attempt=2, catalog_row={"a": 1}
    ) == {
        "schema_version": 1,
        "version": "a" * 64,
        "publication_id": pid,
        "attempt": 2,
        "catalog_row": {"a": 1},
    }
    assert failed_payload(
        version="a" * 64,
        document_id="doc_x",
        attempt=2,
        failure_code="catalog_row_invalid",
        failure_stage="validate",
    ) == {
        "schema_version": 1,
        "version": "a" * 64,
        "publication_id": pid,
        "attempt": 2,
        "failure_code": "catalog_row_invalid",
        "failure_stage": "validate",
    }
    assert succeeded_payload(
        version="a" * 64, document_id="doc_x", attempt=2
    ) == {
        "schema_version": 1,
        "version": "a" * 64,
        "publication_id": pid,
        "attempt": 2,
    }


def test_schema_version_is_an_integer_not_a_bool() -> None:
    payload = accepted_metadata_payload(version="a" * 64, title="T", doc_type="n")

    assert payload["schema_version"] == 1
    assert isinstance(payload["schema_version"], int)
    assert not isinstance(payload["schema_version"], bool)


def test_payloads_are_json_round_trippable() -> None:
    """The audit column is JSONB: the payload must survive a JSON round
    trip unchanged (no sets, no exotic values)."""
    payload = pending_payload(
        version="a" * 64,
        document_id="doc_x",
        attempt=1,
        catalog_row={"document_id": "doc_x", "page_count": 3},
    )

    assert json.loads(json.dumps(payload)) == payload


# ---------- failure classification ----------


def test_a_row_error_maps_to_catalog_row_invalid_validate() -> None:
    assert classify_catalog_failure(
        CatalogRowError("row #0 field 'title' must be a non-empty string")
    ) == ("catalog_row_invalid", "validate")


def test_a_corrupt_error_maps_to_catalog_corrupt_read() -> None:
    assert classify_catalog_failure(
        CatalogCorruptError("not valid JSON")
    ) == ("catalog_corrupt", "read")


def test_an_oserror_fingerprints_the_lock_stage() -> None:
    exc = OSError(13, "Permission denied")
    exc.filename = "/tmp/catalog.json.lock"
    assert classify_catalog_failure(exc) == ("catalog_unavailable", "lock")


def test_an_oserror_fingerprints_the_write_stage() -> None:
    exc = OSError(28, "No space left on device")
    exc.filename = "/tmp/.catalog-ab12cd.tmp"
    assert classify_catalog_failure(exc) == ("catalog_unavailable", "write")


def test_an_oserror_without_a_fingerprint_is_a_read_failure() -> None:
    exc = OSError(2, "No such file or directory")
    exc.filename = "/tmp/other.json"
    assert classify_catalog_failure(exc) == ("catalog_unavailable", "read")


def test_an_unknown_exception_is_propagated_not_swallowed() -> None:
    """A programming error must crash loudly, never become a warning."""
    with pytest.raises(KeyError):
        classify_catalog_failure(KeyError("boom"))


# ---------- reading the durable inputs back ----------


def test_read_accepted_metadata_returns_the_recorded_fields() -> None:
    rows = [_row(UPLOAD_METADATA_ACCEPTED, _accepted(company="Acme"))]

    accepted = read_accepted_metadata(rows)

    assert accepted == AcceptedMetadata(
        version="a" * 64,
        title="Team handbook",
        doc_type="narrative",
        company="Acme",
        period=None,
    )


def test_read_accepted_metadata_ignores_other_actions_and_takes_the_last() -> None:
    rows = [
        _row("job_created", {}),
        _row(UPLOAD_METADATA_ACCEPTED, _accepted(title="First")),
        _row("transition", {}),
        _row(UPLOAD_METADATA_ACCEPTED, _accepted(title="Second")),
    ]

    assert read_accepted_metadata(rows).title == "Second"


def test_read_accepted_metadata_is_none_without_a_row() -> None:
    assert read_accepted_metadata([_row("transition", {})]) is None
    assert read_accepted_metadata([]) is None


def test_read_accepted_metadata_refuses_a_row_missing_required_fields() -> None:
    """A payload without version/title/doc_type is not usable redo input:
    recovery refuses to reconstruct what was never written."""
    rows = [_row(UPLOAD_METADATA_ACCEPTED, {"schema_version": 1})]

    assert read_accepted_metadata(rows) is None


def test_read_push_succeeded_answers_by_version() -> None:
    rows = [_row(PUSH_SUCCEEDED, {"schema_version": 1, "version": "a" * 64})]

    assert read_push_succeeded(rows, version="a" * 64) is True
    assert read_push_succeeded(rows, version="b" * 64) is False


def test_read_push_succeeded_is_false_without_the_row() -> None:
    assert read_push_succeeded([], version="a" * 64) is False


def test_read_pending_publication_returns_the_complete_redo_row() -> None:
    catalog_row = {
        "document_id": "doc_x",
        "version": "a" * 64,
        "title": "T",
        "doc_type": "narrative",
        "page_count": 2,
    }
    rows = [
        _row(
            CATALOG_PUBLICATION_PENDING,
            {
                "schema_version": 1,
                "version": "a" * 64,
                "publication_id": publication_id("doc_x", "a" * 64),
                "attempt": 3,
                "catalog_row": catalog_row,
            },
        )
    ]

    pending = read_pending_publication(rows)

    assert pending is not None
    assert pending.version == "a" * 64
    assert pending.attempt == 3
    assert pending.catalog_row == catalog_row


def test_read_pending_publication_is_none_without_a_row() -> None:
    assert read_pending_publication([]) is None


def test_read_pending_publication_refuses_a_row_without_a_catalog_row() -> None:
    rows = [
        _row(
            CATALOG_PUBLICATION_PENDING,
            {"schema_version": 1, "version": "a" * 64, "attempt": 1},
        )
    ]

    assert read_pending_publication(rows) is None


# ---------- the warning projection ----------


def test_no_warning_without_publication_events() -> None:
    rows = [_row("job_created", {}), _row("transition", {})]

    assert publication_warning(rows) is None


def test_a_pending_row_without_a_terminal_event_warns_pending() -> None:
    rows = [
        _row(
            CATALOG_PUBLICATION_PENDING,
            {"schema_version": 1, "version": "a" * 64, "attempt": 1,
             "publication_id": "p", "catalog_row": {}},
        )
    ]

    warning = publication_warning(rows)

    assert warning == PublicationWarning(
        code="catalog_publication_pending",
        failure_code=None,
        failure_stage=None,
        stage="recovery",
        attempts=1,
    )


def test_a_failed_row_warns_failed_with_its_typed_details() -> None:
    rows = [
        _row(
            CATALOG_PUBLICATION_FAILED,
            {
                "schema_version": 1,
                "version": "a" * 64,
                "publication_id": "p",
                "attempt": 2,
                "failure_code": "catalog_unavailable",
                "failure_stage": "lock",
            },
        )
    ]

    warning = publication_warning(rows)

    assert warning is not None
    assert warning.code == "catalog_publication_failed"
    assert warning.failure_code == "catalog_unavailable"
    assert warning.failure_stage == "lock"
    assert warning.attempts == 2


def test_a_success_clears_an_earlier_warning() -> None:
    rows = [
        _row(
            CATALOG_PUBLICATION_FAILED,
            {"schema_version": 1, "version": "a" * 64, "attempt": 1,
             "publication_id": "p", "failure_code": "catalog_corrupt",
             "failure_stage": "read"},
        ),
        _row(
            CATALOG_PUBLICATION_SUCCEEDED,
            {"schema_version": 1, "version": "a" * 64, "attempt": 2,
             "publication_id": "p"},
        ),
    ]

    assert publication_warning(rows) is None


def test_the_latest_attempt_number_is_reported_not_a_count() -> None:
    """``attempts`` is the LATEST 1-based attempt: two retries of one
    document report ``attempts: 3`` (a poller counts documents, not
    retries)."""
    rows = [
        _row(
            CATALOG_PUBLICATION_PENDING,
            {"schema_version": 1, "version": "a" * 64, "attempt": 1,
             "publication_id": "p", "catalog_row": {}},
        ),
        _row(
            CATALOG_PUBLICATION_FAILED,
            {"schema_version": 1, "version": "a" * 64, "attempt": 2,
             "publication_id": "p", "failure_code": "catalog_unavailable",
             "failure_stage": "write"},
        ),
        _row(
            CATALOG_PUBLICATION_PENDING,
            {"schema_version": 1, "version": "a" * 64, "attempt": 3,
             "publication_id": "p", "catalog_row": {}},
        ),
    ]

    warning = publication_warning(rows)

    assert warning is not None
    assert warning.code == "catalog_publication_pending"
    assert warning.attempts == 3


def test_a_failure_newer_than_the_success_warns_again() -> None:
    rows = [
        _row(
            CATALOG_PUBLICATION_SUCCEEDED,
            {"schema_version": 1, "version": "a" * 64, "attempt": 1,
             "publication_id": "p"},
        ),
        _row(
            CATALOG_PUBLICATION_PENDING,
            {"schema_version": 1, "version": "b" * 64, "attempt": 2,
             "publication_id": "p2", "catalog_row": {}},
        ),
    ]

    warning = publication_warning(rows)

    assert warning is not None
    assert warning.code == "catalog_publication_pending"
    assert warning.attempts == 2
