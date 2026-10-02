"""Every pinned weight the driver reports must carry the digest it measured.

``scripts/g4_pipeline/driver.py`` hashes the mounted artifact cache once per
measured run, into ``heron_artifact`` in the JSONL line. It used to hash only
paths containing ``layout``, so ``tableformer_accurate.safetensors`` (212,758,388
bytes) was reported with a size and **no sha256**: a swap performed after the
build's ``pin.py verify-models`` gate and during a measurement left the run's own
record unable to distinguish the pinned weights from substituted ones. That gap
is recorded in ``docs/validation/2026-09-29-324-c1-reproducible-pin.md`` (L5).

The driver's own rule at its head is that a reading which did not happen is
never a zero, and the committed records cannot show that for an artifact whose
hash was simply omitted. So a file that cannot be read must be reported with
``sha256: null`` and a named ``sha256_error``, never as a missing key and never
as a value.

The driver normally runs inside the pinned image, so it is loaded by path here
and only ``artifact_identity`` is called. Every collaborator is a local double
or a few-byte local file; nothing here touches the network or the real cache.
"""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DRIVER_PATH = PROJECT_ROOT / "scripts" / "g4_pipeline" / "driver.py"
MODELS_LOCK = PROJECT_ROOT / "scripts" / "g4_pipeline" / "models.lock"

# The real relative paths, so no test can pass by inventing a layout-shaped name
# for what is actually a tableformer artifact.
HERON = "docling-project--docling-layout-heron/model.safetensors"
TABLEFORMER = (
    "docling-project--docling-models/model_artifacts/tableformer/accurate"
    "/tableformer_accurate.safetensors"
)


