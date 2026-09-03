"""Chunking determinism byte comparison — small-fixture lane (plan T12, SC12 / SC14).

The default ``pytest -q`` run must not read the big corpus, so the same
logic the full-dataset eval assertion uses (``tests/eval/
test_determinism.py``) is exercised here against a small scripted
fixture: two consecutive ``parse + split`` runs must serialize to the
identical byte string, and the comparison must cover **all four**
asserted aspects — block text, block identity, order and metadata —
not just the text.

What each test witnesses
------------------------
* byte-identical reruns (SC12 / SC14 core assertion);
* the canonical serialization is insensitive to benign dict key
  insertion order and to set/dict **iteration order used to build
  dicts** (``sort_keys`` canonicalization — no false mismatches);
* the comparison is sensitive to real nondeterminism: injecting a
  clock, a random draw, or array-order nondeterminism makes the
  two digests differ — i.e. the assertion FAILS (this is the
  "no hidden nondeterminism" witness; the acceptance requires the
  injected run to break the ``mismatches == 0`` assertion);
* set-iteration-order leakage into an **array** is caught across
  processes (two subprocesses with different ``PYTHONHASHSEED``);
  within one process set order is stable, so only a cross-process
  run can witness it.
"""

from __future__ import annotations

import copy
import json
import os
import random
import subprocess
import sys
import time
from pathlib import Path

from fin.corpus.chunking import split_document, split_unit
from fin.corpus.determinism import (
    canonical_json_bytes,
    chunking_product_sha256,
    document_chunking_product,
    first_mismatches,
    sha256_hex,
    unit_chunking_product,
)
from fin.corpus.parsers._common import ParseResult, build_unit

DOC_ID = "doc-determinism-fixture"
VERSION = "b" * 64  # sha256-shaped version stamp, as the pipeline passes


def _fixture_text() -> str:
    """A deterministic text rich enough to produce several parents and
    children: ATX headings (rank-1 parent cuts), clause/list items,
    table rows and sentence boundaries."""
    parts: list[str] = ["# Determinism Fixture Report\n"]
    for i in range(1, 9):
        parts.append(f"## Section {i} Overview\n")
        parts.append("| key | value |\n| --- | --- |\n")
        for j in range(1, 7):
            parts.append(f"| row{i}-{j} | cell {i}.{j} of the fixture table |\n")
        for j in range(1, 7):
            parts.append(
                f"{i}.{j} The retained party shall deliver the quarterly "
                f"report within fifteen business days of period end. "
                f"Late delivery triggers the remediation clause. "
                f"Schedule {i} item {j} applies to this obligation.\n"
            )
    return "".join(parts)


def _parsed_unit() -> object:
    return build_unit(
        fmt="txt",
        source_sha256=VERSION,
        document_id=DOC_ID,
        version=VERSION,
        unit=1,
        text=_fixture_text(),
    )


def _parsed_document() -> ParseResult:
    unit_a = _parsed_unit()
    unit_b = build_unit(
        fmt="txt",
        source_sha256=VERSION,
        document_id=DOC_ID,
        version=VERSION,
        unit=2,
        text=_fixture_text().replace("Overview", "Annex"),
    )
    return ParseResult(
        fmt="txt",
        source_sha256=VERSION,
        document_id=DOC_ID,
        version=VERSION,
        parse_rule_version=unit_a.parse_rule_version,
        params_table_version=unit_a.params_table_version,
        units=(unit_a, unit_b),
    )


# ---------------------------------------------------------------------------
# Core assertion: two consecutive parse + split runs are byte-identical
# ---------------------------------------------------------------------------


def test_two_consecutive_splits_are_byte_identical() -> None:
    first_unit = unit_chunking_product(split_unit(_parsed_unit(), doc_type="narrative"))
    second_unit = unit_chunking_product(split_unit(_parsed_unit(), doc_type="narrative"))
    first_doc = document_chunking_product(split_document(_parsed_document(), doc_type="narrative"))
    second_doc = document_chunking_product(
        split_document(_parsed_document(), doc_type="narrative")
    )
    assert chunking_product_sha256(first_unit) == chunking_product_sha256(second_unit)
    assert chunking_product_sha256(first_doc) == chunking_product_sha256(second_doc)
    # The comparison actually covers all four aspects on this fixture:
    # several parents and children exist, with identity and metadata.
    assert len(first_unit["parents"]) > 1
    assert len(first_unit["children"]) > 1
    assert len(list(first_mismatches(first_unit, second_unit))) == 0
    assert len(list(first_mismatches(first_doc, second_doc))) == 0


def test_product_covers_text_identity_order_metadata() -> None:
    uc = split_unit(_parsed_unit(), doc_type="narrative")
    product = unit_chunking_product(uc)

    # 1) block text — verbatim
    assert product["unit_text"] == uc.unit_text
    assert [c["text"] for c in product["children"]] == [c.text for c in uc.children]
    # 2) block identity — the deterministic sha256 ids
    assert [c["block_id"] for c in product["children"]] == [c.block_id for c in uc.children]
    assert all(len(c["block_id"]) == 64 for c in product["children"])
    # 3) order — arrays serialize in product order, ordinals ride along
    assert [c["ordinal"] for c in product["children"]] == list(
        range(1, len(uc.children) + 1)
    )
    # 4) metadata — full payload shape (SC20 fields) per child
    payload_keys = set(product["payloads"][0])
    assert {
        "block_id",
        "text",
        "parent_block_id",
        "parent_text",
        "locator",
        "fmt",
        "doc_type",
        "section",
        "density",
        "source_sha256",
        "parse_rule_version",
        "params_table_version",
        "normalization_rule_version",
    } <= payload_keys


