"""T23 structural contracts and small hermetic scenario checks.

The duration-bound pressure runs are opt-in. These tests only exercise the
same drivers with bounded, in-process fakes so the default suite never starts
a service, reads the large corpus, or calls an LLM.
"""

from __future__ import annotations

import concurrent.futures
import json
import threading
from pathlib import Path
from typing import Any

import pytest

from fin.corpus.extracted import Locator
from fin.corpus.push import PushRecord
from fin.eval.concurrency import (
    B1_CONCURRENCY_CONTRACT,
    FIN_EVAL_DIMENSIONS,
    L1Report,
    L2Report,
    L2LatencyContract,
    assert_concurrency_matches_published,
    build_supplement_report,
    first_hit_position_distribution,
    l2_latency_contract,
    main,
    run_idempotency,
    run_l1,
    run_l2,
    validate_fin_dimensions,
    validate_l1_report,
    validate_l2_report,
)
from fin.eval.concurrency.b1 import ConcurrencySpecDriftError
from fin.retrieve.fake_push_port import FakePushPort
from fin.retrieve.push_port import push_records


def _record() -> PushRecord:
    return PushRecord(
        block_id="t23-block",
        source_ref='{"document_id":"T23-2023Q1","unit":1,"version":"a"}',
        content_id="t23-content",
        text="A real enough frozen passage for the T23 write contract.",
        parent_block_id="t23-parent",
        parent_text="A real enough frozen passage for the T23 write contract.",
        metadata={
            "fmt": "txt",
            "doc_type": "narrative",
            "section": "T23",
            "source_sha256": "a" * 64,
            "parse_rule_version": "parse-1",
            "params_table_version": "params-1",
        },
    )


