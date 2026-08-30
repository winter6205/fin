"""Opt-in full-dataset chunking contract (plan T11, SC11 / SC16 / SC17).

Runs under ``uv run pytest -q -m eval`` only (the tests/eval conftest
deselects eval-marked items from the default run, which therefore never
reads the dataset bytes). This test asserts the T11 hard constraints on
**every countable sample of the eval dataset** — not a sample of
samples:

* SC11: every child block text is a contiguous substring of its parent
  block text; every parent block text is a contiguous substring of its
  frozen unit text — violations across the whole dataset = 0;
* SC17 extension: block text is a verbatim slice of the frozen unit
  text — re-running the single normalization outlet on any block is a
  no-op (no second character-replacement rule anywhere);
* SC16: the hit set contains only child blocks; parents are not
  independently retrievable and ride along as ``parent_text`` in every
  child's push payload;
* the per-sample child counts equal the manifest's ``chunk_count``
  records and the total equals ``chunk_total`` (deterministic splitter,
  consistent manifest).

The frozen-unit source per sample kind mirrors the build pipeline
(:mod:`fin.corpus.dataset.build`): primary PDFs are parsed with the T6
entry point, live-fetched HTML primaries come from the committed
extracted tree, and derived samples are parsed from their exported
bytes. Summary numbers are printed with a stable ``T11 chunk contract:``
prefix so the eval run log carries the observed counts (sample count,
unit count, parent count, child count, violations, B1 chunk band).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fin.corpus.chunking import split_unit
from fin.corpus.dataset.build import _load_registry, _load_units, default_paths
from fin.corpus.dataset.manifest import (
    B1_MAX_CHUNKS,
    B1_MIN_CHUNKS,
    CHUNK_TOTAL_STATUS_SPLIT,
    is_countable,
)
from fin.corpus.normalization import normalize_frozen_text
from fin.corpus.parsers import parse_document
from fin.corpus.parsers._common import ParsedUnit, build_unit

pytestmark = pytest.mark.eval


def _manifest() -> dict:
    _, _, dataset_dir = default_paths()
    return json.loads((dataset_dir / "manifest.json").read_text(encoding="utf-8"))


def _repo_root(corpus_dir: Path) -> Path:
    return corpus_dir.parent.parent


def _frozen_units(
    sample: dict, corpus_dir: Path, dataset_dir: Path, units_by_doc: dict
) -> list[tuple[int, str, str]]:
    """The (document_id, unit ordinal, frozen text) triples of one
    sample, produced by the same pipeline the build counts with:
    primary PDFs via ``parse_document``, live-fetched HTML primaries
    via the committed extracted tree, derived samples via their
    exported bytes."""
    if sample["origin"] == "primary":
        if sample["source_kind"] == "pdf":
            result = parse_document(
                _repo_root(corpus_dir) / sample["path"],
                fmt="pdf",
                document_id=sample["document_id"],
                version=sample["sha256"],
            )
            return [
                (sample["document_id"], u.locator.unit, u.text)
                for u in result.units
            ]
        return [
            (sample["document_id"], u.unit, u.text)
            for u in units_by_doc[sample["document_id"]]
            if u.text.strip()
        ]
    derived_path = (
        dataset_dir
        / "documents"
        / sample["format"]
        / Path(sample["path"]).name
    )
    result = parse_document(
        derived_path.read_bytes(),
        fmt=sample["format"],
        document_id=sample["sample_id"],
        version=sample["sha256"],
    )
    return [(sample["sample_id"], u.locator.unit, u.text) for u in result.units]


def _samples_with_units():
    corpus_dir, _, dataset_dir = default_paths()
    registry, _ = _load_registry(corpus_dir)
    units_by_doc = _load_units(corpus_dir / "extracted", registry)
    for sample in _manifest()["documents"]:
        countable, reason = is_countable(sample)
        if not countable:
            continue
        units = _frozen_units(sample, corpus_dir, dataset_dir, units_by_doc)
        yield sample, units


def test_two_level_substring_on_full_dataset() -> None:
    violations: list[str] = []
    stats = {
        "samples": 0,
        "units": 0,
        "parents": 0,
        "children": 0,
        "fixed_point_checks": 0,
        "payload_checks": 0,
    }
    per_sample_children: dict[str, int] = {}
    for sample, units in _samples_with_units():
        stats["samples"] += 1
        sample_children = 0
        for document_id, unit_ordinal, text in units:
            stats["units"] += 1
            parsed: ParsedUnit = build_unit(
                fmt=sample["format"],
                source_sha256=sample["sha256"],
                document_id=document_id,
                version=sample["sha256"],
                unit=unit_ordinal,
                text=text,
            )
            uc = split_unit(parsed, doc_type=sample["doc_type"])
            sid = f"{sample['sample_id']}#{unit_ordinal}"
            parents_by_id = {p.block_id: p for p in uc.parents}
            stats["parents"] += len(uc.parents)
            sample_children += len(uc.children)
            for parent in uc.parents:
                # SC11 level 2: parent ⊂ frozen unit text
                if parent.text not in text:
                    violations.append(f"{sid}: parent not a substring of the unit")
                # SC16: parents are never retrievable
                if parent.is_retrievable:
                    violations.append(f"{sid}: parent marked retrievable")
                if len(parent.text) > uc.params.max_chars:
                    violations.append(f"{sid}: parent above max_chars")
            for child in uc.children:
                # SC11 level 1: child ⊂ parent
                parent = parents_by_id[child.parent_block_id]
                if child.text not in parent.text:
                    violations.append(f"{sid}: child not a substring of its parent")
                # SC16: children are exactly the hit set
                if not child.is_retrievable:
                    violations.append(f"{sid}: child not retrievable")
                if len(child.text) > uc.params.max_chars:
                    violations.append(f"{sid}: child above max_chars")
                # SC17: block text is a fixed point of the single outlet
                stats["fixed_point_checks"] += 1
                if normalize_frozen_text(child.text) != child.text:
                    violations.append(f"{sid}: child text not a normalization fixed point")
                if normalize_frozen_text(parent.text) != parent.text:
                    violations.append(f"{sid}: parent text not a normalization fixed point")
                # SC16: the payload carries the parent text
                stats["payload_checks"] += 1
                payload = uc.payloads()[child.ordinal - 1]
                if payload.text != child.text or payload.block_id != child.block_id:
                    violations.append(f"{sid}: payload does not carry its child")
                if payload.parent_text != parent.text:
                    violations.append(f"{sid}: payload does not carry its parent text")
        per_sample_children[sample["sample_id"]] = sample_children
        stats["children"] += sample_children

    manifest = _manifest()
    manifest_counts = {
        s["sample_id"]: s["chunk_count"] for s in manifest["documents"]
    }
    mismatched = [
        sid
        for sid, n in per_sample_children.items()
        if manifest_counts.get(sid) != n
    ]

    chunk_total = manifest["chunk_total"]
    band_met = B1_MIN_CHUNKS <= chunk_total <= B1_MAX_CHUNKS
    print(
        f"T11 chunk contract: samples={stats['samples']} units={stats['units']} "
        f"parents={stats['parents']} children={stats['children']} "
        f"violations={len(violations)} fixed_point_checks={stats['fixed_point_checks']} "
        f"payload_checks={stats['payload_checks']}"
    )
    print(
        f"T11 chunk contract: manifest chunk_total={chunk_total} "
        f"status={manifest['chunk_total_status']} "
        f"B1 band [{B1_MIN_CHUNKS}, {B1_MAX_CHUNKS}] met={band_met}"
    )
    assert stats["samples"] > 0
    assert stats["children"] == chunk_total
    assert not mismatched, f"manifest chunk_count drift: {mismatched[:10]}"
    assert len(violations) == 0, violations[:20]


def test_manifest_chunk_total_status_is_t11() -> None:
    manifest = _manifest()
    assert manifest["chunk_total_status"] == CHUNK_TOTAL_STATUS_SPLIT
    assert manifest["chunk_total"] > 0
