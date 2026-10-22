"""Contract tests for the capability boundary checker.

``scripts/check_capability_boundaries.py`` is a CI gate
(``.github/workflows/ci.yml``) and the documented self-check for the
capability rule in ``AGENTS.md`` rule 14. These cases pin what it must
catch: every natural spelling of a forbidden import, the recorded
capability edges, and the CLI contract the CI step relies on.

The fixture tree under ``tmp_path`` mirrors the shape the checker reads:
``src/fin/host/<capability>/``, ``src/fin/corpus/``,
``src/fin/retrieve/`` and ``web/src/{capabilities,pages}/``. The clean
fixture holds every recorded edge (host and web) and no unrecorded one,
so "no violations" means the checker really had nothing to complain
about.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "check_capability_boundaries.py"

HOST_CAPABILITIES = (
    "attachment",
    "evidence",
    "grounded",
    "question",
    "runtime",
    "session",
    "task",
)
WEB_CAPABILITIES = ("clarification", "evidence", "task", "trace")


def _load_checker() -> object:
    spec = importlib.util.spec_from_file_location(
        "check_capability_boundaries", SCRIPT
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CHECKER = _load_checker()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _build_clean_tree(root: Path) -> None:
    """A tree that satisfies every recorded edge and nothing else."""
    for capability in HOST_CAPABILITIES:
        _write(root / "src" / "fin" / "host" / capability / "__init__.py", "")
    for capability in WEB_CAPABILITIES:
        _write(root / "web" / "src" / "capabilities" / capability / "index.ts", "")
    _write(root / "src" / "fin" / "corpus" / "catalog.py", '"""Corpus stub."""\n')
    _write(root / "src" / "fin" / "retrieve" / "port.py", '"""Retrieve port stub."""\n')
    _write(
        root / "web" / "src" / "pages" / "HomePage.tsx",
        "export const HomePage = () => null;\n",
    )

    # The recorded host edges: attachment -> evidence, evidence -> attachment,
    # evidence -> grounded, grounded -> question, question -> task.
    _write(
        root / "src" / "fin" / "host" / "attachment" / "attachment.py",
        "from fin.host.evidence.visual_evidence import VisualObservation\n",
    )
    _write(
        root / "src" / "fin" / "host" / "evidence" / "evidence.py",
        "from fin.host.attachment.attachment_prep_worker import DrainCadence\n"
        "from fin.host.grounded.citation_gate import Citation\n",
    )
    _write(
        root / "src" / "fin" / "host" / "grounded" / "grounded.py",
        "from fin.host.question.jev_choice import JevChoiceClient\n",
    )
    _write(
        root / "src" / "fin" / "host" / "question" / "question.py",
        "from fin.host.task.task_runtime_types import TaskRuntimeState\n",
    )

    # The recorded web edges: task -> trace, clarification -> task.
    _write(
        root / "web" / "src" / "capabilities" / "task" / "task.ts",
        "import { TracePanel } from '../trace/trace';\n",
    )
    _write(
        root / "web" / "src" / "capabilities" / "clarification" / "clarification.ts",
        "import { TaskClient } from '../task/task';\n",
    )


@pytest.fixture
def clean_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    _build_clean_tree(root)
    return root


def _violations(root: Path) -> list[str]:
    return CHECKER.check_all(root)  # type: ignore[attr-defined]


# ===== the clean baseline =====


def test_clean_tree_has_no_violations(clean_repo: Path) -> None:
    """A tree that matches the recorded edges is clean.

    The baseline every other case leans on: if the fixture itself were
    dirty, a case asserting "no violation" would pass for the wrong
    reason.
    """
    assert _violations(clean_repo) == []


# ===== the transport, the application boundary, the composition root =====


def test_capability_importing_the_transport_is_a_violation(clean_repo: Path) -> None:
    """``from fin.host.http import ...`` reaches the transport."""
    _write(
        clean_repo / "src" / "fin" / "host" / "session" / "session.py",
        "from fin.host.http import serve\n",
    )
    assert (
        "capability session reaches the transport, the application boundary or "
        "the graph composition root: "
        "src/fin/host/session/session.py:1 imports fin.host.http"
    ) in _violations(clean_repo)


def test_from_fin_host_import_http_is_a_violation(clean_repo: Path) -> None:
    """``from fin.host import http`` binds the transport module."""
    _write(
        clean_repo / "src" / "fin" / "host" / "session" / "session.py",
        "from fin.host import http\n",
    )
    assert (
        "capability session reaches the transport, the application boundary or "
        "the graph composition root: "
        "src/fin/host/session/session.py:1 imports fin.host.http"
    ) in _violations(clean_repo)


# ===== the graph-building entry points of the composition root =====


def test_importing_a_compile_entry_point_is_a_violation(clean_repo: Path) -> None:
    """``from fin.host.compile import compile_graph`` reaches the root."""
    _write(
        clean_repo / "src" / "fin" / "host" / "session" / "session.py",
        "from fin.host.compile import compile_graph\n",
    )
    assert (
        "capability session reaches the graph composition root: "
        "src/fin/host/session/session.py:1 imports compile_graph"
    ) in _violations(clean_repo)


def test_every_name_of_a_from_import_list_is_judged(clean_repo: Path) -> None:
    """The second name of the list is judged too, not only the first.

    ``DEFAULT_BASE_URL`` alone is fine; the ``compile_graph`` behind it
    is not.
    """
    _write(
        clean_repo / "src" / "fin" / "host" / "session" / "session.py",
        "from fin.host.compile import DEFAULT_BASE_URL, compile_graph\n",
    )
    assert (
        "capability session reaches the graph composition root: "
        "src/fin/host/session/session.py:1 imports compile_graph"
    ) in _violations(clean_repo)


def test_bare_import_of_the_compile_module_is_a_violation(clean_repo: Path) -> None:
    """``import fin.host.compile`` binds the entry points as well."""
    _write(
        clean_repo / "src" / "fin" / "host" / "session" / "session.py",
        "import fin.host.compile\n",
    )
    assert (
        "capability session reaches the graph composition root: "
        "src/fin/host/session/session.py:1 imports fin.host.compile"
    ) in _violations(clean_repo)


def test_from_fin_host_import_compile_is_a_violation(clean_repo: Path) -> None:
    """``from fin.host import compile`` binds the whole module."""
    _write(
        clean_repo / "src" / "fin" / "host" / "session" / "session.py",
        "from fin.host import compile\n",
    )
    assert (
        "capability session reaches the graph composition root: "
        "src/fin/host/session/session.py:1 imports fin.host.compile"
    ) in _violations(clean_repo)


def test_bare_import_of_the_host_package_is_a_violation(clean_repo: Path) -> None:
    """``import fin.host`` binds the package, which re-exports the root.

    The host package's public surface carries ``compile_graph``, so
    binding the whole package reaches the graph composition root.
    """
    _write(
        clean_repo / "src" / "fin" / "host" / "session" / "session.py",
        "import fin.host\n",
    )
    assert (
        "capability session reaches the transport, the application boundary or "
        "the graph composition root: "
        "src/fin/host/session/session.py:1 imports fin.host"
    ) in _violations(clean_repo)


def test_from_fin_host_import_compile_graph_is_a_violation(
    clean_repo: Path,
) -> None:
    """``from fin.host import compile_graph`` binds the root's entry point."""
    _write(
        clean_repo / "src" / "fin" / "host" / "session" / "session.py",
        "from fin.host import compile_graph\n",
    )
    assert (
        "capability session reaches the graph composition root: "
        "src/fin/host/session/session.py:1 imports compile_graph"
    ) in _violations(clean_repo)


