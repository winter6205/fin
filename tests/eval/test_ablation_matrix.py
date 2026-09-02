"""Opt-in ablation run over the committed dataset (T22, SC31–SC35).

Runs under ``uv run pytest -q -m eval`` only — the ``tests/eval``
conftest deselects ``eval``-marked items from the default run, so
``uv run pytest -q`` never reads the 234-sample dataset or the extracted
tree for this ticket.

This is the same driver the default lane exercises on a scripted set;
here it runs on the real questions, the real gold locators and the real
frozen unit text, and the citation-gate pass rate is fin's own gate
scoring those passages.
"""

from __future__ import annotations

import json

import pytest

from fin.eval.ablation import (
    B1_CONFIGURATION_TOTAL,
    B1_MIN_REPEATS,
    B1_QUERY_SUBSET_MAX,
    B1_QUERY_SUBSET_MIN,
    METRIC_FIELDS,
    build_report,
    run_ablation,
    validate_report,
)
from fin.eval.ablation.dataset_set import dataset_eval_set

pytestmark = pytest.mark.eval


@pytest.fixture(scope="module")
def dataset_report() -> dict:
    queries, port, known_document_ids, metadata = dataset_eval_set()
    run = run_ablation(
        port,
        queries,
        repeats=B1_MIN_REPEATS,
        known_document_ids=known_document_ids,
        metadata=metadata,
    )
    return build_report(run)


def test_subset_size_is_inside_the_b1_band(dataset_report):
    size = dataset_report["dataset"]["query_subset_size"]
    assert B1_QUERY_SUBSET_MIN <= size <= B1_QUERY_SUBSET_MAX
    assert len(dataset_report["dataset"]["query_ids"]) == size


def test_one_run_covers_every_configuration_with_every_metric(dataset_report):
    summary = dataset_report["summary"]
    assert summary["configuration_count"] == B1_CONFIGURATION_TOTAL
    assert summary["missing_configuration_count"] == 0
    assert summary["configurations_missing_any_metric"] == 0
    assert summary["repeats_below_floor_count"] == 0
    for config in dataset_report["configurations"]:
        for field in METRIC_FIELDS:
            assert len(config["metrics"][field]["raw"]) == B1_MIN_REPEATS
            assert isinstance(config["metrics"][field]["median"], float)


def test_every_claim_has_percent_sample_size_and_baseline_absolute(dataset_report):
    assert dataset_report["summary"]["percent_only_claim_count"] == 0
    for claim in dataset_report["improvement_claims"]:
        assert claim["sample_size"] == dataset_report["dataset"]["query_subset_size"]
        assert isinstance(claim["baseline_absolute"], float)
        assert isinstance(claim["configuration_absolute"], float)


def test_per_capability_contribution_is_complete(dataset_report):
    assert dataset_report["summary"]["missing_contribution_count"] == 0
    assert (
        dataset_report["summary"]["contribution_count"]
        == 8 * len(METRIC_FIELDS)
    )


def test_citation_gate_pass_rate_comes_from_fin_on_the_real_passages(dataset_report):
    all_on = next(
        c for c in dataset_report["configurations"] if c["configuration_id"] == "all_on"
    )
    all_off = next(
        c for c in dataset_report["configurations"] if c["configuration_id"] == "all_off"
    )
    assert all_on["metrics"]["citation_gate_pass_rate"]["median"] > 0.0
    assert all_off["metrics"]["citation_gate_pass_rate"]["median"] == 0.0


def test_report_carries_the_dataset_known_gaps_unpolished(dataset_report):
    # Plan C closed every gap the corpus-build surface can close: tier-1
    # primary floor via EDGAR iXBRL expansion, and the chunk band via
    # the resulting derived export growth. The per-format pdf floor is
    # structural (only the 12 frozen upstream PDFs count toward pdf) and
    # is reported via the unenforceable threshold entry, not here.
    gaps = {gap["gap"] for gap in dataset_report["dataset"]["known_gaps"]}
    assert "primary_pdf_below_per_format_floor" not in gaps
    assert "chunk_total_outside_b1_band" not in gaps
    assert dataset_report["backend"]["kind"] == "contract_fake"
    assert dataset_report["caveats"]


def test_report_validates_clean(dataset_report):
    assert validate_report(dataset_report) == []


def test_the_command_writes_the_report(tmp_path):
    from fin.eval.ablation.__main__ import main

    out = tmp_path / "ablation"
    assert main(["run", "--repeats", str(B1_MIN_REPEATS), "--output", str(out)]) == 0
    report = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert validate_report(report) == []
