"""Tests for the T1 public-source eval corpus (plan T1 / spec Focus 1).

These tests exercise the schema, counting, and verify gates of the
T1 frozen corpus. They NEVER touch the network and NEVER rely on the
default ``uv run pytest -q`` discovering a live retrieve backend — the
frozen text lives on disk under ``data/corpus/dataset/t1/`` and the
manifest under ``data/corpus/dataset/t1_manifest.json``; both are
committed alongside the tests.

Test pyramid: every test is small (no I/O beyond reading the committed
manifest + the small frozen text fixtures). One test (the verify-t1 CLI
exit-code check) shells out to the CLI; it still uses the committed
manifest and the committed frozen files only.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from fin.corpus.dataset import t1 as t1_mod
from fin.corpus.dataset.t1 import (
    T1_MIN_QUERY_PER_SCENE,
    T1_SCENES,
    T1Query,
    T1Sample,
    build_t1_manifest_from_dir,
    is_t1_countable,
    load_t1_manifest,
    verify_t1_manifest,
)


# --------------------------------------------------------------------------
# Paths to committed fixtures
# --------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
T1_TEXT_DIR = _PROJECT_ROOT / "data" / "corpus" / "dataset" / "t1"
T1_MANIFEST_PATH = _PROJECT_ROOT / "data" / "corpus" / "dataset" / "t1_manifest.json"


def _committed_manifest() -> dict:
    return json.loads(T1_MANIFEST_PATH.read_text(encoding="utf-8"))


def _committed_text(path_rel: str) -> str:
    return (T1_TEXT_DIR / path_rel).read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# is_countable
# --------------------------------------------------------------------------


class TestIsT1Countable:
    def test_countable_when_required_fields_present(self) -> None:
        sample = {
            "sample_id": "t1-onboarding-001",
            "scene": 1,
            "source": {"url": "https://example/x", "license": "CC BY-SA 4.0"},
            "fetched_at": "2026-09-06",
            "sha256": "a" * 64,
            "path": "t1/onboarding-001.md",
            "document_id": "t1-onboarding-001",
        }
        ok, reason = is_t1_countable(sample)
        assert ok is True
        assert reason == ""

    def test_synthetic_flag_excludes_from_countable(self) -> None:
        sample = {
            "sample_id": "t1-fake",
            "scene": 1,
            "source": {"url": "https://example/x", "license": "CC BY-SA 4.0"},
            "fetched_at": "2026-09-06",
            "sha256": "a" * 64,
            "path": "t1/fake.md",
            "document_id": "t1-fake",
            "synthetic": True,
        }
        ok, reason = is_t1_countable(sample)
        assert ok is False
        assert reason == "synthetic"

    def test_missing_license_excludes(self) -> None:
        sample = {
            "sample_id": "t1-no-license",
            "scene": 1,
            "source": {"url": "https://example/x", "license": ""},
            "fetched_at": "2026-09-06",
            "sha256": "a" * 64,
            "path": "t1/x.md",
            "document_id": "t1-no-license",
        }
        ok, reason = is_t1_countable(sample)
        assert ok is False
        assert reason == "missing_license"

    def test_missing_url_excludes(self) -> None:
        sample = {
            "sample_id": "t1-no-url",
            "scene": 1,
            "source": {"url": "", "license": "CC BY-SA 4.0"},
            "fetched_at": "2026-09-06",
            "sha256": "a" * 64,
            "path": "t1/x.md",
            "document_id": "t1-no-url",
        }
        ok, reason = is_t1_countable(sample)
        assert ok is False
        assert reason == "missing_source"

    def test_missing_sha256_excludes(self) -> None:
        sample = {
            "sample_id": "t1-no-sha",
            "scene": 1,
            "source": {"url": "https://example/x", "license": "CC BY-SA 4.0"},
            "fetched_at": "2026-09-06",
            "sha256": "",
            "path": "t1/x.md",
            "document_id": "t1-no-sha",
        }
        ok, reason = is_t1_countable(sample)
        assert ok is False
        assert reason == "missing_sha256"

    def test_missing_fetched_at_excludes(self) -> None:
        sample = {
            "sample_id": "t1-no-fetched",
            "scene": 1,
            "source": {"url": "https://example/x", "license": "CC BY-SA 4.0"},
            "fetched_at": "",
            "sha256": "a" * 64,
            "path": "t1/x.md",
            "document_id": "t1-no-fetched",
        }
        ok, reason = is_t1_countable(sample)
        assert ok is False
        assert reason == "missing_fetched_at"

    def test_unknown_scene_excludes(self) -> None:
        sample = {
            "sample_id": "t1-bad-scene",
            "scene": 99,
            "source": {"url": "https://example/x", "license": "CC BY-SA 4.0"},
            "fetched_at": "2026-09-06",
            "sha256": "a" * 64,
            "path": "t1/x.md",
            "document_id": "t1-bad-scene",
        }
        ok, reason = is_t1_countable(sample)
        assert ok is False
        assert reason == "unknown_scene"

    def test_bad_sha256_format_excludes(self) -> None:
        sample = {
            "sample_id": "t1-bad-sha",
            "scene": 1,
            "source": {"url": "https://example/x", "license": "CC BY-SA 4.0"},
            "fetched_at": "2026-09-06",
            "sha256": "not-a-hex-digest",
            "path": "t1/x.md",
            "document_id": "t1-bad-sha",
        }
        ok, reason = is_t1_countable(sample)
        assert ok is False
        assert reason == "missing_sha256"


# --------------------------------------------------------------------------
# Committed manifest structure
# --------------------------------------------------------------------------


class TestCommittedManifest:
    def test_manifest_file_exists(self) -> None:
        assert T1_MANIFEST_PATH.is_file(), (
            f"committed T1 manifest missing at {T1_MANIFEST_PATH}"
        )

    def test_manifest_schema_keys(self) -> None:
        manifest = _committed_manifest()
        for key in ("schema_version", "dataset_id", "samples", "queries", "known_gaps"):
            assert key in manifest, f"manifest missing required key {key!r}"

    def test_manifest_dataset_id(self) -> None:
        manifest = _committed_manifest()
        assert manifest["dataset_id"] == t1_mod.T1_DATASET_ID

    def test_no_duplicate_sample_ids(self) -> None:
        manifest = _committed_manifest()
        ids = [s["sample_id"] for s in manifest["samples"]]
        assert len(ids) == len(set(ids)), "duplicate sample_id in manifest"

    def test_synthetic_samples_are_flagged(self) -> None:
        manifest = _committed_manifest()
        for s in manifest["samples"]:
            if s.get("synthetic") is True:
                assert "synthetic" in s.get("sample_id", "") or s.get(
                    "synthetic_origin"
                ), f"synthetic sample {s['sample_id']} missing provenance marker"


# --------------------------------------------------------------------------
# Countable set: provenance completeness
# --------------------------------------------------------------------------


class TestCommittedProvenance:
    def test_every_countable_has_url_license_fetched_sha(self) -> None:
        manifest = _committed_manifest()
        for s in manifest["samples"]:
            ok, _ = is_t1_countable(s)
            if not ok:
                continue
            assert s["source"]["url"], f"{s['sample_id']}: countable missing source.url"
            assert s["source"]["license"], f"{s['sample_id']}: missing source.license"
            assert s["fetched_at"], f"{s['sample_id']}: missing fetched_at"
            assert len(s["sha256"]) == 64, f"{s['sample_id']}: sha256 not 64 hex chars"

    def test_sha256_matches_frozen_text_on_disk(self) -> None:
        manifest = _committed_manifest()
        for s in manifest["samples"]:
            ok, _ = is_t1_countable(s)
            if not ok:
                continue
            path = T1_TEXT_DIR / s["path"]
            assert path.is_file(), f"{s['sample_id']}: frozen text missing at {path}"
            disk_sha = hashlib.sha256(path.read_bytes()).hexdigest()
            assert disk_sha == s["sha256"], (
                f"{s['sample_id']}: on-disk sha256 {disk_sha} != manifest {s['sha256']}"
            )


# --------------------------------------------------------------------------
# Per-scene coverage: each scene with frozen text has ≥1 human-checked query
# --------------------------------------------------------------------------


class TestPerSceneCoverage:
    def test_every_scene_with_frozen_text_has_query(self) -> None:
        manifest = _committed_manifest()
        scenes_with_text: set[int] = set()
        for s in manifest["samples"]:
            ok, _ = is_t1_countable(s)
            if ok and int(s["scene"]) in T1_SCENES:
                scenes_with_text.add(int(s["scene"]))
        scenes_with_query: set[int] = set()
        for q in manifest["queries"]:
            scene = int(q["scene"])
            if scene in T1_SCENES:
                scenes_with_query.add(scene)
        missing = scenes_with_text - scenes_with_query
        assert not missing, (
            f"scenes {sorted(missing)} have frozen text but no human-checked query"
        )

    def test_minimum_one_query_per_scene(self) -> None:
        manifest = _committed_manifest()
        per_scene: dict[int, int] = {}
        for q in manifest["queries"]:
            per_scene[int(q["scene"])] = per_scene.get(int(q["scene"]), 0) + 1
        for scene in T1_SCENES:
            assert per_scene.get(scene, 0) >= T1_MIN_QUERY_PER_SCENE, (
                f"scene {scene} needs >= {T1_MIN_QUERY_PER_SCENE} queries, "
                f"got {per_scene.get(scene, 0)}"
            )

    def test_query_evidence_span_is_substring_of_frozen_text(self) -> None:
        manifest = _committed_manifest()
        for q in manifest["queries"]:
            sample_id = q["sample_id"]
            sample = next(
                (s for s in manifest["samples"] if s["sample_id"] == sample_id),
                None,
            )
            assert sample is not None, (
                f"query {q['query_id']}: sample {sample_id} not in manifest"
            )
            text = _committed_text(sample["path"])
            span = q["evidence_span"]
            assert span in text, (
                f"query {q['query_id']}: evidence_span not found in {sample['path']}"
            )


# --------------------------------------------------------------------------
# verify_t1_manifest
# --------------------------------------------------------------------------


class TestVerifyT1:
    def test_committed_manifest_passes_verification(self) -> None:
        manifest = load_t1_manifest(T1_MANIFEST_PATH)
        report = verify_t1_manifest(manifest, text_dir=T1_TEXT_DIR)
        assert report.ok, (
            "committed manifest should verify cleanly; "
            f"problems={report.problems} mismatches={report.mismatches}"
        )

    def test_sha_mismatch_is_problem(self, tmp_path: Path) -> None:
        # Build a small in-memory manifest and corrupt the on-disk bytes.
        text_dir = tmp_path / "t1"
        text_dir.mkdir()
        (text_dir / "x.md").write_text("hello world", encoding="utf-8")
        real_sha = hashlib.sha256(b"hello world").hexdigest()
        sample = {
            "sample_id": "x",
            "scene": 1,
            "source": {"url": "https://example", "license": "CC BY-SA 4.0"},
            "fetched_at": "2026-09-06",
            "sha256": real_sha,
            "path": "x.md",
            "document_id": "x",
        }
        manifest = {
            "schema_version": t1_mod.T1_SCHEMA_VERSION,
            "dataset_id": t1_mod.T1_DATASET_ID,
            "samples": [sample],
            "queries": [],
            "known_gaps": [],
        }
        # Corrupt the on-disk bytes.
        (text_dir / "x.md").write_text("tampered", encoding="utf-8")
        report = verify_t1_manifest(manifest, text_dir=text_dir)
        assert not report.ok
        # The mismatch is recorded in `mismatches` (sha256 mismatch is a
        # hard failure distinct from a generic provenance problem).
        assert any(
            m["kind"] == "sha256" and m["sample_id"] == "x"
            for m in report.mismatches
        )

    def test_missing_frozen_text_is_problem(self, tmp_path: Path) -> None:
        text_dir = tmp_path / "t1"
        text_dir.mkdir()
        sample = {
            "sample_id": "ghost",
            "scene": 1,
            "source": {"url": "https://example", "license": "CC BY-SA 4.0"},
            "fetched_at": "2026-09-06",
            "sha256": "a" * 64,
            "path": "ghost.md",
            "document_id": "ghost",
        }
        manifest = {
            "schema_version": t1_mod.T1_SCHEMA_VERSION,
            "dataset_id": t1_mod.T1_DATASET_ID,
            "samples": [sample],
            "queries": [],
            "known_gaps": [],
        }
        report = verify_t1_manifest(manifest, text_dir=text_dir)
        assert not report.ok
        assert any("ghost.md" in p or "ghost" in p for p in report.problems)

    def test_scene_with_no_frozen_text_surfaces_in_known_gaps_and_not_problems(
        self, tmp_path: Path
    ) -> None:
        """When a scene lacks frozen text, verify must record the gap honestly
        (``t1_scene_missing_frozen_text``) and must NOT invent problems for it
        — the run is silent about what was never committed."""
        text_dir = tmp_path / "t1"
        text_dir.mkdir()
        # Scene 1 has a real sample + query; scenes 2 and 6 have nothing.
        (text_dir / "a.md").write_text("alpha", encoding="utf-8")
        real_sha = hashlib.sha256(b"alpha").hexdigest()
        sample = {
            "sample_id": "a",
            "scene": 1,
            "source": {"url": "https://example", "license": "CC BY-SA 4.0"},
            "fetched_at": "2026-09-06",
            "sha256": real_sha,
            "path": "a.md",
            "document_id": "a",
        }
        query = {
            "query_id": "q1",
            "sample_id": "a",
            "scene": 1,
            "question": "what is alpha?",
            "query_class": "narrative",
            "evidence_span": "alpha",
        }
        manifest = {
            "schema_version": t1_mod.T1_SCHEMA_VERSION,
            "dataset_id": t1_mod.T1_DATASET_ID,
            "samples": [sample],
            "queries": [query],
            "known_gaps": [],
        }
        report = verify_t1_manifest(manifest, text_dir=text_dir)
        # The run must not be flagged: problem_count must stay 0 for scenes
        # that simply have no frozen text (we never invent a failure).
        assert report.problem_count == 0
        assert report.ok
        # The gap is surfaced explicitly under known_gaps so the report does
        # not silently say "all six scenes covered" when only one was.
        gap_codes = {g["gap"] for g in report.known_gaps}
        assert t1_mod.T1_GAP_SCENE_MISSING_FROZEN_TEXT in gap_codes
        # Every empty scene is named in the gap entries.
        gap_scenes = {
            int(g["scene"]) for g in report.known_gaps
            if g.get("gap") == t1_mod.T1_GAP_SCENE_MISSING_FROZEN_TEXT
        }
        assert gap_scenes == {2, 3, 4, 5, 6}
        # Each gap carries the standard shape (gap code, criterion, detail).
        for entry in report.known_gaps:
            assert "gap" in entry and "criterion" in entry and "detail" in entry

    def test_scene_with_text_but_no_queries_still_a_problem(self, tmp_path: Path) -> None:
        """A scene with frozen text but no human-checked query is a problem —
        the gap is not 'missing text', it is 'missing contract'."""
        text_dir = tmp_path / "t1"
        text_dir.mkdir()
        (text_dir / "a.md").write_text("alpha", encoding="utf-8")
        real_sha = hashlib.sha256(b"alpha").hexdigest()
        sample = {
            "sample_id": "a",
            "scene": 1,
            "source": {"url": "https://example", "license": "CC BY-SA 4.0"},
            "fetched_at": "2026-09-06",
            "sha256": real_sha,
            "path": "a.md",
            "document_id": "a",
        }
        manifest = {
            "schema_version": t1_mod.T1_SCHEMA_VERSION,
            "dataset_id": t1_mod.T1_DATASET_ID,
            "samples": [sample],
            "queries": [],  # No query for the countable sample.
            "known_gaps": [],
        }
        report = verify_t1_manifest(manifest, text_dir=text_dir)
        assert not report.ok
        assert any("scene 1" in p and "queries" in p for p in report.problems)
        # Scene 1 is NOT in the missing-frozen-text gap set — it has text.
        gap_scenes = {
            int(g.get("scene", -1))
            for g in report.known_gaps
            if g.get("gap") == t1_mod.T1_GAP_SCENE_MISSING_FROZEN_TEXT
        }
        assert 1 not in gap_scenes

    def test_committed_manifest_has_no_empty_scene_gaps(self) -> None:
        """The committed manifest covers all six scenes; the verify report
        must therefore have no ``t1_scene_missing_frozen_text`` entries."""
        manifest = load_t1_manifest(T1_MANIFEST_PATH)
        report = verify_t1_manifest(manifest, text_dir=T1_TEXT_DIR)
        assert report.ok
        gap_codes = {g["gap"] for g in report.known_gaps}
        assert t1_mod.T1_GAP_SCENE_MISSING_FROZEN_TEXT not in gap_codes


# --------------------------------------------------------------------------
# build_t1_manifest_from_dir (in-memory builder; no network)
# --------------------------------------------------------------------------


class TestBuilder:
    def test_builder_reads_dir_and_computes_sha(self, tmp_path: Path) -> None:
        text_dir = tmp_path / "t1"
        text_dir.mkdir()
        (text_dir / "a.md").write_text("alpha", encoding="utf-8")
        sample = T1Sample(
            sample_id="a",
            scene=1,
            source_url="https://example",
            license="CC BY-SA 4.0",
            fetched_at="2026-09-06",
            path="a.md",
            document_id="a",
        )
        manifest = build_t1_manifest_from_dir(
            samples=[sample],
            queries=[],
            text_dir=text_dir,
        )
        sha = hashlib.sha256(b"alpha").hexdigest()
        assert manifest["samples"][0]["sha256"] == sha
        assert manifest["samples"][0]["source"]["url"] == "https://example"
        assert manifest["samples"][0]["source"]["license"] == "CC BY-SA 4.0"


# --------------------------------------------------------------------------
# CLI verify-t1
# --------------------------------------------------------------------------


class TestVerifyT1CLI:
    def test_cli_exits_zero_on_committed_manifest(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "fin.corpus.dataset", "verify-t1"],
            cwd=_PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, (
            f"verify-t1 should exit 0; got {result.returncode}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

    def test_cli_exits_nonzero_on_tampered_frozen_text(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Point the CLI at a tmp copy of the manifest + a tampered text dir.
        tampered_dir = tmp_path / "tampered"
        tampered_dir.mkdir()
        manifest = load_t1_manifest(T1_MANIFEST_PATH)
        # Recreate the same relative paths under tampered_dir and copy the
        # committed text bytes, then flip the bytes of the first sample.
        first_sample = next(
            s for s in manifest["samples"] if is_t1_countable(s)[0]
        )
        rel = first_sample["path"]
        target = tampered_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"intentionally tampered content")
        # Other files are not needed for this negative test (one mismatch
        # is enough to fail verify).
        result = subprocess.run(
            [
                sys.executable, "-m", "fin.corpus.dataset", "verify-t1",
                "--manifest", str(T1_MANIFEST_PATH),
                "--text-dir", str(tampered_dir),
            ],
            cwd=_PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode != 0, (
            f"verify-t1 should exit nonzero on tampered text; "
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )
