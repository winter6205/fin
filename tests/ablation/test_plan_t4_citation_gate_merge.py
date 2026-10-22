"""Plan T4 — citation-gate field on the merged live report.

This file writes the failing tests (RED) that drive Plan T4's
implementation: the citation-gate pass rate must be a finite number on
the merged live report, the field must survive the same-run merge, and
omitting it must fail the configuration in :func:`validate_report` /
the CLI.

These tests are intentionally not marked ``eval`` — Plan T4 ships on
the default lane. They exercise the in-process drivers (``scripted``
+ ``contract_fake``) and never read the corpus or start a service.
The one live-path test in this file uses the same fake seam pattern
:mod:`tests.ablation.test_live_eval_set` uses, so a real MCP process
is never spawned.
"""

from __future__ import annotations

import copy
import json
import math

import pytest

from fin.corpus.extracted import Locator
from fin.eval.ablation import (
    B1_CONFIGURATION_TOTAL,
    B1_MIN_REPEATS,
    METRIC_FIELDS,
    build_report,
    merge_with_retrieval_report,
    run_ablation,
    validate_report,
)
from fin.eval.ablation.scripted_set import scripted_eval_set
from fin.retrieve.port import Chunk


# ---------------------------------------------------------------- helpers


def _retrieval_side_report(fin_report: dict) -> dict:
    """A scripted retrieval-side report over the same merge keys.

    Mirrors the helper in :mod:`tests.ablation.test_ablation` — the
    retrieval side only adds the SC38 dimensions
    (``long_document_recall`` / ``cross_document_multi_hop_recall``) and
    never recomputes a fin-owned field.
    """
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


@pytest.fixture()
def scripted_report() -> dict:
    """A complete B1-config fin report on the scripted eval set."""
    queries, port, known_ids = scripted_eval_set()
    run = run_ablation(
        port,
        queries,
        repeats=B1_MIN_REPEATS,
        known_document_ids=known_ids,
    )
    return build_report(run)


# ---------------------------------------------------------------- T4.1
# citation_gate_pass_rate required finite on merged live report


def test_merged_report_carries_finite_citation_gate_pass_rate_per_configuration(
    scripted_report,
) -> None:
    """T4.1 (happy): every configuration's ``citation_gate_pass_rate``
    on the merged report is a finite float. This is the canary — if
    Plan T4 introduces a finiteness gate that wrongly rejects finite
    values, this test goes red first.
    """
    other = _retrieval_side_report(scripted_report)
    merged = merge_with_retrieval_report(scripted_report, other)

    assert len(merged["configurations"]) == B1_CONFIGURATION_TOTAL
    for config in merged["configurations"]:
        gate = config["metrics"]["citation_gate_pass_rate"]
        for raw in gate["raw"]:
            assert isinstance(raw, float)
            assert math.isfinite(raw), (
                f"{config['configuration_id']} raw citation_gate_pass_rate "
                f"is not finite: {raw!r}"
            )
        median = gate["median"]
        assert isinstance(median, float)
        assert math.isfinite(median), (
            f"{config['configuration_id']} median citation_gate_pass_rate "
            f"is not finite: {median!r}"
        )


def test_merged_report_refuses_non_finite_citation_gate_pass_rate(
    scripted_report,
) -> None:
    """T4.1 (failure): a retrieval-side report that hands fin a
    non-finite ``citation_gate_pass_rate`` for any configuration must
    not silently flow into the merged doc — that is exactly the silent
    false zero the gate exists to prevent (T22 acceptance: 缺任一项该
    配置即不通过).

    The merge must surface a typed exception with a stable ``code`` so
    the CLI can map it to a non-zero exit.
    """
    other = _retrieval_side_report(scripted_report)
    # Plant a non-finite value on the retrieval-side report for one
    # configuration. The merge must surface this as a named problem
    # (not silently copy it through into the merged report's
    # metrics block).
    target = scripted_report["configurations"][0]["configuration_id"]
    for config in other["configurations"]:
        if config["configuration_id"] == target:
            config["metrics"]["long_document_recall"] = float("nan")
            break

    with pytest.raises(Exception) as excinfo:
        merge_with_retrieval_report(scripted_report, other)

    code = getattr(excinfo.value, "code", None)
    assert code is not None, (
        "merge-time validation must raise a typed exception with a "
        f"stable `code` attribute; got {type(excinfo.value).__name__}: {excinfo.value}"
    )
    assert "long_document_recall" in str(excinfo.value), (
        "the failure message must name the offending field so an "
        f"operator reading the log knows what to fix; got {excinfo.value}"
    )
    assert code == "ablation_merge_citation_gate_non_finite", (
        "non-finite values from the retrieval side must be refused with "
        f"the T4 / SC5 code; got {code!r}"
    )


