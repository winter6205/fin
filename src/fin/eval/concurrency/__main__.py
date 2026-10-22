"""Opt-in command line entry point for T23.

Examples
--------
``uv run python -m fin.eval.concurrency l1 --duration 0.2 --max-requests 1200``
    A short, explicitly labelled L1 smoke run.

``uv run python -m fin.eval.concurrency l2 --llm real``
    The real LLM lane. It refuses with a named skip when ``FIN_API_KEY`` is
    absent; it never silently substitutes the fake lane.

The no-argument duration is the exact §B1 duration. Overrides are intended
only for smoke runs and are preserved in the JSON report.
"""

from __future__ import annotations

import argparse
import json
import os
import uuid
from pathlib import Path
from typing import Any, Callable

from fin.eval.ablation.matrix import B1_CONCURRENCY_CONTRACT
from fin.eval.ablation.scripted_set import scripted_eval_set
from fin.eval.concurrency.runner import (
    ConcurrencyRunError,
    L2BackendSetupError,
    L2CredentialsMissingError,
    ReportWriteError,
    build_contract_fake_push_records,
    run_idempotency,
    run_l1,
    run_l2,
    validate_idempotency_report,
    validate_l1_report,
    validate_l2_report,
)
from fin.eval.concurrency.supplements import (
    SupplementError,
    citation_gate_dimension_report,
    duplicate_submission_dimension_report,
    first_hit_dimension_report,
    refusal_rate_dimension_report,
    validate_supplement_report,
)
from fin.host.grounded.citation_gate import DoneResult, EvidenceItem, verify_citations
from fin.retrieve.fake_push_port import FakePushPort
from fin.retrieve.push_port import push_records


DEFAULT_OUTPUT_DIR = Path("data/eval/concurrency")
_REPORT_FILENAMES = {
    "l1": "l1.json",
    "l2": "l2.json",
    "idempotency": "idempotency.json",
    "citation-gate": "citation_gate.json",
    "refusal-rate": "refusal_rate.json",
    "first-hit": "first_hit.json",
    "duplicate-submission": "duplicate_submission.json",
}


def _write_report(output: Path, filename: str, report: dict[str, Any]) -> Path:
    target_dir = output
    target = target_dir / filename
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        raise ReportWriteError(
            f"cannot write T23 report {target}: {type(exc).__name__}: {exc}"
        ) from exc
    return target


