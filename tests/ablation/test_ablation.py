"""Structural contract for the ablation matrix + quantified report (T22).

Why this file is in the default lane and not ``tests/eval/``
------------------------------------------------------------
``tests/eval/`` is the opt-in lane: its conftest deselects every
``eval``-marked item from ``uv run pytest -q``. T22's acceptance is
two-layered:

* the **structural** assertions (configuration count = 10, every
  metric present, per-capability contribution present, "percent
  without absolutes" count = 0, repeats >= the B1 floor) run on a
  small scripted eval set — no big corpus, no retrieval process, no
  network — and therefore belong in the default lane. They live here.
* the **real dataset** run of the same driver is ``eval``-marked and
  lives in ``tests/eval/test_ablation_matrix.py``.

Every number this file asserts against comes from spec §B1 via
:mod:`fin.eval.ablation.matrix` / :mod:`fin.eval.ablation.metrics`,
which read the §B1 rows back out of the spec. Nothing is restated
locally.
"""

from __future__ import annotations

import importlib.util
import json
import math
from pathlib import Path

import pytest

from fin.corpus.extracted import Locator
from fin.eval.ablation import (
    ABLATION_REPORT_VERSION,
    AblationMatrixError,
    AblationReportError,
    AblationRunError,
    B1_CONFIGURATION_TOTAL,
    B1_MIN_REPEATS,
    B1_QUERY_SUBSET_MAX,
    B1_QUERY_SUBSET_MIN,
    METRIC_FIELDS,
    EvalQuery,
    build_ablation_matrix,
    build_report,
    merge_with_retrieval_report,
    run_ablation,
    validate_report,
)
from fin.eval.ablation.matrix import (
    AblationMatrixDriftError,
    AblationSpecRowMissingError,
    assert_matrix_matches_published,
    published_ablation_numbers,
)
from fin.eval.ablation.metrics import (
    GRAPH_ARM_CAPABILITY,
    MetricEnumerationDriftError,
    assert_metrics_match_published,
    published_metric_labels,
)
from fin.eval.ablation.scripted_set import scripted_eval_set
from fin.retrieve.capabilities import load_capability_keys, published_spec_path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def capability_keys() -> tuple[str, ...]:
    return load_capability_keys()


@pytest.fixture()
def scripted_run():
    queries, port, known_ids = scripted_eval_set()
    return run_ablation(
        port,
        queries,
        repeats=B1_MIN_REPEATS,
        known_document_ids=known_ids,
    )


@pytest.fixture()
def scripted_report(scripted_run):
    return build_report(scripted_run)


# ---------------------------------------------------------------- matrix


def test_matrix_holds_exactly_the_b1_configuration_count(capability_keys):
    matrix = build_ablation_matrix()
    assert len(matrix) == B1_CONFIGURATION_TOTAL == 10
    kinds = [config.kind for config in matrix]
    assert kinds.count("all_on") == 1
    assert kinds.count("single_off") == len(capability_keys) == 8
    assert kinds.count("all_off") == 1


def test_single_off_configurations_cover_every_capability_key(capability_keys):
    matrix = build_ablation_matrix()
    disabled = [c.disabled_capability for c in matrix if c.kind == "single_off"]
    assert tuple(disabled) == capability_keys
    for config in matrix:
        # Every configuration decides all 8 keys: an ablation run whose
        # meaning depends on a forgotten key is not an ablation run.
        assert sorted(config.capabilities) == sorted(capability_keys)
    all_on = next(c for c in matrix if c.kind == "all_on")
    assert all(all_on.capabilities.values())
    all_off = next(c for c in matrix if c.kind == "all_off")
    assert not any(all_off.capabilities.values())
    for config in matrix:
        if config.kind != "single_off":
            continue
        off = [k for k, on in config.capabilities.items() if not on]
        assert off == [config.disabled_capability]