class _CheckSnapshotStore(dict[tuple[str, str], dict[str, Any]]):
    """Expose the check-then-write race without timing assumptions.

    The snapshot is taken before the barrier, so two unprotected callers
    deterministically observe the same empty key. Once ``FakePushPort`` owns
    its idempotency lock, the second caller cannot enter ``get`` until the
    first write has completed and therefore skips the barrier.
    """

    def __init__(
        self, owner: FakePushPort, barrier: threading.Barrier
    ) -> None:
        super().__init__()
        self._owner = owner
        self._barrier = barrier

    def get(
        self,
        key: tuple[str, str],
        default: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        snapshot = super().get(key, default)
        lock = getattr(self._owner, "_idempotency_lock", None)
        if lock is None or not lock.locked():
            self._barrier.wait()
        return snapshot


def test_b1_concurrency_numbers_are_the_published_values() -> None:
    contract = B1_CONCURRENCY_CONTRACT

    assert contract.l1.concurrency == 16
    assert contract.l1.duration_seconds == 120
    assert contract.l1.read_write_ratio == (9, 1)
    assert contract.l1.min_requests == 1200
    assert contract.l1.error_count == 0
    assert contract.l1.max_5xx_rate == pytest.approx(0.01)
    assert contract.l2.concurrency == 8
    assert contract.l2.duration_seconds == 120
    assert contract.l2.error_count == 0
    assert contract.idempotency.concurrency == 16
    assert contract.idempotency.duration_seconds == 120
    assert contract.idempotency.error_count == 0
    assert contract.latency.p95_ms == 800
    assert contract.latency.p99_ms == 2000
    assert contract.latency.warmup_excluded is True


def test_b1_drift_is_rejected_in_the_same_execution_boundary(tmp_path: Path) -> None:
    spec = (
        Path(__file__).resolve().parents[2] / "specs" / "corpus-retrieval.md"
    )
    altered = tmp_path / "corpus-retrieval.md"
    altered.write_text(
        spec.read_text(encoding="utf-8").replace(
            "并发 L1（检索 / 读取路径，不含 LLM） | 并发 16",
            "并发 L1（检索 / 读取路径，不含 LLM） | 并发 17",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConcurrencySpecDriftError):
        assert_concurrency_matches_published(altered)


def test_l1_small_driver_excludes_warmup_and_persists_all_percentiles() -> None:
    block_total = {"value": 3}
    reads = 0
    writes = 0

    def read() -> dict[str, int]:
        nonlocal reads
        reads += 1
        return {"status_code": 200}

    def write() -> dict[str, int]:
        nonlocal writes
        writes += 1
        return {"status_code": 200}

    result = run_l1(
        read_operation=read,
        write_operation=write,
        block_count=lambda: block_total["value"],
        duration_seconds=1.0,
        warmup_requests=4,
        max_requests=20,
    )

    assert isinstance(result, L1Report)
    assert result.warmup_requests == 4
    assert result.measured_requests == 20
    assert reads + writes == result.measured_requests + result.warmup_requests
    assert (reads, writes) == (22, 2)  # four warm-up reads are excluded
    assert result.error_count == 0
    assert result.five_xx_count == 0
    assert result.block_total_before == result.block_total_after == 3
    report = result.to_dict()
    assert report["latency_ms"]["p50"] >= 0
    assert report["latency_ms"]["p95"] >= 0
    assert report["latency_ms"]["p99"] >= 0
    assert report["warmup_included_in_latency"] is False
    assert validate_l1_report(result)  # this is a deliberately short smoke


def test_l1_counts_a_five_xx_response_as_an_error_and_records_its_rate() -> None:
    result = run_l1(
        read_operation=lambda: {"status_code": 500},
        write_operation=lambda: {"status_code": 200},
        block_count=lambda: 1,
        duration_seconds=1.0,
        max_requests=10,
    )

    assert result.error_count == 9
    assert result.five_xx_count == 9
    assert set(result.error_codes) == {"http_500"}
    assert result.five_xx_rate == pytest.approx(0.9)
    assert "L1 error count is not the §B1 value" in validate_l1_report(result)


def test_l2_uses_its_own_p50_p95_contract_not_l1_latency_threshold() -> None:
    contract = l2_latency_contract()
    assert isinstance(contract, L2LatencyContract)
    assert contract.p50_operator == "<"
    assert contract.p50_ms == 60_000
    assert contract.p95_operator == "<="
    assert contract.p95_ms == 120_000

    result = L2Report(
        concurrency=8,
        duration_seconds=120,
        measured_requests=8,
        error_count=0,
        error_codes=(),
        latencies_ms=(900.0,) * 8,
        llm_mode="fake",
        duration_matches_b1=True,
    )
    assert validate_l2_report(result) == []
    assert result.to_dict()["l1_latency_threshold_applied"] is False


def test_l2_fake_driver_executes_a_full_run_with_the_fake_llm() -> None:
    calls: list[str] = []

    def full_run() -> dict[str, str]:
        calls.append("called")
        return {"status": "done", "answer": "grounded fake answer"}

    result = run_l2(
        full_run=full_run,
        llm_mode="fake",
        duration_seconds=1.0,
        max_requests=8,
    )

    assert result.measured_requests == 8
    assert len(calls) == 8
    assert result.error_count == 0
    assert result.llm_mode == "fake"


def test_real_l2_without_credentials_is_a_named_skip(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("FIN_API_KEY", raising=False)

    status = main(
        [
            "l2",
            "--llm",
            "real",
            "--output",
            str(tmp_path),
        ]
    )

    assert status == 3
    payload = json.loads((tmp_path / "l2.json").read_text(encoding="utf-8"))
    assert payload["status"] == "skipped"
    assert payload["skip"]["code"] == "l2_credentials_missing"


def test_concurrent_idempotent_all_write_smoke_keeps_block_total() -> None:
    port = FakePushPort()
    record = _record()
    push_records(port, [record])
    before = port.block_total()

    result = run_idempotency(
        write_operation=lambda: push_records(port, [record]),
        block_count=port.block_total,
        duration_seconds=1.0,
        warmup_requests=2,
        max_requests=20,
    )

    assert result.measured_requests == 20
    assert result.error_count == 0
    assert result.block_total_before == before
    assert result.block_total_after == before
    assert result.to_dict()["all_writes"] is True


def test_idempotency_check_and_write_is_one_concurrent_critical_section() -> None:
    """Two concurrent first pushes must produce one write and one duplicate."""
    port = FakePushPort()
    port._by_key = _CheckSnapshotStore(
        port, threading.Barrier(2)
    )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(
            pool.map(lambda _: port.push([_record().to_wire()]), range(2))
        )

    assert sum(result["written"] for result in results) == 1
    assert sum(result["duplicates"] for result in results) == 1
    assert port.written_total() == 1
    assert port.block_total() == 1


def test_four_supplement_reports_are_complete_and_first_hit_distribution_is_stable() -> None:
    report = build_supplement_report(
        citation_gate_passes=(True, False, True),
        refusal_expected=(True, True, False),
        refusal_observed=(True, False, False),
        first_hit_positions=(1, 3, 0, 3),
        duplicate_submission=(0, 0, 0),
    )

    assert report["missing_dimension_count"] == 0
    assert report["citation_gate"]["pass_rate"] == pytest.approx(2 / 3)
    assert report["refusal_rate"]["expected_and_refused"] == 1
    assert report["refusal_rate"]["expected_but_not_refused"] == 1
    assert first_hit_position_distribution((1, 3, 0, 3)) == {
        "1": 1,
        "3": 2,
        "not_found": 1,
    }


def test_fin_dimension_catalog_excludes_retrieval_service_owned_dimensions() -> None:
    assert validate_fin_dimensions(FIN_EVAL_DIMENSIONS) == []
    assert sum(
        dimension in {"长文档召回", "跨文档多跳召回"}
        for dimension in FIN_EVAL_DIMENSIONS
    ) == 0


@pytest.mark.parametrize(
    ("command", "filename"),
    (
        ("citation-gate", "citation_gate.json"),
        ("refusal-rate", "refusal_rate.json"),
        ("first-hit", "first_hit.json"),
        ("duplicate-submission", "duplicate_submission.json"),
    ),
)
def test_each_supplement_command_writes_one_complete_report(
    tmp_path: Path, command: str, filename: str
) -> None:
    assert main([command, "--output", str(tmp_path)]) == 0

    payload = json.loads((tmp_path / filename).read_text(encoding="utf-8"))
    assert payload["missing_dimension_count"] == 0
    assert payload["dimension"] == command
