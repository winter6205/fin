#!/usr/bin/env python3
"""Run the ``seed:demo`` chain against the isolated dev stack.

Usage::

    uv run --no-sync python scripts/seed_demo.py                # preview only
    uv run --no-sync python scripts/seed_demo.py --reset --yes   # seed for real

The chain itself lives in :mod:`fin.corpus.demo_seed`; this script is the
composition root and owns exactly three things the library deliberately does
not: the CLI flags, the construction of the concrete ports, and the exit code.

Exit codes
----------
``0`` the run finished (a preview included); ``2`` a guard refusal -- unset
``IKNOW_MEMORY_HOME``, the default memory home, a non-dev port, a storage
backend that does not persist, or an embedding generation mismatch; ``1``
every other named failure. The mapping lives on the exception classes
(:func:`fin.corpus.demo_seed.exit_code_for`), so it cannot drift with print
order.

Required environment
--------------------
``IKNOW_MEMORY_HOME`` names the dev stack, and ``GRAPHRAG_MEMORY_DB_URL`` /
``GRAPHRAG_MEMORY_ES_URL`` / ``GRAPHRAG_MEMORY_NEO4J_URI`` name its ports. The
guard refuses anything that is not 5434 / 9201 / 7688.
``GRAPHRAG_MEMORY_STORAGE`` must name the persisting backend the MCP resolves;
unset means iknow's in-memory default, which accepts rows and stores none.

No credential or connection string is read for its own sake, printed, or logged
here: the guard reports host and port only, and every store URL is taken from
the environment the operator already exported.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import signal
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import psycopg

from fin.corpus.demo_seed import (
    DB_URL_ENV,
    DEMO_FIXTURE_DIR,
    DEFAULT_POLL_INTERVAL_SECONDS,
    DEFAULT_POLL_TIMEOUT_SECONDS,
    ES_URL_ENV,
    MEMORY_HOME_ENV,
    SEED_RESULT_FAILED,
    SEED_RESULT_REFUSED,
    DemoSeedDeps,
    DemoSeedError,
    DemoSeedOptions,
    DemoWorkerUnavailableError,
    ElasticsearchIndexReset,
    JobOutcome,
    Neo4jGraphReset,
    PlanADevStackAdapter,
    PostgresEmbedProbe,
    PostgresStoreReset,
    SubprocessWorker,
    WorkerHandle,
    exit_code_for,
    format_seed_result,
    run_demo_seed,
    worker_command,
)
from fin.host.compile import DEFAULT_MCP_COMMAND
from fin.retrieve.mcp_index_job_port import MCPIndexJobPort
from fin.retrieve.mcp_session import MCPStdioSession
from fin.retrieve.mcp_push_port import MCPPushPort

#: The paired repository the Plan A scripts and the worker run from. It is a
#: command-line argument, never a machine path baked into ``src/``.
DEFAULT_IKNOW_ROOT_ENV: str = "FIN_IKNOW_ROOT"


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="seed_demo.py",
        description=(
            "Seed the ZEUS demo corpus into the isolated dev stack "
            "(5434 / 9201 / 7688). Prints a preview unless --yes is given."
        ),
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help=(
            "Truncate Plan A's data-table allowlist, delete the ES indices, "
            "clear the Neo4j graph and drop the catalog demo rows. Requires "
            "--yes; a preview never writes."
        ),
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Perform the writes. Without it the run only prints the plan.",
    )
    parser.add_argument(
        "--fixture-dir",
        default=None,
        help="Demo corpus directory; defaults to fixtures/demo-corpus.",
    )
    parser.add_argument(
        "--catalog-path",
        default=None,
        help="Catalog file to write; defaults to the development catalog.",
    )
    parser.add_argument(
        "--iknow-root",
        default=None,
        help=(
            "The paired iknow-memory checkout the npm scripts and the worker "
            f"run from; defaults to ${DEFAULT_IKNOW_ROOT_ENV}."
        ),
    )
    parser.add_argument(
        "--mcp-command",
        default=None,
        help="The knowledge MCP launch command; defaults to the fin default.",
    )
    parser.add_argument(
        "--embed-dimensions",
        type=int,
        required=True,
        help=(
            "Vector dimension the dev stack indexes with. Required and "
            "undefaulted on purpose: the model and dimension are one fixed "
            "value on both sides."
        ),
    )
    parser.add_argument(
        "--embed-generation",
        required=True,
        help=(
            "Embedding generation (active_model) the dev stack indexes with. "
            "A mismatch exits 2."
        ),
    )
    parser.add_argument(
        "--poll-timeout-seconds",
        type=float,
        default=DEFAULT_POLL_TIMEOUT_SECONDS,
        help="Total seconds the chain polls before reporting a timeout.",
    )
    parser.add_argument(
        "--poll-interval-seconds",
        type=float,
        default=DEFAULT_POLL_INTERVAL_SECONDS,
        help="Seconds between poll rounds.",
    )
    parser.add_argument(
        "--user-home",
        default=None,
        help=(
            "The home directory the guard compares against when refusing the "
            "default memory home; defaults to the real HOME."
        ),
    )
    return parser.parse_args(list(argv))


def _mcp_command(command: str | None) -> list[str]:
    configured = command or os.environ.get("FIN_MCP_COMMAND")
    if configured:
        return configured.split()
    return list(DEFAULT_MCP_COMMAND)


def _iknow_root(argv: Sequence[str]) -> Path:
    if argv:
        return Path(argv)
    configured = os.environ.get(DEFAULT_IKNOW_ROOT_ENV)
    if configured:
        return Path(configured)
    raise DemoSeedError(
        f"--iknow-root is required (or ${DEFAULT_IKNOW_ROOT_ENV}); the npm "
        "scripts and index:worker run inside the paired repository"
    )


def _store_reset_port(environ: dict[str, str]) -> PostgresStoreReset:
    """The storage half of ``--reset``: dev PostgreSQL, dev Elasticsearch.

    The Neo4j half has no client on the fin side and is refused by name at the
    moment the chain reaches it, so a reset never silently clears two thirds of
    the stack.
    """
    dsn = environ.get(DB_URL_ENV, "")
    es_url = environ.get(ES_URL_ENV, "")

    class _CombinedResetPort(PostgresStoreReset):
        """Truncate on Postgres, drop the ES indices, refuse Neo4j by name."""

        def __init__(self) -> None:
            super().__init__(lambda: psycopg.connect(dsn, autocommit=False))
            self._es = ElasticsearchIndexReset(es_url) if es_url else None
            self._neo4j = Neo4jGraphReset(uri=environ.get("GRAPHRAG_MEMORY_NEO4J_URI"))

        def delete_elasticsearch_indices(self) -> tuple[str, ...]:
            if self._es is None:
                raise DemoSeedError(
                    f"{ES_URL_ENV} is not set, so the Elasticsearch indices "
                    "--reset would delete cannot be addressed"
                )
            return self._es.delete_elasticsearch_indices()

        def clear_neo4j_graph(self) -> tuple[str, ...]:
            return self._neo4j.clear_neo4j_graph()

    return _CombinedResetPort()


def _worker_factory(iknow_root: Path, environ: dict[str, str]):
    def factory() -> WorkerHandle:
        command = worker_command()
        npm_path = shutil.which(command[0])
        if npm_path is None:
            raise DemoWorkerUnavailableError(
                "npm is not available on PATH; index:worker cannot be started"
            )
        command[0] = str(Path(npm_path).resolve())
        return SubprocessWorker(
            command, cwd=iknow_root, environ=environ
        )

    return factory


def _report_line(outcome: JobOutcome) -> str:
    state = "skipped" if outcome.skipped else outcome.status
    suffix = f" failure_code={outcome.failure_code}" if outcome.failure_code else ""
    return (
        f"[seed:demo] {outcome.document_id} unit={outcome.unit}: {state} "
        f"(job_id={outcome.job_id}, reused={outcome.reused}){suffix}"
    )


class _SeedTerminated(SystemExit):
    """SIGTERM during a run, turned into an exception so ``finally`` runs.

    A ``finally`` covers an exception and a KeyboardInterrupt, but not SIGTERM:
    the default disposition for SIGTERM ends the process at the signal, so the
    worker's teardown would never run and the npm-spawned worker would outlive
    the seed. The handler raises instead, which routes the interruption through
    the same ``finally`` that every other exit takes -- and this is a
    ``SystemExit`` so the CLI's ``except DemoSeedError`` does not swallow it into
    a "refused" message that never happened.
    """

    def __init__(self, signum: int, frame: object) -> None:
        super().__init__(128 + signum)
        self.signum = signum


def _raise_on_sigterm(signum: int, frame: object) -> None:
    raise _SeedTerminated(signum, frame)


def _install_sigterm_handler() -> Any:
    """Route SIGTERM through the teardown; restore the prior handler on exit."""
    try:
        previous = signal.signal(signal.SIGTERM, _raise_on_sigterm)
    except ValueError:
        # Not the main thread (a test, or a library caller): the handler can only
        # be installed there, so the seed keeps SIGTERM's default disposition
        # rather than pretending it is covered.
        return None
    return previous


def main(argv: Sequence[str] | None = None) -> int:
    # The chain's teardown reports through a library logger; without this the
    # one line that names the process group it ended would never be printed.
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        stream=sys.stderr,
    )
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    environ = dict(os.environ)
    previous_handler = _install_sigterm_handler()
    try:
        try:
            iknow_root = _iknow_root(args.iknow_root)
            options = DemoSeedOptions(
                fixture_dir=Path(args.fixture_dir)
                if args.fixture_dir
                else DEMO_FIXTURE_DIR,
                expected_dimensions=args.embed_dimensions,
                expected_generation=args.embed_generation,
                reset=args.reset,
                write=args.yes,
                catalog_path=args.catalog_path,
                user_home=args.user_home,
                poll_timeout_seconds=args.poll_timeout_seconds,
                poll_interval_seconds=args.poll_interval_seconds,
            )
            dsn = environ.get(DB_URL_ENV, "")
            with MCPStdioSession(_mcp_command(args.mcp_command)) as mcp_session:
                deps = DemoSeedDeps(
                    environ=environ,
                    push_port=MCPPushPort(session=mcp_session),
                    index_job_port=MCPIndexJobPort(session=mcp_session),
                    reset_port=_store_reset_port(environ),
                    plan_a=PlanADevStackAdapter(iknow_root, environ=environ),
                    embed_probe=PostgresEmbedProbe(
                        lambda: psycopg.connect(dsn, autocommit=True)
                    ),
                    worker_factory=_worker_factory(iknow_root, environ),
                )
                report = run_demo_seed(deps, options)
        except DemoSeedError as exc:
            print(f"[seed:demo] refused ({exc.code}): {exc}", file=sys.stderr)
            # The typed line is how a caller across the process boundary
            # learns which failure happened without reading prose. The
            # human-readable line above stays exactly as it was.
            exit_code = exit_code_for(exc)
            status = SEED_RESULT_REFUSED if exit_code == 2 else SEED_RESULT_FAILED
            print(format_seed_result(status, exc.code), file=sys.stderr)
            return exit_code
    finally:
        if previous_handler is not None:
            signal.signal(signal.SIGTERM, previous_handler)
    for outcome in report.jobs:
        print(_report_line(outcome))
    if not report.wrote:
        print(
            "[seed:demo] preview only; re-run with --yes to write "
            "(add --reset to start from an empty dev stack)"
        )
        return 0
    print(
        f"[seed:demo] pushed {len(report.ingested)} source(s), "
        f"{len(report.jobs)} job(s); wrote {report.catalog_rows_written} "
        f"catalog row(s) -> {report.catalog_path or 'the default development catalog'} "
        f"in {report.elapsed_seconds:.1f}s"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