def _known_gaps() -> list[dict[str, Any]]:
    from fin.corpus.dataset.build import default_paths

    manifest_path = default_paths()[2] / "manifest.json"
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReportWriteError(
            f"cannot read dataset known_gaps from {manifest_path}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    gaps = payload.get("known_gaps", [])
    if not isinstance(gaps, list):
        raise ReportWriteError(
            f"dataset known_gaps is not a list in {manifest_path}"
        )
    return gaps


def _l1_smoke_or_run(args: argparse.Namespace):
    queries, retrieve_port, _ = scripted_eval_set()
    push_port = FakePushPort()
    records = build_contract_fake_push_records()
    push_records(push_port, records)
    query = queries[0].question

    def read() -> dict[str, Any]:
        return retrieve_port.retrieve(query)

    def write() -> Any:
        return push_records(push_port, records)

    result = run_l1(
        read_operation=read,
        write_operation=write,
        block_count=push_port.block_total,
        duration_seconds=args.duration,
        warmup_requests=args.warmup_requests,
        max_requests=args.max_requests,
    )
    report = result.to_dict()
    report["backend"] = {
        "kind": "contract_fake",
        "retrieval": "fin.retrieve.fake_capability_port",
        "push": "fin.retrieve.fake_push_port",
    }
    report["known_gaps"] = _known_gaps()
    return result, report


class _ScriptedFullRun:
    """A complete retrieval → fake LLM → citation-gate run for L2 structure."""

    def __init__(self, retrieve_port: Any, known_document_ids: set[str]) -> None:
        self._retrieve_port = retrieve_port
        self._known_document_ids = known_document_ids
        self.llm_calls = 0

    def __call__(self) -> dict[str, Any]:
        payload = self._retrieve_port.retrieve("total net sales for the quarter")
        chunks = payload["chunks"]
        self.llm_calls += 1
        answer = chunks[-1].text if chunks else "No grounded answer."
        evidence = [
            EvidenceItem(locator=chunk.source_ref, unit_text=chunk.text)
            for chunk in chunks
        ]
        gate_result = verify_citations(
            answer,
            evidence,
            0,
            known_document_ids=self._known_document_ids,
        )
        return {
            "status": "done" if isinstance(gate_result, DoneResult) else "refuse",
            "answer": answer,
        }


def _real_full_run_factory() -> Callable[[], Any]:
    from fin.host.compile import FIN_API_KEY_ENV, compile_for_runtime_dev

    if not os.environ.get(FIN_API_KEY_ENV):
        raise L2CredentialsMissingError(
            f"{FIN_API_KEY_ENV} is not set; the real L2 lane will not "
            "substitute the fake LLM"
        )
    try:
        graph = compile_for_runtime_dev(env=os.environ)
    except Exception as exc:
        raise L2BackendSetupError(
            f"could not construct the real L2 graph: "
            f"{type(exc).__name__}: {exc}"
        ) from exc

    def full_run() -> Any:
        from langchain_core.messages import HumanMessage

        return graph.invoke(
            {
                "messages": [HumanMessage(content="What was total revenue?")],
                "evidence": [],
                "repair_count": 0,
                "step_count": 0,
            },
            config={
                "configurable": {
                    "thread_id": f"t23-l2-{uuid.uuid4().hex}"
                }
            },
        )

    return full_run


def _l2_smoke_or_run(args: argparse.Namespace):
    scripted: _ScriptedFullRun | None = None
    if args.llm == "real":
        full_run = _real_full_run_factory()
        mode = "real"
        backend: dict[str, Any] = {
            "kind": "production_graph",
            "module": "fin.host.compile.compile_for_runtime_dev",
        }
    else:
        _, retrieve_port, known_document_ids = scripted_eval_set()
        scripted = _ScriptedFullRun(retrieve_port, known_document_ids)
        full_run = scripted
        mode = "fake"
        backend = {
            "kind": "contract_fake",
            "module": "fin.eval.concurrency.__main__._ScriptedFullRun",
            "llm_calls": scripted,
        }

    result = run_l2(
        full_run=full_run,
        llm_mode=mode,
        duration_seconds=args.duration,
        warmup_requests=args.warmup_requests,
        max_requests=args.max_requests,
    )
    report = result.to_dict()
    # ``scripted`` is bound in exactly the branch that sets mode="fake",
    # so the guard is always true here; it narrows the callable union so
    # the count is read off the object that owns ``llm_calls``.
    if mode == "fake" and scripted is not None:
        backend["llm_calls"] = scripted.llm_calls
    report["backend"] = backend
    report["known_gaps"] = _known_gaps()
    report["caveats"] = (
        ["fake LLM structural path; no real LLM call was made"]
        if mode == "fake"
        else []
    )
    return result, report


def _idempotency_smoke_or_run(args: argparse.Namespace):
    port = FakePushPort()
    records = build_contract_fake_push_records()
    push_records(port, records)
    result = run_idempotency(
        write_operation=lambda: push_records(port, records),
        block_count=port.block_total,
        duration_seconds=args.duration,
        warmup_requests=args.warmup_requests,
        max_requests=args.max_requests,
    )
    report = result.to_dict()
    report["backend"] = {
        "kind": "contract_fake",
        "module": "fin.retrieve.fake_push_port",
    }
    report["known_gaps"] = _known_gaps()
    return result, report


def _scenario_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--duration",
        type=float,
        default=None,
        help="explicit smoke duration; omitted means the §B1 duration",
    )
    parser.add_argument("--warmup-requests", type=int, default=0)
    parser.add_argument("--max-requests", type=int, default=None)
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_DIR))


