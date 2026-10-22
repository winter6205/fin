"""Database identity and backup/restore on a private scratch Postgres.

Each module run gets a uniquely named container on a dynamically assigned
loopback port. Every database touched by a test ends in ``_test`` and its URL
is derived from the fixture's ``FIN_TEST_DSN``. Teardown removes only the
databases created by this module and its own container.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from typing import Iterator
from urllib.parse import urlparse

import psycopg
from psycopg import sql
import pytest

from fin.host.backup import (
    BackupError,
    append_thread_tombstone,
    backup_checkpoint,
    restore_checkpoint,
)
from fin.host.cli import backup_main
from fin.host.instance_identity import InstanceIdentityError
from fin.host.thread_store import PostgresThreadStore, RunRecord

_SUFFIX = uuid.uuid4().hex
_DRILL = f"fin-batch1-drill-{_SUFFIX}"
_ADMIN_DATABASE = "fin_batch1_admin_test"
_CREATED_DATABASES: list[str] = []
_SHIM_CONTAINER = "__FIN_DRILL_CONTAINER__"
_SHIM_PORT = "__FIN_TEST_PORT__"
_SHIM = """\
#!/usr/bin/env python3
import os
import subprocess
import sys
from urllib.parse import urlparse

CONTAINER = "__FIN_DRILL_CONTAINER__"
TEST_PORT = __FIN_TEST_PORT__
TOOL = os.path.basename(sys.argv[0])


def flag(name: str) -> str | None:
    argv = sys.argv[1:]
    if name not in argv:
        return None
    return argv[argv.index(name) + 1]


def database(url: str) -> tuple[str, str | None, int | None]:
    parsed = urlparse(url)
    return parsed.path.lstrip("/"), parsed.hostname, parsed.port


def is_private_test_database(url: str) -> bool:
    name, host, port = database(url)
    return name.endswith("_test") and host == "127.0.0.1" and port == TEST_PORT


