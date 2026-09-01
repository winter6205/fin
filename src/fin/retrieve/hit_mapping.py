"""Hit → locator mapping and its failure ledger (plan T17, SC22 / SC43).

A retrieval hit carries the *retrieval service's* own identifier. Only
fin can map that identifier back to a **locator**
(``{document_id, version, page}``), and only a locator may appear as
citation evidence: passing a retrieval-side id to the citation gate
would put a foreign identity in front of the user (ADR 0030).

So a hit that does not map is **dropped**. What this module adds is the
other half of SC22: the drop is *counted*, and the count is persistable,
so a stale ``locator_map`` no longer hides behind a healthy-looking
``{"chunks": []}``.

Two authorities live here:

* :func:`map_hits_to_chunks` — the wire-hit → :class:`~fin.retrieve.port.Chunk`
  mapping used by :class:`~fin.retrieve.mcp_adapter.MCPRetrieveAdapter`.
* :class:`MappingFailureLedger` — the counter. In memory by default;
  an append-only JSONL log when given a ``path``, which is what makes
  「计数落盘且 >0 时可断言」true after the producing process exits. The
  host side (``fin.host.evidence_admission``) records into the same
  ledger, so one number covers both places a hit can fail to map.

A canonical fin ``source_ref`` is parsed directly into a locator. A
retrieval-side identifier, when an integration still needs one, must be
carried under the explicitly transitional ``retrieval_id`` field and
resolved through ``locator_map``. The legacy path that accepts a
non-canonical ``source_ref`` key in an explicitly supplied
``locator_map`` remains only for existing callers during migration; it
is never emitted by fin and is not the canonical field meaning.

Retrieval-side identifiers are written to the ledger — that record is the
operator's diagnosis, and it is the only place the id survives. It never
travels to citation evidence, which is what SC22 forbids.

Failure vocabulary
------------------

The drop reasons are a closed set of stable codes
(``unmapped_source_ref`` / ``missing_source_ref`` / ``malformed_hit`` /
``non_locator_source_ref``) so a caller asserts on
:meth:`MappingFailureLedger.count_by_code` instead of on message text.
Ledger *failures* — an unwritable path, a corrupt log, a record outside
the vocabulary — are named errors in the :class:`HitMappingError` family
(same shape as T14's ``PushError``): a count that cannot be persisted is
reported, never quietly kept in memory as if it had landed.

What this module does NOT touch: the citation gate's judgement. Dropping
a hit before admission changes *what evidence exists*, not how the gate
rules on the evidence it gets — ``unknown_locator`` and the repair /
malformed-evidence contracts are unchanged.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Mapping, Sequence

from fin.corpus.extracted import Locator
from fin.corpus.push import deserialize_source_ref
from fin.retrieve._ledger.jsonl import append_jsonl_object, replay_jsonl_objects
from fin.retrieve.port import Chunk


# ----- vocabulary -----

# The retrieval-side id was well-formed but is absent from the locator
# map (the classic ADR 0030 mapper-drop: stale or partial map).
MAPPING_FAILURE_UNMAPPED_SOURCE_REF: Final[str] = "unmapped_source_ref"

# The hit carries no usable retrieval-side id at all, so there is
# nothing to look up.
MAPPING_FAILURE_MISSING_SOURCE_REF: Final[str] = "missing_source_ref"

# The hit itself is off-shape (not a mapping, no text, non-string text)
# — distinct from "the id did not resolve".
MAPPING_FAILURE_MALFORMED_HIT: Final[str] = "malformed_hit"

# Something that is not a fin ``Locator`` reached the locator position:
# a misconfigured map value, or a chunk arriving at the host with the
# retrieval-side id still in ``source_ref``.
MAPPING_FAILURE_NON_LOCATOR_SOURCE_REF: Final[str] = "non_locator_source_ref"

MAPPING_FAILURE_CODES: Final[frozenset[str]] = frozenset(
    {
        MAPPING_FAILURE_UNMAPPED_SOURCE_REF,
        MAPPING_FAILURE_MISSING_SOURCE_REF,
        MAPPING_FAILURE_MALFORMED_HIT,
        MAPPING_FAILURE_NON_LOCATOR_SOURCE_REF,
    }
)

# Where the failure was observed: mapping the transport's hits, or
# admitting already-mapped chunks as citation evidence.
MAPPING_STAGE_RETRIEVE: Final[str] = "retrieve"
MAPPING_STAGE_EVIDENCE_ADMISSION: Final[str] = "evidence_admission"

MAPPING_STAGES: Final[frozenset[str]] = frozenset(
    {MAPPING_STAGE_RETRIEVE, MAPPING_STAGE_EVIDENCE_ADMISSION}
)

# Operator-set path for the persisted count. Unset (or blank) means the
# ledger is in-memory only, which is the default test suite's shape.
MAPPING_FAILURE_LOG_ENV: Final[str] = "FIN_MAPPING_FAILURE_LOG"


# ----- named failures -----


class HitMappingError(RuntimeError):
    """Base class for the mapping ledger's named failures.

    Each subclass carries a distinct stable :attr:`code`, so a caller
    branches on ``exc.code`` rather than on message text (the T14
    ``PushError`` shape).
    """

    code: str = "hit_mapping_error"


class MappingFailureLedgerUnwritableError(HitMappingError):
    """The ledger's path could not be appended to.

    Raised instead of counting in memory and reporting success: a count
    the operator cannot read later is not a persisted count.
    """

    code = "mapping_failure_ledger_unwritable"


class MappingFailureLedgerCorruptError(HitMappingError):
    """A persisted ledger line could not be replayed.

    Skipping the line would silently lower the count — the exact
    silence SC22 exists to prevent — so replay fails by name.
    """

    code = "mapping_failure_ledger_corrupt"


class MappingFailureRecordInvalidError(HitMappingError):
    """A record was offered with a code or stage outside the vocabulary.

    The vocabulary is closed so ``count_by_code`` stays assertable; an
    unknown code is a caller bug, not a new failure class.
    """

    code = "mapping_failure_record_invalid"


# ----- the ledger -----


@dataclass(frozen=True)
class MappingFailure:
    """One hit that could not be mapped back to a fin locator.

    ``retrieval_ref`` is the retrieval service's own identifier for the
    hit (or a repr of whatever occupied that position). It lives here
    and nowhere else — it is a diagnosis, never citation evidence.
    """

    code: str
    stage: str
    retrieval_ref: str
    message: str


class MappingFailureLedger:
    """Counts mapping failures; persists them when given a ``path``.

    With a ``path`` the ledger is an append-only JSONL log: every
    recorded failure is one line, and constructing a ledger over an
    existing path replays it, so ``count() > 0`` is assertable in a
    later process. Without one it counts in memory only.
    """

    def __init__(self, *, path: Path | None = None) -> None:
        self._path = path
        self._lock = threading.RLock()
        self._failures: list[MappingFailure] = []
        if path is not None and path.exists():
            self._failures.extend(self._replay(path))

    @property
    def path(self) -> Path | None:
        return self._path

    # ----- reading -----

    def count(self) -> int:
        """Total recorded mapping failures (the SC22 counter)."""
        with self._lock:
            return len(self._failures)

    def count_by_code(self) -> dict[str, int]:
        """Per-code totals, omitting codes with no occurrences."""
        with self._lock:
            counts: dict[str, int] = {}
            for failure in self._failures:
                counts[failure.code] = counts.get(failure.code, 0) + 1
            return counts

    def failures(self) -> tuple[MappingFailure, ...]:
        """Every recorded failure, in record order."""
        with self._lock:
            return tuple(self._failures)

    # ----- recording -----

    def record(
        self,
        *,
        code: str,
        stage: str,
        retrieval_ref: str,
        message: str,
    ) -> MappingFailure:
        """Record one mapping failure and return it.

        Persistence happens before the in-memory count moves, so a
        ledger that raises :class:`MappingFailureLedgerUnwritableError`
        never leaves a count the caller believes was written.
        """
        with self._lock:
            if code not in MAPPING_FAILURE_CODES:
                raise MappingFailureRecordInvalidError(
                    f"mapping failure code {code!r} is outside the vocabulary "
                    f"{sorted(MAPPING_FAILURE_CODES)}"
                )
            if stage not in MAPPING_STAGES:
                raise MappingFailureRecordInvalidError(
                    f"mapping failure stage {stage!r} is outside the vocabulary "
                    f"{sorted(MAPPING_STAGES)}"
                )
            failure = MappingFailure(
                code=code,
                stage=stage,
                retrieval_ref=retrieval_ref,
                message=message,
            )
            self._append(failure)
            self._failures.append(failure)
            return failure

    # ----- persistence -----

    def _append(self, failure: MappingFailure) -> None:
        with self._lock:
            append_jsonl_object(
                self._path,
                {
                    "code": failure.code,
                    "stage": failure.stage,
                    "retrieval_ref": failure.retrieval_ref,
                    "message": failure.message,
                },
                unwritable=MappingFailureLedgerUnwritableError,
                message=(
                    f"cannot append the mapping failure count to {self._path}"
                ),
            )

    def _replay(self, path: Path) -> list[MappingFailure]:
        return replay_jsonl_objects(
            path,
            lambda event, number: self._record_of_event(event, path, number),
            on_read_oserror=lambda exc: MappingFailureLedgerCorruptError(
                f"cannot read the mapping failure ledger at {path}: "
                f"{type(exc).__name__}: {exc}"
            ),
            on_unicode=lambda exc: MappingFailureLedgerCorruptError(
                f"cannot read the mapping failure ledger at {path}: "
                f"{type(exc).__name__}: {exc}"
            ),
            on_json=lambda exc, number: MappingFailureLedgerCorruptError(
                f"mapping failure ledger {path} line {number} is not a "
                f"replayable record: {type(exc).__name__}: {exc}"
            ),
        )

    @staticmethod
    def _record_of_event(
        event: object, path: Path, number: int
    ) -> MappingFailure:
        try:
            if not isinstance(event, dict):
                raise TypeError(
                    f"event is {type(event).__name__}, expected an object"
                )
            return MappingFailure(
                code=str(event["code"]),
                stage=str(event["stage"]),
                retrieval_ref=str(event["retrieval_ref"]),
                message=str(event["message"]),
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise MappingFailureLedgerCorruptError(
                f"mapping failure ledger {path} line {number} is not a "
                f"replayable record: {type(exc).__name__}: {exc}"
            ) from exc


def ledger_from_env(
    env: Mapping[str, str] | None = None,
) -> MappingFailureLedger:
    """Build the ledger the operator configured.

    ``FIN_MAPPING_FAILURE_LOG`` set to a path makes the count persist;
    unset or blank keeps it in memory. Defaults to ``os.environ`` so the
    product boot picks the operator's choice up without extra wiring.
    """
    source = os.environ if env is None else env
    configured = (source.get(MAPPING_FAILURE_LOG_ENV) or "").strip()
    if not configured:
        return MappingFailureLedger()
    return MappingFailureLedger(path=Path(configured))


# ----- the mapping seam -----


def map_hits_to_chunks(
    hits: Sequence[Any],
    *,
    locator_map: Mapping[str, Locator],
    limit: int,
    ledger: MappingFailureLedger,
) -> tuple[Chunk, ...]:
    """Map transport ``hits`` to chunks, dropping and counting failures.

    ``limit`` caps the *mapped* chunks, so a page of unmappable hits
    does not consume the caller's budget. Every dropped hit is recorded
    on ``ledger`` with the code that says why, which is what makes the
    drop auditable instead of silent.
    """
    chunks: list[Chunk] = []
    for hit in hits:
        if len(chunks) >= limit:
            break
        locator = _locator_of_hit(hit, locator_map=locator_map, ledger=ledger)
        if locator is None:
            continue
        chunks.append(
            Chunk(
                text=hit["text"],
                source_ref=locator,
                score=_score_of_hit(hit),
            )
        )
    return tuple(chunks)


def _locator_of_hit(
    hit: Any,
    *,
    locator_map: Mapping[str, Locator],
    ledger: MappingFailureLedger,
) -> Locator | None:
    """The fin locator for one hit, or ``None`` after recording why not.

    ``None`` here means "this hit is dropped, and the ledger says so" —
    the count is the caller's signal, so the drop is not a silent
    failure returned as an empty value.
    """
    if not isinstance(hit, Mapping):
        ledger.record(
            code=MAPPING_FAILURE_MALFORMED_HIT,
            stage=MAPPING_STAGE_RETRIEVE,
            retrieval_ref=_retrieval_ref_of(hit),
            message=f"hit is {type(hit).__name__}, expected a mapping",
        )
        return None
    text = hit.get("text")
    if not isinstance(text, str):
        ledger.record(
            code=MAPPING_FAILURE_MALFORMED_HIT,
            stage=MAPPING_STAGE_RETRIEVE,
            retrieval_ref=_retrieval_ref_of(hit.get("source_ref")),
            message=f"hit text is {type(text).__name__}, expected str",
        )
        return None
    source_ref = hit.get("source_ref")
    retrieval_id = hit.get("retrieval_id")
    lookup_ref: Any = source_ref
    if not isinstance(source_ref, str):
        if isinstance(retrieval_id, str):
            locator = locator_map.get(retrieval_id)
            lookup_ref = retrieval_id
            if locator is None:
                ledger.record(
                    code=MAPPING_FAILURE_UNMAPPED_SOURCE_REF,
                    stage=MAPPING_STAGE_RETRIEVE,
                    retrieval_ref=retrieval_id,
                    message=(
                        "transitional retrieval_id is absent from the "
                        "locator map"
                    ),
                )
                return None
        else:
            ledger.record(
                code=MAPPING_FAILURE_MISSING_SOURCE_REF,
                stage=MAPPING_STAGE_RETRIEVE,
                retrieval_ref=_retrieval_ref_of(source_ref),
                message=(
                    f"hit source_ref is {type(source_ref).__name__}, expected "
                    "a canonical fin locator serialization"
                ),
            )
            return None
    else:
        try:
            canonical_locator = deserialize_source_ref(source_ref)
        except ValueError:
            if isinstance(retrieval_id, str):
                locator = locator_map.get(retrieval_id)
                lookup_ref = retrieval_id
                lookup_message = (
                    "transitional retrieval_id is absent from the locator map"
                )
            elif source_ref in locator_map:
                # Transitional compatibility for callers that explicitly
                # supplied the pre-B4 locator map and still receive its old
                # source_ref key. New integrations must use retrieval_id.
                locator = locator_map.get(source_ref)
                lookup_ref = source_ref
                lookup_message = (
                    "transitional source_ref key is absent from the locator "
                    "map"
                )
            else:
                locator = None
                lookup_ref = source_ref
                lookup_message = "source_ref is neither canonical nor mapped"
            if locator is None:
                ledger.record(
                    code=MAPPING_FAILURE_UNMAPPED_SOURCE_REF,
                    stage=MAPPING_STAGE_RETRIEVE,
                    retrieval_ref=lookup_ref,
                    message=lookup_message,
                )
                return None
        else:
            locator = Locator(
                document_id=canonical_locator.document_id,
                version=canonical_locator.version,
                page=canonical_locator.unit,
            )
    if not isinstance(locator, Locator):
        ledger.record(
            code=MAPPING_FAILURE_NON_LOCATOR_SOURCE_REF,
            stage=MAPPING_STAGE_RETRIEVE,
            retrieval_ref=_retrieval_ref_of(lookup_ref),
            message=(
                f"locator map holds {type(locator).__name__} for this id, "
                "expected a fin Locator"
            ),
        )
        return None
    return locator


def _score_of_hit(hit: Mapping[str, Any]) -> float:
    """The hit's score, defaulting to 0.0 for an unusable value.

    A score is a ranking hint, not identity: an unreadable one leaves
    the hit citable (its locator resolved) at the bottom of the order,
    so it is not a mapping failure.
    """
    try:
        return float(hit.get("score", 0.0))
    except (TypeError, ValueError):
        return 0.0


def _retrieval_ref_of(value: Any) -> str:
    """The retrieval-side identifier as a ledger-safe string."""
    return value if isinstance(value, str) else repr(value)


__all__ = [
    "MAPPING_FAILURE_CODES",
    "MAPPING_FAILURE_LOG_ENV",
    "MAPPING_FAILURE_MALFORMED_HIT",
    "MAPPING_FAILURE_MISSING_SOURCE_REF",
    "MAPPING_FAILURE_NON_LOCATOR_SOURCE_REF",
    "MAPPING_FAILURE_UNMAPPED_SOURCE_REF",
    "MAPPING_STAGES",
    "MAPPING_STAGE_EVIDENCE_ADMISSION",
    "MAPPING_STAGE_RETRIEVE",
    "HitMappingError",
    "MappingFailure",
    "MappingFailureLedger",
    "MappingFailureLedgerCorruptError",
    "MappingFailureLedgerUnwritableError",
    "MappingFailureRecordInvalidError",
    "ledger_from_env",
    "map_hits_to_chunks",
]
