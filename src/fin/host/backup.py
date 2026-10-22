"""Checkpoint backup, restore, and tombstone replay.

A failed dump removes its temporary directory before the error escapes.
The backup directory holds the dump plus every ``FIN_HOME`` entry except
``backups/`` and ``tombstones/``. Tombstones stay beside the home and are
not copied in.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import psycopg

_DUMP_NAME = "checkpoint.dump"
_STAMP = "%Y%m%dT%H%M%SZ"
_HOME_SKIP = frozenset({"backups", "tombstones"})


class BackupError(Exception):
    """Backup or restore did not finish."""


def backup_checkpoint(
    *,
    source_url: str,
    fin_home: Path,
    now: datetime | None = None,
) -> Path:
    """Write one backup directory. Return it only after the dump succeeds."""
    created = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    final = Path(fin_home) / "backups" / created.strftime(_STAMP)
    if final.exists():
        raise BackupError(f"backup already exists: {final}")
    parent = Path(fin_home) / "backups"
    parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(parent, 0o700)
    temp = Path(tempfile.mkdtemp(prefix=".partial-", dir=parent))
    try:
        _fill_backup(temp, source_url=source_url, fin_home=Path(fin_home))
        os.chmod(temp, 0o700)
        os.rename(temp, final)
    except Exception:
        if temp.exists():
            shutil.rmtree(temp)
        raise
    return final


def restore_checkpoint(
    *,
    backup_dir: Path,
    source_url: str,
    destination_url: str,
    fin_home: Path,
) -> None:
    """Restore into ``destination_url``, then replay later tombstones."""
    if source_url.strip() == destination_url.strip():
        raise BackupError("destination URL equals the source URL")
    directory = Path(backup_dir)
    dump = _dump_file(directory)
    created = _created_at(directory)
    _run(_restore_argv(destination_url, dump), "pg_restore failed")
    _replay(Path(fin_home), destination_url, created)


def append_thread_tombstone(
    fin_home: Path,
    *,
    thread_id: str,
    deleted_at: datetime,
) -> None:
    """Append one tombstone outside the backup directory."""
    directory = Path(fin_home) / "tombstones"
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)
    path = directory / "threads.jsonl"
    moment = deleted_at.astimezone(timezone.utc)
    line = json.dumps(
        {"deleted_at": moment.isoformat(), "thread_id": thread_id},
        sort_keys=True,
    )
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, (line + "\n").encode())
    finally:
        os.close(fd)
    os.chmod(path, 0o600)


def _fill_backup(directory: Path, *, source_url: str, fin_home: Path) -> None:
    dump_path = directory / _DUMP_NAME
    _run(
        ["pg_dump", "-Fc", "--file", str(dump_path), "--dbname", source_url],
        "pg_dump failed",
    )
    _copy_home(fin_home, directory)
    _protect(directory)


def _copy_home(fin_home: Path, directory: Path) -> None:
    for child in fin_home.iterdir():
        if child.name in _HOME_SKIP:
            continue
        if child.name == _DUMP_NAME or child.is_symlink() or not child.exists():
            raise BackupError(f"home entry cannot be copied: {child.name}")
        target = directory / child.name
        if child.is_dir():
            shutil.copytree(child, target)
        elif child.is_file():
            shutil.copyfile(child, target)
        else:
            raise BackupError(f"home entry cannot be copied: {child.name}")


def _protect(directory: Path) -> None:
    os.chmod(directory, 0o700)
    for child in directory.rglob("*"):
        if child.is_symlink():
            raise BackupError(f"home entry cannot be copied: {child.name}")
        os.chmod(child, 0o700 if child.is_dir() else 0o600)


def _dump_file(directory: Path) -> Path:
    if not directory.is_dir():
        raise BackupError(f"backup missing: {directory}")
    dump = directory / _DUMP_NAME
    if not dump.is_file():
        raise BackupError(f"backup missing: {dump}")
    return dump


def _created_at(directory: Path) -> datetime:
    try:
        parsed = datetime.strptime(directory.name, _STAMP)
    except ValueError as exc:
        raise BackupError(f"backup missing: {directory}") from exc
    return parsed.replace(tzinfo=timezone.utc)


def _restore_argv(destination_url: str, dump: Path) -> list[str]:
    return [
        "pg_restore",
        "--clean",
        "--if-exists",
        "--no-owner",
        "--exit-on-error",
        "--dbname",
        destination_url,
        str(dump),
    ]


def _replay(fin_home: Path, destination_url: str, created_at: datetime) -> None:
    path = fin_home / "tombstones" / "threads.jsonl"
    if not path.is_file():
        return
    doomed = _tombstones_after(path, created_at)
    if not doomed:
        return
    try:
        with psycopg.connect(destination_url) as conn:
            for thread_id in doomed:
                conn.execute("DELETE FROM fin_thread WHERE id = %s", (thread_id,))
            conn.commit()
    except psycopg.Error as exc:
        raise BackupError("tombstone replay failed") from exc


def _tombstones_after(path: Path, created_at: datetime) -> list[str]:
    doomed: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip() == "":
            continue
        row = json.loads(line)
        deleted = datetime.fromisoformat(str(row["deleted_at"]))
        if deleted.tzinfo is None:
            deleted = deleted.replace(tzinfo=timezone.utc)
        if deleted > created_at:
            doomed.append(str(row["thread_id"]))
    return doomed


def _run(argv: list[str], failed: str) -> None:
    try:
        completed = subprocess.run(argv, capture_output=True, text=True, check=False)
    except OSError as exc:
        raise BackupError(failed) from exc
    if completed.returncode != 0:
        raise BackupError(failed)


__all__ = [
    "BackupError",
    "append_thread_tombstone",
    "backup_checkpoint",
    "restore_checkpoint",
]
