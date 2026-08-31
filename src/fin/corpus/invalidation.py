"""Content-invalidation trigger: which old blocks went stale, and why
(plan T18, SC24, §B4「内容失效触发条件」).

This module is the corpus-side half of T18. It answers one question —
*given what fin pushed before and what the current rules produce now,
which pushed blocks must be invalidated?* — and names the reason. The
call to the retrieval service, its idempotency and its named failures
live in :mod:`fin.retrieve.invalidation_port`.

§B4 lists two triggers, and both reduce to the same observation on the
T14 idempotency key:

new ``version`` for the same ``{document_id, unit}``
    The `source_ref` is the stable serialization of the locator triple
    ``{document_id, version, unit}``, so a new version changes the
    `source_ref` and therefore the idempotency key of every block of
    that unit. The old keys are the stale ones.

rule / params-table version change that changed the block text
    The `source_ref` is unchanged but the content identity (block text
    + the three rule versions) is not, so again the old keys disappear
    from the current set.

"Old block" is therefore identified by its **T14 idempotency key**
(``(source_ref, content identity)``) — no second identity scheme and no
second `source_ref` spelling. A block whose key survives the rebuild is
byte-identical content and is left alone.

Reason attribution consumes T13's signal
----------------------------------------
Whether a text change came from the parse rules or from the params
table is not re-derived here: :func:`version_signal_for_product_area`
delegates to :func:`fin.corpus.rebuild.detect_version_changes`, the same
``parse_changed`` / ``params_changed`` pair
``uv run python -m fin.corpus.rebuild check`` prints. Callers that
already hold a signal (from a rebuild report, say) pass it straight in.
With no signal available the change is still named, as
``block_text_changed`` — never silently unattributed.

Scope
-----
:func:`plan_invalidations` judges per document: a document the current
record set says nothing about is left alone, so a per-document re-push
cannot invalidate its neighbours. Within a document the current set does
mention, the set must be **complete** — a unit that produces no blocks
any more is read as gone and named ``unit_absent``, which is what a
re-parse dropping a page looks like and is exactly the case a
unit-scoped comparison would leave hittable forever.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final, Iterable, Mapping, Protocol, Sequence

from fin.corpus.push import PushRecord, idempotency_keys_digest

# Reason vocabulary: a closed set, so a caller may branch on it and a
# test may assert the whole vocabulary is covered.
REASON_UNIT_VERSION_CHANGED: Final[str] = "unit_version_changed"
REASON_UNIT_ABSENT: Final[str] = "unit_absent"
REASON_PARSE_RULE_VERSION_CHANGED: Final[str] = "parse_rule_version_changed"
REASON_PARAMS_TABLE_VERSION_CHANGED: Final[str] = "params_table_version_changed"
REASON_BLOCK_TEXT_CHANGED: Final[str] = "block_text_changed"

INVALIDATION_REASONS: Final[frozenset[str]] = frozenset(
    {
        REASON_UNIT_VERSION_CHANGED,
        REASON_UNIT_ABSENT,
        REASON_PARSE_RULE_VERSION_CHANGED,
        REASON_PARAMS_TABLE_VERSION_CHANGED,
        REASON_BLOCK_TEXT_CHANGED,
    }
)


class InvalidationPlanError(RuntimeError):
    """Base class for the trigger side's named failures.

    Same shape as the push path's :class:`fin.retrieve.push_port.PushError`
    family: every subclass carries a distinct stable :attr:`code`, and a
    failure is never signalled by returning an empty plan.
    """

    code: str = "invalidation_plan_error"


class InvalidationLocatorMissingError(InvalidationPlanError):
    """A pushed record carries no readable locator, so the frozen unit
    its blocks belong to cannot be determined.

    Refused by name rather than by quietly dropping the record from the
    comparison — a dropped record is a block that would never be
    invalidated.
    """

    code = "invalidation_locator_missing"


class VersionChange(Protocol):
    """The part of T13's ``VersionChangeSignal`` this module reads."""

    parse_changed: bool
    params_changed: bool


@dataclass(frozen=True)
class InvalidationTarget:
    """One old block to invalidate.

    Identity is the T14 idempotency key (:attr:`idempotency_key`); the
    ``block_id`` rides along so the service can answer with the block
    ids it closed and so the record is readable. ``reason`` is one of
    :data:`INVALIDATION_REASONS`.
    """

    source_ref: str
    content_id: str
    block_id: str
    document_id: str
    version: str
    unit: int
    reason: str

    @property
    def idempotency_key(self) -> tuple[str, str]:
        """§B4: the ``(source_ref, content identity)`` pair — the same
        write identity the push path uses."""
        return (self.source_ref, self.content_id)

    def to_wire(self) -> dict[str, Any]:
        """The wire shape of one invalidation target."""
        return {
            "source_ref": self.source_ref,
            "content_id": self.content_id,
            "block_id": self.block_id,
            "reason": self.reason,
        }

    def with_content_id(self, content_id: str) -> "InvalidationTarget":
        """A copy pointing at another content identity — the scripted
        shape of a target the store never accepted (used to assert the
        unknown-target refusal; planned targets always come from records
        that were pushed)."""
        return replace(self, content_id=content_id)