def main() -> int:
    if TOOL == "pg_dump":
        dest = flag("--file")
        url = flag("--dbname")
        if dest is None or url is None:
            return 2
        db, _, _ = database(url)
        if not is_private_test_database(url):
            return 1
        remote = "/tmp/fin-batch1-drill.dump"
        rc = subprocess.call(
            [
                "docker",
                "exec",
                CONTAINER,
                "pg_dump",
                "-U",
                "postgres",
                "-d",
                db,
                "-Fc",
                "-f",
                remote,
            ]
        )
        if rc != 0:
            return rc
        rc = subprocess.call(["docker", "cp", f"{CONTAINER}:{remote}", dest])
        subprocess.call(["docker", "exec", CONTAINER, "rm", "-f", remote])
        return rc
    if TOOL == "pg_restore":
        url = flag("--dbname")
        dump = sys.argv[-1]
        if url is None:
            return 2
        db, _, _ = database(url)
        if not is_private_test_database(url):
            return 1
        remote = "/tmp/fin-batch1-drill.restore"
        rc = subprocess.call(["docker", "cp", dump, f"{CONTAINER}:{remote}"])
        if rc != 0:
            return rc
        rc = subprocess.call(
            [
                "docker",
                "exec",
                CONTAINER,
                "pg_restore",
                "--clean",
                "--if-exists",
                "--no-owner",
                "--exit-on-error",
                "-U",
                "postgres",
                "-d",
                db,
                remote,
            ]
        )
        subprocess.call(["docker", "exec", CONTAINER, "rm", "-f", remote])
        return rc
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
"""


def _require_private_drill_names() -> None:
    if not _DRILL.startswith("fin-batch1-drill-"):
        raise RuntimeError(_DRILL)


def _shim_text(port: int) -> str:
    _require_private_drill_names()
    text = _SHIM.replace(_SHIM_CONTAINER, _DRILL, 1).replace(
        _SHIM_PORT, str(port), 1
    )
    if _SHIM_CONTAINER in text or _SHIM_PORT in text:
        raise RuntimeError("drill shim kept a placeholder")
    if 'CONTAINER = "fin-batch1-drill"' in text:
        raise RuntimeError("drill shim kept a shared container name")
    if f'CONTAINER = "{_DRILL}"' not in text:
        raise RuntimeError("drill shim is missing its container name")
    if f"TEST_PORT = {port}" not in text:
        raise RuntimeError("drill shim is missing its private port")
    return text


def _remove_drill() -> None:
    _require_private_drill_names()
    subprocess.run(["docker", "rm", "-f", _DRILL], check=False)
    container = subprocess.run(
        ["docker", "inspect", _DRILL],
        capture_output=True,
        check=False,
    )
    if container.returncode == 0:
        raise RuntimeError("scratch drill resources were not removed")


def _ensure_drill() -> str:
    _require_private_drill_names()
    if not shutil.which("docker"):
        raise RuntimeError("docker is required for the database identity tests")
    try:
        subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                _DRILL,
                "-e",
                "POSTGRES_HOST_AUTH_METHOD=trust",
                "-e",
                f"POSTGRES_DB={_ADMIN_DATABASE}",
                "-p",
                "127.0.0.1::5432",
                "postgres:16-alpine",
            ],
            capture_output=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        _remove_drill()
        raise


    try:
        published = subprocess.run(
            ["docker", "port", _DRILL, "5432/tcp"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        match = re.fullmatch(r"127\.0\.0\.1:(\d+)", published)
        if match is None:
            raise RuntimeError("scratch Postgres is not bound to a loopback port")
        port = int(match.group(1))
        admin_dsn = f"postgresql://postgres@127.0.0.1:{port}/{_ADMIN_DATABASE}"
        for _ in range(40):
            try:
                with psycopg.connect(admin_dsn, connect_timeout=1) as conn:
                    conn.execute("SELECT 1")
                return admin_dsn
            except psycopg.OperationalError:
                time.sleep(0.5)
        raise RuntimeError(
            "scratch Postgres did not become ready within the wait window"
        )
    except Exception:
        _remove_drill()
        raise


def test_scratch_postgres_readiness_requires_tcp_sql_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    docker_commands: list[list[str]] = []
    connection_calls: list[tuple[str, dict[str, int]]] = []
    cleanup_calls: list[bool] = []
    sleeps: list[float] = []
    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.__exit__.return_value = False

    def run(command: list[str], **_: object) -> SimpleNamespace:
        docker_commands.append(command)
        if command[1] == "port":
            return SimpleNamespace(returncode=0, stdout="127.0.0.1:45555\n")
        # Model pg_isready reporting the entrypoint's temporary Unix-socket server.
        if command[1] == "exec":
            return SimpleNamespace(returncode=0, stdout=b"")
        return SimpleNamespace(returncode=0, stdout=b"")

    def connect(dsn: str, *, connect_timeout: int) -> MagicMock:
        connection_calls.append((dsn, {"connect_timeout": connect_timeout}))
        if len(connection_calls) < 3:
            raise psycopg.OperationalError("server is not accepting TCP connections")
        return connection

    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(psycopg, "connect", connect)
    monkeypatch.setattr(time, "sleep", sleeps.append)
    monkeypatch.setattr(
        sys.modules[__name__],
        "_remove_drill",
        lambda: cleanup_calls.append(True),
    )

    admin_dsn = _ensure_drill()

    parsed = psycopg.conninfo.conninfo_to_dict(admin_dsn)
    assert parsed["host"] == "127.0.0.1"
    assert parsed["port"] == "45555"
    assert parsed["dbname"].endswith("_test")
    assert len(connection_calls) == 3
    assert all(dsn == admin_dsn for dsn, _ in connection_calls)
    assert all(options == {"connect_timeout": 1} for _, options in connection_calls)
    connection.execute.assert_called_once_with("SELECT 1")
    assert sleeps == [0.5, 0.5]
    assert not any(command[1] == "exec" for command in docker_commands)
    assert cleanup_calls == []


def test_scratch_postgres_readiness_timeout_removes_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection_calls: list[str] = []
    cleanup_calls: list[bool] = []
    sleeps: list[float] = []

    def run(command: list[str], **_: object) -> SimpleNamespace:
        if command[1] == "port":
            return SimpleNamespace(returncode=0, stdout="127.0.0.1:45555\n")
        return SimpleNamespace(returncode=0, stdout=b"")

    def connect(dsn: str, *, connect_timeout: int) -> MagicMock:
        assert connect_timeout == 1
        connection_calls.append(dsn)
        raise psycopg.OperationalError("server is not accepting TCP connections")

    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(psycopg, "connect", connect)
    monkeypatch.setattr(time, "sleep", sleeps.append)
    monkeypatch.setattr(
        sys.modules[__name__],
        "_remove_drill",
        lambda: cleanup_calls.append(True),
    )

    with pytest.raises(RuntimeError, match="did not become ready"):
        _ensure_drill()

    assert len(connection_calls) == 40
    assert len(sleeps) == 40
    assert cleanup_calls == [True]


def _test_admin_dsn() -> str:
    dsn = (os.environ.get("FIN_TEST_DSN") or "").strip()
    if not dsn:
        raise RuntimeError("storage identity tests require FIN_TEST_DSN")
    database = psycopg.conninfo.conninfo_to_dict(dsn).get("dbname", "")
    if not database.endswith("_test"):
        raise RuntimeError("FIN_TEST_DSN must name a database ending in _test")
    return dsn


def _drop_test_databases() -> None:
    if not _CREATED_DATABASES:
        return
    with psycopg.connect(_test_admin_dsn(), autocommit=True) as conn:
        for name in reversed(_CREATED_DATABASES):
            conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (name,),
            )
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name))
            )
    _CREATED_DATABASES.clear()


def _recreate(name: str) -> str:
    if not name.startswith("fin_batch1_") or name.endswith("_test"):
        raise AssertionError(name)
    database = f"{name}_{_SUFFIX[:12]}_test"
    admin_dsn = _test_admin_dsn()
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()",
            (database,),
        )
        conn.execute(
            sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database))
        )
        conn.execute(
            sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database))
        )
    _CREATED_DATABASES.append(database)
    parsed = urlparse(admin_dsn)
    return parsed._replace(path=f"/{database}").geturl()


def _row_id(url: str) -> str | None:
    with psycopg.connect(url) as conn:
        rows = conn.execute("SELECT id::text FROM fin_instance").fetchall()
    if not rows:
        return None
    assert len(rows) == 1
    return str(rows[0][0])


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _count(url: str, table: str) -> int:
    with psycopg.connect(url) as conn:
        row = conn.execute(f"SELECT count(*) FROM {table}").fetchone()
    assert row is not None
    return int(row[0])


@pytest.fixture(scope="module")
def drill(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    previous = os.environ.get("PATH", "")
    previous_test_dsn = os.environ.get("FIN_TEST_DSN")
    previous_checkpoint_url = os.environ.get("FIN_CHECKPOINT_URL")
    admin_dsn: str | None = None
    _CREATED_DATABASES.clear()
    try:
        admin_dsn = _ensure_drill()
        os.environ["FIN_TEST_DSN"] = admin_dsn
        os.environ["FIN_CHECKPOINT_URL"] = admin_dsn
        port = urlparse(admin_dsn).port
        if port is None:
            raise RuntimeError("scratch Postgres DSN has no published port")
        bindir = tmp_path_factory.mktemp("pgbin")
        shim = _shim_text(port)
        script = bindir / "pg_dump"
        script.write_text(shim, encoding="utf-8")
        script.chmod(0o755)
        restore = bindir / "pg_restore"
        restore.write_text(shim, encoding="utf-8")
        restore.chmod(0o755)
        os.environ["PATH"] = f"{bindir}{os.pathsep}{previous}"
        yield admin_dsn
    finally:
        os.environ["PATH"] = previous
        try:
            if admin_dsn is not None:
                os.environ["FIN_TEST_DSN"] = admin_dsn
                _drop_test_databases()
        finally:
            try:
                if admin_dsn is not None:
                    _remove_drill()
            finally:
                if previous_test_dsn is None:
                    os.environ.pop("FIN_TEST_DSN", None)
                else:
                    os.environ["FIN_TEST_DSN"] = previous_test_dsn
                if previous_checkpoint_url is None:
                    os.environ.pop("FIN_CHECKPOINT_URL", None)
                else:
                    os.environ["FIN_CHECKPOINT_URL"] = previous_checkpoint_url


def test_both_absent_writes_one_matching_id(drill: str, tmp_path: Path) -> None:
    del drill
    url = _recreate("fin_batch1_new")
    home = tmp_path / "home"
    PostgresThreadStore(url, fin_home=home)
    text = (home / "instance_id").read_text(encoding="utf-8").strip()
    assert uuid.UUID(text)
    assert text == _row_id(url)
    assert _mode(home / "instance_id") == 0o600
    PostgresThreadStore(url, fin_home=home)
    assert (home / "instance_id").read_text(encoding="utf-8").strip() == text


def test_empty_instance_file_refuses_without_overwrite(
    drill: str, tmp_path: Path
) -> None:
    del drill
    url = _recreate("fin_batch1_empty")
    home = tmp_path / "home"
    PostgresThreadStore(url, fin_home=home)
    row_id = _row_id(url)
    assert row_id is not None
    (home / "instance_id").write_text("", encoding="utf-8")
    with pytest.raises(InstanceIdentityError) as caught:
        PostgresThreadStore(url, fin_home=home)
    message = str(caught.value)
    assert "missing" in message
    assert row_id in message
    assert (home / "instance_id").read_text(encoding="utf-8") == ""
    assert _row_id(url) == row_id


def test_missing_file_refuses_without_creating_one(
    drill: str, tmp_path: Path
) -> None:
    del drill
    url = _recreate("fin_batch1_nofile")
    home = tmp_path / "home"
    PostgresThreadStore(url, fin_home=home)
    row_id = _row_id(url)
    assert row_id is not None
    (home / "instance_id").unlink()
    with pytest.raises(InstanceIdentityError) as caught:
        PostgresThreadStore(url, fin_home=home)
    message = str(caught.value)
    assert "missing" in message
    assert row_id in message
    assert not (home / "instance_id").exists()
    assert _row_id(url) == row_id


def test_file_without_a_row_refuses(drill: str, tmp_path: Path) -> None:
    del drill
    src = _recreate("fin_batch1_file")
    empty = _recreate("fin_batch1_norow")
    home = tmp_path / "home"
    PostgresThreadStore(src, fin_home=home)
    file_id = (home / "instance_id").read_text(encoding="utf-8").strip()
    with pytest.raises(InstanceIdentityError) as caught:
        PostgresThreadStore(empty, fin_home=home)
    message = str(caught.value)
    assert file_id in message
    assert "missing" in message
    assert (home / "instance_id").read_text(encoding="utf-8").strip() == file_id
    with psycopg.connect(empty) as conn:
        count = conn.execute("SELECT count(*) FROM fin_instance").fetchone()
    assert count is not None
    assert count[0] == 0


def test_second_database_with_a_different_id_refuses(
    drill: str, tmp_path: Path
) -> None:
    del drill
    src = _recreate("fin_batch1_src")
    other = _recreate("fin_batch1_other")
    home = tmp_path / "home"
    PostgresThreadStore(src, fin_home=home)
    file_id = (home / "instance_id").read_text(encoding="utf-8").strip()
    other_id = str(uuid.uuid4())
    with psycopg.connect(other) as conn:
        conn.execute("CREATE TABLE fin_instance (id uuid primary key)")
        conn.execute("INSERT INTO fin_instance (id) VALUES (%s)", (other_id,))
        conn.commit()
    with pytest.raises(InstanceIdentityError) as caught:
        PostgresThreadStore(other, fin_home=home)
    message = str(caught.value)
    assert file_id in message
    assert other_id in message
    assert (home / "instance_id").read_text(encoding="utf-8").strip() == file_id
    assert _row_id(other) == other_id


def test_restore_refuses_when_destination_equals_source(tmp_path: Path) -> None:
    url = "postgresql://example/scratch"
    with pytest.raises(BackupError, match="equals"):
        restore_checkpoint(
            backup_dir=tmp_path / "missing",
            source_url=url,
            destination_url=url,
            fin_home=tmp_path,
        )


def test_restore_refuses_a_missing_backup(tmp_path: Path) -> None:
    with pytest.raises(BackupError, match="missing"):
        restore_checkpoint(
            backup_dir=tmp_path / "no-such-backup",
            source_url="postgresql://example/source",
            destination_url="postgresql://example/dest",
            fin_home=tmp_path,
        )


def test_failed_dump_leaves_no_backup_directory(drill: str, tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    missing_database = urlparse(drill)._replace(
        path="/fin_batch1_missing_dump_test"
    ).geturl()
    with pytest.raises(BackupError):
        backup_checkpoint(
            source_url=missing_database,
            fin_home=home,
        )
    backups = home / "backups"
    assert not backups.exists() or list(backups.iterdir()) == []


def test_backup_then_delete_then_restore_omits_the_tombstoned_thread(
    drill: str, tmp_path: Path
) -> None:
    del drill
    src = _recreate("fin_batch1_backup_src")
    dst = _recreate("fin_batch1_backup_dst")
    home = tmp_path / "home"
    store = PostgresThreadStore(src, fin_home=home)
    store.create_thread(id="thread-a", title="A")
    store.create_thread(id="thread-b", title="B")
    store.create_thread(id="thread-c", title="C")
    store.append_run(
        RunRecord(id="run-a", thread_id="thread-a", question="qa", status="done", answer="gone")
    )
    store.append_run(
        RunRecord(id="run-b", thread_id="thread-b", question="qb", status="done", answer="kept")
    )
    (home / "settings.yaml").write_text("marker-settings\n", encoding="utf-8")
    (home / "user.md").write_text("marker-profile\n", encoding="utf-8")
    (home / "corpus").mkdir()
    (home / "corpus" / "pushed_records.jsonl").write_text(
        "marker-ledger\n", encoding="utf-8"
    )
    with psycopg.connect(src, autocommit=True) as conn:
        conn.execute("CREATE TABLE checkpoints (thread_id text primary key)")
        conn.execute("INSERT INTO checkpoints (thread_id) VALUES ('thread-b')")
    backup_threads = _count(src, "fin_thread")
    backup_runs = _count(src, "fin_run")
    backup_checkpoints = _count(src, "checkpoints")
    with psycopg.connect(dst, autocommit=True) as conn:
        conn.execute("CREATE TABLE leftover (id integer primary key)")
        conn.execute("INSERT INTO leftover (id) VALUES (1)")
    when = datetime(2026, 10, 12, 1, 2, 3, tzinfo=timezone.utc)
    backup_dir = backup_checkpoint(source_url=src, fin_home=home, now=when)
    assert _mode(backup_dir) == 0o700
    dump = backup_dir / "checkpoint.dump"
    copied = backup_dir / "instance_id"
    assert dump.is_file()
    assert copied.read_bytes() == (home / "instance_id").read_bytes()
    assert _mode(dump) == 0o600
    assert _mode(copied) == 0o600
    assert (backup_dir / "settings.yaml").read_text(encoding="utf-8") == "marker-settings\n"
    assert (backup_dir / "user.md").read_text(encoding="utf-8") == "marker-profile\n"
    assert (backup_dir / "corpus" / "pushed_records.jsonl").read_text(
        encoding="utf-8"
    ) == "marker-ledger\n"
    assert _mode(backup_dir / "settings.yaml") == 0o600
    assert _mode(backup_dir / "user.md") == 0o600
    assert _mode(backup_dir / "corpus") == 0o700
    assert _mode(backup_dir / "corpus" / "pushed_records.jsonl") == 0o600
    assert not (backup_dir / "tombstones").exists()
    assert not (backup_dir / "backups").exists()
    store.update_thread_title("thread-b", title="changed-after-backup")
    append_thread_tombstone(
        home, thread_id="thread-a", deleted_at=when + timedelta(seconds=1)
    )
    append_thread_tombstone(
        home, thread_id="thread-c", deleted_at=when - timedelta(seconds=1)
    )
    assert (home / "tombstones").is_dir()
    assert _mode(home / "tombstones") == 0o700
    assert not str(home / "tombstones").startswith(str(backup_dir))
    store.delete_thread("thread-a")
    store.delete_thread("thread-c")
    restore_checkpoint(
        backup_dir=backup_dir,
        source_url=src,
        destination_url=dst,
        fin_home=home,
    )
    PostgresThreadStore(dst, fin_home=home)
    assert _count(dst, "checkpoints") == backup_checkpoints
    assert _count(dst, "fin_thread") == backup_threads - 1
    assert _count(dst, "fin_run") == backup_runs - 1
    with psycopg.connect(dst) as conn:
        threads = conn.execute(
            "SELECT id, title FROM fin_thread ORDER BY id"
        ).fetchall()
        runs = conn.execute(
            "SELECT id, thread_id, answer FROM fin_run ORDER BY id"
        ).fetchall()
        leftover = conn.execute("SELECT id FROM leftover").fetchall()
    assert threads == [("thread-b", "B"), ("thread-c", "C")]
    assert runs == [("run-b", "thread-b", "kept")]
    assert leftover == [(1,)]


def test_backup_main_names_a_missing_checkpoint_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("FIN_HOME", str(tmp_path / "fin-home"))
    monkeypatch.delenv("FIN_CHECKPOINT_URL", raising=False)
    assert backup_main([]) == 1
    captured = capsys.readouterr()
    assert "missing" in captured.err
    assert "://" not in captured.err
