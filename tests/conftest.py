"""Corpus fixtures shared by the corpus and host test packages.

``multi_form_corpus`` is the "test corpus includes those forms" half of
spec SC3. The committed corpus is 10-Q only — that is what makes the
SC3 negative case (``form=10-K`` / ``form=8-K`` -> ``{"filings": []}``)
meaningful — so the positive case needs its own catalog.

The fixture builds a catalog metadata table through
``fin.corpus.catalog.record_filings`` — the same seam the ingest
endpoint (T3) uses — so tests exercise the real row contract
``{document_id, company, period, version, page_count}`` rather than a
hand-rolled manifest.

It yields ``{"catalog_path": ...}`` so callers can splat it straight
into ``list_files`` / ``list_filings``.

T3 / SC3 / ADR 0035 also live here: we DEL ``FIN_MEMORY_ENABLED`` so the
default pytest run builds an OFF memory port even when
``~/.fin/settings.yaml`` has ``memory.enabled: true``. Memory cases opt
back in via the
``@pytest.mark.memory_on`` marker + ``memory_on_env`` fixture in
``tests/host/conftest.py``.

The live-Jev gate lives here too (``jev_live_key_missing`` →
:func:`build_jev_live_client` → the ``jev_live_client`` fixture → the
``jev_live`` deselection hook): the ``jev_live`` suites are spread over
``tests/host/`` and ``tests/retrieve/``, so a per-directory conftest could
not serve both without a second copy of the skip-vs-fail semantics — and
two copies of a gate drift.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Final

import pytest

# ``tests/host/conftest.py`` is also imported as ``conftest`` and would
# otherwise force a second execution of this file. Publish this module so
# ``from tests.conftest import _PROCESS_SNAPSHOT`` sees the snapshot below.
if __name__ == "conftest":
    import types

    if "tests" not in sys.modules:
        _tests_pkg = types.ModuleType("tests")
        _tests_pkg.__path__ = []
        _tests_pkg.__package__ = "tests"
        sys.modules["tests"] = _tests_pkg
        del _tests_pkg
    sys.modules.setdefault("tests.conftest", sys.modules[__name__])

from fin.corpus.demo_seed import seed_demo_catalog
from fin.host.question.jev_choice import JevChoiceClient, build_jev_client

REPO_ROOT = Path(__file__).resolve().parents[1]

# Captured before the hermetic pops. Default pytest and opt-in live gates
# read this snapshot. They do not open ``~/.fin/settings.yaml``.
_LIVE_PROCESS_KEYS = (
    "FIN_API_KEY",
    "FIN_MODEL",
    "FIN_BASE_URL",
    "FIN_JEV_API_KEY",
    "FIN_JEV_MODEL",
    "FIN_JEV_BASE_URL",
    "TYPESAFE_API_KEY",
    "TYPESAFE_DEFAULT_MODEL",
    "TYPESAFE_BASE_URL",
)
_PROCESS_SNAPSHOT = {
    key: value
    for key in _LIVE_PROCESS_KEYS
    if (value := os.environ.get(key, "").strip())
}

# SC3 / ADR 0035: default ``uv run pytest -q`` MUST stay OFF even when
# ``~/.fin/settings.yaml`` sets memory on. We strip the
# var here so any ``build_memory_port()`` call in unmarked tests sees
# an unset ``FIN_MEMORY_ENABLED`` and resolves to ``OffMemoryAdapter``.
#
# This is the session-level half of the strip. The per-test half lives
# in ``tests/host/test_memory_pytest_off.py._strip_operator_memory_env``
# and is needed because a sibling test in the same process can re-add
# the var after this import-time clear.
#
# Opt-in: tests marked ``@pytest.mark.memory_on`` re-set the var via the
# ``memory_on_env`` fixture in ``tests/host/conftest.py``.
os.environ.pop("FIN_MEMORY_ENABLED", None)

# Default pytest must stay hermetic (spec: Fake / no live MCP). A shell
# export may point retrieve spawn at a sibling checkout; that hangs
# unit tests. Product ``fin-serve`` still reads the var from settings.
os.environ.pop("FIN_KNOWLEDGE_MCP_CWD", None)
os.environ.pop("FIN_MODEL", None)
os.environ.pop("FIN_BASE_URL", None)
os.environ.pop("FIN_API_KEY", None)
os.environ.pop("FIN_JEV_API_KEY", None)
# The structured-evidence identity is operator configuration read at boot. A
# developer settings file that fills it in would otherwise change what `serve()` compiles,
# so every test that stubs `compile_for_http` with a zero-argument lambda would
# take a branch it does not assert. Strip it: the identity has its own test file.
for _identity_var in tuple(os.environ):
    if _identity_var.startswith("FIN_EVIDENCE_"):
        os.environ.pop(_identity_var, None)
os.environ.pop("FIN_JEV_MODEL", None)
os.environ.pop("FIN_JEV_BASE_URL", None)
os.environ.pop("TYPESAFE_API_KEY", None)
os.environ.pop("TYPESAFE_DEFAULT_MODEL", None)
os.environ.pop("TYPESAFE_BASE_URL", None)

# The default run declares its corpus-ingest job store process-local, the way
# it declares no live MCP and no operator LLM: an accepted upload must not
# depend on services this run does not control. Production boot has no such
# default -- without a DSN or this declaration the ingest door refuses to
# start -- so a suite that wants the durable leg sets the key itself.
os.environ["FIN_CORPUS_INGEST_STORE"] = "memory"


class DatabaseGateError(RuntimeError):
    """``FIN_TEST_DSN`` points at a database that is not a ``_test`` library."""


def _postgres_database_name(dsn: str) -> str:
    """Return libpq's effective database name, or an empty string if absent.

    Psycopg delegates parsing to libpq for URI and keyword conninfo forms.
    Parse failures become a generic gate error without retaining parser details.
    """
    from psycopg.conninfo import conninfo_to_dict

    try:
        settings = conninfo_to_dict(dsn)
    except Exception:
        # EXIT: Parser details may retain connection fields; raise the typed error below.
        settings = None

    if settings is None:
        raise DatabaseGateError(
            "FIN_TEST_DSN is not a valid PostgreSQL connection setting."
        )

    database = settings.get("dbname", "")
    return database if isinstance(database, str) else ""


def _configure_test_database() -> None:
    """Bind every Postgres-backed test to the ``_test`` library a DSN names.

    The single source for the run's DSN is ``FIN_TEST_DSN`` (spec: AGENTS.md
    hard boundary 4); a development ``FIN_CHECKPOINT_URL`` left in the shell or
    ``.env`` is never used. Unset ``FIN_TEST_DSN`` drops the ambient
    ``FIN_CHECKPOINT_URL``, so the env-gated Postgres suites skip and the default
    run stays hermetic. A set ``FIN_TEST_DSN`` MUST name a database ending in
    ``_test``; anything else aborts collection instead of reading or writing the
    development library, whose seed data a stray test run could clear. The DSN is
    bridged onto ``FIN_CHECKPOINT_URL`` because that is the key the stores and
    the durable suites already read.
    """
    test_dsn = (os.environ.get("FIN_TEST_DSN") or "").strip()
    if not test_dsn:
        os.environ.pop("FIN_CHECKPOINT_URL", None)
        return

    database = _postgres_database_name(test_dsn)
    if not database.endswith("_test"):
        raise DatabaseGateError(
            f"FIN_TEST_DSN names database {database!r}, which does not end in "
            "``_test``. Tests connect only to a test library; the development "
            "DSN is off-limits (AGENTS.md hard boundary 4)."
        )

    os.environ["FIN_CHECKPOINT_URL"] = test_dsn


_configure_test_database()


@pytest.fixture(autouse=True)
def _strip_operator_llm_and_mcp_cwd_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep default pytest off the operator LLM / live-MCP settings.

    Session-level ``pop`` is not enough: a sibling test can put the
    vars back. Tests that need a value call ``monkeypatch.setenv`` after this.
    """
    monkeypatch.delenv("FIN_KNOWLEDGE_MCP_CWD", raising=False)
    monkeypatch.delenv("FIN_MODEL", raising=False)
    monkeypatch.delenv("FIN_BASE_URL", raising=False)
    monkeypatch.delenv("FIN_API_KEY", raising=False)
    monkeypatch.delenv("FIN_JEV_API_KEY", raising=False)
    monkeypatch.delenv("FIN_JEV_MODEL", raising=False)
    monkeypatch.delenv("FIN_JEV_BASE_URL", raising=False)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.delenv("TYPESAFE_DEFAULT_MODEL", raising=False)
    monkeypatch.delenv("TYPESAFE_BASE_URL", raising=False)


