"""Content-invalidation trigger: which old blocks go stale, and why
(plan T18, SC24, §B4「内容失效触发条件」).

This file covers the **trigger** half of T18 (the corpus side): given
what was pushed before and what the current rules produce now, which
frozen-unit blocks must be invalidated and under which named reason.
The call to the retrieval service, the idempotency assertion and the
named-failure surface live in ``tests/retrieve/test_invalidation.py``.

The version-change attribution consumes T13's signal
(``fin.corpus.rebuild.detect_version_changes``, surfaced by
``uv run python -m fin.corpus.rebuild check`` as ``parse_changed`` /
``params_changed``) rather than probing rule versions a second way.
"""

from __future__ import annotations

import json

import pytest

from fin.corpus.chunking import split_unit
from fin.corpus.invalidation import (
    INVALIDATION_REASONS,
    REASON_BLOCK_TEXT_CHANGED,
    REASON_PARAMS_TABLE_VERSION_CHANGED,
    REASON_PARSE_RULE_VERSION_CHANGED,
    REASON_UNIT_ABSENT,
    REASON_UNIT_VERSION_CHANGED,
    InvalidationLocatorMissingError,
    InvalidationTarget,
    plan_invalidations,
    request_key,
    version_signal_for_product_area,
)
from fin.corpus.normalization import NORMALIZATION_RULE_VERSION
from fin.corpus.params import PARAMS_TABLE_VERSION
from fin.corpus.parsers import PARSE_RULE_VERSION
from fin.corpus.parsers._common import build_unit
from fin.corpus.push import unit_push_records
from fin.corpus.rebuild import REBUILD_MANIFEST_NAME

DOCUMENT_ID = "TEST-2023Q1"
VERSION_A = "a" * 64
VERSION_B = "b" * 64


def _sentences(n: int, start: int = 0, topic: str = "operating revenue") -> str:
    return "".join(
        f"Sentence {i:03d} reports that {topic} grew in the quarter "
        "and the gross margin improved again. "
        for i in range(start, start + n)
    )


def _records(
    *,
    unit: int = 1,
    version: str = VERSION_A,
    text: str | None = None,
    document_id: str = DOCUMENT_ID,
):
    chunking = split_unit(
        build_unit(
            fmt="txt",
            source_sha256=version,
            document_id=document_id,
            version=version,
            unit=unit,
            text=text if text is not None else _sentences(40),
        ),
        doc_type="narrative",
    )
    return unit_push_records(chunking)


# ---------------------------------------------------------------------------
# Trigger 1 (§B4): a new `version` for the same {document_id, unit}
# ---------------------------------------------------------------------------


def test_new_version_of_the_same_document_unit_invalidates_the_old_blocks() -> None:
    previous = _records(version=VERSION_A)
    current = _records(version=VERSION_B)
    assert len(previous) > 1

    targets = plan_invalidations(previous, current)

    assert {target.block_id for target in targets} == {
        record.block_id for record in previous
    }
    assert {target.idempotency_key for target in targets} == {
        record.idempotency_key for record in previous
    }
    assert {target.reason for target in targets} == {REASON_UNIT_VERSION_CHANGED}
    # the new version's blocks are not invalidated
    assert {record.block_id for record in current}.isdisjoint(
        {target.block_id for target in targets}
    )


def test_unchanged_records_invalidate_nothing() -> None:
    previous = _records()

    assert plan_invalidations(previous, previous) == ()


def test_other_units_of_the_same_document_are_not_invalidated() -> None:
    previous = [*_records(unit=1), *_records(unit=2)]
    current = [*_records(unit=1, version=VERSION_B), *_records(unit=2)]

    targets = plan_invalidations(previous, current)

    assert {target.unit for target in targets} == {1}


def test_a_document_absent_from_the_current_set_is_out_of_scope() -> None:
    """A per-document re-push must not invalidate the documents it says
    nothing about."""
    other = "TEST-2022Q4"
    previous = [*_records(unit=1), *_records(unit=1, document_id=other)]
    current = _records(unit=1, version=VERSION_B)

    targets = plan_invalidations(previous, current)

    assert {target.document_id for target in targets} == {DOCUMENT_ID}


def test_a_unit_that_disappeared_from_a_covered_document_is_named() -> None:
    previous = [*_records(unit=1), *_records(unit=2)]
    current = _records(unit=1, version=VERSION_B)

    targets = plan_invalidations(previous, current)

    reasons = {target.unit: target.reason for target in targets}
    assert reasons[1] == REASON_UNIT_VERSION_CHANGED
    assert reasons[2] == REASON_UNIT_ABSENT


# ---------------------------------------------------------------------------
# Trigger 2 (§B4): rule / params-table version change changed block text
# ---------------------------------------------------------------------------


class _Signal:
    """The two flags T13's `rebuild check` prints, scripted."""

    def __init__(self, *, parse_changed: bool, params_changed: bool) -> None:
        self.parse_changed = parse_changed
        self.params_changed = params_changed


