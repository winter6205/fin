"""Opt-in full-dataset determinism contract (plan T12, SC12 / SC14).

Runs under ``uv run pytest -q -m eval`` only (the tests/eval conftest
deselects eval-marked items from the default run). This is the T12
assertion entry the two spec success criteria share:

* SC12 — same byte input, same rule versions + params table version:
  two consecutive parse + split passes produce byte-identical
  products; per-sample sha256 comparison over **every countable sample
  of the eval dataset**, mismatches = 0;
* SC14 — the same entry doubles as the "no hidden nondeterminism"
  witness: the compared serialization covers block text, block
  identity, order and metadata (not text alone), and a companion test
  injects real nondeterministic sources (clock, random) into the
  product path and shows the assertion FAILS under injection while a
  clean run over the same prefix reports mismatches = 0.

The product serialization (``fin.corpus.determinism``) is canonical:
``sort_keys`` JSON with order-preserving arrays, so benign dict key
order cannot false-positive while real value/array differences are
caught. Summary numbers print with a stable ``T12 determinism:``
prefix.
"""

from __future__ import annotations

import hashlib
import json
import random
import time
from pathlib import Path

import pytest

from fin.corpus.chunking import ChunkPayload, split_unit
from fin.corpus.determinism import (
    canonical_json_bytes,
    first_mismatches,
    unit_chunking_product,
)
from fin.corpus.dataset.build import _load_registry, _load_units, default_paths
from fin.corpus.dataset.manifest import is_countable
from fin.corpus.parsers import parse_document
from fin.corpus.parsers._common import build_unit

pytestmark = pytest.mark.eval

# Injection witness runs over a bounded prefix of the dataset: the
# witness must show the assertion breaking, not double the full-lane
# parse budget.
_INJECTION_SAMPLE_LIMIT = 24


def _frozen_units(
    sample: dict, corpus_dir: Path, dataset_dir: Path, units_by_doc: dict
) -> list[tuple[str, int, str]]:
    """The (document_id, unit ordinal, frozen text) triples of one
    sample — same pipeline the T11 contract uses (mirror of the build
    pipeline: primary PDFs via ``parse_document``, live-fetched HTML
    primaries via the committed extracted tree, derived samples via
    their exported bytes)."""
    if sample["origin"] == "primary":
        if sample["source_kind"] == "pdf":
            result = parse_document(
                corpus_dir.parent.parent / sample["path"],
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
        dataset_dir / "documents" / sample["format"] / Path(sample["path"]).name
    )
    result = parse_document(
        derived_path.read_bytes(),
        fmt=sample["format"],
        document_id=sample["sample_id"],
        version=sample["sha256"],
    )
    return [(sample["sample_id"], u.locator.unit, u.text) for u in result.units]


def _manifest_documents() -> list[dict]:
    _, _, dataset_dir = default_paths()
    return json.loads(
        (dataset_dir / "manifest.json").read_text(encoding="utf-8")
    )["documents"]


def _iter_samples(limit: int | None = None):
    corpus_dir, _, dataset_dir = default_paths()
    registry, _ = _load_registry(corpus_dir)
    units_by_doc = _load_units(corpus_dir / "extracted", registry)
    yielded = 0
    for sample in _manifest_documents():
        countable, _reason = is_countable(sample)
        if not countable:
            continue
        if limit is not None and yielded >= limit:
            return
        units = _frozen_units(sample, corpus_dir, dataset_dir, units_by_doc)
        if units:
            yield sample, units
            yielded += 1


def _sample_digests(limit: int | None = None) -> dict[str, str]:
    """One full parse + split pass; per-sample sha256 over the
    canonical serialization of every unit's chunking product."""
    digests: dict[str, str] = {}
    for sample, units in _iter_samples(limit):
        digest = hashlib.sha256()
        for document_id, unit_ordinal, text in units:
            parsed = build_unit(
                fmt=sample["format"],
                source_sha256=sample["sha256"],
                document_id=document_id,
                version=sample["sha256"],
                unit=unit_ordinal,
                text=text,
            )
            uc = split_unit(parsed, doc_type=sample["doc_type"])
            digest.update(canonical_json_bytes(unit_chunking_product(uc)))
        digests[sample["sample_id"]] = digest.hexdigest()
    return digests