def _driver() -> ModuleType:
    spec = importlib.util.spec_from_file_location("g4_pipeline_artifact_hash", DRIVER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def cache(tmp_path: Path):
    """A planted cache: `plant(rel, body)` writes, `walk()` measures."""
    root = tmp_path / "models"

    def plant(rel: str, body: bytes) -> Path:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        return path

    plant.root = root  # type: ignore[attr-defined]
    return plant


def _entry(info: dict[str, object], key: str, rel: str) -> dict[str, object]:
    entries = info[key]
    assert isinstance(entries, list)
    found = [e for e in entries if e["path"] == rel]
    assert len(found) == 1, f"{rel} must be reported exactly once in {key}, got {found}"
    return found[0]


# --------------------------------------------------------------------------
# Normal behavior: the non-layout weights are hashed, like the layout ones.
# --------------------------------------------------------------------------


def test_a_large_non_layout_artifact_carries_a_sha256(cache) -> None:
    """The defect itself: TableFormer is reported with a size and no digest."""
    driver = _driver()
    # Over the 1 MB line the driver reports `files[]` entries at, and distinct
    # bodies so the two digests cannot be confused.
    heron = b"heron-weights" * 100_000
    tableformer = b"tableformer-weights" * 100_000
    cache(HERON, heron)
    cache(TABLEFORMER, tableformer)

    entry = _entry(driver.artifact_identity(cache.root), "files", TABLEFORMER)

    assert entry.get("sha256") == hashlib.sha256(tableformer).hexdigest(), (
        "the driver must hash every pinned artifact it reports, not only the "
        f"layout ones; got {entry!r}"
    )


def test_a_reported_artifact_bytes_is_the_size_that_was_hashed(cache) -> None:
    """Size and digest must describe the same bytes.

    A `stat()` taken before the read and a digest taken during it describe two
    different files if the file moves underneath the measurement, which is
    exactly the swap this contract exists to make visible.
    """
    driver = _driver()
    cache(TABLEFORMER, b"abc" * 400_000)

    entry = _entry(driver.artifact_identity(cache.root), "files", TABLEFORMER)

    assert entry["bytes"] == 1_200_000
    assert entry["sha256"] == hashlib.sha256(b"abc" * 400_000).hexdigest()


def test_the_layout_artifact_still_lands_in_layout_files(cache) -> None:
    """`layout_files[]` is the lane the committed records carry; it must not
    change, or every recorded measurement's shape moves under a later reader.
    """
    driver = _driver()
    cache(HERON, b"h" * 2_000_000)
    cache(TABLEFORMER, b"t" * 2_000_000)

    info = driver.artifact_identity(cache.root)

    layout = _entry(info, "layout_files", HERON)
    assert layout["sha256"] == hashlib.sha256(b"h" * 2_000_000).hexdigest()
    assert not [e for e in info["layout_files"] if e["path"] == TABLEFORMER], (
        "a tableformer path is not a layout path; putting it in layout_files[] "
        "would change what that lane means"
    )


def test_a_small_unreported_file_is_counted_but_not_hashed(cache) -> None:
    """`files[]` only ever carried artifacts over 1 MB. Widening the hashing
    must not widen the reported set, and must not change what the cache totals
    say: those count every file walked, hashed or not.
    """
    driver = _driver()
    cache(HERON, b"h" * 2_000_000)
    cache(TABLEFORMER, b"t" * 2_000_000)
    cache("notes.json", b"{}")

    info = driver.artifact_identity(cache.root)

    assert info["file_count"] == 3
    assert info["cache_total_bytes"] == 4_000_000 + 2
    assert not [e for e in info["files"] if e["path"] == "notes.json"]


# --------------------------------------------------------------------------
# Failure behavior: a swap is visible, and an unreadable file is a named gap.
# --------------------------------------------------------------------------


def test_a_swapped_tableformer_payload_changes_the_reported_digest(cache) -> None:
    """A swap mid-run must not be able to produce the same record twice."""
    driver = _driver()
    path = cache(TABLEFORMER, b"t" * 2_000_000)
    before = _entry(driver.artifact_identity(cache.root), "files", TABLEFORMER)["sha256"]

    # Same size, different bytes: a size-only check cannot see this.
    path.write_bytes(b"s" * 2_000_000)
    after = _entry(driver.artifact_identity(cache.root), "files", TABLEFORMER)["sha256"]

    assert before != after, "a replaced payload of equal size must change the digest"
    assert after == hashlib.sha256(b"s" * 2_000_000).hexdigest()


def test_an_unreadable_artifact_is_a_named_gap_not_a_zero(cache, monkeypatch) -> None:
    """A failed read is `None` plus a reason, never an absent key.

    The entry itself is still reported: dropping it would make an unread
    artifact indistinguishable from one that was never there, which is the
    absence this harness is not allowed to emit.
    """
    driver = _driver()
    cache(TABLEFORMER, b"t" * 2_000_000)

    def refuse(path: Path) -> tuple[int, str]:
        raise OSError(13, "simulated unreadable artifact")

    monkeypatch.setattr(driver, "sha256_bytes", refuse)

    entry = _entry(driver.artifact_identity(cache.root), "files", TABLEFORMER)

    assert entry["sha256"] is None, f"an unread digest is null, not {entry.get('sha256')!r}"
    assert "PermissionError" in str(entry.get("sha256_error")), (
        f"the gap must name the failure that caused it: {entry!r}"
    )
    assert "simulated unreadable artifact" in str(entry["sha256_error"])


def test_a_legible_artifact_reports_no_error_sibling(cache) -> None:
    """The `<key>_error` sibling exists only together with the null, so a run
    that did read its weights keeps the key tree the committed measurements
    carry.
    """
    driver = _driver()
    cache(TABLEFORMER, b"t" * 2_000_000)

    entry = _entry(driver.artifact_identity(cache.root), "files", TABLEFORMER)

    assert entry["bytes"] == 2_000_000
    assert "sha256_error" not in entry


def test_an_absent_cache_is_still_a_gap(cache) -> None:
    """The pre-existing rule this widens must not be lost: nothing was walked,
    so nothing is reported and the totals are null rather than zero.
    """
    driver = _driver()

    info = driver.artifact_identity(cache.root / "never-mounted")

    assert info["exists"] is False
    assert info["cache_total_bytes"] is None
    assert info["file_count"] is None
    assert info["files"] == []


def test_an_artifact_too_large_to_hash_is_a_named_gap(cache, monkeypatch) -> None:
    """A weight past the hashing ceiling is a digest that was not measured.

    It stays in the reported set with `sha256: null` and the reason. Dropping
    the entry, or hashing it anyway and running the measurement into its 4 GiB
    ceiling, are the two failures this leg rules out.
    """
    driver = _driver()
    cache(TABLEFORMER, b"t" * 1_000_001)

    def refuse(path: Path) -> tuple[int, str]:
        raise AssertionError(f"the driver must not hash {path}: it is over the ceiling")

    # The real ceiling is 2 GiB, so a sparse file that size is planted rather
    # than written; a 1 MB one with the ceiling lowered tests the same branch.
    monkeypatch.setattr(driver, "_MAX_HASHED_ARTIFACT_BYTES", 1_000_000, raising=True)
    monkeypatch.setattr(driver, "sha256_bytes", refuse)

    entry = _entry(driver.artifact_identity(cache.root), "files", TABLEFORMER)

    assert entry["bytes"] == 1_000_001
    assert entry["sha256"] is None
    assert "over the" in str(entry.get("sha256_error")), (
        f"the gap must say why no digest was taken: {entry!r}"
    )


def test_a_layout_artifact_too_large_to_hash_falls_out_of_the_layout_lane(cache, monkeypatch) -> None:
    """A layout path over the ceiling is reported in `files[]`, exactly as it
    was before: `layout_files[]` is the lane of artifacts that carry a measured
    digest, and an unhashed weight would make a reader of that lane assume one.
    """
    driver = _driver()
    cache(HERON, b"h" * 1_000_001)

    monkeypatch.setattr(
        driver, "_MAX_HASHED_ARTIFACT_BYTES", 1_000_000, raising=True
    )

    info = driver.artifact_identity(cache.root)

    assert info["layout_files"] == []
    assert _entry(info, "files", HERON)["sha256"] is None


# --------------------------------------------------------------------------
# The pin this measures against has one source of truth.
# --------------------------------------------------------------------------


def test_the_pinned_tableformer_digest_is_still_the_one_models_lock_names() -> None:
    """The expected value lives in `models.lock`; the driver measures, it does
    not carry a second copy of the pin. Guarded so the two cannot drift into two
    truths about the same 212,758,388 bytes.
    """
    block = MODELS_LOCK.read_text(encoding="utf-8").split("[tableformer-accurate]", 1)[1]
    digest = next(
        line.split("=", 1)[1].strip()
        for line in block.splitlines()
        if line.strip().startswith("file_sha256")
    )
    assert digest == "2a7d6c924b3cd12fb99a09280ca9c33a89c5d60b93253617d2e088c1a40374d9"
    assert len(digest) == 64
