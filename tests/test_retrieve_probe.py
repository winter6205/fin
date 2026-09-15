"""The opt-in retrieve probe's own contract (plan `first-closed-loop` T1).

``scripts/retrieve_probe.py`` prints the two numbers the closed loop
turns on — how many chunks the port returned, and how many received hits
the mapper dropped. The README claims those two integers tell an empty
**资料集合** apart from a mapper drop; that claim is only true if the
second number is **this call's** drop count. The ledger is process-wide
and ``FIN_MAPPING_FAILURE_LOG`` can replay an earlier process's
failures, in which case an absolute count would print a non-zero number
beside ``n_chunks=0`` for a perfectly healthy empty collection — the
exact collapse the probe exists to prevent.

These tests drive the script's seam directly (adapter + session
injected); no MCP process is ever spawned, and the default ``uv run
pytest`` run stays hermetic (spec SC6).
"""

from __future__ import annotations

import importlib.util
import io
import sys
import threading
from pathlib import Path
from types import ModuleType
from typing import Any

from fin.retrieve.hit_mapping import (
    MAPPING_FAILURE_UNMAPPED_SOURCE_REF,
    MAPPING_STAGE_RETRIEVE,
    MappingFailureLedger,
)
from fin.retrieve.mcp_adapter import MCPRetrieveAdapter


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _load_probe() -> ModuleType:
    """Import ``scripts/retrieve_probe.py`` by path.

    The script lives outside the ``fin`` package on purpose (it is
    operator tooling, not product code), so there is no import path to
    it.
    """
    path = _repo_root() / "scripts" / "retrieve_probe.py"
    spec = importlib.util.spec_from_file_location("retrieve_probe", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _ScriptedProbeAdapter(MCPRetrieveAdapter):
    """The real adapter with ``_call_mcp`` scripted; no subprocess."""

    def __init__(
        self, *, response: dict[str, Any], ledger: MappingFailureLedger
    ) -> None:
        super().__init__(
            locator_map={}, timeout_seconds=1.0, mapping_ledger=ledger
        )
        self._response = response

    async def _call_mcp(self, payload: dict[str, Any]) -> dict[str, Any]:  # type: ignore[override]
        return self._response


# ----- the per-call delta -----


def test_a_replayed_ledger_does_not_show_as_this_calls_drop() -> None:
    """``FIN_MAPPING_FAILURE_LOG`` from an earlier process is not this
    call's drop: an empty collection must still print
    ``mapping_failures=0``. A concurrent run's drop is the same case one
    step later — one ledger serves the whole process."""
    ledger = MappingFailureLedger()
    ledger.record(
        code=MAPPING_FAILURE_UNMAPPED_SOURCE_REF,
        stage=MAPPING_STAGE_RETRIEVE,
        retrieval_ref="iknow-row-from-yesterday",
        message="recorded by an earlier process",
    )
    adapter = _ScriptedProbeAdapter(response={"chunks": []}, ledger=ledger)
    module = _load_probe()
    out = io.StringIO()

    code = module.run_probe(adapter, "revenue", limit=8, out=out)

    assert code == 0
    assert out.getvalue().splitlines() == [
        "n_chunks=0",
        "mapping_failures=0",
    ]


def test_a_concurrent_runs_drop_is_not_this_calls_number() -> None:
    """Same ledger, one more thread: a drop recorded by another run while
    this one is in flight must not print beside ``n_chunks=0``. Counting
    per-thread instead of over the process is what keeps that apart."""
    ledger = MappingFailureLedger()
    inside = threading.Event()
    return_to_probe = threading.Event()
    other_done = threading.Event()

    class _GatedProbeAdapter(_ScriptedProbeAdapter):
        async def _call_mcp(self, payload: dict[str, Any]) -> dict[str, Any]:  # type: ignore[override]
            inside.set()
            return_to_probe.wait(timeout=10)
            return {"chunks": []}

    adapter = _GatedProbeAdapter(response={"chunks": []}, ledger=ledger)
    module = _load_probe()
    out = io.StringIO()
    codes: list[int] = []
    failures: list[BaseException] = []

    def other_run() -> None:
        try:
            inside.wait(timeout=10)
            ledger.record(
                code=MAPPING_FAILURE_UNMAPPED_SOURCE_REF,
                stage=MAPPING_STAGE_RETRIEVE,
                retrieval_ref="id-other-run",
                message="another run's drop",
            )
            other_done.set()
        except BaseException as exc:  # noqa: BLE001 - collect worker failures
            failures.append(exc)
        finally:
            return_to_probe.set()

    worker = threading.Thread(target=other_run, name="other-run")
    worker.start()
    try:
        codes.append(module.run_probe(adapter, "revenue", limit=8, out=out))
    finally:
        return_to_probe.set()
        worker.join(timeout=10)

    assert not failures, failures
    assert other_done.is_set()
    assert codes == [0]
    assert out.getvalue().splitlines() == [
        "n_chunks=0",
        "mapping_failures=0",
    ]


def test_a_drop_this_call_caused_prints_a_non_zero_delta() -> None:
    """The other half of the pair: an unmappable hit is this call's
    drop, so ``n_chunks=0`` arrives with a non-zero second number."""
    ledger = MappingFailureLedger()
    adapter = _ScriptedProbeAdapter(
        response={
            "hits": [{"text": "hit", "source_ref": "id-X", "score": 1.0}]
        },
        ledger=ledger,
    )
    module = _load_probe()
    out = io.StringIO()

    code = module.run_probe(adapter, "revenue", limit=8, out=out)

    assert code == 0
    assert out.getvalue().splitlines() == [
        "n_chunks=0",
        "mapping_failures=1",
    ]


# ----- the launch command -----


def test_a_missing_binary_is_an_exit_2() -> None:
    """EXIT: the probe could not run. The resolved argv is validated
    with ``shutil.which``, so the documented ``2`` is reachable instead
    of dying inside the subprocess launch."""
    module = _load_probe()
    ns = module._parse_args(
        ["revenue", "--command", "fin-probe-no-such-binary"]
    )

    assert module._resolve_command(ns) is None


def test_a_resolvable_binary_is_returned_with_its_args() -> None:
    """``sys.executable`` is the interpreter running this test, so the
    binary is on ``PATH`` by construction — a literal ``python3`` would
    fail on a PATH that has no such entry."""
    module = _load_probe()
    ns = module._parse_args(
        ["revenue", "--command", sys.executable, "--arg", "server.py"]
    )

    assert module._resolve_command(ns) == [sys.executable, "server.py"]


def test_main_reports_exit_2_for_an_unresolvable_command(
    capsys: Any,
) -> None:
    module = _load_probe()

    code = module.main(["revenue", "--command", "fin-probe-no-such-binary"])

    assert code == 2
    assert "could not resolve" in capsys.readouterr().err