def test_matrix_numbers_are_read_back_out_of_spec_b1(tmp_path):
    published = assert_matrix_matches_published()
    assert published.total == B1_CONFIGURATION_TOTAL
    assert published.all_on == 1
    assert published.single_off == 8
    assert published.all_off == 1
    assert published.min_repeats == B1_MIN_REPEATS
    assert (published.subset_min, published.subset_max) == (
        B1_QUERY_SUBSET_MIN,
        B1_QUERY_SUBSET_MAX,
    )

    # Drift on the published side is a named failure printing both
    # sides, not a stale local constant nobody notices.
    spec_text = published_spec_path().read_text(encoding="utf-8")
    drifted = tmp_path / "drifted-spec.md"
    drifted.write_text(
        spec_text.replace("每配置重复 ≥ 3 次", "每配置重复 ≥ 5 次"),
        encoding="utf-8",
    )
    with pytest.raises(AblationMatrixDriftError) as excinfo:
        assert_matrix_matches_published(spec_path=drifted)
    assert "3" in str(excinfo.value) and "5" in str(excinfo.value)

    rowless = tmp_path / "rowless-spec.md"
    rowless.write_text("# no B1 here\n", encoding="utf-8")
    with pytest.raises(AblationSpecRowMissingError):
        published_ablation_numbers(spec_path=rowless)


def test_run_refuses_when_published_matrix_drifts(monkeypatch, tmp_path):
    from fin.eval.ablation import matrix as matrix_module

    spec_text = published_spec_path().read_text(encoding="utf-8")
    drifted = tmp_path / "drifted-matrix-spec.md"
    drifted.write_text(
        spec_text.replace("每配置重复 ≥ 3 次", "每配置重复 ≥ 4 次"),
        encoding="utf-8",
    )
    monkeypatch.setattr(matrix_module, "published_spec_path", lambda: drifted)
    queries, port, known_ids = scripted_eval_set()

    with pytest.raises(AblationMatrixDriftError):
        run_ablation(
            port,
            queries,
            repeats=B1_MIN_REPEATS,
            known_document_ids=known_ids,
        )


def test_metric_fields_are_read_back_out_of_spec_b1(tmp_path):
    labels = assert_metrics_match_published()
    assert labels == (
        "P@5",
        "R@5",
        "Recall@20",
        "nDCG@10",
        "MRR",
        "引用门通过率",
        "图臂命中率",
        "错误率",
        "延迟 p50 / p95 / p99",
    )
    assert published_metric_labels() == labels
    # The three latency percentiles are one §B1 label and three fields.
    assert len(METRIC_FIELDS) == len(labels) + 2

    spec_text = published_spec_path().read_text(encoding="utf-8")
    drifted = tmp_path / "drifted-spec.md"
    drifted.write_text(
        spec_text.replace("| 指标集 | P@5、R@5", "| 指标集 | P@5、R@7"),
        encoding="utf-8",
    )
    with pytest.raises(MetricEnumerationDriftError) as excinfo:
        assert_metrics_match_published(spec_path=drifted)
    assert "R@7" in str(excinfo.value)


def test_run_refuses_when_published_metric_set_drifts(monkeypatch, tmp_path):
    from fin.eval.ablation import metrics as metrics_module

    spec_text = published_spec_path().read_text(encoding="utf-8")
    drifted = tmp_path / "drifted-metric-spec.md"
    drifted.write_text(
        spec_text.replace("| 指标集 | P@5、R@5", "| 指标集 | P@5、R@7"),
        encoding="utf-8",
    )
    monkeypatch.setattr(metrics_module, "published_spec_path", lambda: drifted)
    queries, port, known_ids = scripted_eval_set()

    with pytest.raises(MetricEnumerationDriftError):
        run_ablation(
            port,
            queries,
            repeats=B1_MIN_REPEATS,
            known_document_ids=known_ids,
        )


# ---------------------------------------------------------------- runner


def test_one_run_covers_every_configuration_with_no_missing_config(scripted_run):
    assert len(scripted_run.configurations) == B1_CONFIGURATION_TOTAL
    assert scripted_run.missing_configuration_count == 0
    ids = [c.configuration_id for c in scripted_run.configurations]
    assert len(set(ids)) == B1_CONFIGURATION_TOTAL