# ---------------------------------------------------------------------------
# Canonicalization: benign key order / dict-construction iteration order
# must NOT change the digest (no false mismatches)
# ---------------------------------------------------------------------------


def _reverse_dict_key_insertion(obj: object) -> object:
    """Rebuild every dict with its keys inserted in reverse order."""
    if isinstance(obj, dict):
        return {k: _reverse_dict_key_insertion(obj[k]) for k in reversed(list(obj))}
    if isinstance(obj, list):
        return [_reverse_dict_key_insertion(v) for v in obj]
    return obj


def test_serialization_ignores_dict_key_insertion_order() -> None:
    product = unit_chunking_product(split_unit(_parsed_unit(), doc_type="narrative"))
    reordered = _reverse_dict_key_insertion(product)
    # insertion order really differs (plain dumps preserves it)
    assert json.dumps(reordered) != json.dumps(product)
    # ... but the canonical digest is identical: sort_keys neutralizes it
    assert chunking_product_sha256(reordered) == chunking_product_sha256(product)


def test_serialization_ignores_set_iteration_order_in_dict_construction() -> None:
    """A dict rebuilt by iterating a **set** of pairs (arbitrary order)
    serializes to the same bytes: ``sort_keys`` neutralizes key order."""
    uc = split_unit(_parsed_unit(), doc_type="narrative")
    product = unit_chunking_product(uc)
    locator_pairs = {
        ("document_id", uc.locator.document_id),
        ("version", uc.locator.version),
        ("unit", uc.locator.unit),
    }
    from_set = dict(locator_pairs)
    assert from_set == dict(product["locator"])
    rebuilt = copy.deepcopy(product)
    rebuilt["locator"] = from_set
    assert chunking_product_sha256(rebuilt) == chunking_product_sha256(product)


def test_serialization_is_canonical_json_with_sorted_keys() -> None:
    product = unit_chunking_product(split_unit(_parsed_unit(), doc_type="narrative"))
    raw = canonical_json_bytes(product)

    def _assert_sorted(pairs: list[tuple[str, object]]) -> dict:
        keys = [k for k, _ in pairs]
        assert keys == sorted(keys), f"unsorted keys in canonical bytes: {keys}"
        return dict(pairs)

    # every dict level of the canonical bytes is key-sorted
    json.loads(raw, object_pairs_hook=_assert_sorted)
    assert sha256_hex(raw) == chunking_product_sha256(product)


# ---------------------------------------------------------------------------
# Injection witnesses: the assertion FAILS under injected nondeterminism
# ---------------------------------------------------------------------------


def _with_payload_noise(product: dict, noise: object) -> dict:
    """Simulate a hidden nondeterministic field riding every payload."""
    noisy = copy.deepcopy(product)
    for payload in noisy["payloads"]:
        payload["injected_noise"] = noise
    return noisy


def test_injected_clock_makes_the_comparison_fail() -> None:
    product = unit_chunking_product(split_unit(_parsed_unit(), doc_type="narrative"))
    h1 = chunking_product_sha256(_with_payload_noise(product, time.time_ns()))
    h2 = chunking_product_sha256(_with_payload_noise(product, time.time_ns()))
    assert h1 != h2, "clock injection must break the mismatches == 0 assertion"
    assert len(list(first_mismatches(_with_payload_noise(product, 1), product))) > 0


def test_injected_random_makes_the_comparison_fail() -> None:
    product = unit_chunking_product(split_unit(_parsed_unit(), doc_type="narrative"))
    rng = random.Random()
    h1 = chunking_product_sha256(_with_payload_noise(product, rng.random()))
    h2 = chunking_product_sha256(_with_payload_noise(product, rng.random()))
    assert h1 != h2, "random injection must break the mismatches == 0 assertion"


def test_injected_array_order_makes_the_comparison_fail() -> None:
    """Order is asserted: a product whose block array came out in a
    different order (e.g. built from set iteration) hashes differently."""
    product = unit_chunking_product(split_unit(_parsed_unit(), doc_type="narrative"))
    shuffled = copy.deepcopy(product)
    shuffled["children"] = list(reversed(shuffled["children"]))
    assert chunking_product_sha256(shuffled) != chunking_product_sha256(product)
    paths = list(first_mismatches(product, shuffled))
    assert paths and any("children" in p for p in paths)


_SUBPROCESS_SNIPPET = """
import sys
from fin.corpus.chunking import split_unit
from fin.corpus.determinism import chunking_product_sha256, unit_chunking_product
from fin.corpus.parsers._common import build_unit

text = sys.stdin.read()
unit = build_unit(
    fmt="txt", source_sha256="s", document_id="d", version="v", unit=1, text=text
)
product = unit_chunking_product(split_unit(unit, doc_type="narrative"))
# Simulate a pipeline that derives an array from SET ITERATION order:
texts = {c["text"] for c in product["children"]}
product["children_texts_from_set"] = list(texts)
print(chunking_product_sha256(product))
"""


def test_set_iteration_order_in_arrays_is_caught_across_processes() -> None:
    """Within one process a set's iteration order is stable, so the
    witness must cross processes: two runs with different
    ``PYTHONHASHSEED`` produce different array orders and therefore
    different digests — the comparison catches set-order leakage."""
    src_dir = str(Path(__file__).resolve().parents[2] / "src")
    digests: list[str] = []
    for seed in ("0", "1"):
        env = dict(os.environ)
        env["PYTHONHASHSEED"] = seed
        env["PYTHONPATH"] = src_dir + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.run(
            [sys.executable, "-c", _SUBPROCESS_SNIPPET],
            input=_fixture_text(),
            capture_output=True,
            text=True,
            env=env,
            check=True,
        )
        digests.append(proc.stdout.strip())
    assert len(digests) == 2
    assert digests[0] != digests[1], (
        "set-iteration-order leakage into an array must change the digest"
    )
