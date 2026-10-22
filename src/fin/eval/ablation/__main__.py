"""CLI: ``uv run python -m fin.eval.ablation <command>`` (T22, opt-in).

Commands
--------
``run``
    Run all 10 §B1 configurations over the eval subset, repeat each at
    least 3 times, and write ``report.json`` under ``--output``. Exit 0
    only when the report passes :func:`validate_report` — a report with
    a missing configuration, a missing metric, a missing contribution or
    a bare percentage is a failed run, not a written file with a
    footnote.

    ``--eval-set`` selects the eval set:
      * ``dataset`` (default): the committed T10 subset, backend =
        ``contract_fake`` (SC2).
      * ``scripted``: the in-code smoke set, backend = ``contract_fake``.
      * ``live`` (T3, opt-in): drives a real ``MCPRetrieveAdapter``
        through the ablation seam; backend = ``"mcp"``. iknow down is
        a named ``retrieve_unavailable`` failure and exits non-zero;
        a ``degraded: true`` envelope with no chunks is the same named
        failure. A successful live run writes a
        ``backend.kind == "mcp"`` report.
``matrix``
    Print the 10 configurations without running anything.

Opt-in means opt-in: nothing here is imported by the product path, and
the default ``uv run pytest -q`` neither runs nor collects it (the
dataset-backed contract test carries the ``eval`` mark).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from fin.eval.ablation.matrix import (
    B1_MIN_REPEATS,
    B1_QUERY_SUBSET_MAX,
    AblationMatrixError,
    build_ablation_matrix,
)
from fin.eval.ablation.metrics import METRIC_FIELDS, MetricEnumerationError
from fin.eval.ablation.report import (
    REPORT_FILENAME,
    build_report,
    validate_report,
    write_report_atomically,
)
from fin.eval.ablation.runner import AblationRunError, run_ablation


# Re-exported at module level so tests can monkeypatch the factory
# without rerunning the live seam (which would spawn an MCP subprocess
# in the default lane). The default implementation is the live path;
# the CLI never instantiates a real ``MCPRetrieveAdapter`` itself —
# :func:`fin.eval.ablation.live_set.live_eval_set` does, only when
# ``--eval-set live`` is selected.
from fin.eval.ablation.live_set import live_eval_set  # noqa: E402

DEFAULT_OUTPUT_DIR = Path("data/eval/ablation")


def _print_summary(report: dict) -> None:
    summary = report["summary"]
    dataset = report["dataset"]
    print(
        f"dataset: {dataset['dataset_id']} "
        f"({dataset['query_subset_size']} questions, "
        f"subset {dataset['query_subset_hash'][:12]})"
    )
    print(
        f"configurations: {summary['configuration_count']} "
        f"(missing {summary['missing_configuration_count']}); "
        f"missing-metric configs {summary['configurations_missing_any_metric']}; "
        f"repeats below floor {summary['repeats_below_floor_count']}"
    )
    print(
        f"improvement claims: {summary['improvement_claim_count']} "
        f"(percent-only {summary['percent_only_claim_count']}, "
        f"undefined percent {summary['claims_with_undefined_percent']})"
    )
    print(
        f"capability contributions: {summary['contribution_count']} "
        f"(missing {summary['missing_contribution_count']})"
    )
    header = "configuration".ljust(22) + "".join(
        field[:14].rjust(16) for field in METRIC_FIELDS[:6]
    )
    print(header)
    for config in report["configurations"]:
        row = config["configuration_id"].ljust(22)
        for field in METRIC_FIELDS[:6]:
            row += f"{config['metrics'][field]['median']:.4f}".rjust(16)
        print(row)
    for gap in dataset.get("known_gaps", []):
        print(f"known gap: {gap.get('gap')}: {gap.get('detail')}")
    for caveat in report.get("caveats", []):
        print(f"caveat: {caveat}")


def _run(args: argparse.Namespace) -> int:
    if args.eval_set == "scripted":
        from fin.eval.ablation.scripted_set import SCRIPTED_METADATA, scripted_eval_set

        queries, port, known_document_ids = scripted_eval_set()
        metadata = dict(SCRIPTED_METADATA)
    elif args.eval_set == "live":
        queries, port, known_document_ids, metadata = live_eval_set()
    else:
        from fin.eval.ablation.dataset_set import dataset_eval_set

        queries, port, known_document_ids, metadata = dataset_eval_set(
            size=args.queries
        )

    try:
        run = run_ablation(
            port,
            queries,
            repeats=args.repeats,
            limit=args.limit,
            known_document_ids=known_document_ids,
            metadata=metadata,
        )
    except (AblationRunError, AblationMatrixError, MetricEnumerationError) as exc:
        print(f"ablation run refused: {getattr(exc, 'code', 'error')}: {exc}")
        return 2

    # Live path: every query errored with the same typed code is the
    # SC4「live unreachable」 signal — refuse to write a 「done with
    # empty chunks」 report (SC4 says: never ``{chunks: []}`` as
    # success). The named error code is the operator-facing signal.
    if args.eval_set == "live":
        codes = {
            code
            for config in run.configurations
            for code in config.error_codes
        }
        live_codes = {"retrieve_unavailable", "retrieve_degraded"}
        transport_failure = codes & live_codes
        if transport_failure and all(
            config.error_codes and set(config.error_codes) <= transport_failure
            for config in run.configurations
            if config.error_codes
        ):
            named = ", ".join(sorted(transport_failure))
            print(f"ablation run refused (live): {named}: iknow unreachable")
            return 4

    report = build_report(run)
    problems = validate_report(report)
    if problems:
        for problem in problems:
            print(f"  PROBLEM {problem}")
        print("ablation: FAILED")
        return 1
    _print_summary(report)

    out_dir = Path(args.output)
    report_path = out_dir / REPORT_FILENAME
    write_report_atomically(report_path, report)
    print(f"report written to {report_path}")
    print("ablation: OK (缺失配置数 = 0，任一指标缺失的配置数 = 0)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fin.eval.ablation")
    sub = parser.add_subparsers(dest="command", required=True)
    run_parser = sub.add_parser("run", help="run the full ablation matrix and write the report")
    run_parser.add_argument(
        "--eval-set",
        choices=("dataset", "scripted", "live"),
        default="dataset",
        help=(
            "dataset: the committed T10 subset (default; backend = contract_fake); "
            "scripted: the in-code smoke set (backend = contract_fake); "
            "live: opt-in MCP retrieve (backend = mcp; iknow unreachable = "
            "retrieve_unavailable, exit 4)"
        ),
    )
    run_parser.add_argument(
        "--repeats",
        type=int,
        default=B1_MIN_REPEATS,
        help=f"repeats per configuration (§B1 floor: {B1_MIN_REPEATS})",
    )
    run_parser.add_argument(
        "--queries",
        type=int,
        default=B1_QUERY_SUBSET_MAX,
        help="question count for the dataset subset (§B1 band: 20–40)",
    )
    run_parser.add_argument("--limit", type=int, default=20, help="hits per query")
    run_parser.add_argument("--output", default=str(DEFAULT_OUTPUT_DIR))
    sub.add_parser("matrix", help="print the 10 configurations")

    args = parser.parse_args(argv)

    if args.command == "run":
        return _run(args)

    if args.command == "matrix":
        for config in build_ablation_matrix():
            off = [key for key, on in config.capabilities.items() if not on]
            print(
                f"{config.configuration_id:<24} kind={config.kind:<11} "
                f"off={off or ['-']}"
            )
        return 0

    parser.error(f"unknown command {args.command!r}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