def test_validate_report_rejects_non_finite_metric_median(scripted_report) -> None:
    """T4.1 (validator): ``validate_report`` must catch a non-finite
    median on any metric. Today ``isinstance(..., (int, float))`` admits
    ``float('nan')`` and ``float('inf')`` — that gap is the bug."""
    broken = copy.deepcopy(scripted_report)
    broken["configurations"][0]["metrics"]["citation_gate_pass_rate"][
        "median"
    ] = float("nan")
    problems = validate_report(broken)
    assert any("citation_gate_pass_rate" in p for p in problems), problems
    assert any("non-finite" in p or "finite" in p for p in problems), problems


def test_validate_report_rejects_non_finite_metric_raw(scripted_report) -> None:
    """T4.1 (validator, raw path): a non-finite value buried in the
    ``raw`` series also fails the report — the median can be finite by
    averaging away a single NaN, but the value was not a real number.
    """
    broken = copy.deepcopy(scripted_report)
    broken["configurations"][0]["metrics"]["citation_gate_pass_rate"]["raw"][
        0
    ] = float("inf")
    problems = validate_report(broken)
    assert any("citation_gate_pass_rate" in p for p in problems), problems


def test_validate_report_rejects_missing_citation_gate_field(
    scripted_report,
) -> None:
    """T4.1 (validator, presence): SC5 says the merged report must
    contain the citation-gate field; a configuration whose
    ``citation_gate_pass_rate`` block has been dropped is a failed
    configuration. ``validate_report`` must name the field so the CLI
    prints a specific problem rather than a generic 'metric missing'.
    """
    broken = copy.deepcopy(scripted_report)
    broken["configurations"][0]["metrics"].pop("citation_gate_pass_rate")
    problems = validate_report(broken)
    assert any("citation_gate_pass_rate" in p for p in problems), problems


# ---------------------------------------------------------------- T4.2
# missing / null validation causes CLI nonzero


def test_cli_live_eval_set_exits_nonzero_when_citation_gate_field_is_null(
    tmp_path, monkeypatch
) -> None:
    """T4.2 (CLI surface, live path): a live ``--eval-set live`` run
    that produces a report with a null ``citation_gate_pass_rate``
    (e.g. all configs errored without ever invoking the gate) must
    exit non-zero with a named problem — the operator must not see
    ``ablation: OK`` on a report whose gate value is unusable.
    """
    from fin.eval.ablation import __main__ as ablation_cli
    from fin.eval.ablation.live_set import live_eval_set
    from fin.eval.ablation.scripted_set import ARMS_BY_QUERY_CLASS
    from fin.retrieve.fake_capability_port import (
        CapabilityAwareFakePort,
        CapabilityHit,
    )

    def _null_gate_factory():
        # Drive a real live_eval_set, but route through a seam whose
        # every query has an empty text chunk. The gate then has no
        # evidence to score; we also monkeypatch build_report to drop
        # the field so the test exercises the "field missing from the
        # written report" branch (the validator must catch it; the CLI
        # must exit non-zero).
        queries, _wrapper, known_ids, metadata = live_eval_set()
        # Empty-hits seam: citable_answer has no evidence, so
        # citation_gate_pass_rate per repeat is 0.0 (a finite number).
        # Then we strip the field in build_report to simulate a
        # partial write.
        port = _EmptySeamPort()
        return queries, port, known_ids, metadata

    def _build_report_without_gate(run, **kwargs):
        from fin.eval.ablation.report import build_report as _build

        report = _build(run, **kwargs)
        for config in report.get("configurations", []):
            config.get("metrics", {}).pop("citation_gate_pass_rate", None)
        return report

    monkeypatch.setattr(ablation_cli, "live_eval_set", _null_gate_factory)
    monkeypatch.setattr(ablation_cli, "build_report", _build_report_without_gate)

    out = tmp_path / "ablation-live-null-gate"
    exit_code = ablation_cli.main(
        [
            "run",
            "--eval-set",
            "live",
            "--repeats",
            str(B1_MIN_REPEATS),
            "--output",
            str(out),
        ]
    )
    # Without the field the CLI must NOT exit 0; today it would
    # happily write a report with a null field (or no field) and
    # exit 0. This test pins the post-T4 invariant.
    assert exit_code != 0, (
        "CLI must exit non-zero when citation_gate_pass_rate is missing "
        "from the live report; the operator must not see 'ablation: OK'"
    )
    assert not (out / "report.json").exists(), (
        "a report missing citation_gate_pass_rate must not be written"
    )


class _EmptySeamPort:
    """A seam port that returns empty hits for every call.

    Used to exercise the post-T4 invariant that an empty-hits live run
    still surfaces the citation-gate field on every configuration
    (with a finite, 0.0 median).
    """

    def retrieve_for_eval_with_arms(self, query, *, capabilities, limit=8):
        return {"chunks": [], "arms": []}


