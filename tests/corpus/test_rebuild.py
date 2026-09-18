"""In-place full rebuild on rule / params-table version change (plan T13).

Default-suite tests: small scripted fixtures only (derived ``txt``
samples + one synthetic ``html_live`` primary backed by a tiny
extracted tree) — the command's full-dataset run is the operator
command, never executed by tests.

What is covered here:

* one command rebuilds ALL frozen units + blocks of every countable
  dataset sample in place (input source = the dataset manifest samples
  / extracted authoritative tree — the same source the T11 full
  assertion consumes);
* every rebuilt block's metadata carries the CURRENT version constants
  (``PARSE_RULE_VERSION`` / ``PARAMS_TABLE_VERSION``) — after a
  rebuild no old/new version coexistence can survive;
* a mixed state (an old-version artifact mixed into the product area)
  is detected by the validator and cleared by the next rebuild;
* two consecutive rebuilds are byte-identical (T12 determinism,
  compared by this module's own double-run diff);
* the rebuild is reentrant: a simulated mid-run interruption leaves
  the previous complete product intact (staging + atomic swap), and a
  rerun converges to the same final state;
* a detectable "version change happened" signal (previous vs. current
  constants + ``changed`` flag) is exposed for T18 to consume.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

import fin.corpus.rebuild as rebuild_module
from fin.corpus.params import PARAMS_TABLE_VERSION
from fin.corpus.parsers import PARSE_RULE_VERSION
from fin.corpus.rebuild import (
    REBUILD_MANIFEST_NAME,
    detect_version_changes,
    rebuild,
    validate_no_mixed_versions,
)

DOC_A_VERSION = "a" * 64
LIVE_VERSION = "b" * 64


def _sentences(n: int, start: int = 0) -> str:
    """n deterministic ~100-char sentences, each ending in a period."""
    return "".join(
        f"Sentence {i:03d} reports that operating revenue grew in the "
        "quarter and the gross margin improved again. "
        for i in range(start, start + n)
    )


def _narrative_text() -> str:
    return _sentences(12) + "\n\n" + _sentences(12, start=100)


def _clause_text() -> str:
    lines = []
    for i in range(1, 13):
        lines.append(
            f"第{i}条 公司应当在每一会计年度终了时编制财务会计报告，"
            "并经会计师事务所审计。"
        )
    return "\n".join(lines)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _derived_sample(sample_id: str, doc_type: str, text: str, doc_id: str) -> tuple[dict, bytes]:
    payload = text.encode("utf-8")
    sample = {
        "sample_id": sample_id,
        "format": "txt",
        "origin": "derived",
        "tier": 3,
        "path": f"data/corpus/dataset/documents/txt/{sample_id}.txt",
        "bytes": len(payload),
        "sha256": _sha(payload),
        "license": "test license (derived from the frozen corpus)",
        "doc_type": doc_type,
        "is_hard": False,
        "hard_kind": None,
        "derived_from": {"document_id": doc_id, "version": DOC_A_VERSION},
        "source_units": [{"unit": 1}],
        "unit_count": 1,
        "chunk_count": 0,
    }
    return sample, payload


def _make_dataset(
    tmp_path: Path, *, with_primary: bool = False
) -> tuple[Path, Path]:
    """A small dataset manifest + two derived txt samples; optionally
    one primary ``html_live`` sample backed by a synthetic extracted
    tree. Returns ``(corpus_dir, manifest_path)``."""
    corpus_dir = tmp_path / "data" / "corpus"
    dataset_dir = corpus_dir / "dataset"
    documents: list[dict] = []

    for sid, doc_type, text, doc_id in [
        ("DOC-A__narrative__txt", "narrative", _narrative_text(), "DOC-A"),
        ("DOC-A__clause_list__txt", "clause_list", _clause_text(), "DOC-A"),
    ]:
        sample, payload = _derived_sample(sid, doc_type, text, doc_id)
        target = dataset_dir / "documents" / "txt" / f"{sid}.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        documents.append(sample)

    if with_primary:
        (corpus_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "documents": [
                        {
                            "document_id": "LIVE-1",
                            "sha256": LIVE_VERSION,
                            "filename": "live-1.htm",
                            "period": "",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        extracted = corpus_dir / "extracted"
        extracted.mkdir(parents=True, exist_ok=True)
        (extracted / "_index.json").write_text(
            json.dumps(
                {
                    "documents": {
                        "LIVE-1": {"page_count": 1, "version": LIVE_VERSION}
                    }
                }
            ),
            encoding="utf-8",
        )
        (extracted / "LIVE-1").mkdir(parents=True, exist_ok=True)
        (extracted / "LIVE-1" / "page-001.json").write_text(
            json.dumps({"page": 1, "text": _sentences(20)}),
            encoding="utf-8",
        )
        documents.append(
            {
                "sample_id": "LIVE-1__primary",
                "format": "html",
                "origin": "primary",
                "tier": 1,
                "path": "data/corpus/extracted/LIVE-1",
                "bytes": None,
                "sha256": LIVE_VERSION,
                "license": "test license (primary)",
                "doc_type": "mixed",
                "is_hard": False,
                "hard_kind": None,
                "document_id": "LIVE-1",
                "source_kind": "html_live",
                "period": "",
            }
        )

    manifest_path = dataset_dir / "manifest.json"
    manifest_path.write_text(json.dumps({"documents": documents}), encoding="utf-8")
    return corpus_dir, manifest_path


def _tree(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def _walk_version_dicts(node: object):
    if isinstance(node, dict):
        if "parse_rule_version" in node or "params_table_version" in node:
            yield node
        for value in node.values():
            yield from _walk_version_dicts(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_version_dicts(item)


# ---------------------------------------------------------------------------
# Acceptance 1: one command rebuilds everything, in place
# ---------------------------------------------------------------------------


def test_rebuild_produces_all_samples_from_the_authoritative_sources(
    tmp_path,
) -> None:
    corpus_dir, manifest_path = _make_dataset(tmp_path, with_primary=True)
    target = tmp_path / "data" / "corpus" / "blocks"

    report = rebuild(target, manifest_path, corpus_dir=corpus_dir)

    # every countable sample has a product file
    files = sorted(p.name for p in target.glob("*.json"))
    assert files == [
        "DOC-A__clause_list__txt.json",
        "DOC-A__narrative__txt.json",
        "LIVE-1__primary.json",
        REBUILD_MANIFEST_NAME,
    ]
    assert report.samples == 3
    assert report.units > 0
    assert report.parents > 0
    assert report.children > 0

    # the primary sample really came from the extracted tree: its
    # product carries the frozen page text as unit text
    primary = json.loads((target / "LIVE-1__primary.json").read_text("utf-8"))
    assert primary["origin"] == "primary"
    assert any(
        _sentences(20)[:40] in str(unit.get("unit_text", ""))
        for unit in primary["units"]
    )


# ---------------------------------------------------------------------------
# Acceptance 2 (final state): current versions everywhere, no coexistence
# ---------------------------------------------------------------------------


def test_rebuilt_products_carry_only_current_versions(tmp_path) -> None:
    corpus_dir, manifest_path = _make_dataset(tmp_path, with_primary=True)
    target = tmp_path / "blocks"

    report = rebuild(target, manifest_path, corpus_dir=corpus_dir)

    assert report.parse_rule_version == PARSE_RULE_VERSION == "parse-2"
    assert report.params_table_version == PARAMS_TABLE_VERSION == "params-1"
    for path in target.glob("*.json"):
        payload = json.loads(path.read_text("utf-8"))
        for node in _walk_version_dicts(payload):
            assert node["parse_rule_version"] == PARSE_RULE_VERSION, path
            assert node["params_table_version"] == PARAMS_TABLE_VERSION, path
    assert validate_no_mixed_versions(target) == []


def test_mixed_state_is_detected_and_cleared_by_rebuild(tmp_path) -> None:
    corpus_dir, manifest_path = _make_dataset(tmp_path)
    target = tmp_path / "blocks"
    rebuild(target, manifest_path, corpus_dir=corpus_dir)
    baseline = _tree(target)

    # Forge a mixed state: one payload carries an old params version.
    victim = target / "DOC-A__narrative__txt.json"
    payload = json.loads(victim.read_text("utf-8"))
    payload["units"][0]["payloads"][0]["params_table_version"] = "params-0"
    victim.write_bytes(
        (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    )

    violations = validate_no_mixed_versions(target)
    assert violations, "the validator must reject the mixed state"
    assert any("params_table_version" in v for v in violations)

    # An old-version rebuild record is the detectable version-change signal.
    record = json.loads((target / REBUILD_MANIFEST_NAME).read_text("utf-8"))
    record["parse_rule_version"] = "parse-0"
    (target / REBUILD_MANIFEST_NAME).write_bytes(
        (json.dumps(record, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    )
    signal = detect_version_changes(target)
    assert signal.has_record
    assert signal.recorded_parse_rule_version == "parse-0"
    assert signal.parse_changed
    assert signal.changed

    # The rebuild clears the mixed state in place: the final state holds
    # only current-version artifacts, byte-identical to the clean build.
    rebuild(target, manifest_path, corpus_dir=corpus_dir)
    assert validate_no_mixed_versions(target) == []
    assert _tree(target) == baseline


# ---------------------------------------------------------------------------
# Acceptance 3: rebuild is repeatable — two runs, byte-identical products
# ---------------------------------------------------------------------------


def test_rebuild_twice_is_byte_identical(tmp_path) -> None:
    corpus_dir, manifest_path = _make_dataset(tmp_path, with_primary=True)
    target_a = tmp_path / "blocks-a"
    target_b = tmp_path / "blocks-b"

    rebuild(target_a, manifest_path, corpus_dir=corpus_dir)
    rebuild(target_b, manifest_path, corpus_dir=corpus_dir)
    # and a rerun into the same target is stable too
    before = _tree(target_a)
    rebuild(target_a, manifest_path, corpus_dir=corpus_dir)

    assert _tree(target_a) == _tree(target_b)
    assert _tree(target_a) == before


# ---------------------------------------------------------------------------
# Acceptance 4: reentrant — interruption leaves no half-new half-old set
# ---------------------------------------------------------------------------


def test_rebuild_survives_midway_interruption_and_rerun(tmp_path, monkeypatch) -> None:
    corpus_dir, manifest_path = _make_dataset(tmp_path, with_primary=True)
    target = tmp_path / "blocks"
    rebuild(target, manifest_path, corpus_dir=corpus_dir)
    baseline = _tree(target)

    real = rebuild_module._rebuild_sample
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("simulated interruption")
        return real(*args, **kwargs)

    monkeypatch.setattr(rebuild_module, "_rebuild_sample", flaky)
    with pytest.raises(RuntimeError, match="simulated interruption"):
        rebuild(target, manifest_path, corpus_dir=corpus_dir)

    # the previous COMPLETE product survives untouched — no half-new
    # half-old mixture is visible at any point
    assert _tree(target) == baseline

    # rerun converges to the same final state, no leftover staging area
    rebuild(target, manifest_path, corpus_dir=corpus_dir)
    assert _tree(target) == baseline
    siblings = {p.name for p in target.parent.iterdir()}
    assert not any(
        name.startswith("blocks.") for name in siblings
    ), f"leftover staging/trash area: {siblings}"


def test_rebuild_from_scratch_after_interruption_is_complete(tmp_path, monkeypatch) -> None:
    corpus_dir, manifest_path = _make_dataset(tmp_path)
    target = tmp_path / "blocks"

    real = rebuild_module._rebuild_sample
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated interruption")
        return real(*args, **kwargs)

    monkeypatch.setattr(rebuild_module, "_rebuild_sample", flaky)
    with pytest.raises(RuntimeError, match="simulated interruption"):
        rebuild(target, manifest_path, corpus_dir=corpus_dir)
    # no product area was published by the failed run
    assert not target.exists()

    monkeypatch.setattr(rebuild_module, "_rebuild_sample", real)
    rebuild(target, manifest_path, corpus_dir=corpus_dir)
    reference = tmp_path / "reference"
    rebuild(reference, manifest_path, corpus_dir=corpus_dir)
    assert _tree(target) == _tree(reference)


# ---------------------------------------------------------------------------
# Acceptance 5: the version-change signal T18 consumes
# ---------------------------------------------------------------------------


def test_detect_version_changes_no_record_then_clean_record(tmp_path) -> None:
    corpus_dir, manifest_path = _make_dataset(tmp_path)
    target = tmp_path / "blocks"

    signal = detect_version_changes(target)
    assert not signal.has_record
    assert signal.changed  # nothing committed yet → everything is stale

    rebuild(target, manifest_path, corpus_dir=corpus_dir)
    signal = detect_version_changes(target)
    assert signal.has_record
    assert signal.recorded_parse_rule_version == PARSE_RULE_VERSION
    assert signal.recorded_params_table_version == PARAMS_TABLE_VERSION
    assert not signal.parse_changed
    assert not signal.params_changed
    assert not signal.changed


# ---------------------------------------------------------------------------
# Defensive boundary: an empty / all-uncountable manifest is refused
# ---------------------------------------------------------------------------


def test_rebuild_refuses_a_manifest_without_countable_samples(tmp_path) -> None:
    corpus_dir, manifest_path = _make_dataset(tmp_path)
    documents = json.loads(manifest_path.read_text("utf-8"))["documents"]
    for sample in documents:
        sample["origin"] = "synthetic"
    broken = manifest_path.parent / "broken.json"
    broken.write_text(json.dumps({"documents": documents}), encoding="utf-8")

    with pytest.raises(ValueError, match="countable"):
        rebuild(tmp_path / "blocks", broken, corpus_dir=corpus_dir)


# ---------------------------------------------------------------------------
# The command form: `uv run python -m fin.corpus.rebuild`
# ---------------------------------------------------------------------------


def test_cli_rebuild_and_check(tmp_path) -> None:
    corpus_dir, manifest_path = _make_dataset(tmp_path)
    target = tmp_path / "blocks"

    built = subprocess.run(
        [
            sys.executable, "-m", "fin.corpus.rebuild", "rebuild",
            "--target", str(target),
            "--manifest", str(manifest_path),
            "--corpus-dir", str(corpus_dir),
        ],
        capture_output=True,
        text=True,
    )
    assert built.returncode == 0, built.stderr
    assert (target / REBUILD_MANIFEST_NAME).is_file()
    # first run: nothing committed yet → the change signal fires; a
    # second run against the now-committed record reports no change
    assert "changed=True" in built.stdout
    built_again = subprocess.run(
        [
            sys.executable, "-m", "fin.corpus.rebuild", "rebuild",
            "--target", str(target),
            "--manifest", str(manifest_path),
            "--corpus-dir", str(corpus_dir),
        ],
        capture_output=True,
        text=True,
    )
    assert built_again.returncode == 0, built_again.stderr
    assert "changed=False" in built_again.stdout

    check = subprocess.run(
        [
            sys.executable, "-m", "fin.corpus.rebuild", "check",
            "--target", str(target),
            "--manifest", str(manifest_path),
            "--corpus-dir", str(corpus_dir),
        ],
        capture_output=True,
        text=True,
    )
    assert check.returncode == 0, check.stderr
    assert "violations=0" in check.stdout
