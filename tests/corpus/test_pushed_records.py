"""TDD: the durable per-document pushed-record store.

Context: a shipped boot reopens the job and moves the pointer on a
replacement upload, but the stale-block closure is a documented no-op —
nothing durable stored the previous generation's PushRecords, so after a
restart fin cannot know what is stale.

Contract:
  * the provider reader is zero-argument at the call site and returns
    Sequence[PushRecord] (the IngestEndpoint seam);
  * records carry the locator triple metadata._locator_of needs;
  * re-recording the same (document_id, version) replaces, not duplicates;
  * a new store instance over the same path replays the log — restart
    durability;
  * deterministic replay order.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fin.corpus.invalidation import (
    REASON_UNIT_VERSION_CHANGED,
    _locator_of,
    plan_replacement_invalidations,
)
from fin.corpus.push import PushRecord
from fin.corpus.pushed_records import (
    PushedRecordsCorruptError,
    PushedRecordsStore,
    PushedRecordsUnwritableError,
    _payload_to_record,
    pushed_records_path_for_catalog,
)


def _record(block_id: str, document_id: str, version: str, unit: int = 0) -> PushRecord:
    return PushRecord(
        block_id=block_id,
        source_ref=json.dumps(
            {"document_id": document_id, "version": version, "unit": unit},
            sort_keys=True,
        ),
        content_id=f"cid-{block_id}",
        text=f"text of {block_id}",
        parent_block_id=f"parent-{unit}",
        parent_text="parent text",
        metadata={
            "company": "ACME",
            "period": version,
            "locator": {"document_id": document_id, "version": version, "unit": unit},
        },
    )


def test_in_memory_store_round_trips_records() -> None:
    store = PushedRecordsStore()
    records = [_record("a-1", "DOC-A", "2024-01"), _record("a-2", "DOC-A", "2024-01")]
    store.replace("DOC-A", "2024-01", records)

    assert store.all_records() == records
    assert store.records_for("DOC-A") == records
    assert store.records_for("DOC-B") == []


def test_path_backed_store_replays_records_after_restart(tmp_path) -> None:
    path = tmp_path / "pushed_records.jsonl"
    store = PushedRecordsStore(path=path)
    records = [
        _record("a-1", "DOC-A", "2024-01"),
        _record("a-2", "DOC-A", "2024-01"),
    ]
    store.replace("DOC-A", "2024-01", records)

    rebuilt = PushedRecordsStore(path=path)
    by_id = {r.block_id: r for r in rebuilt.all_records()}
    assert set(by_id) == {"a-1", "a-2"}
    for original in records:
        restored = by_id[original.block_id]
        assert restored.source_ref == original.source_ref
        assert restored.content_id == original.content_id
        assert restored.text == original.text
        assert restored.parent_block_id == original.parent_block_id
        assert restored.parent_text == original.parent_text
        assert restored.metadata == original.metadata
    # The replayed records survive the planner's locator check.
    assert _locator_of(restored)["document_id"] == "DOC-A"


def test_same_generation_replaced_is_not_duplicated(tmp_path) -> None:
    path = tmp_path / "pushed_records.jsonl"
    store = PushedRecordsStore(path=path)
    first = [_record("a-1", "DOC-A", "2024-01")]
    second = [_record("a-1", "DOC-A", "2024-01", unit=1)]
    store.replace("DOC-A", "2024-01", first)
    store.replace("DOC-A", "2024-01", second)

    assert store.all_records() == second

    rebuilt = PushedRecordsStore(path=path)
    assert rebuilt.all_records() == second
    with path.open(encoding="utf-8") as handle:
        assert len([line for line in handle if line.strip()]) == 2


def test_drop_of_a_block_survives_the_replace(tmp_path) -> None:
    path = tmp_path / "pushed_records.jsonl"
    store = PushedRecordsStore(path=path)
    store.replace(
        "DOC-A", "2024-01", [_record("a-1", "DOC-A", "2024-01"), _record("a-2", "DOC-A", "2024-01")]
    )
    store.replace("DOC-A", "2024-01", [_record("a-1", "DOC-A", "2024-01")])

    rebuilt = PushedRecordsStore(path=path)
    assert [r.block_id for r in rebuilt.all_records()] == ["a-1"]


def test_versions_recorded_in_order(tmp_path) -> None:
    path = tmp_path / "pushed_records.jsonl"
    store = PushedRecordsStore(path=path)
    store.replace("DOC-A", "2024-01", [_record("a-1", "DOC-A", "2024-01")])
    store.replace("DOC-A", "2024-02", [_record("a-2", "DOC-A", "2024-02")])
    store.replace("DOC-B", "2024-01", [_record("b-1", "DOC-B", "2024-01")])

    assert store.versions("DOC-A") == ["2024-01", "2024-02"]
    assert [r.block_id for r in store.records_for("DOC-A")] == ["a-1", "a-2"]
    assert [r.block_id for r in store.all_records()] == ["a-1", "a-2", "b-1"]


def test_replay_is_deterministic(tmp_path) -> None:
    path = tmp_path / "pushed_records.jsonl"
    store = PushedRecordsStore(path=path)
    store.replace("DOC-A", "2024-01", [_record(f"a-{i}", "DOC-A", "2024-01") for i in range(5)])
    store.replace("DOC-B", "2024-01", [_record(f"b-{i}", "DOC-B", "2024-01") for i in range(3)])
    expected = [r.block_id for r in store.all_records()]

    for _ in range(3):
        rebuilt = PushedRecordsStore(path=path)
        assert [r.block_id for r in rebuilt.all_records()] == expected


def test_zero_arg_view_is_the_single_wired_provider_shape(tmp_path) -> None:
    """Boot wires ``all_records`` itself; no per-document provider seam.

    The endpoint seam is a zero-argument callable and the planner
    filters the returned view by document, so a per-document closure
    factory (the old ``provider()``) would be a second, unwired path —
    it must not come back.
    """
    path = tmp_path / "pushed_records.jsonl"
    store = PushedRecordsStore(path=path)
    store.replace("DOC-A", "2024-01", [_record("a-1", "DOC-A", "2024-01")])
    store.replace("DOC-B", "2024-01", [_record("b-1", "DOC-B", "2024-01")])

    assert not hasattr(store, "provider")
    previous_records = store.all_records
    targets = plan_replacement_invalidations(
        previous_records(), "DOC-A", version="2024-02"
    )
    assert [t.reason for t in targets] == [REASON_UNIT_VERSION_CHANGED]
    assert [t.block_id for t in targets] == ["a-1"]


def test_all_records_excludes_the_incoming_generation() -> None:
    store = PushedRecordsStore()
    store.replace("DOC-A", "2024-01", [_record("a-1", "DOC-A", "2024-01")])
    store.replace("DOC-A", "2024-02", [_record("a-2", "DOC-A", "2024-02")])

    stale = store.all_records(exclude_generation=("DOC-A", "2024-02"))
    assert [r.block_id for r in stale] == ["a-1"]
    targets = plan_replacement_invalidations(stale, "DOC-A", version="2024-02")
    assert [t.block_id for t in targets] == ["a-1"]
    # A same-version re-ingest sees no stale candidate: the store holds
    # only that generation and the view excludes it.
    fresh = PushedRecordsStore()
    fresh.replace("DOC-A", "2024-01", [_record("a-1", "DOC-A", "2024-01")])
    assert fresh.all_records(exclude_generation=("DOC-A", "2024-01")) == []
    assert (
        plan_replacement_invalidations(
            fresh.all_records(exclude_generation=("DOC-A", "2024-01")),
            "DOC-A",
            version="2024-01",
        )
        == ()
    )
    # The planner alone also skips the incoming version's own records.
    assert (
        plan_replacement_invalidations(
            store.all_records(), "DOC-A", version="2024-02"
        )
        == targets
    )


def test_exclusion_is_pair_scoped_not_cross_document() -> None:
    """A same-named version on another document must survive exclusion."""
    store = PushedRecordsStore()
    store.replace("DOC-A", "2024-01", [_record("a-1", "DOC-A", "2024-01")])
    store.replace("DOC-B", "2024-01", [_record("b-1", "DOC-B", "2024-01")])

    stale = store.all_records(exclude_generation=("DOC-A", "2024-01"))
    assert [r.block_id for r in stale] == ["b-1"]


def test_exclusion_needs_a_full_pair() -> None:
    store = PushedRecordsStore()
    store.replace("DOC-A", "2024-01", [_record("a-1", "DOC-A", "2024-01")])

    with pytest.raises(ValueError):
        store.all_records(exclude_generation=("DOC-A",))
    with pytest.raises(ValueError):
        store.all_records(exclude_generation=("DOC-A", ""))


def test_in_memory_store_writes_no_file(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    store = PushedRecordsStore()
    store.replace("DOC-A", "2024-01", [_record("a-1", "DOC-A", "2024-01")])

    assert list(tmp_path.iterdir()) == []


def test_unwritable_append_raises_the_named_error(tmp_path) -> None:
    blocked = tmp_path / "ro"
    blocked.mkdir()
    blocked.chmod(0o500)
    try:
        with pytest.raises(PushedRecordsUnwritableError) as excinfo:
            PushedRecordsStore(path=blocked / "pushed_records.jsonl").replace(
                "DOC-A", "2024-01", [_record("a-1", "DOC-A", "2024-01")]
            )
        assert excinfo.value.code == "pushed_records_unwritable"
    finally:
        blocked.chmod(0o700)


def test_rebuild_names_a_corrupt_line_by_name(tmp_path) -> None:
    path = tmp_path / "pushed_records.jsonl"
    store = PushedRecordsStore(path=path)
    store.replace("DOC-A", "2024-01", [_record("a-1", "DOC-A", "2024-01")])
    store.replace("DOC-A", "2024-02", [_record("a-2", "DOC-A", "2024-02")])
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"event": "recorded", "document_id": "DOC-A", "ver')

    with pytest.raises(PushedRecordsCorruptError) as excinfo:
        PushedRecordsStore(path=path)
    assert excinfo.value.code == "pushed_records_corrupt"
    assert "line 3" in str(excinfo.value)


def test_rebuild_names_a_line_that_is_not_a_record_event(tmp_path) -> None:
    path = tmp_path / "pushed_records.jsonl"
    store = PushedRecordsStore(path=path)
    store.replace("DOC-A", "2024-01", [_record("a-1", "DOC-A", "2024-01")])
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"event": "other"}\n{"block_id": "x"}\n')

    with pytest.raises(PushedRecordsCorruptError) as excinfo:
        PushedRecordsStore(path=path)
    assert excinfo.value.code == "pushed_records_corrupt"
    assert "line 2" in str(excinfo.value)


def test_path_is_beside_the_configured_catalog(tmp_path) -> None:
    assert pushed_records_path_for_catalog(tmp_path / "catalog.json") == (
        tmp_path / "pushed_records.jsonl"
    )


def test_record_reconstructs_from_full_payload() -> None:
    record = _record("a-1", "DOC-A", "2024-01")
    payload = {
        "block_id": record.block_id,
        "source_ref": record.source_ref,
        "content_id": record.content_id,
        "text": record.text,
        "parent_block_id": record.parent_block_id,
        "parent_text": record.parent_text,
        "metadata": dict(record.metadata),
    }

    restored = _payload_to_record(payload, Path("x.jsonl"), 1)
    assert restored == record
