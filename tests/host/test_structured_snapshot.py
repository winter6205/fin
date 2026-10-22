"""The type-preserving canonical profile and its cross-language vectors.

Three jobs, in order of how loudly they fail:

* the committed golden vectors recompute byte-for-byte, so a change to the
  encoder cannot pass by agreeing with itself;
* every named refusal still refuses with the named reason, so a loosened
  guard is caught here rather than in production;
* ADR 0092's legacy textified digest is untouched, because this profile is
  additive and E12 makes the legacy cases a regression gate.

The vectors in ``data/conformance/evidence/structured_snapshot/`` are the
cross-language contract: the TypeScript runtime must reproduce the same
``canonical_json`` and ``digest`` from the same ``snapshot``.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pytest

from fin.host.evidence.evidence_contract import compute_snapshot_digest, snapshot_canonical_json
from fin.host.evidence.structured_snapshot import (
    SAFE_INTEGER_MAX,
    STRUCTURED_SNAPSHOT_FIELDS,
    StructuredSnapshotError,
    canonical_bytes,
    canonical_value,
    is_structured_snapshot_digest,
    structured_snapshot_bytes,
    structured_snapshot_canonical_json,
    structured_snapshot_digest,
)

FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "data"
    / "conformance"
    / "evidence"
    / "structured_snapshot"
    / "digest_worksheet.json"
)

LEGACY_WORKSHEET = (
    Path(__file__).resolve().parents[2]
    / "data"
    / "conformance"
    / "evidence"
    / "database_row"
    / "digest_worksheet.json"
)


def _worksheet() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _build(construction: dict[str, Any]) -> Any:
    """Execute a vector's declarative construction in this runtime."""
    kind = construction["kind"]
    if kind == "float":
        spelling = construction["value"]
        if spelling == "nan":
            return math.nan
        if spelling == "inf":
            return math.inf
        if spelling == "-inf":
            return -math.inf
        return float(spelling)
    if kind == "int":
        return construction["value"]
    if kind == "surrogate_string":
        return "\ud800"
    if kind == "unsupported_type":
        return {1, 2}
    if kind == "non_string_key":
        return {1: "one"}
    raise AssertionError(f"unknown construction kind: {kind}")


def test_the_worksheet_declares_the_frozen_key_set() -> None:
    assert sorted(_worksheet()["snapshot_key_set"]) == sorted(STRUCTURED_SNAPSHOT_FIELDS)


def test_every_committed_vector_recomputes_byte_for_byte() -> None:
    worksheet = _worksheet()
    snapshots = worksheet["snapshots"]
    assert snapshots, "the vector file declares no snapshot"
    for entry in snapshots:
        assert (
            structured_snapshot_canonical_json(entry["snapshot"]) == entry["canonical_json"]
        ), entry["case"]
        assert structured_snapshot_digest(entry["snapshot"]) == entry["digest"], entry["case"]


def test_each_rule_vector_is_distinguishable_from_every_other() -> None:
    """Guards the vectors themselves.

    A rule vector that hashes the same as another one is not testing its
    rule — it is testing nothing, and it would pass identically on a
    runtime with the rule missing. The only permitted collision is a
    ``pair_with`` group, where equality IS the assertion.
    """
    by_digest: dict[str, list[str]] = {}
    for entry in _worksheet()["snapshots"]:
        by_digest.setdefault(entry["digest"], []).append(entry["case"])
    for digest, cases in by_digest.items():
        if len(cases) == 1:
            continue
        paired = {
            entry.get("pair_with") for entry in _worksheet()["snapshots"] if entry["case"] in cases
        }
        assert set(cases) == paired, f"{digest[:12]} collides outside a declared pair: {cases}"


def test_the_paired_schema_version_spellings_hash_identically() -> None:
    """ADR 0093, as a cross-language vector rather than only a unit test."""
    by_case = {entry["case"]: entry for entry in _worksheet()["snapshots"]}
    integer = by_case["structured_snapshot/schema-version-integer-spelling"]
    floating = by_case["structured_snapshot/schema-version-float-spelling"]
    assert integer["snapshot"]["schema_version"] == 1
    assert floating["snapshot"]["schema_version"] == 1.0
    assert integer["canonical_json"] == floating["canonical_json"]
    assert integer["digest"] == floating["digest"]