def test_parenthesised_from_import_list_is_judged(clean_repo: Path) -> None:
    """A multi-line import list is judged name by name as well.

    The repository's own house style wraps long import lists, so a
    line-by-line reader would miss the entry point inside one.
    """
    _write(
        clean_repo / "src" / "fin" / "host" / "session" / "session.py",
        "from fin.host.compile import (\n"
        "    DEFAULT_BASE_URL,\n"
        "    compile_graph,\n"
        ")\n",
    )
    assert (
        "capability session reaches the graph composition root: "
        "src/fin/host/session/session.py:1 imports compile_graph"
    ) in _violations(clean_repo)


# ===== the recorded capability edges =====


def test_unrecorded_capability_edge_is_a_violation(clean_repo: Path) -> None:
    """``from fin.host import evidence`` is a capability edge too."""
    _write(
        clean_repo / "src" / "fin" / "host" / "session" / "session.py",
        "from fin.host import evidence\n",
    )
    assert (
        "unrecorded capability edge: session -> evidence. Record it in "
        "scripts/check_capability_boundaries.py and in the reliability map."
    ) in _violations(clean_repo)


def test_relative_capability_import_is_an_edge(clean_repo: Path) -> None:
    """``from ..evidence import ...`` names the same neighbour."""
    _write(
        clean_repo / "src" / "fin" / "host" / "session" / "session.py",
        "from ..evidence import evidence_contract\n",
    )
    assert (
        "unrecorded capability edge: session -> evidence. Record it in "
        "scripts/check_capability_boundaries.py and in the reliability map."
    ) in _violations(clean_repo)


def test_missing_recorded_capability_edge_is_a_violation(clean_repo: Path) -> None:
    """A recorded edge that the tree no longer has is reported."""
    _write(clean_repo / "src" / "fin" / "host" / "attachment" / "attachment.py", "")
    assert (
        "recorded capability edge is gone: attachment -> evidence. "
        "Remove it from the recorded set or restore the dependency."
    ) in _violations(clean_repo)


# ===== the web rules =====


def test_web_capability_importing_the_page_directory_is_a_violation(
    clean_repo: Path,
) -> None:
    """A web capability must not reach into the page directory."""
    _write(
        clean_repo / "web" / "src" / "capabilities" / "task" / "task.ts",
        "import { TracePanel } from '../trace/trace';\n"
        "import { HomePage } from '../../pages/HomePage';\n",
    )
    assert (
        "web capability task reaches the page directory: "
        "web/src/capabilities/task/task.ts:2 imports ../../pages/HomePage"
    ) in _violations(clean_repo)


# ===== the configuration constants of the composition root stay importable =====


def test_compile_constants_alone_are_not_a_violation(clean_repo: Path) -> None:
    """``from fin.host.compile import DEFAULT_BASE_URL, FIN_API_KEY_ENV`` is fine.

    This is the one real compile import inside a capability package
    today (``src/fin/host/attachment/attachment_vision.py``), so the
    checker must keep it importable.
    """
    _write(
        clean_repo / "src" / "fin" / "host" / "session" / "session.py",
        "from fin.host.compile import DEFAULT_BASE_URL, FIN_API_KEY_ENV\n",
    )
    assert _violations(clean_repo) == []


# ===== the CLI contract the CI step relies on =====


def test_script_exits_zero_on_the_real_repository() -> None:
    """The real script, run as a subprocess, passes on the real tree.

    This is the step ``.github/workflows/ci.yml`` runs; a checker that
    only passes in-process would still let the gate go red.
    """
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "capability boundaries: ok"
