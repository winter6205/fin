"""Hit → locator mapping failures are dropped, counted and persisted
(plan T17, SC22 / SC43).

SC22: 「命中映射回 **locator** 失败的块被丢弃（不进入引用证据），检索侧标识
永不作为引用证据传给用户；同时该次映射失败**计数**落盘且 >0 时可断言——
丢弃不静默」.

The retrieve half lives here: the MCP adapter already dropped unmapped
hits (ADR 0030 mapper-drop), but the drop was silent — an adapter with a
stale ``locator_map`` answered every query with ``{"chunks": []}`` and
looked healthy. This module asserts the drop is now counted, that the
count survives the process that produced it, and that a ledger which
cannot persist fails by name instead of losing the count.

The host half (citation-evidence admission) is
``tests/host/test_evidence_admission.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from fin.corpus.extracted import Locator
from fin.retrieve.hit_mapping import (
    MAPPING_FAILURE_LOG_ENV,
    MAPPING_FAILURE_MALFORMED_HIT,
    MAPPING_FAILURE_MISSING_SOURCE_REF,
    MAPPING_FAILURE_NON_LOCATOR_SOURCE_REF,
    MAPPING_FAILURE_UNMAPPED_SOURCE_REF,
    MAPPING_STAGE_RETRIEVE,
    MappingFailureLedger,
    MappingFailureLedgerCorruptError,
    MappingFailureLedgerUnwritableError,
    MappingFailureRecordInvalidError,
    ledger_from_env,
    map_hits_to_chunks,
)
from fin.retrieve.mcp_adapter import MCPRetrieveAdapter


def _loc(document_id: str = "DOC-2023Q1", page: int = 1) -> Locator:
    return Locator(document_id=document_id, version="v-" + "a" * 60, page=page)


class _ScriptedAdapter(MCPRetrieveAdapter):
    """``MCPRetrieveAdapter`` with a scripted transport (the documented
    seam; see ``tests/retrieve/test_mcp_adapter.py``)."""

    def __init__(self, *, scripted_response: dict, **kwargs: Any) -> None:
        super().__init__(command=["never-launched"], **kwargs)
        self._scripted_response = scripted_response

    async def _call_mcp(self, payload: dict) -> dict:  # type: ignore[override]
        return self._scripted_response


# ----- the pure mapping seam -----


def test_unmapped_source_ref_is_dropped_and_counted() -> None:
    """An id absent from ``locator_map`` yields no chunk AND one
    counted failure — the drop stops being silent."""
    ledger = MappingFailureLedger()
    chunks = map_hits_to_chunks(
        [
            {"text": "kept", "source_ref": "id-A", "score": 3.0},
            {"text": "dropped", "source_ref": "id-MISSING", "score": 2.0},
        ],
        locator_map={"id-A": _loc()},
        limit=10,
        ledger=ledger,
    )

    assert [chunk.text for chunk in chunks] == ["kept"]
    assert ledger.count() == 1
    failure = ledger.failures()[0]
    assert failure.code == MAPPING_FAILURE_UNMAPPED_SOURCE_REF
    assert failure.stage == MAPPING_STAGE_RETRIEVE
    assert failure.retrieval_ref == "id-MISSING"


def test_fully_mapped_hits_record_no_failure() -> None:
    """The counter is a failure counter, not a hit counter."""
    ledger = MappingFailureLedger()
    chunks = map_hits_to_chunks(
        [{"text": "kept", "source_ref": "id-A", "score": 1.0}],
        locator_map={"id-A": _loc()},
        limit=10,
        ledger=ledger,
    )

    assert len(chunks) == 1
    assert ledger.count() == 0
    assert ledger.count_by_code() == {}


@pytest.mark.parametrize(
    "hit, expected_code",
    [
        ("not-a-dict", MAPPING_FAILURE_MALFORMED_HIT),
        ({"source_ref": "id-A"}, MAPPING_FAILURE_MALFORMED_HIT),
        ({"text": 17, "source_ref": "id-A"}, MAPPING_FAILURE_MALFORMED_HIT),
        ({"text": "t"}, MAPPING_FAILURE_MISSING_SOURCE_REF),
        ({"text": "t", "source_ref": 42}, MAPPING_FAILURE_MISSING_SOURCE_REF),
    ],
)
def test_each_unmappable_hit_shape_has_its_own_code(
    hit: Any, expected_code: str
) -> None:
    """The failure classes stay distinguishable: a malformed hit and a
    hit with no usable retrieval-side id are different diagnoses."""
    ledger = MappingFailureLedger()
    chunks = map_hits_to_chunks(
        [hit], locator_map={"id-A": _loc()}, limit=10, ledger=ledger
    )

    assert chunks == ()
    assert ledger.count() == 1
    assert ledger.count_by_code() == {expected_code: 1}


def test_locator_map_holding_a_non_locator_is_counted_not_passed_through() -> None:
    """A misconfigured map value must never reach the citation gate as a
    locator — it is dropped and counted like any mapping failure."""
    ledger = MappingFailureLedger()
    chunks = map_hits_to_chunks(
        [{"text": "t", "source_ref": "id-A", "score": 1.0}],
        locator_map={"id-A": "iknow-internal-row-7"},  # type: ignore[dict-item]
        limit=10,
        ledger=ledger,
    )

    assert chunks == ()
    assert ledger.count_by_code() == {
        MAPPING_FAILURE_NON_LOCATOR_SOURCE_REF: 1
    }


def test_count_by_code_aggregates_repeats() -> None:
    ledger = MappingFailureLedger()
    map_hits_to_chunks(
        [
            {"text": "a", "source_ref": "id-X", "score": 1.0},
            {"text": "b", "source_ref": "id-Y", "score": 1.0},
            {"text": "c"},
        ],
        locator_map={},
        limit=10,
        ledger=ledger,
    )

    assert ledger.count() == 3
    assert ledger.count_by_code() == {
        MAPPING_FAILURE_UNMAPPED_SOURCE_REF: 2,
        MAPPING_FAILURE_MISSING_SOURCE_REF: 1,
    }


def test_limit_is_applied_to_the_mapped_subset() -> None:
    """Pre-existing behaviour (``tests/retrieve/test_mcp_adapter.py``):
    the cap counts locatable chunks, not raw hits."""
    ledger = MappingFailureLedger()
    hits = [
        {"text": f"hit {i}", "source_ref": f"id-{i}", "score": 1.0}
        for i in range(30)
    ]
    chunks = map_hits_to_chunks(
        hits,
        locator_map={f"id-{i}": _loc(page=i + 1) for i in range(30)},
        limit=5,
        ledger=ledger,
    )

    assert len(chunks) == 5
    assert ledger.count() == 0


# ----- persistence: the count outlives the process -----


def test_failure_count_is_persisted_and_reloadable(tmp_path: Path) -> None:
    """SC22「计数落盘且 >0 时可断言」: a fresh ledger over the same path
    replays the count, so an operator can assert on it after the run
    that produced it exited."""
    path = tmp_path / "mapping" / "failures.jsonl"
    ledger = MappingFailureLedger(path=path)
    map_hits_to_chunks(
        [{"text": "t", "source_ref": "id-MISSING", "score": 1.0}],
        locator_map={},
        limit=10,
        ledger=ledger,
    )

    assert path.exists()
    event = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert event["code"] == MAPPING_FAILURE_UNMAPPED_SOURCE_REF
    assert event["retrieval_ref"] == "id-MISSING"

    reloaded = MappingFailureLedger(path=path)
    assert reloaded.count() > 0
    assert reloaded.count() == 1
    assert reloaded.failures()[0].retrieval_ref == "id-MISSING"


def test_records_append_across_ledger_instances(tmp_path: Path) -> None:
    path = tmp_path / "failures.jsonl"
    first = MappingFailureLedger(path=path)
    first.record(
        code=MAPPING_FAILURE_UNMAPPED_SOURCE_REF,
        stage=MAPPING_STAGE_RETRIEVE,
        retrieval_ref="id-1",
        message="one",
    )
    second = MappingFailureLedger(path=path)
    second.record(
        code=MAPPING_FAILURE_UNMAPPED_SOURCE_REF,
        stage=MAPPING_STAGE_RETRIEVE,
        retrieval_ref="id-2",
        message="two",
    )

    assert MappingFailureLedger(path=path).count() == 2


def test_unwritable_ledger_path_fails_by_name(tmp_path: Path) -> None:
    """A ledger that cannot persist raises a named error rather than
    counting in memory and pretending the count landed."""
    ledger = MappingFailureLedger(path=tmp_path / "as-a-dir")
    (tmp_path / "as-a-dir").mkdir()

    with pytest.raises(MappingFailureLedgerUnwritableError) as excinfo:
        ledger.record(
            code=MAPPING_FAILURE_UNMAPPED_SOURCE_REF,
            stage=MAPPING_STAGE_RETRIEVE,
            retrieval_ref="id-1",
            message="one",
        )

    assert excinfo.value.code == "mapping_failure_ledger_unwritable"


def test_corrupt_ledger_line_fails_by_name(tmp_path: Path) -> None:
    """A half-written log is not silently skipped: replaying it is a
    named failure, so nobody asserts on a count that lost records."""
    path = tmp_path / "failures.jsonl"
    path.write_text("{not json\n", encoding="utf-8")

    with pytest.raises(MappingFailureLedgerCorruptError) as excinfo:
        MappingFailureLedger(path=path)

    assert excinfo.value.code == "mapping_failure_ledger_corrupt"


def test_unknown_code_or_stage_is_refused() -> None:
    """The vocabulary is closed so ``count_by_code`` stays assertable."""
    ledger = MappingFailureLedger()
    with pytest.raises(MappingFailureRecordInvalidError):
        ledger.record(
            code="made_up",
            stage=MAPPING_STAGE_RETRIEVE,
            retrieval_ref="id-1",
            message="m",
        )
    with pytest.raises(MappingFailureRecordInvalidError):
        ledger.record(
            code=MAPPING_FAILURE_UNMAPPED_SOURCE_REF,
            stage="made_up",
            retrieval_ref="id-1",
            message="m",
        )
    assert ledger.count() == 0


def test_ledger_from_env_uses_the_configured_path(tmp_path: Path) -> None:
    path = tmp_path / "configured.jsonl"
    ledger = ledger_from_env({MAPPING_FAILURE_LOG_ENV: str(path)})
    assert ledger.path == path

    memory_only = ledger_from_env({})
    assert memory_only.path is None
    assert ledger_from_env({MAPPING_FAILURE_LOG_ENV: "   "}).path is None


# ----- the MCP adapter uses the seam -----


def test_adapter_counts_mapper_drops_into_the_injected_ledger() -> None:
    ledger = MappingFailureLedger()
    adapter = _ScriptedAdapter(
        scripted_response={
            "hits": [
                {"text": "kept", "source_ref": "id-A", "score": 2.0},
                {"text": "dropped", "source_ref": "id-MISSING", "score": 1.0},
            ]
        },
        locator_map={"id-A": _loc()},
        mapping_ledger=ledger,
    )

    result = adapter.retrieve("revenue", limit=10)

    assert [chunk.text for chunk in result["chunks"]] == ["kept"]
    assert ledger.count() == 1
    assert adapter.mapping_failures is ledger


def test_adapter_counts_drops_even_without_a_configured_ledger() -> None:
    """The default is an in-memory ledger, not "no counting": a
    map-less adapter is diagnosable from its own surface."""
    adapter = _ScriptedAdapter(
        scripted_response={
            "hits": [{"text": "any", "source_ref": "id-X", "score": 1.0}]
        },
        locator_map={},
    )

    assert adapter.retrieve("revenue") == {"chunks": []}
    assert adapter.mapping_failures.count() > 0
