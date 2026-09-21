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
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from fin.corpus.catalog import record_filings
from fin.host.operator_env import apply_dotenv

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
