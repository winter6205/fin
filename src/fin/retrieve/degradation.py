"""Reading and recording the `degraded` signal (plan T20, SC39, §B3).

§B3 is the only authority for this signal, and it fixes two things this
module refuses to relax:

* the field names are **verbatim** ``degraded`` and ``degraded_reasons``
  — no translation, no equivalent spelling, on either side of the wire;
* a failure must leave an assertable trace. So an envelope this module
  cannot read does **not** resolve to "not degraded": it fails by name,
  because "unreadable flag treated as healthy" is precisely the silent
  shape §B3 forbids.

    T16 / T18 already declared the same two field names on the *push* and
*invalidation* error families (a capability that was attempted and
failed sets them; invalid caller input does not). This module is the
consumption side: reading the flag off a **retrieval response** and
recording every reason it carries.

What lives here
---------------

* :func:`read_degradation_signal` — the strict envelope reader used at
  the seams (the MCP adapter, the host tools).
* :func:`degraded_reasons_for_display` — the best-effort reader used by
  the rendering path, which must never raise mid-render.
* :class:`DegradationLedger` — the record. One entry per **item** of
  ``degraded_reasons``, because SC39 asks for 「每一项」 rather than a
  single "something degraded" tick. In memory by default; an
  append-only JSONL log when given a ``path``, so the count outlives the
  producing process.
* :func:`unsignalled_degradation_count` — SC39's assertion in one
  number: reasons recorded on the retrieve side that never reached a
  user-visible notice. The acceptance value is 0.

What does NOT live here: any judgement about citation evidence. A
degraded response is 可用但必须可见 (§B2 第 4 条) — the flag changes what
the operator is told, never what the citation gate accepts.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Mapping, Sequence


# ----- the two field names, verbatim -----

DEGRADED_FIELD: Final[str] = "degraded"
DEGRADED_REASONS_FIELD: Final[str] = "degraded_reasons"

# Spellings §B3 rules out. Reading one of these as if it were the real
# field would legitimise a second name for the signal, and ignoring it
# would let a mis-spelled envelope pass as healthy; both are refused.
FORBIDDEN_DEGRADED_ALIASES: Final[frozenset[str]] = frozenset(
    {
        "isDegraded",
        "is_degraded",
        "degradedReasons",
        "degraded_reason",
        "degradation_reasons",
        "degrade",
        "downgraded",
        "fallback_reasons",
        "降级",
        "降级原因",
    }
)

# Where a reason was recorded. The retrieve stage is「记录原因」; the
# notice stage is「向用户侧暴露可见信号」. SC39 asks for both, which is
# why they are two stages of one ledger rather than two counters.
DEGRADATION_STAGE_RETRIEVE: Final[str] = "retrieve"
DEGRADATION_STAGE_USER_NOTICE: Final[str] = "user_notice"

DEGRADATION_STAGES: Final[frozenset[str]] = frozenset(
    {DEGRADATION_STAGE_RETRIEVE, DEGRADATION_STAGE_USER_NOTICE}
)

# Operator-set path for the persisted record. Unset (or blank) keeps the
# ledger in memory, which is the default test suite's shape.
DEGRADATION_LOG_ENV: Final[str] = "FIN_DEGRADATION_LOG"


# ----- named failures -----


class DegradationError(RuntimeError):
    """Base class for this module's named failures.

    Each subclass carries a distinct stable :attr:`code`, so a caller
    branches on ``exc.code`` rather than on message text (the T14
    ``PushError`` shape).
    """

    code: str = "degradation_error"


class DegradedEnvelopeInvalidError(DegradationError):
    """The response's degradation fields cannot be read as §B3 defines.

    Raised instead of falling back to "not degraded": a flag that cannot
    be trusted is a failure with a trace, not a healthy response.
    """

    code = "degraded_envelope_invalid"


class DegradedFieldAliasError(DegradationError):
    """The response spelled the signal with a name §B3 forbids."""

    code = "degraded_field_alias"


class DegradationLedgerUnwritableError(DegradationError):
    """The ledger's path could not be appended to.

    Raised instead of counting in memory and reporting success: a reason
    the operator cannot read later is not a recorded reason.
    """

    code = "degradation_ledger_unwritable"


class DegradationLedgerCorruptError(DegradationError):
    """A persisted ledger line could not be replayed.

    Skipping it would silently lower the count SC39 asserts on.
    """

    code = "degradation_ledger_corrupt"


class DegradationRecordInvalidError(DegradationError):
    """A record was offered with a stage outside the vocabulary."""

    code = "degradation_record_invalid"


# ----- the signal -----


@dataclass(frozen=True)
class DegradationSignal:
    """One response's degradation state, read verbatim off the envelope.

    Defaults are the undegraded shape, so a caller that never saw the
    fields holds the same value as one that saw ``degraded: false``.
    """

    degraded: bool = False
    degraded_reasons: tuple[str, ...] = ()

    def as_envelope_fields(self) -> dict[str, Any]:
        """The two verbatim fields, for re-emitting on fin's own envelope.

        Empty when undegraded, because §B3 makes the absent flag mean
        「未降级」— re-emitting ``degraded: false`` everywhere would change
        the shape of every existing healthy response for no gain.
        """
        if not self.degraded:
            return {}
        return {
            DEGRADED_FIELD: True,
            DEGRADED_REASONS_FIELD: list(self.degraded_reasons),
        }


NOT_DEGRADED: Final[DegradationSignal] = DegradationSignal()


def read_degradation_signal(envelope: Any) -> DegradationSignal:
    """Read ``degraded`` / ``degraded_reasons`` off ``envelope``, strictly.

    A non-mapping ``envelope`` holds no fields to read and yields the
    undegraded signal — the caller's own shape validation owns that
    failure (the MCP adapter, for one, already refuses a non-dict
    response by name).

    Everything else is validated: an aliased field name, a non-boolean
    flag, a true flag without reasons, a reason that is not a non-blank
    string, and reasons without the flag all raise.
    """
    if not isinstance(envelope, Mapping):
        return NOT_DEGRADED

    for alias in FORBIDDEN_DEGRADED_ALIASES:
        if alias in envelope:
            raise DegradedFieldAliasError(
                f"response carries {alias!r}; §B3 fixes the field names "
                f"verbatim as {DEGRADED_FIELD!r} and "
                f"{DEGRADED_REASONS_FIELD!r}"
            )

    has_flag = DEGRADED_FIELD in envelope
    raw_reasons = envelope.get(DEGRADED_REASONS_FIELD)

    if not has_flag:
        if raw_reasons:
            raise DegradedEnvelopeInvalidError(
                f"response carries {DEGRADED_REASONS_FIELD!r} without "
                f"{DEGRADED_FIELD!r}: the flag is what says whether this "
                "result is complete"
            )
        return NOT_DEGRADED

    flag = envelope[DEGRADED_FIELD]
    if not isinstance(flag, bool):
        raise DegradedEnvelopeInvalidError(
            f"{DEGRADED_FIELD!r} is {type(flag).__name__}, expected bool"
        )

    if not flag:
        if raw_reasons:
            raise DegradedEnvelopeInvalidError(
                f"{DEGRADED_FIELD!r} is false but "
                f"{DEGRADED_REASONS_FIELD!r} is non-empty: §B3 rule 3 / 4 "
                "make an unset flag mean there is nothing to report"
            )
        return NOT_DEGRADED

    return DegradationSignal(
        degraded=True, degraded_reasons=_read_reasons(raw_reasons)
    )


def _read_reasons(raw_reasons: Any) -> tuple[str, ...]:
    """The reason list behind a true flag, or a named failure."""
    if isinstance(raw_reasons, str) or not isinstance(raw_reasons, Sequence):
        raise DegradedEnvelopeInvalidError(
            f"{DEGRADED_REASONS_FIELD!r} is "
            f"{type(raw_reasons).__name__}, expected a list of reason "
            "strings"
        )
    reasons = tuple(raw_reasons)
    if not reasons:
        raise DegradedEnvelopeInvalidError(
            f"{DEGRADED_FIELD!r} is true with no "
            f"{DEGRADED_REASONS_FIELD!r}: a degradation fin cannot name "
            "cannot be recorded or shown"
        )
    for reason in reasons:
        if not isinstance(reason, str) or not reason.strip():
            raise DegradedEnvelopeInvalidError(
                f"{DEGRADED_REASONS_FIELD!r} holds {reason!r}, expected a "
                "non-blank reason string naming the capability item"
            )
    return tuple(str(reason) for reason in reasons)


def degraded_reasons_for_display(envelope: Any) -> tuple[str, ...]:
    """The reasons to render, best effort — never raises.

    The rendering path runs after a seam already validated the envelope
    with :func:`read_degradation_signal`; re-validating there would let a
    late shape problem take down the operator's view of the run instead
    of the run itself. An unreadable envelope renders no reasons.
    """
    if not isinstance(envelope, Mapping):
        return ()
    if envelope.get(DEGRADED_FIELD) is not True:
        return ()
    raw_reasons = envelope.get(DEGRADED_REASONS_FIELD)
    if isinstance(raw_reasons, str) or not isinstance(raw_reasons, Sequence):
        return ()
    return tuple(
        reason for reason in raw_reasons if isinstance(reason, str) and reason
    )


# ----- the ledger -----


@dataclass(frozen=True)
class DegradationRecord:
    """One recorded item of one response's ``degraded_reasons``."""

    reason: str
    stage: str
    detail: str


