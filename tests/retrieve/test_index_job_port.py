"""Contract tests for the index-job port module: pinned codes, shapes,
protocol conformance, and the wire-code projection helper.

The codes are part of the cross-repo contract (iknow-memory's
``submit_index_job`` / ``poll_index_job`` typed errors), so they are
asserted as literals — a rename here breaks the seam silently.
"""

from __future__ import annotations

import dataclasses

import pytest

from fin.retrieve.fake_index_job_port import FakeIndexJobPort
from fin.retrieve.index_job_port import (
    INDEX_JOB_ERROR_TYPES,
    INDEX_JOB_STATUSES,
    LOCATOR_CEILING_CODEPOINTS,
    CommunityIndexState,
    IndexJobError,
    IndexJobIdentityMismatchError,
    IndexJobInProgressError,
    IndexJobInvalidLocatorError,
    IndexJobPoll,
    IndexJobPort,
    IndexJobRecord,
    IndexJobSubmission,
    IndexJobUnknownJobError,
    IndexJobUnknownSourceError,
    IndexJobUnavailableError,
    index_job_error_for_code,
)
from fin.retrieve.mcp_index_job_port import MCPIndexJobPort


def test_wire_error_codes_are_pinned() -> None:
    assert {
        IndexJobInvalidLocatorError.code: IndexJobInvalidLocatorError,
        IndexJobUnknownSourceError.code: IndexJobUnknownSourceError,
        IndexJobIdentityMismatchError.code: IndexJobIdentityMismatchError,
        IndexJobUnknownJobError.code: IndexJobUnknownJobError,
        IndexJobInProgressError.code: IndexJobInProgressError,
        IndexJobUnavailableError.code: IndexJobUnavailableError,
    } == INDEX_JOB_ERROR_TYPES
    assert set(INDEX_JOB_ERROR_TYPES) == {
        "index_job_invalid_locator",
        "index_job_unknown_source",
        "index_job_identity_mismatch",
        "index_job_unknown_job",
        "index_job_in_progress",
        "index_job_unavailable",
    }


def test_base_error_has_own_code_never_reused() -> None:
    assert IndexJobError.code == "index_job_error"
    assert IndexJobError.code not in INDEX_JOB_ERROR_TYPES


@pytest.mark.parametrize("code", sorted(INDEX_JOB_ERROR_TYPES))
def test_error_for_code_projects_the_typed_class(code: str) -> None:
    error = index_job_error_for_code(code, "prose that must not be matched")
    assert isinstance(error, INDEX_JOB_ERROR_TYPES[code])
    assert error.code == code


def test_error_for_unknown_code_degrades_to_unavailable() -> None:
    error = index_job_error_for_code("index_job_cosmic_ray", "prose")
    assert isinstance(error, IndexJobUnavailableError)
    assert "index_job_cosmic_ray" in str(error)


def test_in_progress_error_carries_active_job_id() -> None:
    error = index_job_error_for_code(
        "index_job_in_progress", "open", active_job_id="job-7"
    )
    assert isinstance(error, IndexJobInProgressError)
    assert error.active_job_id == "job-7"
    plain = index_job_error_for_code("index_job_in_progress", "open")
    assert isinstance(plain, IndexJobInProgressError)
    assert plain.active_job_id is None


def test_statuses_are_the_four_persisted_states() -> None:
    assert INDEX_JOB_STATUSES == frozenset(
        {"in_progress", "ready", "failed", "stale"}
    )


def test_locator_ceiling_is_the_shared_budget() -> None:
    assert LOCATOR_CEILING_CODEPOINTS == 1024


def _record() -> IndexJobRecord:
    return IndexJobRecord(
        job_id="job-1",
        source_ref="src-1",
        content_identity="id-1",
        status="in_progress",
        started_at=None,
        finished_at=None,
        failure_code=None,
    )


def test_carriers_are_frozen() -> None:
    carriers = [
        CommunityIndexState("in_progress", None),
        _record(),
        IndexJobSubmission(_record(), reused=False),
        IndexJobPoll(_record(), CommunityIndexState("in_progress", None)),
    ]
    for carrier in carriers:
        with pytest.raises(dataclasses.FrozenInstanceError):
            carrier.status = "ready"  # type: ignore[attr-defined]


def test_fake_and_mcp_ports_satisfy_the_protocol() -> None:
    assert isinstance(FakeIndexJobPort(), IndexJobPort)
    assert isinstance(MCPIndexJobPort(session=object()), IndexJobPort)


# ----- fake-port contract behavior (what the faked service guarantees) -----


def test_fake_submit_joins_the_same_pair_and_names_the_active_job() -> None:
    port = FakeIndexJobPort()
    first = port.submit_index_job("src-1", "id-1")
    again = port.submit_index_job("src-1", "id-1")
    assert again.reused is True
    assert again.record.job_id == first.record.job_id
    # A different identity while the first job is open is refused by
    # name, and the refusal names the holding job.
    with pytest.raises(IndexJobInProgressError) as caught:
        port.submit_index_job("src-1", "id-2")
    assert caught.value.active_job_id == first.record.job_id


def test_fake_poll_is_a_pure_read() -> None:
    port = FakeIndexJobPort()
    job = port.submit_index_job("src-1", "id-1").record
    port.set_community_index_state("ready")
    before = port.poll_index_job(job.job_id)
    after = port.poll_index_job(job.job_id)
    assert before == after
    assert before.community_index_state == CommunityIndexState("ready", None)


def test_fake_rejects_blank_and_oversized_locators() -> None:
    port = FakeIndexJobPort()
    with pytest.raises(IndexJobInvalidLocatorError):
        port.submit_index_job("", "id-1")
    with pytest.raises(IndexJobInvalidLocatorError):
        port.submit_index_job("src-1", "x" * (LOCATOR_CEILING_CODEPOINTS + 1))
    with pytest.raises(IndexJobInvalidLocatorError):
        port.poll_index_job(" ")
    with pytest.raises(IndexJobUnknownJobError):
        port.poll_index_job("no-such-job")