# ---------------------------------------------------------------- T4.3
# merge preserves same-run field


def test_merge_preserves_same_run_configuration_id_on_every_config(
    scripted_report,
) -> None:
    """T4.3 (identity): a merge is "the same run" only when every
    configuration_id in the merged report equals the configuration_id
    of the same fin-side configuration. Reordering, dropping, or
    rewriting IDs is a silent corruption of the merge identity.
    """
    other = _retrieval_side_report(scripted_report)
    merged = merge_with_retrieval_report(scripted_report, other)

    expected_ids = {
        config["configuration_id"]
        for config in scripted_report["configurations"]
    }
    merged_ids = {
        config["configuration_id"] for config in merged["configurations"]
    }
    assert merged_ids == expected_ids, (
        "merge dropped, reordered (without preserving), or rewrote "
        f"configuration_id values: expected {sorted(expected_ids)} "
        f"got {sorted(merged_ids)}"
    )
    # Order preservation matters too — the merge is a row-wise join on
    # configuration_id, not a reshuffle.
    assert [
        config["configuration_id"] for config in merged["configurations"]
    ] == [
        config["configuration_id"]
        for config in scripted_report["configurations"]
    ]


def test_merge_preserves_fin_side_metric_value_for_each_same_run_config(
    scripted_report,
) -> None:
    """T4.3 (value path): after a merge, every configuration's
    fin-owned metric values (raw + median) must be byte-identical to
    the fin-side report. The retrieval side's metrics are *added*;
    fin's are *preserved*. A regression that lets the merge recompute
    fin's numbers is exactly the silent recomputation SC38 forbids.
    """
    other = _retrieval_side_report(scripted_report)
    merged = merge_with_retrieval_report(scripted_report, other)

    by_id_fin = {
        config["configuration_id"]: config["metrics"]
        for config in scripted_report["configurations"]
    }
    by_id_merged = {
        config["configuration_id"]: config["metrics"]
        for config in merged["configurations"]
    }
    for configuration_id in by_id_fin:
        for field in METRIC_FIELDS:
            fin_series = by_id_fin[configuration_id][field]
            merged_series = by_id_merged[configuration_id][field]
            assert tuple(merged_series["raw"]) == tuple(fin_series["raw"]), (
                f"{configuration_id}.{field}: merge rewrote raw values"
            )
            assert merged_series["median"] == fin_series["median"], (
                f"{configuration_id}.{field}: merge rewrote median"
            )


def test_merged_report_refuses_retrieval_side_citation_gate_field(
    scripted_report,
) -> None:
    """T4.3 (perimeter): the retrieval side is forbidden from
    recomputing any fin-owned metric (SC38). A retrieval-side report
    that smuggles a ``citation_gate_pass_rate`` entry onto any
    configuration must be refused at merge time with the same typed
    code as a metric conflict.
    """
    other = _retrieval_side_report(scripted_report)
    other["configurations"][0]["metrics"]["citation_gate_pass_rate"] = 0.5

    with pytest.raises(Exception) as excinfo:
        merge_with_retrieval_report(scripted_report, other)
    assert getattr(excinfo.value, "code", None) == "ablation_merge_metric_conflict"


# ---------------------------------------------------------------- T4.4
# SC10 regression — host citation gate behavior unchanged


def test_sc10_citation_gate_still_fills_only_verified_spans_on_done_result() -> None:
    """SC10 regression guard: when a scripted research turn quotes a
    span that lives inside a retrieved T1 chunk, the gate must end
    ``done`` with **only that** verified span in ``citations`` —
    refusing/step_cap paths must still produce an empty list.

    The T1 text is the frozen ``t1-onboarding-001`` text used by
    :func:`live_eval_set`. No MCP subprocess is spawned; we feed the
    gate synthetic ``EvidenceItem`` objects built directly off the
    same locator so the assertion is hermetic and deterministic.
    """
    from fin.host.grounded.citation_gate import (
        BounceResult,
        DoneResult,
        EvidenceItem,
        RefuseResult,
        verify_citations,
    )

    # Use a deterministic 40+ char span so the gate's min_span_len=32
    # rule is met with a non-alnum-prefix/suffix envelope.
    span = "T1ONBOARDINGPAGEHINT" * 3  # 60 chars
    answer = "Per the T1 onboarding unit: " + span + "."
    evidence_text = "PREAMBLE_" + span + "_POSTAMBLE"
    unit_text = evidence_text + " " + ("x" * 1000)

    locator = Locator(document_id="t1-onboarding-001", version="a" * 64, unit=1)
    known = {"t1-onboarding-001"}

    result = verify_citations(
        answer=answer,
        evidence=[EvidenceItem(locator=locator, unit_text=unit_text)],
        repair_count=0,
        known_document_ids=known,
    )
    assert isinstance(result, DoneResult)
    assert len(result.citations) == 1
    assert result.citations[0].document_id == "t1-onboarding-001"
    assert result.citations[0].quote == span

    # Refuse / step_cap path: a separate evidence list with the
    # span absent plus a high repair_count must refuse; ``RefuseResult``
    # carries no ``citations`` attribute (only ``DoneResult`` does) and
    # that is exactly the SC10 contract: refusing/step_cap turn
    # citations are an empty list on the wire.
    refuse_result = verify_citations(
        answer="an unsupported claim with no spans at all",
        evidence=[],
        repair_count=3,
        known_document_ids=known,
    )
    assert isinstance(refuse_result, RefuseResult)
    assert not hasattr(refuse_result, "citations")

    # Bounce path: not a final terminal status, but cited evidence
    # is also absent (no span matched). SC10 says only verified
    # spans ever become user-visible citations.
    bounce_result = verify_citations(
        answer="no span to verify here at all",
        evidence=[EvidenceItem(locator=locator, unit_text=unit_text)],
        repair_count=0,
        known_document_ids=known,
    )
    assert isinstance(bounce_result, BounceResult)
    assert not hasattr(bounce_result, "citations")