def test_every_configuration_reports_every_b1_metric(scripted_run):
    for config in scripted_run.configurations:
        assert tuple(config.metrics) == METRIC_FIELDS
        for field in METRIC_FIELDS:
            series = config.metrics[field]
            assert len(series.raw) == B1_MIN_REPEATS
            assert all(isinstance(v, float) for v in series.raw)
            assert not any(math.isnan(v) for v in series.raw)
    assert scripted_run.configurations_missing_any_metric == 0


def test_raw_values_and_median_are_both_recorded(scripted_run):
    import statistics

    for config in scripted_run.configurations:
        for field in METRIC_FIELDS:
            series = config.metrics[field]
            assert series.median == pytest.approx(statistics.median(series.raw))
            assert series.repeats == len(series.raw) >= B1_MIN_REPEATS


def test_repeats_below_the_b1_floor_is_a_named_failure():
    queries, port, known_ids = scripted_eval_set()
    with pytest.raises(AblationRunError) as excinfo:
        run_ablation(
            port,
            queries,
            repeats=B1_MIN_REPEATS - 1,
            known_document_ids=known_ids,
        )
    assert excinfo.value.code == "ablation_repeats_below_floor"


def test_a_port_without_arm_attribution_fails_named_instead_of_reporting_zero():
    queries, _port, known_ids = scripted_eval_set()

    class _ArmlessPort:
        def retrieve_for_eval(self, query, *, capabilities, limit=8):
            return {"chunks": []}

    with pytest.raises(AblationRunError) as excinfo:
        run_ablation(
            _ArmlessPort(),
            queries,
            repeats=B1_MIN_REPEATS,
            known_document_ids=known_ids,
        )
    assert excinfo.value.code == "graph_arm_attribution_unavailable"


def test_graph_arm_hit_rate_follows_the_graph_capability(scripted_run):
    assert GRAPH_ARM_CAPABILITY == "graph_ppr"
    off_graph = scripted_run.configuration("off_graph_ppr")
    assert off_graph.metrics["graph_arm_hit_rate"].median == 0.0
    all_on = scripted_run.configuration("all_on")
    assert all_on.metrics["graph_arm_hit_rate"].median > 0.0


def test_citation_gate_pass_rate_is_produced_by_fin_on_the_same_results(scripted_run):
    """The gate runs on this run's retrieved chunks, not on a re-run.

    ``all_on`` retrieves the gold passage, so a grounded answer passes;
    ``all_off`` retrieves nothing, so the same answer has no evidence to
    stand on and the gate does not pass it.
    """
    assert scripted_run.configuration("all_on").metrics[
        "citation_gate_pass_rate"
    ].median == 1.0
    assert scripted_run.configuration("all_off").metrics[
        "citation_gate_pass_rate"
    ].median == 0.0


def test_all_off_baseline_retrieves_nothing_so_quality_metrics_are_zero(scripted_run):
    all_off = scripted_run.configuration("all_off")
    for field in ("p_at_5", "r_at_5", "recall_at_20", "ndcg_at_10", "mrr"):
        assert all_off.metrics[field].median == 0.0
    # An empty result set is a legal empty result, not an error (T21).
    assert all_off.metrics["error_rate"].median == 0.0


# ---------------------------------------------------------------- report


def test_report_carries_every_configuration_and_no_missing_metric(scripted_report):
    summary = scripted_report["summary"]
    assert summary["configuration_count"] == B1_CONFIGURATION_TOTAL
    assert summary["missing_configuration_count"] == 0
    assert summary["configurations_missing_any_metric"] == 0
    assert summary["repeats_below_floor_count"] == 0
    assert scripted_report["report_version"] == ABLATION_REPORT_VERSION
    assert scripted_report["spec_basis"]["configuration_total"] == B1_CONFIGURATION_TOTAL
    assert scripted_report["spec_basis"]["min_repeats"] == B1_MIN_REPEATS


