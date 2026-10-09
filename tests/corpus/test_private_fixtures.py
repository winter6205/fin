"""Contracts for loading maintainer-held fixtures outside the repository."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
from pathlib import Path
import subprocess
import sys

import pytest

from fin.corpus._private_fixtures import (
    MAX_FIXTURE_BYTES,
    PrivateFixtureInvalidError,
    PrivateFixtureMissingError,
    load_private_fixture,
)

FIXTURE_ROOT_ENV = "FIN_PRIVATE_FIXTURES"


def _write_fixture(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, payload: bytes
) -> tuple[str, Path]:
    root = tmp_path / "vault"
    values = root / "values"
    values.mkdir(parents=True)
    key = hashlib.sha256(payload).hexdigest()
    path = values / f"{key}.json"
    path.write_bytes(payload)
    monkeypatch.setenv(FIXTURE_ROOT_ENV, str(root))
    return key, path


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (b'"fixture-only text: caf\xc3\xa9"', "fixture-only text: café"),
        (b"17", 17),
        (b'["synthetic", 3]', ["synthetic", 3]),
        (b'{"case": "synthetic", "enabled": true}', {"case": "synthetic", "enabled": True}),
    ],
)
def test_loads_digest_named_utf8_json_and_preserves_json_type(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    payload: bytes,
    expected: object,
) -> None:
    key, _ = _write_fixture(monkeypatch, tmp_path, payload)

    result = load_private_fixture(key)

    assert type(result) is type(expected)
    assert result == expected


def test_concurrent_reads_return_stable_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    key, _ = _write_fixture(
        monkeypatch, tmp_path, b'{"fixture": ["synthetic", 11]}'
    )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(load_private_fixture, [key] * 32))

    assert results == [{"fixture": ["synthetic", 11]}] * 32


def test_unset_fixture_root_is_a_named_missing_fixture_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(FIXTURE_ROOT_ENV, raising=False)

    with pytest.raises(PrivateFixtureMissingError):
        load_private_fixture(hashlib.sha256(b"[]").hexdigest())


def test_missing_fixture_file_is_a_named_missing_fixture_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(FIXTURE_ROOT_ENV, str(tmp_path / "empty-vault"))

    with pytest.raises(PrivateFixtureMissingError):
        load_private_fixture(hashlib.sha256(b"[]").hexdigest())


@pytest.mark.parametrize(
    "key",
    [
        None,
        -1,
        "",
        "../../" + "a" * 58,
        "g" * 64,
        "a" * 63,
        "f" * 65,
        "A" * 64,
    ],
)
def test_invalid_key_shape_is_rejected_before_file_access(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, key: object
) -> None:
    monkeypatch.setenv(FIXTURE_ROOT_ENV, str(tmp_path / "empty-vault"))

    with pytest.raises(PrivateFixtureInvalidError):
        load_private_fixture(key)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "payload",
    [
        b'"synthetic private marker: source text must stay out of errors',
        b'{"private_text": "synthetic private marker",',
        b'\xffsynthetic private marker',
    ],
)
def test_invalid_fixture_bytes_fail_without_echoing_source_text(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    payload: bytes,
) -> None:
    key, _ = _write_fixture(monkeypatch, tmp_path, payload)

    with pytest.raises(PrivateFixtureInvalidError) as exc_info:
        load_private_fixture(key)

    assert "synthetic private marker" not in str(exc_info.value)


def test_digest_mismatch_is_invalid_without_echoing_payload(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    payload = b'"synthetic private marker"'
    key = hashlib.sha256(b'"different"').hexdigest()
    values = tmp_path / "vault" / "values"
    values.mkdir(parents=True)
    (values / f"{key}.json").write_bytes(payload)
    monkeypatch.setenv(FIXTURE_ROOT_ENV, str(values.parent))

    with pytest.raises(PrivateFixtureInvalidError) as exc_info:
        load_private_fixture(key)

    assert "synthetic private marker" not in str(exc_info.value)


def test_fixture_over_limit_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = b" " * (MAX_FIXTURE_BYTES + 1)
    key, _ = _write_fixture(monkeypatch, tmp_path, payload)

    with pytest.raises(PrivateFixtureInvalidError):
        load_private_fixture(key)


def test_read_oserror_becomes_missing_fixture_error_without_echoing_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    key, target = _write_fixture(monkeypatch, tmp_path, b"[]")
    original_open = Path.open

    def fail_for_fixture(path: Path, *args: object, **kwargs: object):
        if path == target:
            raise OSError("synthetic private marker")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_for_fixture)

    with pytest.raises(PrivateFixtureMissingError) as exc_info:
        load_private_fixture(key)

    assert "synthetic private marker" not in str(exc_info.value)


def test_import_does_not_require_private_fixture_environment(tmp_path: Path) -> None:
    env = os.environ.copy()
    env.pop(FIXTURE_ROOT_ENV, None)

    completed = subprocess.run(
        [sys.executable, "-c", "import fin.corpus._private_fixtures"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