def _mismatches(first: dict[str, str], second: dict[str, str]) -> list[str]:
    return sorted(sid for sid in first if first[sid] != second.get(sid))


def test_full_dataset_two_passes_byte_identical() -> None:
    """SC12 / SC14: two consecutive parse + split passes over the whole
    countable dataset — per-sample sha256 mismatches = 0."""
    first = _sample_digests()
    second = _sample_digests()
    mismatches = _mismatches(first, second)
    print(
        f"T12 determinism: samples={len(first)} passes=2 "
        f"mismatches={len(mismatches)}"
    )
    assert len(first) > 0
    if mismatches:
        print(f"T12 determinism: first mismatch {mismatches[0]}")
        for label, digests in (("pass1", first), ("pass2", second)):
            print(
                f"T12 determinism: {label} {mismatches[0]} "
                f"sha256={digests[mismatches[0]]}"
            )
    assert mismatches == [], f"non-reproducible samples: {mismatches[:10]}"


def test_injected_nondeterminism_makes_the_assertion_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The "no hidden nondeterminism" witness (SC14): with a clock and
    a random draw injected into the product path, the byte comparison
    MUST report mismatches — i.e. the assertion fails under injection
    and only the clean run passes it."""
    original_to_dict = ChunkPayload.to_dict

    def noisy_to_dict(self: ChunkPayload) -> dict:
        payload = dict(original_to_dict(self))
        payload["injected_clock"] = time.time_ns()
        payload["injected_random"] = random.random()
        return payload

    monkeypatch.setattr(ChunkPayload, "to_dict", noisy_to_dict)

    first = _sample_digests(limit=_INJECTION_SAMPLE_LIMIT)
    second = _sample_digests(limit=_INJECTION_SAMPLE_LIMIT)
    mismatches = _mismatches(first, second)
    print(
        f"T12 determinism: injected witness samples={len(first)} "
        f"mismatches={len(mismatches)} (assertion expected to FAIL)"
    )
    assert len(first) > 0
    assert mismatches, "injected nondeterminism was NOT caught by the comparison"
    assert len(mismatches) == len(first), (
        "expected every injected sample to mismatch"
    )


def test_clean_run_matches_the_injected_baseline() -> None:
    """Polarity check on the witness: the same comparison over the same
    bounded prefix WITHOUT injection reports mismatches = 0 — the
    injection, not the harness, is what breaks the assertion."""
    first = _sample_digests(limit=_INJECTION_SAMPLE_LIMIT)
    second = _sample_digests(limit=_INJECTION_SAMPLE_LIMIT)
    mismatches = _mismatches(first, second)
    print(
        f"T12 determinism: clean witness samples={len(first)} "
        f"mismatches={len(mismatches)}"
    )
    assert mismatches == []


def test_injection_is_caught_through_metadata_not_text() -> None:
    """The injected field rides the payload METADATA (block text
    untouched), so the caught mismatch proves the comparison covers
    metadata — the acceptance's 「不只比块文本」 exercised end to end."""
    sample, units = next(iter(_iter_samples(limit=1)))
    document_id, unit_ordinal, text = units[0]
    parsed = build_unit(
        fmt=sample["format"],
        source_sha256=sample["sha256"],
        document_id=document_id,
        version=sample["sha256"],
        unit=unit_ordinal,
        text=text,
    )
    uc = split_unit(parsed, doc_type=sample["doc_type"])
    clean = unit_chunking_product(uc)
    perturbed_payload = dict(clean["payloads"][0])
    perturbed_payload["injected_clock"] = time.time_ns()
    perturbed = dict(clean)
    perturbed["payloads"] = [perturbed_payload, *clean["payloads"][1:]]
    paths = list(first_mismatches(clean, perturbed))
    assert paths and any("payloads" in p for p in paths)
    # block text untouched: the caught mismatch is metadata-only
    assert clean["children"][0]["text"] == perturbed["children"][0]["text"]
