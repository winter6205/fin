"""Atomic report write: two concurrent eval processes must not tear
``report.json`` (T3 / ADR 0042).

The baseline file is the canonical §B1 artifact; spec SC1 / ADR 0042
require that two simultaneous ``run`` invocations do not leave a
half-written file behind. The protection is a ``fcntl`` flock on a
sibling lock file plus a temp-file-then-rename swap.

These tests run in the default lane: the lock + temp-file pattern is
deterministic and exercises no subprocess / network.
"""

from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from fin.eval.ablation.report import (
    REPORT_FILENAME,
    write_report_atomically,
)


def _payload(tag: str) -> dict:
    return {
        "summary": {"configuration_count": 10, "tag": tag},
        "configurations": [],
        "improvement_claims": [],
        "capability_contributions": [],
    }


def test_write_report_atomically_writes_a_well_formed_file(tmp_path: Path) -> None:
    out = tmp_path / "ablation"
    write_report_atomically(out / REPORT_FILENAME, _payload("solo"))
    written = json.loads((out / REPORT_FILENAME).read_text(encoding="utf-8"))
    assert written["summary"]["tag"] == "solo"


def test_write_report_atomically_leaves_no_tmp_file_on_success(tmp_path: Path) -> None:
    out = tmp_path / "ablation"
    target = out / REPORT_FILENAME
    write_report_atomically(target, _payload("clean"))
    # The temp file must have been renamed over the target. The lock
    # sibling is allowed to remain — it is the serialization handle.
    assert not (out / (REPORT_FILENAME + ".tmp")).exists()
    assert target.is_file()


def test_concurrent_writers_do_not_tear_the_report(tmp_path: Path) -> None:
    """Two threads call ``write_report_atomically`` with their own
    payloads. Each invocation holds an exclusive flock on the sibling
    lock file, so the final document is always one of the two whole
    payloads (never a half-written mix)."""
    out = tmp_path / "ablation"
    target = out / REPORT_FILENAME

    def write(tag: str) -> str:
        write_report_atomically(target, _payload(tag))
        return json.loads(target.read_text(encoding="utf-8"))["summary"]["tag"]

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [
            pool.submit(write, tag)
            for tag in ("alpha", "beta", "gamma", "delta")
        ]
        observed = [f.result() for f in futures]

    final = json.loads(target.read_text(encoding="utf-8"))
    assert final["summary"]["tag"] in {"alpha", "beta", "gamma", "delta"}
    # Every observed read on the file saw a complete document — none of
    # them returned an empty string, which is what a torn write would
    # yield if the rename raced.
    assert all(tag in {"alpha", "beta", "gamma", "delta"} for tag in observed), observed


def test_failed_write_does_not_leave_a_partial_report(tmp_path: Path) -> None:
    """If serialisation raises mid-write, the existing ``report.json``
    is left untouched (the temp-file rename never happens)."""
    out = tmp_path / "ablation"
    target = out / REPORT_FILENAME
    write_report_atomically(target, _payload("initial"))
    initial_bytes = target.read_bytes()

    class _Exploding:
        def __call__(self, *_args, **_kwargs):
            raise RuntimeError("simulated encode failure")

    with pytest.raises(RuntimeError):
        write_report_atomically(target, _Exploding())
    assert target.read_bytes() == initial_bytes


def test_lock_is_released_after_write(tmp_path: Path) -> None:
    """A second writer that starts after the first finishes acquires
    the lock without blocking past the timeout. Use the function's
    own helpers rather than poking at the lock fd directly."""
    out = tmp_path / "ablation"
    target = out / REPORT_FILENAME
    write_report_atomically(target, _payload("first"))
    started = time.perf_counter()
    write_report_atomically(target, _payload("second"))
    elapsed = time.perf_counter() - started
    # Loose bound — the second call only needs to observe the released
    # lock, not race against an in-flight write. 5 s is comfortable on
    # a loaded CI box; the test fails only if the lock was leaked.
    assert elapsed < 5.0
    assert json.loads(target.read_text(encoding="utf-8"))["summary"]["tag"] == "second"