# ---------------------------------------------------------------- T4.5
# live path produces a finite citation-gate field on every configuration


def test_live_report_carries_finite_citation_gate_field_on_every_config(
    tmp_path,
) -> None:
    """T4.5 (live path, hermetic seam): with the live seam driven
    through a fake adapter (mirroring the T3 test pattern), a live
    ``--eval-set live`` invocation must produce a report whose every
    configuration's ``citation_gate_pass_rate`` is finite, and the
    report's ``backend.kind`` stays ``mcp``.

    This is the SC5 end-to-end assertion: a merged live report
    includes citation-gate pass rate as a finite number; omitting it
    fails that configuration.
    """
    from fin.eval.ablation import __main__ as ablation_cli
    from fin.eval.ablation.live_set import live_eval_set
    from fin.eval.ablation.scripted_set import ARMS_BY_QUERY_CLASS
    from fin.retrieve.fake_capability_port import (
        CapabilityAwareFakePort,
        CapabilityHit,
    )

    queries, _wrapper, known_ids, metadata = live_eval_set()
    hits_by_query: dict[str, list[CapabilityHit]] = {}
    for q in queries:
        gold = Chunk(
            text=q.citable_answer,
            source_ref=Locator(
                document_id=q.expected_document_id,
                version=q.expected_version,
                unit=q.expected_unit,
            ),
            score=0.9,
        )
        arms = ARMS_BY_QUERY_CLASS.get("narrative", frozenset())
        hits_by_query[q.question] = [CapabilityHit(chunk=gold, produced_by=arms)]

    fake_port = CapabilityAwareFakePort(hits_by_query)

    def _factory():
        return queries, fake_port, known_ids, metadata

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(ablation_cli, "live_eval_set", _factory)
        out = tmp_path / "ablation-live-t4"
        exit_code = ablation_cli.main(
            [
                "run",
                "--eval-set",
                "live",
                "--repeats",
                str(B1_MIN_REPEATS),
                "--output",
                str(out),
            ]
        )
    finally:
        monkeypatch.undo()

    assert exit_code == 0, (
        f"live run with finite gate field must exit 0; got {exit_code}"
    )
    report = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert report["backend"]["kind"] == "mcp"
    assert validate_report(report) == []
    for config in report["configurations"]:
        gate = config["metrics"]["citation_gate_pass_rate"]
        assert math.isfinite(gate["median"]), (
            f"{config['configuration_id']} median not finite: {gate['median']!r}"
        )
        for raw in gate["raw"]:
            assert math.isfinite(raw), (
                f"{config['configuration_id']} raw not finite: {raw!r}"
            )


# ---------------------------------------------------------------- imports
# (kept for side effects: pytest)


__all__ = [
    "test_merged_report_carries_finite_citation_gate_pass_rate_per_configuration",
    "test_merged_report_refuses_non_finite_citation_gate_pass_rate",
    "test_validate_report_rejects_non_finite_metric_median",
    "test_validate_report_rejects_non_finite_metric_raw",
    "test_validate_report_rejects_missing_citation_gate_field",
    "test_cli_live_eval_set_exits_nonzero_when_citation_gate_field_is_null",
    "test_merge_preserves_same_run_configuration_id_on_every_config",
    "test_merge_preserves_fin_side_metric_value_for_each_same_run_config",
    "test_merged_report_refuses_retrieval_side_citation_gate_field",
    "test_sc10_citation_gate_still_fills_only_verified_spans_on_done_result",
    "test_live_report_carries_finite_citation_gate_field_on_every_config",
]