class DegradationLedger:
    """Records degradation reasons; persists them when given a ``path``.

    With a ``path`` the ledger is an append-only JSONL log, and building
    a ledger over an existing path replays it, so ``count() > 0`` stays
    assertable in a later process. Without one it records in memory.
    """

    def __init__(self, *, path: Path | None = None) -> None:
        self._path = path
        self._lock = threading.RLock()
        self._records: list[DegradationRecord] = []
        if path is not None and path.exists():
            self._replay(path)

    @property
    def path(self) -> Path | None:
        return self._path

    # ----- reading -----

    def count(self) -> int:
        """Total recorded reason items across every stage."""
        with self._lock:
            return len(self._records)

    def records(self) -> tuple[DegradationRecord, ...]:
        with self._lock:
            return tuple(self._records)

    def reasons_at(self, stage: str) -> tuple[str, ...]:
        """Every reason recorded at ``stage``, in record order."""
        with self._lock:
            return tuple(
                record.reason
                for record in self._records
                if record.stage == stage
            )

    def count_by_reason(self) -> dict[str, int]:
        """Per-reason totals at the retrieve stage.

        The notice stage mirrors the retrieve stage by construction, so
        counting both would double every number the operator reads.
        """
        with self._lock:
            counts: dict[str, int] = {}
            for record in self._records:
                if record.stage == DEGRADATION_STAGE_RETRIEVE:
                    counts[record.reason] = counts.get(record.reason, 0) + 1
            return counts

    # ----- recording -----

    def record_signal(
        self,
        signal: DegradationSignal,
        *,
        stage: str,
        detail: str = "",
    ) -> tuple[DegradationRecord, ...]:
        """Record **every** item of ``signal.degraded_reasons``.

        An undegraded signal records nothing and returns an empty tuple:
        §B3 rule 3 / 4 mean there is nothing to report, which is not a
        failure. Persistence happens before the in-memory count moves, so
        a ledger that raises never leaves a count the caller believes was
        written.
        """
        with self._lock:
            if stage not in DEGRADATION_STAGES:
                raise DegradationRecordInvalidError(
                    f"degradation stage {stage!r} is outside the vocabulary "
                    f"{sorted(DEGRADATION_STAGES)}"
                )
            if not signal.degraded:
                return ()
            recorded: list[DegradationRecord] = []
            for reason in signal.degraded_reasons:
                record = DegradationRecord(
                    reason=reason, stage=stage, detail=detail
                )
                self._append(record)
                self._records.append(record)
                recorded.append(record)
            return tuple(recorded)

    # ----- persistence -----

    def _append(self, record: DegradationRecord) -> None:
        with self._lock:
            if self._path is None:
                return
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with self._path.open("a", encoding="utf-8") as handle:
                    handle.write(
                        json.dumps(
                            {
                                "reason": record.reason,
                                "stage": record.stage,
                                "detail": record.detail,
                            },
                            sort_keys=True,
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
            except OSError as exc:
                raise DegradationLedgerUnwritableError(
                    f"cannot append the degradation record to {self._path}: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc

    def _replay(self, path: Path) -> None:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError) as exc:
            raise DegradationLedgerCorruptError(
                f"cannot read the degradation ledger at {path}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        for number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            self._records.append(self._parse_line(line, path, number))

    @staticmethod
    def _parse_line(line: str, path: Path, number: int) -> DegradationRecord:
        try:
            event = json.loads(line)
            return DegradationRecord(
                reason=str(event["reason"]),
                stage=str(event["stage"]),
                detail=str(event.get("detail", "")),
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise DegradationLedgerCorruptError(
                f"degradation ledger {path} line {number} is not a "
                f"replayable record: {type(exc).__name__}: {exc}"
            ) from exc


def degradation_ledger_from_env(
    env: Mapping[str, str] | None = None,
) -> DegradationLedger:
    """Build the ledger the operator configured.

    ``FIN_DEGRADATION_LOG`` set to a path makes the record persist;
    unset or blank keeps it in memory. Defaults to ``os.environ`` so the
    product boot picks the operator's choice up without extra wiring.
    """
    source = os.environ if env is None else env
    configured = (source.get(DEGRADATION_LOG_ENV) or "").strip()
    if not configured:
        return DegradationLedger()
    return DegradationLedger(path=Path(configured))


def unsignalled_degradation_count(ledger: DegradationLedger) -> int:
    """SC39's number: reasons recorded but never shown to the user.

    Counted per reason as a multiset difference, so a response whose
    notice named only half its reasons reports the missing half rather
    than passing because "a notice happened".
    """
    shown: dict[str, int] = {}
    for reason in ledger.reasons_at(DEGRADATION_STAGE_USER_NOTICE):
        shown[reason] = shown.get(reason, 0) + 1
    missing = 0
    for reason in ledger.reasons_at(DEGRADATION_STAGE_RETRIEVE):
        if shown.get(reason, 0) > 0:
            shown[reason] -= 1
        else:
            missing += 1
    return missing


__all__ = [
    "DEGRADATION_LOG_ENV",
    "DEGRADATION_STAGES",
    "DEGRADATION_STAGE_RETRIEVE",
    "DEGRADATION_STAGE_USER_NOTICE",
    "DEGRADED_FIELD",
    "DEGRADED_REASONS_FIELD",
    "FORBIDDEN_DEGRADED_ALIASES",
    "NOT_DEGRADED",
    "DegradationError",
    "DegradationLedger",
    "DegradationLedgerCorruptError",
    "DegradationLedgerUnwritableError",
    "DegradationRecord",
    "DegradationRecordInvalidError",
    "DegradationSignal",
    "DegradedEnvelopeInvalidError",
    "DegradedFieldAliasError",
    "degradation_ledger_from_env",
    "degraded_reasons_for_display",
    "read_degradation_signal",
    "unsignalled_degradation_count",
]