def test_every_improvement_claim_gives_percent_sample_size_and_baseline_absolute(
    scripted_report,
):
    claims = scripted_report["improvement_claims"]
    assert claims, "a report with no improvement claim proves nothing"
    for claim in claims:
        assert claim["baseline_configuration_id"] == "all_off"
        assert isinstance(claim["sample_size"], int) and claim["sample_size"] > 0
        assert isinstance(claim["baseline_absolute"], float)
        assert isinstance(claim["configuration_absolute"], float)
        assert isinstance(claim["delta_absolute"], float)
        assert claim["percent_status"] in ("computed", "undefined_zero_baseline")
        if claim["percent_status"] == "computed":
            assert isinstance(claim["percent_improvement"], float)
    # SC35: a claim carrying a percentage but no sample size / baseline
    # absolute is the failure mode; the count of those must be 0.
    assert scripted_report["summary"]["percent_only_claim_count"] == 0


def test_percent_improvement_is_a_number_not_a_word(scripted_report):
    computed = [
        claim
        for claim in scripted_report["improvement_claims"]
        if claim["percent_status"] == "computed"
    ]
    assert computed, "no percentage was computed at all"
    for claim in computed:
        expected = (
            (claim["configuration_absolute"] - claim["baseline_absolute"])
            / abs(claim["baseline_absolute"])
            * 100.0
        )
        assert claim["percent_improvement"] == pytest.approx(expected)


def test_zero_baseline_claims_are_not_labeled_as_improvements(scripted_report):
    undefined = [
        claim
        for claim in scripted_report["improvement_claims"]
        if claim["percent_status"] == "undefined_zero_baseline"
    ]
    assert undefined
    assert all(
        claim["direction"] != "improvement" for claim in undefined
    )


def test_per_capability_contribution_is_complete(scripted_report, capability_keys):
    contributions = scripted_report["capability_contributions"]
    assert scripted_report["summary"]["missing_contribution_count"] == 0
    covered = {(c["capability"], c["metric"]) for c in contributions}
    assert covered == {
        (key, field) for key in capability_keys for field in METRIC_FIELDS
    }
    for contribution in contributions:
        # 贡献度 = 全开 - 仅关掉该项.
        assert contribution["all_on_absolute"] - contribution[
            "single_off_absolute"
        ] == pytest.approx(contribution["delta_absolute"])
        assert contribution["sample_size"] > 0


def test_graph_ppr_contribution_is_a_real_number_on_the_scripted_set(scripted_report):
    graph = next(
        c
        for c in scripted_report["capability_contributions"]
        if c["capability"] == "graph_ppr" and c["metric"] == "graph_arm_hit_rate"
    )
    assert graph["delta_absolute"] > 0.0


def test_validate_report_reports_no_problem_for_a_complete_report(scripted_report):
    assert validate_report(scripted_report) == []


def test_validate_report_catches_a_dropped_metric(scripted_report):
    broken = json.loads(json.dumps(scripted_report))
    broken["configurations"][0]["metrics"].pop("mrr")
    problems = validate_report(broken)
    assert any("mrr" in problem for problem in problems)


def test_validate_report_catches_a_percent_only_claim(scripted_report):
    broken = json.loads(json.dumps(scripted_report))
    broken["improvement_claims"][0].pop("baseline_absolute")
    problems = validate_report(broken)
    assert any("baseline_absolute" in problem for problem in problems)


# ---------------------------------------------------------------- merge


def _retrieval_side_report(fin_report: dict) -> dict:
    """A scripted retrieval-side report over the same merge keys."""
    return {
        "produced_by": "retrieval-service",
        "dataset_id": fin_report["dataset"]["dataset_id"],
        "query_subset_hash": fin_report["dataset"]["query_subset_hash"],
        "configurations": [
            {
                "configuration_id": config["configuration_id"],
                "metrics": {
                    "long_document_recall": 0.5,
                    "cross_document_multi_hop_recall": 0.25,
                },
            }
            for config in fin_report["configurations"]
        ],
    }


def test_report_merges_with_the_retrieval_side_report(scripted_report):
    merged = merge_with_retrieval_report(
        scripted_report, _retrieval_side_report(scripted_report)
    )
    assert len(merged["configurations"]) == B1_CONFIGURATION_TOTAL
    for config in merged["configurations"]:
        for field in METRIC_FIELDS:
            assert field in config["metrics"]
        assert "long_document_recall" in config["metrics"]
    # fin never computes the retrieval-side-only dimensions itself.
    assert scripted_report["merge"]["dimensions_not_computed_by_fin"] == (
        "长文档召回",
        "跨文档多跳召回",
    ) or scripted_report["merge"]["dimensions_not_computed_by_fin"] == [
        "长文档召回",
        "跨文档多跳召回",
    ]
    for config in scripted_report["configurations"]:
        assert "long_document_recall" not in config["metrics"]


