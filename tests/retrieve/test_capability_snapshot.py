"""T19 / SC25 K1: the capability-key snapshot is present, exactly 8, non-empty.

The snapshot is fin's local copy of an enumeration whose naming authority
belongs to the retrieval service (ADR 0039). These cases pin both halves of
K1: the shape of the snapshot itself, and the single explicit sync point
against the published enumeration (spec §B4 「消融的 8 个能力项」).
"""

from __future__ import annotations

import json

import pytest

from fin.retrieve.capabilities import (
    CAPABILITY_COUNT,
    CAPABILITY_SNAPSHOT_PATH,
    SPEC_ENUMERATION_ROW_LABEL,
    CapabilityEnumerationDriftError,
    CapabilityEnumerationRowMissingError,
    CapabilitySnapshotCountError,
    CapabilitySnapshotMalformedError,
    CapabilitySnapshotMissingError,
    assert_snapshot_matches_published,
    load_capability_keys,
    published_capability_keys,
    published_spec_path,
)


# The 8 keys, transcribed verbatim from spec §B4 「消融的 8 个能力项」.
# Hard-coded here on purpose: if either the snapshot file or the spec row
# drifts, this literal is the third witness that says which side moved.
EXPECTED_KEYS = (
    "vector",
    "keyword_bm25",
    "cjk_segmentation",
    "phrase",
    "field_boost",
    "graph_ppr",
    "rrf",
    "rerank",
)


def test_snapshot_file_exists() -> None:
    assert CAPABILITY_SNAPSHOT_PATH.is_file()


def test_snapshot_has_exactly_eight_entries() -> None:
    assert len(load_capability_keys()) == CAPABILITY_COUNT == 8


def test_snapshot_is_not_empty_and_every_key_is_a_non_blank_string() -> None:
    keys = load_capability_keys()
    assert keys
    for key in keys:
        assert isinstance(key, str)
        assert key.strip() == key
        assert key


def test_snapshot_keys_are_verbatim_and_ordered() -> None:
    assert load_capability_keys() == EXPECTED_KEYS


def test_snapshot_keys_are_unique() -> None:
    keys = load_capability_keys()
    assert len(set(keys)) == len(keys)


# ----- the sync point against the retrieval service's published enumeration -----


def test_published_enumeration_is_read_from_the_spec_b4_row() -> None:
    assert published_spec_path().is_file()
    assert published_capability_keys() == EXPECTED_KEYS


def test_snapshot_matches_published_enumeration() -> None:
    # The one explicit sync point: enumeration change on the retrieval side
    # lands in the spec row, and this call is what fails until the snapshot
    # is updated to match.
    assert_snapshot_matches_published()


def test_enumeration_drift_is_a_named_failure(tmp_path) -> None:
    spec = published_spec_path().read_text(encoding="utf-8")
    drifted = spec.replace("`rerank`", "`reranking`", 1)
    assert drifted != spec
    spec_copy = tmp_path / "corpus-retrieval.md"
    spec_copy.write_text(drifted, encoding="utf-8")

    with pytest.raises(CapabilityEnumerationDriftError) as excinfo:
        assert_snapshot_matches_published(spec_path=spec_copy)
    assert excinfo.value.code == "capability_enumeration_drift"
    # Both sides are printed, not just "they differ".
    message = str(excinfo.value)
    assert "reranking" in message
    assert "rerank" in message


def test_missing_enumeration_row_is_a_named_failure(tmp_path) -> None:
    spec_copy = tmp_path / "corpus-retrieval.md"
    spec_copy.write_text("# no B4 here\n", encoding="utf-8")
    with pytest.raises(CapabilityEnumerationRowMissingError) as excinfo:
        published_capability_keys(spec_path=spec_copy)
    assert excinfo.value.code == "capability_enumeration_row_missing"
    assert SPEC_ENUMERATION_ROW_LABEL in str(excinfo.value)


# ----- snapshot-side named failures (多、缺、为空即失败) -----


def _write_snapshot(tmp_path, payload: object):
    path = tmp_path / "capability_keys.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_missing_snapshot_file_is_a_named_failure(tmp_path) -> None:
    with pytest.raises(CapabilitySnapshotMissingError) as excinfo:
        load_capability_keys(path=tmp_path / "absent.json")
    assert excinfo.value.code == "capability_snapshot_missing"


def test_malformed_snapshot_json_is_a_named_failure(tmp_path) -> None:
    path = tmp_path / "capability_keys.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(CapabilitySnapshotMalformedError) as excinfo:
        load_capability_keys(path=path)
    assert excinfo.value.code == "capability_snapshot_malformed"


@pytest.mark.parametrize(
    "payload",
    [
        {"keys": "vector"},
        {"keys": [1, 2, 3, 4, 5, 6, 7, 8]},
        {"no_keys_field": []},
        ["vector"],
    ],
)
def test_snapshot_with_a_wrong_shape_is_a_named_failure(tmp_path, payload) -> None:
    with pytest.raises(CapabilitySnapshotMalformedError) as excinfo:
        load_capability_keys(path=_write_snapshot(tmp_path, payload))
    assert excinfo.value.code == "capability_snapshot_malformed"


def test_snapshot_with_a_blank_key_is_a_named_failure(tmp_path) -> None:
    payload = {"keys": list(EXPECTED_KEYS[:-1]) + ["   "]}
    with pytest.raises(CapabilitySnapshotMalformedError) as excinfo:
        load_capability_keys(path=_write_snapshot(tmp_path, payload))
    assert excinfo.value.code == "capability_snapshot_malformed"


def test_snapshot_with_a_duplicate_key_is_a_named_failure(tmp_path) -> None:
    payload = {"keys": list(EXPECTED_KEYS[:-1]) + ["vector"]}
    with pytest.raises(CapabilitySnapshotMalformedError) as excinfo:
        load_capability_keys(path=_write_snapshot(tmp_path, payload))
    assert excinfo.value.code == "capability_snapshot_malformed"


@pytest.mark.parametrize(
    "keys",
    [
        [],
        list(EXPECTED_KEYS[:7]),
        list(EXPECTED_KEYS) + ["long_document_recall"],
    ],
    ids=["empty", "short_by_one", "long_by_one"],
)
def test_snapshot_entry_count_other_than_eight_is_a_named_failure(
    tmp_path, keys
) -> None:
    with pytest.raises(CapabilitySnapshotCountError) as excinfo:
        load_capability_keys(path=_write_snapshot(tmp_path, {"keys": keys}))
    assert excinfo.value.code == "capability_snapshot_count_mismatch"
    assert str(len(keys)) in str(excinfo.value)
