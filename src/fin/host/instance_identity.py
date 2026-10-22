"""Compare ``<FIN_HOME>/instance_id`` with the single ``fin_instance`` row.

Both sides absent: create one uuid in the row and the file. Any other
disagreement raises and leaves both sides unchanged.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

_MISSING = "missing"


class InstanceIdentityError(Exception):
    """The checkpoint database is not the instance this home expects."""


def ensure_instance_identity(connection: Any, *, fin_home: Path) -> str:
    """Return the shared id, or raise ``InstanceIdentityError``."""
    home = Path(fin_home)
    file_present, file_id = _read_file(home)
    row_present, row_id = _read_row(connection)
    if not file_present and not row_present:
        return _create(connection, home)
    if file_present and row_present and file_id is not None and file_id == row_id:
        return file_id
    raise InstanceIdentityError(
        "instance identity mismatch: "
        f"file {_label(file_present, file_id)} database {_label(row_present, row_id)}"
    )


def _label(present: bool, value: str | None) -> str:
    if not present or value is None:
        return _MISSING
    return value


def _read_file(fin_home: Path) -> tuple[bool, str | None]:
    path = fin_home / "instance_id"
    if not path.is_file():
        return False, None
    return True, _canonical(path.read_text(encoding="utf-8").strip())


def _read_row(connection: Any) -> tuple[bool, str | None]:
    rows = connection.execute("SELECT id::text FROM fin_instance").fetchall()
    if len(rows) == 0:
        return False, None
    if len(rows) == 1:
        return True, _canonical(str(rows[0][0]))
    joined = ",".join(str(row[0]) for row in rows)
    return True, joined


def _canonical(text: str) -> str | None:
    if text == "":
        return None
    try:
        return str(uuid.UUID(text))
    except ValueError:
        return None


def _create(connection: Any, fin_home: Path) -> str:
    new_id = str(uuid.uuid4())
    fin_home.mkdir(parents=True, exist_ok=True)
    path = fin_home / "instance_id"
    _write_exclusive(path, new_id)
    try:
        connection.execute("INSERT INTO fin_instance (id) VALUES (%s)", (new_id,))
        connection.commit()
    except Exception:
        path.unlink(missing_ok=True)
        connection.rollback()
        raise
    return new_id


def _write_exclusive(path: Path, new_id: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, f"{new_id}\n".encode())
    except Exception:
        os.close(fd)
        path.unlink(missing_ok=True)
        raise
    os.close(fd)
    os.chmod(path, 0o600)


__all__ = ["InstanceIdentityError", "ensure_instance_identity"]