@pytest.fixture(scope="session")
def _hermetic_home(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One empty HOME reused for the whole default run.

    Several product seams resolve a default path from ``$HOME`` at CALL
    time and MUST be off the operator's real home in the hermetic run:
    :func:`fin.host.graph.compile_graph` builds its default
    ``user_profile_store`` from ``$HOME/.fin/user.md``, and the CLI /
    HTTP / :meth:`fin.host.user_profile.UserProfileStore.from_env` seams
    resolve ``$HOME/.fin/default`` the same way (all read
    ``os.environ["HOME"]`` or ``Path.home()``). A real ``~/.fin/user.md``
    on a developer box leaked a ``[FIN_USER_PROFILE]`` block into the
    compiled system prompt and broke
    ``tests/host/test_clarify_prompt.py``, making the default suite
    depend on the home directory. An empty per-run home removes that
    dependency at the single source; it also covers the ``$HOME``-derived
    workspace defaults, which are the identical root cause.
    """
    return tmp_path_factory.mktemp("fin-hermetic-home")


@pytest.fixture(autouse=True)
def _redirect_home_off_operator(
    _hermetic_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Point ``HOME`` at the empty per-run home for every test.

    Applied per-test rather than once at session start so a sibling that
    mutates ``HOME`` cannot leak it into the next test — the same
    reasoning as :func:`_strip_operator_llm_and_mcp_cwd_env`. On POSIX
    ``Path.home()`` and ``~`` resolve via ``HOME``, so overriding it also
    neutralises ``Path.home()`` callers. Tests that genuinely need a home
    set ``HOME`` themselves (via ``monkeypatch``); as a non-autouse
    fixture/body that runs after this one, their value wins for that test.
    """
    monkeypatch.setenv("HOME", str(_hermetic_home))


@pytest.fixture(autouse=True)
def _operator_settings_file_is_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Default pytest must not load the developer ``~/.fin/settings.yaml``.

    ``FIN_SETTINGS_FILE`` is a path, not a settings bag. Pointing it at a
    file that does not exist makes ``apply_operator_settings`` a no-op.
    """
    monkeypatch.setenv("FIN_SETTINGS_FILE", str(tmp_path / "missing-settings.yaml"))


@pytest.fixture
def multi_form_corpus(tmp_path: Path) -> dict[str, Path]:
    """A catalog of one 10-K, one 8-K and one 10-Q for the same issuer.

    The rows come from :func:`fin.corpus.demo_seed.seed_demo_catalog` -- the
    same generator the ``seed:demo`` command writes into the development
    catalog -- so the shape asserted here is the shape that lands in
    development. ``tmp_path`` keeps a routine run off the real catalog file.
    """
    catalog_path = tmp_path / "catalog.json"
    seed_demo_catalog(catalog_path)

    return {"catalog_path": catalog_path}


# ----- live Jev Choice gate (shared by every ``jev_live`` suite) -----

JEV_LIVE_KEY_MISSING: Final[str] = "jev_live_key_missing"


def build_jev_live_client(environ: Mapping[str, str]) -> JevChoiceClient:
    """Build the real client or name the missing credential as a FAIL.

    Single source for every live suite on purpose: skip-vs-fail is gate
    semantics, and two copies of it drift. A missing key is a blocking
    named failure (``jev_live_key_missing``, non-zero exit), never a green
    skip — a Fake pass does not count as live evidence (ADR 0080 / test
    rule 7), and neither does an absent credential.
    """
    client = build_jev_client(environ=environ)
    if client is None:
        pytest.fail(
            f"{JEV_LIVE_KEY_MISSING}: export FIN_JEV_API_KEY",
            pytrace=False,
        )
    assert client is not None  # for type checkers; pytest.fail raises
    return client


@pytest.fixture(scope="module")
def jev_live_client() -> JevChoiceClient:
    """The live client for one ``jev_live`` module.

    This file strips the Jev env at session start. The gate reads
    ``_PROCESS_SNAPSHOT``, captured before that strip.
    """
    keys = (
        "FIN_JEV_API_KEY",
        "FIN_JEV_MODEL",
        "FIN_JEV_BASE_URL",
        "TYPESAFE_API_KEY",
        "TYPESAFE_DEFAULT_MODEL",
        "TYPESAFE_BASE_URL",
    )
    merged = dict(os.environ)
    merged.update(
        {key: _PROCESS_SNAPSHOT[key] for key in keys if key in _PROCESS_SNAPSHOT}
    )
    return build_jev_live_client(merged)


#: Opt-in marks that appear outside a single test directory. Each is
#: deselected unless the operator names it in ``-m``. The marks are
#: independent: selecting one never drags the other's items into the run.
_ROOT_OPT_IN_MARKS: tuple[str, ...] = ("jev_live", "runtime_pg")


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Deselect root-level opt-in items unless the CLI ``-m`` names the mark.

    The ADR 0080 live Jev Choice round-trip talks to ``api.typesafe.ai``,
    and the P1 ``runtime_pg`` suites require an isolated ``_test``
    database. Neither may leak into the default hermetic run, and both
    marks appear in more than one test directory, which is why the switch
    is here rather than in a per-directory hook. Register-style: a CLI
    ``-m jev_live`` (or any expression naming the mark) re-selects exactly
    that mark's items. ``sql_live`` keeps its pre-P1 semantics; it is owned
    by the suites that already used it. Other opt-in marks
    (``soft_track``, ``memory_on``, ``mcp``) are owned by the directory
    that uses them.
    """
    markexpr = config.getoption("markexpr") or ""

    kept: list[pytest.Item] = []
    deselected: list[pytest.Item] = []
    for item in items:
        if any(
            mark not in markexpr
            and item.get_closest_marker(mark) is not None
            for mark in _ROOT_OPT_IN_MARKS
        ):
            deselected.append(item)
        else:
            kept.append(item)

    if deselected:
        config.hook.pytest_deselected(items=deselected)
        items[:] = kept