def _locator_of(record: PushRecord) -> Mapping[str, Any]:
    locator = record.metadata.get("locator")
    if not isinstance(locator, Mapping):
        raise InvalidationLocatorMissingError(
            f"push record {record.block_id!r} carries no locator metadata; "
            "the frozen unit of its blocks cannot be determined"
        )
    missing = [
        field
        for field in ("document_id", "version", "unit")
        if locator.get(field) in (None, "")
    ]
    if missing:
        raise InvalidationLocatorMissingError(
            f"push record {record.block_id!r} locator misses "
            f"{', '.join(missing)}"
        )
    return locator


def _unit_scope(record: PushRecord) -> tuple[str, int]:
    """The ``{document_id, unit}`` pair §B4's first trigger is stated on."""
    locator = _locator_of(record)
    return (str(locator["document_id"]), int(locator["unit"]))


def version_signal_for_product_area(target_dir: Path | str):
    """T13's version-change signal for a rebuild product area.

    Delegates to :func:`fin.corpus.rebuild.detect_version_changes` — the
    single version-probing authority — so the trigger side never grows a
    second way to ask "did the rules move?". The import is local because
    the rebuild module pulls in the dataset loader, which the trigger
    path does not otherwise need.
    """
    from fin.corpus.rebuild import detect_version_changes

    return detect_version_changes(Path(target_dir))


def _text_change_reason(signal: VersionChange | None) -> str:
    if signal is not None and getattr(signal, "parse_changed", False):
        return REASON_PARSE_RULE_VERSION_CHANGED
    if signal is not None and getattr(signal, "params_changed", False):
        return REASON_PARAMS_TABLE_VERSION_CHANGED
    return REASON_BLOCK_TEXT_CHANGED


def _index_by_scope(
    records: Iterable[PushRecord],
) -> dict[tuple[str, int], list[PushRecord]]:
    index: dict[tuple[str, int], list[PushRecord]] = {}
    for record in records:
        index.setdefault(_unit_scope(record), []).append(record)
    return index


def plan_invalidations(
    previous: Sequence[PushRecord],
    current: Sequence[PushRecord],
    *,
    version_signal: VersionChange | None = None,
) -> tuple[InvalidationTarget, ...]:
    """The invalidation targets implied by moving from ``previous`` to
    ``current`` (§B4's two triggers).

    A previously pushed block is stale when its T14 idempotency key is
    absent from ``current`` — which covers both a new unit ``version``
    (the `source_ref` moved) and a rule / params-table bump that changed
    the block text (the content identity moved).

    Only documents ``current`` covers are judged (see module doc). The
    result is in canonical order (by `source_ref`, then content id, then
    block id), so it does not depend on the order the records arrive in
    and neither does :func:`request_key`.
    """
    current_by_scope = _index_by_scope(current)
    current_documents = {document_id for document_id, _ in current_by_scope}
    current_keys = {record.idempotency_key for record in current}

    targets: list[InvalidationTarget] = []
    for record in previous:
        scope = _unit_scope(record)
        document_id, unit = scope
        if document_id not in current_documents:
            continue
        if record.idempotency_key in current_keys:
            continue
        survivors = current_by_scope.get(scope, [])
        if not survivors:
            reason = REASON_UNIT_ABSENT
        elif str(_locator_of(record)["version"]) not in {
            str(_locator_of(other)["version"]) for other in survivors
        }:
            reason = REASON_UNIT_VERSION_CHANGED
        else:
            reason = _text_change_reason(version_signal)
        targets.append(
            InvalidationTarget(
                source_ref=record.source_ref,
                content_id=record.content_id,
                block_id=record.block_id,
                document_id=document_id,
                version=str(_locator_of(record)["version"]),
                unit=unit,
                reason=reason,
            )
        )
    return tuple(
        sorted(
            targets,
            key=lambda target: (
                target.source_ref,
                target.content_id,
                target.block_id,
            ),
        )
    )


def request_key(targets: Sequence[InvalidationTarget]) -> str:
    """The idempotency key of one invalidation request.

    The digest of the targets' T14 idempotency keys
    (:func:`fin.corpus.push.idempotency_keys_digest`, the same
    construction the push batch key uses — no second hashing scheme).
    The keys are sorted first, so the request key identifies the *set*
    of old blocks and is not perturbed by their order; the ``reason`` is
    deliberately not part of it, since re-attributing the same stale
    blocks is the same request.
    """
    return idempotency_keys_digest(
        sorted(target.idempotency_key for target in targets)
    )


__all__ = [
    "INVALIDATION_REASONS",
    "REASON_BLOCK_TEXT_CHANGED",
    "REASON_PARAMS_TABLE_VERSION_CHANGED",
    "REASON_PARSE_RULE_VERSION_CHANGED",
    "REASON_UNIT_ABSENT",
    "REASON_UNIT_VERSION_CHANGED",
    "InvalidationLocatorMissingError",
    "InvalidationPlanError",
    "InvalidationTarget",
    "VersionChange",
    "plan_invalidations",
    "request_key",
    "version_signal_for_product_area",
]
