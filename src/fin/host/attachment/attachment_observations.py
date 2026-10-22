"""Versioned visual-observation envelopes and the host's reuse decision.

A visual observation is derived material about one physical page of one
attachment content version: what a configured provider saw in a rendered
page image, bound to that exact image. This module owns the shape of that
record and the only question the host may ask of it — may a later visual
question be answered from it without a new provider call — and nothing
else: the provider call lives in :mod:`fin.host.attachment.attachment_vision`, the
run's call budget in :mod:`fin.host.attachment.attachment_budgets`, and the durability
in :mod:`fin.host.attachment.attachment_store`.

Two rules bound the reuse decision. It is deterministic: reuse happens only
when the stored identity (content version, page-image hash, coverage
schema) matches what the caller expects, every required detail key exactly
equals a covered key, and the observed scope contains the requested
scope. It is conservative: an unknown schema, an unknown scope shape, a
missing or ambiguous target, or any detail that cannot be located under an
exact key answers "inspect again", never "probably covered".

The plotted-value convention gives the conservative rule its teeth. A
``plotted_value`` detail is complete only when its target binds the chart,
the series, and the category; a generic ``plotted_value`` key is
representable — a page scan may legitimately say it saw an unlocatable
number — but it never settles a specific request, because exact-key
equality cannot match a blank target to a named one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Final, Sequence

from fin.host.attachment.attachment_vision import (
    VisionCallResult,
    VisionCost,
    VisionCrop,
    VisionUsage,
)

__all__ = [
    "COVERAGE_SCHEMA_VERSION",
    "PLOTTED_VALUE_KIND",
    "CoveredDetail",
    "DetailKey",
    "ObservationAttempt",
    "ObservationContractError",
    "ObservationEnvelope",
    "observation_covers",
    "observation_from_call_result",
    "observations_jointly_cover",
    "plotted_value_key",
]


# Bumping this string is the only way to change what an envelope means; a
# reader that does not recognize the exact value treats every stored
# observation of that schema as insufficient rather than guessing.
COVERAGE_SCHEMA_VERSION: Final[str] = "fin.visual-observation/1"

PLOTTED_VALUE_KIND: Final[str] = "plotted_value"

_TARGET_PART_SEPARATOR: Final[str] = "|"
_TARGET_PAIR_SEPARATOR: Final[str] = "="
_PLOTTED_TARGET_FIELDS: Final[tuple[str, str, str]] = ("chart", "series", "category")


class ObservationContractError(ValueError):
    """An observation shape the coverage contract cannot represent or read."""


def _require_text(name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ObservationContractError(
            f"{name} must be a non-empty string, got {value!r}"
        )
    return value


def _require_positive_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ObservationContractError(
            f"{name} must be a positive integer, got {value!r}"
        )
    return value


def _require_scope(name: str, value: object) -> VisionCrop | None:
    if value is None or isinstance(value, VisionCrop):
        return value  # type: ignore[return-value]
    raise ObservationContractError(
        f"{name} must be a VisionCrop or None (whole page), got {value!r}"
    )


# ----- detail keys -----------------------------------------------------


@dataclass(frozen=True, slots=True)
class DetailKey:
    """What a question asks about: a stable kind plus an optional complete
    target. Two keys match only when both fields are equal."""

    kind: str
    target_key: str | None = None

    def __post_init__(self) -> None:
        _require_text("detail kind", self.kind)
        if self.target_key is not None:
            _require_text("detail target", self.target_key)


def plotted_value_key(*, chart: str, series: str, category: str) -> DetailKey:
    """The only key shape that can demand or grant one plotted number.

    The three parts may not contain the separators, so the canonical
    ``chart=..|series=..|category=..`` target round-trips through
    :func:`_completes_plotted_target` without ambiguity.
    """
    parts = (
        ("chart", chart),
        ("series", series),
        ("category", category),
    )
    for name, value in parts:
        _require_text(name, value)
        if any(sep in value for sep in (_TARGET_PART_SEPARATOR, _TARGET_PAIR_SEPARATOR)):
            raise ObservationContractError(
                f"plotted value {name} must not contain a target separator"
            )
    target = _TARGET_PART_SEPARATOR.join(
        f"{name}{_TARGET_PAIR_SEPARATOR}{value}" for name, value in parts
    )
    return DetailKey(kind=PLOTTED_VALUE_KIND, target_key=target)


def _completes_plotted_target(target_key: str | None) -> bool:
    """Whether ``target_key`` is exactly one chart/series/category binding."""
    if target_key is None:
        return False
    segments = target_key.split(_TARGET_PART_SEPARATOR)
    if len(segments) != len(_PLOTTED_TARGET_FIELDS):
        return False
    for expected, segment in zip(_PLOTTED_TARGET_FIELDS, segments):
        name, sep, value = segment.partition(_TARGET_PAIR_SEPARATOR)
        if name != expected or not sep or not value.strip():
            return False
    return True


# ----- covered detail and per-attempt record ----------------------------


@dataclass(frozen=True, slots=True)
class CoveredDetail:
    """One detail key paired with the scope actually observed for it.

    ``scope is None`` means the whole rendered page was looked at, and only
    such a coverage can contain a whole-page request.
    """

    key: DetailKey
    scope: VisionCrop | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.key, DetailKey):
            raise ObservationContractError(
                f"covered detail expects a DetailKey, got {self.key!r}"
            )
        _require_scope("covered detail scope", self.scope)


@dataclass(frozen=True, slots=True)
class ObservationAttempt:
    """One outbound call's provider-reported numbers.

    ``cost is None`` records that the provider reported no cost —
    unavailable, not zero; a reported zero arrives as a ``VisionCost`` and
    stays distinguishable from silence.
    """

    usage: VisionUsage | None = None
    cost: VisionCost | None = None

    def __post_init__(self) -> None:
        if self.usage is not None and not isinstance(self.usage, VisionUsage):
            raise ObservationContractError(
                f"attempt usage must be a VisionUsage, got {self.usage!r}"
            )
        if self.cost is not None and not isinstance(self.cost, VisionCost):
            raise ObservationContractError(
                f"attempt cost must be a VisionCost, got {self.cost!r}"
            )


# ----- the envelope -----------------------------------------------------


@dataclass(frozen=True, slots=True)
class ObservationEnvelope:
    """One persisted observation of one page of one attachment version.

    ``scope`` is the region the call looked at (``None`` = whole page);
    every covered detail's observed scope must be contained in it.
    ``answer_text`` is the provider's answer payload: the derived search
    material a verdict is cited from, persisted with the provenance rather
    than reconstructible from its keys. The empty string is reserved for
    writers that recorded provenance before answers were persisted.
    ``created_at`` is passed in by the caller so the record stays pure and
    testable — nothing here reads a clock.
    """

    attachment_id: str
    coverage_schema_version: str
    observation_version: int
    attachment_version: int
    source_unit: int
    page: int
    page_image_sha256: str
    provider: str
    model_version: str
    scope: VisionCrop | None = None
    covered_details: tuple[CoveredDetail, ...] = ()
    attempts: tuple[ObservationAttempt, ...] = ()
    answer_text: str = ""
    created_at: datetime = field(default=None)  # type: ignore[assignment]

    def __post_init__(self) -> None:
        _require_text("attachment_id", self.attachment_id)
        _require_text("coverage_schema_version", self.coverage_schema_version)
        _require_text("page_image_sha256", self.page_image_sha256)
        _require_text("provider", self.provider)
        _require_text("model_version", self.model_version)
        _require_positive_int("observation_version", self.observation_version)
        _require_positive_int("attachment_version", self.attachment_version)
        _require_positive_int("source_unit", self.source_unit)
        _require_positive_int("page", self.page)
        _require_scope("observation scope", self.scope)
        # WHY empty is allowed here: a provenance-only row is a real stored
        # shape, but the field's type is the payload contract, so a
        # non-string answer is refused at the boundary, not at the store.
        if not isinstance(self.answer_text, str):
            raise ObservationContractError(
                f"answer_text must be a string (empty allowed), got "
                f"{self.answer_text!r}"
            )
        if not isinstance(self.created_at, datetime) or self.created_at.tzinfo is None:
            raise ObservationContractError(
                "created_at must be a tz-aware datetime supplied by the caller"
            )
        details = tuple(self.covered_details)
        for covered in details:
            if not isinstance(covered, CoveredDetail):
                raise ObservationContractError(
                    f"covered_details expects CoveredDetail values, got {covered!r}"
                )
            # WHY containment here: a detail may not claim coverage of a
            # region the inspected scope never included; the envelope is
            # where that promise becomes checkable.
            if not _scope_contains(self.scope, covered.scope):
                raise ObservationContractError(
                    f"covered detail {covered.key.kind!r} scope exceeds the "
                    f"observation scope {self.scope!r}"
                )
        attempts = tuple(self.attempts)
        for attempt in attempts:
            if not isinstance(attempt, ObservationAttempt):
                raise ObservationContractError(
                    f"attempts expects ObservationAttempt values, got {attempt!r}"
                )
        object.__setattr__(self, "covered_details", details)
        object.__setattr__(self, "attempts", attempts)


def observation_from_call_result(
    result: VisionCallResult,
    *,
    observation_version: int,
    source_unit: int,
    page_image_sha256: str,
    provider: str,
    created_at: datetime,
    covered_details: Sequence[CoveredDetail] = (),
    coverage_schema_version: str = COVERAGE_SCHEMA_VERSION,
) -> ObservationEnvelope:
    """Bind one vision answer to the provenance the call itself cannot know.

    WHY the version is re-read here: :class:`~fin.host.attachment.attachment_vision.VisionTarget`
    carries the content version as text while the store addresses versions
    as integers, so this is the boundary that reconciles the two — a target
    whose version is not an integer is refused rather than coerced.

    WHY the answer text is always copied: an envelope minted from a call is
    the only version of that answer, and a stored observation that may
    ground a verdict must be citable from storage alone.
    """
    try:
        attachment_version = int(result.target.version.strip())
    except (AttributeError, ValueError) as exc:
        raise ObservationContractError(
            "vision target version must be an integer-formatted string to "
            f"address a content version, got {result.target.version!r}"
        ) from exc
    return ObservationEnvelope(
        attachment_id=result.target.attachment_id,
        coverage_schema_version=coverage_schema_version,
        observation_version=observation_version,
        attachment_version=attachment_version,
        source_unit=source_unit,
        page=result.target.page,
        page_image_sha256=page_image_sha256,
        provider=provider,
        model_version=result.model_id,
        scope=result.target.crop,
        covered_details=tuple(covered_details),
        attempts=(ObservationAttempt(usage=result.usage, cost=result.cost),),
        answer_text=result.text,
        created_at=created_at,
    )


# ----- the deterministic reuse decision ---------------------------------


def _scope_contains(observed: VisionCrop | None, requested: VisionCrop | None) -> bool:
    """Whole page contains every region; a crop contains only crops inside it."""
    if observed is None:
        return True
    if requested is None:
        return False
    return (
        requested.left >= observed.left
        and requested.top >= observed.top
        and requested.right <= observed.right
        and requested.bottom <= observed.bottom
    )


def _identity_matches(
    observation: ObservationEnvelope,
    *,
    attachment_version: int,
    page_image_sha256: str,
    coverage_schema_version: str,
) -> bool:
    # WHY equality and not ranges: the caller states what it is looking at
    # now, and any drift — a re-rendered page, a new content version, a
    # schema this build does not understand — voids reuse wholesale.
    if coverage_schema_version != COVERAGE_SCHEMA_VERSION:
        return False
    if observation.coverage_schema_version != COVERAGE_SCHEMA_VERSION:
        return False
    if observation.attachment_version != attachment_version:
        return False
    return observation.page_image_sha256 == page_image_sha256


def _covers_detail(
    observation: ObservationEnvelope,
    key: DetailKey,
    requested_scope: VisionCrop | None,
) -> bool:
    for covered in observation.covered_details:
        if covered.key != key:
            continue
        if _scope_contains(covered.scope, requested_scope):
            return True
    return False


def observations_jointly_cover(
    observations: Sequence[ObservationEnvelope],
    required_details: Sequence[DetailKey],
    requested_scope: VisionCrop | None,
    *,
    attachment_version: int,
    page_image_sha256: str,
    coverage_schema_version: str,
) -> bool:
    """Whether this set of stored observations answers the question already.

    Joint reuse is allowed only when every observation in the set describes
    the same source unit and physical page: the question is about one region
    of one page, and mixing pages or units into one verdict would let an
    observation from elsewhere stand in for the detail that is missing here.
    """
    rows = tuple(observations)
    required = tuple(required_details)
    if not rows or not required:
        return False
    if requested_scope is not None and not isinstance(requested_scope, VisionCrop):
        return False  # EXIT: an unknown requested scope is insufficient.
    for observation in rows:
        if not _identity_matches(
            observation,
            attachment_version=attachment_version,
            page_image_sha256=page_image_sha256,
            coverage_schema_version=coverage_schema_version,
        ):
            return False
    sources = {(row.source_unit, row.page) for row in rows}
    if len(sources) != 1:
        return False
    for key in required:
        if not isinstance(key, DetailKey):
            return False
        if key.kind == PLOTTED_VALUE_KIND and not _completes_plotted_target(
            key.target_key
        ):
            # EXIT: a plotted-value request without a complete chart/series/
            # category target is an ambiguous target, and ambiguous targets
            # are never covered — not even by an identically shaped entry.
            return False
        if not any(_covers_detail(row, key, requested_scope) for row in rows):
            return False
    return True


def observation_covers(
    observation: ObservationEnvelope,
    required_details: Sequence[DetailKey],
    requested_scope: VisionCrop | None,
    *,
    attachment_version: int,
    page_image_sha256: str,
    coverage_schema_version: str,
) -> bool:
    """The reuse verdict for one candidate observation.

    ``requested_scope is None`` means the question wants whole-page
    coverage. No semantic or fuzzy matching happens anywhere on this path:
    every required key must exactly equal a covered key whose observed
    scope contains the request, under an identity that matches source,
    version, hash, and schema.
    """
    return observations_jointly_cover(
        (observation,),
        required_details,
        requested_scope,
        attachment_version=attachment_version,
        page_image_sha256=page_image_sha256,
        coverage_schema_version=coverage_schema_version,
    )
