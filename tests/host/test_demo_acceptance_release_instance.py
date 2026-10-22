"""Contract for the demo acceptance instance-claim release.

The browser acceptance gives every run a throwaway ``FIN_HOME``. The demo
checkpoint database is a dedicated ``_test`` target that outlives that home,
so the claim a previous run wrote there has to be released before the fresh
home can boot. This suite pins that the release touches the claim row only,
refuses a database that is not a ``_test`` target, and never prints a DSN.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest


_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "demo_acceptance_release_instance.py"


def _load_module() -> Any:
    spec = importlib.util.spec_from_file_location("demo_acceptance_release_instance", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _FakeCursor:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows
        self.rowcount = 0
        self.executed: list[str] = []

    def execute(self, statement: str, *args: Any) -> "_FakeCursor":
        self.executed.append(statement)
        if statement.startswith("SELECT current_database()"):
            return self
        if statement.startswith("DELETE FROM fin_instance"):
            self.rowcount = 1
            return self
        raise AssertionError(f"unexpected statement: {statement}")

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *args: Any) -> None:
        return None


class _FakeConnection:
    def __init__(self, database: str) -> None:
        self._database = database
        self.cursor_obj = _FakeCursor([(database,)])
        self.committed = False

    def cursor(self) -> _FakeCursor:
        return self.cursor_obj

    def commit(self) -> None:
        self.committed = True

    def __enter__(self) -> "_FakeConnection":
        return self

    def __exit__(self, *args: Any) -> None:
        return None


def test_release_deletes_only_the_instance_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module()
    connection = _FakeConnection("fin_plan05_demo_test")
    monkeypatch.setattr(module, "_verified_database", lambda dsn: "fin_plan05_demo_test")

    import psycopg

    monkeypatch.setattr(psycopg, "connect", lambda dsn, **kwargs: connection)

    removed = module.release_claim("postgresql://user:secret@127.0.0.1:5434/fin_plan05_demo_test")

    assert removed == 1
    assert connection.committed is True
    statements = connection.cursor_obj.executed
    assert any(statement.startswith("DELETE FROM fin_instance") for statement in statements)
    for statement in statements:
        assert "fin_thread" not in statement
        assert "checkpoint" not in statement
        assert "fin_ingest_job" not in statement


def test_release_refuses_a_database_that_is_not_a_test_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module()

    with pytest.raises(ValueError, match="_test"):
        module._verified_database("postgresql://user:secret@127.0.0.1:5432/fin_plan05_demo")


def test_release_refuses_a_dsn_that_uses_a_service_file() -> None:
    module = _load_module()

    with pytest.raises(ValueError, match="environment"):
        module._verified_database("service=fin_demo")


def test_main_reports_the_release_without_printing_the_dsn(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _load_module()
    monkeypatch.setenv("FIN_CHECKPOINT_URL", "postgresql://user:secret@127.0.0.1:5434/fin_plan05_demo_test")
    monkeypatch.setattr(module, "release_claim", lambda dsn: 1)

    exit_code = module.main()

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "released the instance claim" in captured.out
    assert "secret" not in captured.out
    assert "postgresql://" not in captured.out
    assert "secret" not in captured.err


def test_main_refuses_a_missing_setting(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _load_module()
    monkeypatch.delenv("FIN_CHECKPOINT_URL", raising=False)

    exit_code = module.main()

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "FIN_CHECKPOINT_URL" in captured.err


def test_the_browser_acceptance_script_releases_the_claim_before_the_demo_starts() -> None:
    script = Path(__file__).resolve().parents[2] / "scripts" / "demo-browser-acceptance.sh"
    source = script.read_text(encoding="utf-8")

    release_index = source.index('python "$RELEASE_CLAIM"')
    demo_index = source.index('"$FIN_COMMAND" demo >/dev/null')
    assert release_index < demo_index, "the claim must be released before the demo boots"