def test_block_text_change_under_a_parse_rule_bump_is_attributed_to_it() -> None:
    previous = _records()
    current = _records(text=_sentences(40, topic="net interest income"))

    targets = plan_invalidations(
        previous,
        current,
        version_signal=_Signal(parse_changed=True, params_changed=False),
    )

    assert {target.block_id for target in targets} == {
        record.block_id for record in previous
    }
    assert {target.reason for target in targets} == {
        REASON_PARSE_RULE_VERSION_CHANGED
    }


def test_block_text_change_under_a_params_table_bump_is_attributed_to_it() -> None:
    previous = _records()
    current = _records(text=_sentences(40, topic="net interest income"))

    targets = plan_invalidations(
        previous,
        current,
        version_signal=_Signal(parse_changed=False, params_changed=True),
    )

    assert {target.reason for target in targets} == {
        REASON_PARAMS_TABLE_VERSION_CHANGED
    }


def test_block_text_change_without_a_version_signal_is_still_named() -> None:
    previous = _records()
    current = _records(text=_sentences(40, topic="net interest income"))

    targets = plan_invalidations(previous, current)

    assert {target.reason for target in targets} == {REASON_BLOCK_TEXT_CHANGED}


def test_the_reason_vocabulary_is_a_closed_set() -> None:
    assert INVALIDATION_REASONS == frozenset(
        {
            REASON_UNIT_VERSION_CHANGED,
            REASON_UNIT_ABSENT,
            REASON_PARSE_RULE_VERSION_CHANGED,
            REASON_PARAMS_TABLE_VERSION_CHANGED,
            REASON_BLOCK_TEXT_CHANGED,
        }
    )


def test_only_the_blocks_whose_key_disappeared_go_stale() -> None:
    """A rewrite that leaves some blocks intact invalidates only the rest."""
    previous = _records(text=_sentences(40))
    rewritten = _records(text=_sentences(40, topic="net interest income"))
    current = (*previous[:2], *rewritten[2:])

    targets = plan_invalidations(previous, current)

    assert {target.idempotency_key for target in targets} == {
        record.idempotency_key for record in previous[2:]
    }
    surviving = {record.idempotency_key for record in current}
    assert all(target.idempotency_key not in surviving for target in targets)


# ---------------------------------------------------------------------------
# T13 signal consumption: the product area's recorded rule versions
# ---------------------------------------------------------------------------


def test_version_signal_comes_from_the_t13_rebuild_record(tmp_path) -> None:
    product = tmp_path / "blocks"
    product.mkdir()
    (product / REBUILD_MANIFEST_NAME).write_text(
        json.dumps(
            {
                "parse_rule_version": "parse-0-old",
                "params_table_version": PARAMS_TABLE_VERSION,
                "normalization_rule_version": NORMALIZATION_RULE_VERSION,
                "samples": {},
            }
        ),
        encoding="utf-8",
    )

    signal = version_signal_for_product_area(product)

    assert signal.parse_changed is True
    assert signal.params_changed is False
    assert signal.current_parse_rule_version == PARSE_RULE_VERSION

    targets = plan_invalidations(
        _records(),
        _records(text=_sentences(40, topic="loss provision")),
        version_signal=signal,
    )
    assert {target.reason for target in targets} == {
        REASON_PARSE_RULE_VERSION_CHANGED
    }


def test_an_empty_product_area_reports_everything_stale(tmp_path) -> None:
    signal = version_signal_for_product_area(tmp_path / "missing")

    assert signal.has_record is False
    assert signal.changed is True


# ---------------------------------------------------------------------------
# Request key: built from the T14 idempotency keys, one construction
# ---------------------------------------------------------------------------


def test_request_key_is_stable_and_order_independent() -> None:
    targets = plan_invalidations(_records(), _records(version=VERSION_B))

    assert request_key(targets) == request_key(tuple(reversed(targets)))
    assert request_key(targets) != request_key(targets[:-1])


def test_request_key_ignores_the_reason_and_keys_on_the_idempotency_pairs() -> None:
    targets = plan_invalidations(_records(), _records(version=VERSION_B))
    relabelled = tuple(
        InvalidationTarget(
            source_ref=target.source_ref,
            content_id=target.content_id,
            block_id=target.block_id,
            document_id=target.document_id,
            version=target.version,
            unit=target.unit,
            reason=REASON_BLOCK_TEXT_CHANGED,
        )
        for target in targets
    )

    assert request_key(relabelled) == request_key(targets)


def test_planned_targets_are_in_a_canonical_order() -> None:
    previous = _records()
    current = _records(version=VERSION_B)

    forward = plan_invalidations(previous, current)
    backward = plan_invalidations(tuple(reversed(previous)), current)

    assert forward == backward


# ---------------------------------------------------------------------------
# Named failure on the trigger side
# ---------------------------------------------------------------------------


def test_a_record_without_locator_metadata_fails_by_name() -> None:
    previous = _records()
    crippled = previous[0].without_metadata_field("locator")

    with pytest.raises(InvalidationLocatorMissingError) as excinfo:
        plan_invalidations([crippled], _records(version=VERSION_B))

    assert excinfo.value.code == "invalidation_locator_missing"