def _print_pressure_result(
    name: str,
    report: dict[str, Any],
    problems: list[str],
    target: Path,
) -> int:
    print(
        f"{name}: requests={report.get('workload', {}).get('measured_requests')} "
        f"errors={report.get('errors', {}).get('count')} "
        f"report={target}"
    )
    if report.get("report_mode") == "smoke":
        print("T23: SMOKE ONLY (not a B1-duration pass)")
        for problem in problems:
            print(f"  smoke note: {problem}")
        return 0 if report.get("errors", {}).get("count") == 0 else 1
    if problems:
        for problem in problems:
            print(f"  PROBLEM {problem}")
        print(f"{name}: FAILED")
        return 1
    print(f"{name}: OK")
    return 0


def _supplement_command(
    command: str, output: Path
) -> int:
    builders = {
        "citation-gate": citation_gate_dimension_report,
        "refusal-rate": refusal_rate_dimension_report,
        "first-hit": first_hit_dimension_report,
        "duplicate-submission": duplicate_submission_dimension_report,
    }
    report = builders[command]()
    target = _write_report(output, _REPORT_FILENAMES[command], report)
    print(
        f"{command}: missing_dimension_count="
        f"{report['missing_dimension_count']} report={target}"
    )
    problems = validate_supplement_report(report)
    for problem in problems:
        print(f"  PROBLEM {problem}")
    return 0 if not problems else 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fin.eval.concurrency")
    sub = parser.add_subparsers(dest="command", required=True)
    for command, help_text in (
        ("l1", "run concurrent retrieval/read workload"),
        ("idempotency", "run concurrent all-write idempotency workload"),
    ):
        command_parser = sub.add_parser(command, help=help_text)
        _scenario_args(command_parser)
    l2_parser = sub.add_parser("l2", help="run concurrent complete runs")
    l2_parser.add_argument("--llm", choices=("fake", "real"), default="fake")
    _scenario_args(l2_parser)
    for command in (
        "citation-gate",
        "refusal-rate",
        "first-hit",
        "duplicate-submission",
    ):
        command_parser = sub.add_parser(command, help=f"write {command} dimension")
        command_parser.add_argument("--output", default=str(DEFAULT_OUTPUT_DIR))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    output = Path(args.output)
    try:
        if args.command == "l1":
            measured, result = _l1_smoke_or_run(args)
            target = _write_report(output, _REPORT_FILENAMES["l1"], result)
            return _print_pressure_result(
                "l1", result, validate_l1_report(measured), target
            )
        if args.command == "l2":
            measured, result = _l2_smoke_or_run(args)
            target = _write_report(output, _REPORT_FILENAMES["l2"], result)
            return _print_pressure_result(
                "l2", result, validate_l2_report(measured), target
            )
        if args.command == "idempotency":
            measured, result = _idempotency_smoke_or_run(args)
            target = _write_report(
                output, _REPORT_FILENAMES["idempotency"], result
            )
            return _print_pressure_result(
                "idempotency",
                result,
                validate_idempotency_report(measured),
                target,
            )
        return _supplement_command(args.command, output)
    except L2CredentialsMissingError as exc:
        report = {
            "scenario": "l2",
            "status": "skipped",
            "skip": {"code": exc.code, "reason": str(exc)},
            "llm": {"mode": "real", "real_call_attempted": False},
            "spec_basis": {
                "source": "specs/corpus-retrieval.md §B1",
                "concurrency": B1_CONCURRENCY_CONTRACT.l2.concurrency,
                "duration_seconds": B1_CONCURRENCY_CONTRACT.l2.duration_seconds,
                "p50_operator": B1_CONCURRENCY_CONTRACT.l2.p50_operator,
                "p50_limit_ms": B1_CONCURRENCY_CONTRACT.l2.p50_ms,
                "p95_operator": B1_CONCURRENCY_CONTRACT.l2.p95_operator,
                "p95_limit_ms": B1_CONCURRENCY_CONTRACT.l2.p95_ms,
                "l1_latency_threshold_applied": False,
            },
        }
        target = _write_report(output, _REPORT_FILENAMES["l2"], report)
        print(f"l2: SKIPPED ({exc.code}) report={target}")
        return 3
    except (ConcurrencyRunError, SupplementError) as exc:
        print(f"T23: FAILED ({getattr(exc, 'code', 'error')}): {exc}")
        return 2

if __name__ == "__main__":
    raise SystemExit(main())
