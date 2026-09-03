"""CLI: ``uv run python -m fin.corpus.dataset <command>``.

Commands
--------
``build``
    Rebuild the dataset under ``data/corpus/dataset/`` from the frozen
    tier-1 corpus + extracted tree and write ``manifest.json``.
``verify``
    Rebuild in memory and compare every sample sha256 / byte count and
    the query-set hash against the committed manifest (SC30). Exit 0
    iff mismatch count = 0 and no on-disk drift.
``report``
    Print the counts summary (per-format counts, tiers, hard samples,
    query classes) without rebuilding sample bytes.
``expand-primary``
    Opt-in driver to freeze additional tier-1 EDGAR 10-Q PDFs via the
    existing UA-gated :mod:`fin.corpus.live_fetch`. Never run by
    build / verify / tests; requires ``FIN_EDGAR_USER_AGENT`` and
    explicit ``--cik`` / ``--accession`` pairs.
"""

from __future__ import annotations

import argparse
import json
import sys

from fin.corpus.dataset.manifest import validate_manifest
from fin.corpus.dataset.build import (
    _load_registry,
    build_dataset,
    default_paths,
    verify_dataset,
)


def _print_report(payload: dict) -> None:
    counts = payload["counts"]
    print(f"dataset_id: {payload['dataset_id']} (schema {payload['schema_version']})")
    print(f"countable samples: {counts['total']}")
    print("by format:")
    for fmt, n in sorted(counts["by_format"].items()):
        hard = counts["hard_by_format"].get(fmt, 0)
        print(f"  {fmt:>5}: {n:>3} (hard: {hard})")
    print(f"by origin: {counts['by_origin']}")
    print(f"by tier: {counts['by_tier']}")
    print(f"by doc_type: {counts['by_doc_type']}")
    if counts["excluded"]:
        print("excluded from counts:")
        for exc in counts["excluded"]:
            print(f"  {exc['sample_id']}: {exc['reason']}")
    queries = payload["queries"]
    print(f"queries: {queries['total']}")
    by_class: dict[str, int] = {}
    for q in queries["items"]:
        by_class[q["query_class"]] = by_class.get(q["query_class"], 0) + 1
    print(f"query classes: {dict(sorted(by_class.items()))}")
    print(f"chunk_total: {payload['chunk_total']} ({payload['chunk_total_status']})")
    if payload["known_gaps"]:
        print("known gaps:")
        for gap in payload["known_gaps"]:
            print(f"  {gap['gap']}: {gap['detail']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fin.corpus.dataset")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("build", help="(re)build the dataset and write manifest.json")
    sub.add_parser("verify", help="rebuild in memory and compare hashes (SC30)")
    sub.add_parser("report", help="print counts summary from the committed manifest")
    expand = sub.add_parser(
        "expand-primary", help="opt-in: freeze additional tier-1 EDGAR 10-Q PDFs"
    )
    expand.add_argument("--cik", action="append", required=True, help="10-digit CIK (repeatable)")
    expand.add_argument(
        "--accession", action="append", required=True, help="accession no. (repeatable, parallel to --cik)"
    )
    args = parser.parse_args(argv)

    corpus_dir, _, dataset_dir = default_paths()

    if args.command == "build":
        payload = build_dataset(corpus_dir=corpus_dir, out_dir=dataset_dir, write=True)
        print(f"dataset written to {dataset_dir}")
        _print_report(payload)
        return 0

    if args.command == "verify":
        report = verify_dataset(corpus_dir=corpus_dir, dataset_dir=dataset_dir)
        print(f"checked samples: {report.checked_samples}")
        print(f"mismatch count: {report.mismatch_count}")
        for m in report.mismatches:
            print(f"  MISMATCH {m}")
        for p in report.problems:
            print(f"  PROBLEM {p}")
        if report.ok:
            print("verify: OK (重跑不一致样本数 = 0)")
            return 0
        print("verify: FAILED")
        return 1

    if args.command == "report":
        manifest_path = dataset_dir / "manifest.json"
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        _, versions = _load_registry(corpus_dir)
        report = validate_manifest(payload, primary_registry=versions)
        _print_report(payload)
        print(f"validation problems: {len(report.problems)}")
        for p in report.problems:
            print(f"  PROBLEM {p}")
        for entry in report.threshold_report:
            mark = "MET " if entry["met"] else "MISS"
            print(f"  [{mark}] {entry['criterion']} — {entry['detail']}")
        return 0 if report.ok else 1

    if args.command == "expand-primary":
        from fin.corpus.dataset.expand_primary import run_expand_primary

        pairs = list(zip(args.cik, args.accession))
        return run_expand_primary(pairs)

    parser.error(f"unknown command {args.command!r}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