def test_merge_refuses_to_recompute_the_other_side_dimension(scripted_report):
    other = _retrieval_side_report(scripted_report)
    other["configurations"][0]["metrics"]["mrr"] = 0.9
    with pytest.raises(AblationReportError) as excinfo:
        merge_with_retrieval_report(scripted_report, other)
    assert excinfo.value.code == "ablation_merge_metric_conflict"


def test_merge_refuses_a_report_over_a_different_query_subset(scripted_report):
    other = _retrieval_side_report(scripted_report)
    other["query_subset_hash"] = "0" * 64
    with pytest.raises(AblationReportError) as excinfo:
        merge_with_retrieval_report(scripted_report, other)
    assert excinfo.value.code == "ablation_merge_key_mismatch"


# ---------------------------------------------------------------- command


def test_command_runs_every_configuration_and_writes_the_report(tmp_path):
    from fin.eval.ablation.__main__ import main

    out = tmp_path / "ablation"
    exit_code = main(
        [
            "run",
            "--eval-set",
            "scripted",
            "--repeats",
            str(B1_MIN_REPEATS),
            "--output",
            str(out),
        ]
    )
    assert exit_code == 0
    report = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert report["summary"]["configuration_count"] == B1_CONFIGURATION_TOTAL
    assert report["summary"]["missing_configuration_count"] == 0
    assert report["summary"]["configurations_missing_any_metric"] == 0
    assert report["summary"]["missing_contribution_count"] == 0
    assert report["summary"]["percent_only_claim_count"] == 0
    assert validate_report(report) == []


def test_command_rejects_repeats_below_the_b1_floor(tmp_path):
    from fin.eval.ablation.__main__ import main

    exit_code = main(
        [
            "run",
            "--eval-set",
            "scripted",
            "--repeats",
            str(B1_MIN_REPEATS - 1),
            "--output",
            str(tmp_path / "ablation"),
        ]
    )
    assert exit_code == 2
    assert not (tmp_path / "ablation" / "report.json").exists()


def test_command_prints_the_matrix(capsys):
    from fin.eval.ablation.__main__ import main

    assert main(["matrix"]) == 0
    printed = capsys.readouterr().out
    assert printed.count("\n") >= B1_CONFIGURATION_TOTAL
    assert "all_on" in printed and "all_off" in printed


def test_the_real_dataset_run_is_opt_in_only():
    """The dataset-backed run of this same command is ``eval``-marked.

    "默认 ``uv run pytest -q`` 不收集该命令" is enforced by the
    ``tests/eval`` conftest for marked items; this asserts the marker is
    actually on the module that reads the 234-sample dataset.
    """
    path = PROJECT_ROOT / "tests" / "eval" / "test_ablation_matrix.py"
    spec = importlib.util.spec_from_file_location("_t22_eval_module", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    marks = module.pytestmark
    names = {mark.name for mark in (marks if isinstance(marks, list) else [marks])}
    assert "eval" in names


def test_the_scripted_eval_set_reads_no_corpus_bytes():
    """The default-lane set is in-code: no dataset, no extracted tree."""
    queries, port, known_ids = scripted_eval_set()
    assert 1 <= len(queries) <= B1_QUERY_SUBSET_MAX
    assert all(isinstance(q, EvalQuery) for q in queries)
    assert known_ids
    result = port.retrieve_for_eval_with_arms(
        queries[0].question,
        capabilities={key: True for key in load_capability_keys()},
        limit=20,
    )
    assert result["chunks"]
    assert len(result["arms"]) == len(result["chunks"])
    assert isinstance(result["chunks"][0].source_ref, Locator)


def test_matrix_error_family_is_one_tree():
    assert issubclass(AblationMatrixDriftError, AblationMatrixError)
    assert issubclass(AblationSpecRowMissingError, AblationMatrixError)
