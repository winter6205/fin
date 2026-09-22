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

T3 / SC3 / ADR 0035 also live here: after ``apply_dotenv`` populates the
process env, we DEL ``FIN_MEMORY_ENABLED`` so the default pytest run
builds an OFF memory port even when the operator ``.env`` has
``FIN_MEMORY_ENABLED=true``. Memory cases opt back in via the
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
from collections.abc import Mapping
from pathlib import Path
from typing import Final

import pytest

from fin.corpus.catalog import record_filings
from fin.host.jev_choice import JevChoiceClient, build_jev_client
from fin.host.operator_env import apply_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]

# Load the operator ``.env`` (``FIN_CHECKPOINT_URL`` &c.) before any
# fixture reads ``os.environ``, so Postgres-backed tests are not
# skipped just because the shell session did not export the vars.
# Product boot uses the same ``apply_dotenv`` (SSOT), which is a
# ``setdefault`` fill: an explicitly exported var still wins, and a
# missing ``.env`` is a no-op (CI stays hermetic).
apply_dotenv(start=Path(__file__).resolve().parent.parent)

# SC3 / ADR 0035: default ``uv run pytest -q`` MUST stay OFF even when
# the operator ``.env`` has ``FIN_MEMORY_ENABLED=true``. We strip the
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

# Default pytest must stay hermetic (spec: Fake / no live MCP). Operator
# ``.env`` may point retrieve spawn at a sibling checkout; that hangs
# unit tests. Product ``fin-serve`` still reads the var via apply_dotenv.
os.environ.pop("FIN_KNOWLEDGE_MCP_CWD", None)
os.environ.pop("FIN_MODEL", None)
os.environ.pop("FIN_BASE_URL", None)
os.environ.pop("FIN_API_KEY", None)
os.environ.pop("FIN_JEV_API_KEY", None)
os.environ.pop("FIN_JEV_MODEL", None)
os.environ.pop("FIN_JEV_BASE_URL", None)
os.environ.pop("TYPESAFE_API_KEY", None)
os.environ.pop("TYPESAFE_DEFAULT_MODEL", None)
os.environ.pop("TYPESAFE_BASE_URL", None)


def _drop_unreachable_checkpoint_url() -> None:
    """If ``FIN_CHECKPOINT_URL`` cannot accept a connection, unset it.

    ``apply_dotenv`` loads the operator DSN so Postgres-backed tests can
    run. A down listener (or a stale port after Compose stop) makes
    ``psycopg.connect`` block ingest/host tests for minutes. Unset the
    DSN so those paths use in-memory stores, matching a machine with no
    ``.env``. A reachable DSN is left in place for CI / live Postgres.
    """
    url = (os.environ.get("FIN_CHECKPOINT_URL") or "").strip()
    if not url:
        return
    try:
        import psycopg

        with psycopg.connect(url, connect_timeout=2) as connection:
            connection.execute("SELECT 1")
    except Exception:
        # EXIT: DSN not reachable; default pytest uses in-memory stores
        os.environ.pop("FIN_CHECKPOINT_URL", None)


_drop_unreachable_checkpoint_url()


@pytest.fixture(autouse=True)
def _strip_operator_llm_and_mcp_cwd_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep default pytest off the operator LLM / live-MCP .env.

    Session-level ``pop`` is not enough: a sibling test can put the
    vars back, and ``apply_dotenv`` setdefault would refill them.
    Tests that need a value call ``monkeypatch.setenv`` after this.
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


# One row per form the operator should be able to see in the library.
_MULTI_FORM_ROWS: tuple[tuple[str, str, str], ...] = (
    ("ZEUS-2023-10K", "10-K", "2023 FY"),
    ("ZEUS-2023-8K", "8-K", "2023-05-04"),
    ("ZEUS-2023Q1", "10-Q", "2023 Q1"),
)


@pytest.fixture
def multi_form_corpus(tmp_path: Path) -> dict[str, Path]:
    """A catalog of one 10-K, one 8-K and one 10-Q for the same issuer."""
    catalog_path = tmp_path / "catalog.json"
    rows = [
        {
            "document_id": document_id,
            "company": "ZEUS",
            "period": period,
            "version": str(index + 1) * 64,
            "page_count": 1,
            "form": form,
            "cik": "0000000042",
            "ticker": "ZEUS",
            "filing_date": "2023-05-04",
        }
        for index, (document_id, form, period) in enumerate(_MULTI_FORM_ROWS)
    ]
    record_filings(rows, catalog_path=catalog_path)

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
            f"{JEV_LIVE_KEY_MISSING}: set FIN_JEV_API_KEY in the repo .env",
            pytrace=False,
        )
    assert client is not None  # for type checkers; pytest.fail raises
    return client


@pytest.fixture(scope="module")
def jev_live_client() -> JevChoiceClient:
    """The live client for one ``jev_live`` module.

    This file strips the Jev env at session start, so this re-applies
    ``apply_dotenv`` exactly like the product boot (``fin-serve``) does
    before building the client — the same
    :func:`fin.host.jev_choice.build_jev_client` the product cascade uses.
    """
    apply_dotenv(start=REPO_ROOT)
    return build_jev_live_client(os.environ)


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Deselect ``jev_live`` items unless the CLI ``-m`` names that mark.

    The ADR 0080 live Jev Choice round-trip talks to ``api.typesafe.ai``,
    so the default hermetic run must not reach it — and the mark appears in
    more than one test directory, which is why the switch is here rather
    than in a per-directory hook. Register-style: a CLI ``-m jev_live`` (or
    any expression naming the mark) re-selects them. Other opt-in marks are
    owned by the directory that uses them.
    """
    markexpr = config.getoption("markexpr") or ""
    if "jev_live" in markexpr:
        return

    kept: list[pytest.Item] = []
    deselected: list[pytest.Item] = []
    for item in items:
        if item.get_closest_marker("jev_live") is not None:
            deselected.append(item)
        else:
            kept.append(item)

    if deselected:
        config.hook.pytest_deselected(items=deselected)
        items[:] = kept