def test_every_named_refusal_still_refuses_with_that_reason() -> None:
    worksheet = _worksheet()
    refusals = worksheet["refusals"]
    assert refusals, "the vector file declares no refusal"
    for entry in refusals:
        with pytest.raises(StructuredSnapshotError) as caught:
            canonical_value(_build(entry["construction"]))
        assert caught.value.reason == entry["reason"], entry["case"]
        assert caught.value.code == "malformed_evidence_contract"


def test_a_refusal_is_never_a_silent_rewrite() -> None:
    with pytest.raises(StructuredSnapshotError):
        canonical_value({"a": math.nan})


def test_the_three_numeric_spellings_have_three_digests() -> None:
    """Rule 1: 1, "1" and true must not collide.

    The legacy textified profile collapses all three onto the same input;
    that is the property this profile exists to remove, so it is pinned
    here rather than left to the vector file.
    """
    number = structured_snapshot_digest({"v": 1})
    text = structured_snapshot_digest({"v": "1"})
    boolean = structured_snapshot_digest({"v": True})
    assert len({number, text, boolean}) == 3


def test_integer_one_and_decoded_float_one_agree() -> None:
    """ADR 0093: ``schema_version: 1.0`` and ``1`` are the same version."""
    assert structured_snapshot_digest({"v": 1}) == structured_snapshot_digest({"v": 1.0})
    assert canonical_value({"v": -0.0}) == canonical_value({"v": 0})


def test_the_safe_integer_ceiling_is_inclusive_and_the_next_value_is_not() -> None:
    assert canonical_value(SAFE_INTEGER_MAX) == str(SAFE_INTEGER_MAX)
    with pytest.raises(StructuredSnapshotError):
        canonical_value(SAFE_INTEGER_MAX + 1)
    with pytest.raises(StructuredSnapshotError):
        canonical_value(-SAFE_INTEGER_MAX - 1)


def test_control_characters_escape_by_contract_not_by_library_default() -> None:
    """Rule 3 pins the bytes; ``json.dumps`` would emit ``\\n`` and ``\\t``."""
    assert canonical_value("a\nb") == '"a\\u000ab"'
    assert canonical_value("\x00") == '"\\u0000"'
    assert canonical_value("\x1f") == '"\\u001f"'
    assert canonical_value("\x7f") == '"\x7f"'
    assert canonical_value('"\\') == '"\\"\\\\"'


def test_keys_sort_by_utf8_bytes_at_every_level() -> None:
    encoded = canonical_value({"b": {"z": 1, "a": 2}, "a": 3})
    assert encoded == '{"a":3,"b":{"a":2,"z":1}}'


def test_array_order_is_never_reordered() -> None:
    assert canonical_value([3, 1, 2]) == "[3,1,2]"


def test_no_whitespace_is_emitted() -> None:
    assert canonical_value({"a": [1, 2], "b": {"c": 3}}) == '{"a":[1,2],"b":{"c":3}}'


def test_the_digest_is_bare_lowercase_hex_and_distinct_from_the_legacy_prefix() -> None:
    digest = structured_snapshot_digest({"v": 1})
    assert is_structured_snapshot_digest(digest)
    assert not digest.startswith("sha256:")
    assert not is_structured_snapshot_digest("sha256:" + digest)
    assert not is_structured_snapshot_digest(digest.upper())


def test_snapshot_bytes_measures_the_canonical_encoding() -> None:
    snapshot = {"v": "é"}
    assert structured_snapshot_bytes(snapshot) == len(canonical_bytes(snapshot))


def test_a_non_mapping_snapshot_refuses() -> None:
    with pytest.raises(StructuredSnapshotError):
        structured_snapshot_canonical_json([1, 2])  # type: ignore[arg-type]


def test_the_legacy_adr_0092_digest_is_untouched() -> None:
    """E12: the new profile is additive; the legacy identity must not move."""
    legacy = json.loads(LEGACY_WORKSHEET.read_text(encoding="utf-8"))
    checked = 0
    for entry in legacy["snapshots"]:
        if "raw_ingress_snapshot" not in entry:
            continue
        snapshot = entry["raw_ingress_snapshot"]
        assert snapshot_canonical_json(snapshot) == entry["canonical_json"]
        assert compute_snapshot_digest(snapshot) == entry["snapshot_digest"]
        checked += 1
    assert checked == 2
