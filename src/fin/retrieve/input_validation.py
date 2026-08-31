"""The invalid-input / legal-empty dichotomy (plan T21, SC42 / SC43, §B2 第 5 条).

§B2 第 5 条 splits every "nothing came back" answer in two, and calls
conflating them a contract violation:

* **invalid input** — the caller handed over something unusable (an
  empty document, an empty query, an empty block batch, mutually
  exclusive filter conditions). The answer is a named typed error, it is
  counted as an input-validation failure, and it does **not** set
  ``degraded``: 入参非法不是能力失败.
* **legal empty result** — the input was valid and the capability ran
  but matched nothing (a query with no hits, an unknown filter value
  that is syntactically fine). The answer is a success carrying an empty
  result list; nothing raises and ``degraded`` stays unset (§B3 rule 3).

This module is the single authority for that split:

* the named errors for the retrieve side (:class:`EmptyQueryError`,
  :class:`ConflictingFiltersError`) — the push side's two live on the
  ``PushError`` family (T16) and inherit :class:`InputValidationError`
  from here, so one classification covers both;
* :func:`classify_input_failure` — which of the four classes a failure
  belongs to, over a closed code registry, so "some other exception"
  can never be quietly counted as invalid input;
* :data:`INPUT_VALIDATION_CLASSES` and the ledger — the「输入校验失败」
  count SC42 asserts on;
* :func:`observe_input_outcome` — one seam that answers "which side of
  the dichotomy did this call land on", which is what makes 不被混同
  assertable in one comparison rather than by eyeballing two shapes.

The ``degraded`` question is **not** re-answered here: the flag's
authority is :mod:`fin.retrieve.degradation` (T20), and
:func:`degradation_signal_of_failure` routes every failure through it
rather than reading the two field names a second way.

Deliberately out of scope: ``fin.corpus.catalog.list_filings``. The
catalog is the ADR 0009 read tool, not the retrieval envelope §B2
governs; its documented contract keeps answering unknown / conflicting /
empty filters with ``{"filings": []}``. Bringing it under this dichotomy
would need the classification to live below ``fin.corpus``, which is
where the layering says it must not be.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Final, Mapping, NoReturn, Sequence

from fin.corpus.errors import EmptySourceParseError, NoUnitsParseError
from fin.retrieve.degradation import (
    DegradationSignal,
    read_degradation_signal,
)


# ----- the four invalid-input classes -----


INPUT_CLASS_EMPTY_DOCUMENT: Final[str] = "empty_document"
INPUT_CLASS_EMPTY_QUERY: Final[str] = "empty_query"
INPUT_CLASS_EMPTY_BATCH: Final[str] = "empty_batch"
INPUT_CLASS_CONFLICTING_FILTERS: Final[str] = "conflicting_filters"

INPUT_VALIDATION_CLASSES: Final[tuple[str, ...]] = (
    INPUT_CLASS_EMPTY_DOCUMENT,
    INPUT_CLASS_EMPTY_QUERY,
    INPUT_CLASS_EMPTY_BATCH,
    INPUT_CLASS_CONFLICTING_FILTERS,
)

# Operator-set path for the persisted count. Unset (or blank) keeps the
# ledger in memory, which is the default test suite's shape.
INPUT_VALIDATION_LOG_ENV: Final[str] = "FIN_INPUT_VALIDATION_LOG"


# ----- named failures -----


class InputValidationError(RuntimeError):
    """Base class for「入参非法」failures.

    Every subclass carries a distinct stable :attr:`code` (the T14
    ``PushError`` shape) and names its :attr:`input_class` — one of
    :data:`INPUT_VALIDATION_CLASSES`.

    :attr:`degraded` / :attr:`degraded_reasons` are declared (field names
    verbatim per §B3) and are **not** overridable to a set flag by any
    subclass of this family: invalid input is not a capability failure,
    and :func:`record_input_failure` refuses to count a failure that
    claims otherwise.
    """

    code: str = "input_validation_error"
    input_class: str = ""
    degraded: bool = False
    degraded_reasons: tuple[str, ...] = ()


class EmptyQueryError(InputValidationError):
    """The query is empty or whitespace-only (§B2 第 5 条).

    Raised instead of answering ``{"chunks": []}``: an empty result would
    tell the caller "the corpus has nothing to say about this", when the
    truth is that nothing was asked.
    """

    code = "empty_query"
    input_class = INPUT_CLASS_EMPTY_QUERY


class ConflictingFiltersError(InputValidationError):
    """Two filter conditions that no single document can satisfy (SC43).

    Distinct from an **unknown** filter value, which is syntactically
    fine and simply matches nothing: that stays a legal empty result. The
    conflict is only named where fin can prove it from bindings it holds
    (see :func:`detect_filter_conflict`) — fin does not invent a
    contradiction it cannot demonstrate.
    """

    code = "conflicting_filters"
    input_class = INPUT_CLASS_CONFLICTING_FILTERS


class UnclassifiedInputFailureError(RuntimeError):
    """A failure was offered to the input-validation count without
    belonging to any of the four classes.

    Raised rather than counted under a catch-all bucket: a count that
    accepts anything stops being evidence for SC42.
    """

    code = "unclassified_input_failure"


class InvalidInputMarkedDegradedError(RuntimeError):
    """An invalid-input failure arrived with ``degraded`` set.

    The one rule 不可变通 in this ticket, enforced where the count is
    taken rather than only in a test: a caller error must never be
    reported as a capability degradation.
    """

    code = "invalid_input_marked_degraded"


class InputValidationLedgerUnwritableError(RuntimeError):
    """The ledger's path could not be appended to.

    Raised instead of counting in memory and reporting success: a
    validation failure the operator cannot read later is not counted.
    """

    code = "input_validation_ledger_unwritable"


class InputValidationLedgerCorruptError(RuntimeError):
    """A persisted ledger line could not be replayed.

    Skipping it would silently lower the count SC42 asserts on.
    """

    code = "input_validation_ledger_corrupt"


# ----- the closed code registry -----


# Every code that counts as an input-validation failure, mapped to its
# class. The two parse codes are read off the corpus error classes
# rather than re-typed, so a rename there cannot silently drop the
# document class out of the count. The push codes are strings because
# ``fin.retrieve.push_port`` imports *this* module (a test pins them
# against ``PushEmptyBatchError.code`` / ``PushEmptyDocumentError.code``
# so the pair cannot drift apart unnoticed).
_INPUT_CLASS_BY_CODE: Final[dict[str, str]] = {
    EmptyQueryError.code: INPUT_CLASS_EMPTY_QUERY,
    ConflictingFiltersError.code: INPUT_CLASS_CONFLICTING_FILTERS,
    EmptySourceParseError.code: INPUT_CLASS_EMPTY_DOCUMENT,
    NoUnitsParseError.code: INPUT_CLASS_EMPTY_DOCUMENT,
    "push_empty_document": INPUT_CLASS_EMPTY_DOCUMENT,
    "push_empty_batch": INPUT_CLASS_EMPTY_BATCH,
}


def input_validation_codes() -> tuple[str, ...]:
    """Every code the count accepts, sorted."""
    return tuple(sorted(_INPUT_CLASS_BY_CODE))


def codes_for_input_class(input_class: str) -> tuple[str, ...]:
    """The codes belonging to one of the four classes, sorted."""
    return tuple(
        sorted(
            code
            for code, klass in _INPUT_CLASS_BY_CODE.items()
            if klass == input_class
        )
    )


def classify_input_failure_code(code: str) -> str | None:
    """The input class ``code`` belongs to, or ``None``."""
    return _INPUT_CLASS_BY_CODE.get(code)


def classify_input_failure(exc: BaseException) -> str | None:
    """The input class ``exc`` belongs to, or ``None`` for anything else.

    Classification goes through the code registry, so an exception type
    that grew a new subclass is classified by the code it publishes
    rather than by where it sits in a class tree.
    """
    code = getattr(exc, "code", None)
    if not isinstance(code, str):
        return None
    return classify_input_failure_code(code)


def degradation_signal_of_failure(exc: BaseException) -> DegradationSignal:
    """The §B3 signal ``exc`` declares, read through T20's authority.

    The two field names are spelled once, in
    :mod:`fin.retrieve.degradation`; this function builds that module's
    envelope shape from the failure's own attributes and hands it to
    :func:`read_degradation_signal`, so "is this failure degraded" has
    exactly one answer in the codebase — and a failure claiming a set
    flag without reasons fails there rather than passing here.
    """
    flag = bool(getattr(exc, "degraded", False))
    reasons = tuple(getattr(exc, "degraded_reasons", ()) or ())
    signal = DegradationSignal(degraded=flag, degraded_reasons=reasons)
    return read_degradation_signal(signal.as_envelope_fields())


# ----- the count -----


@dataclass(frozen=True)
class InputValidationRecord:
    """One counted input-validation failure."""

    code: str
    input_class: str
    detail: str


class InputValidationLedger:
    """Counts input-validation failures; persists them given a ``path``.

    With a ``path`` the ledger is an append-only JSONL log and building
    one over an existing path replays it, so ``count() > 0`` stays
    assertable in a later process. Without one it counts in memory —
    which is still a count: a seam built without a ledger path is quiet,
    never blind.
    """

    def __init__(self, *, path: Path | None = None) -> None:
        self._path = path
        self._records: list[InputValidationRecord] = []
        if path is not None and path.exists():
            self._replay(path)

    @property
    def path(self) -> Path | None:
        return self._path

    # ----- reading -----

    def count(self) -> int:
        """Total counted input-validation failures."""
        return len(self._records)

    def records(self) -> tuple[InputValidationRecord, ...]:
        return tuple(self._records)

    def count_by_class(self) -> dict[str, int]:
        """Per-class totals over the four classes."""
        counts: dict[str, int] = {}
        for record in self._records:
            counts[record.input_class] = counts.get(record.input_class, 0) + 1
        return counts

    def count_by_code(self) -> dict[str, int]:
        """Per-code totals — the closed vocabulary makes this assertable."""
        counts: dict[str, int] = {}
        for record in self._records:
            counts[record.code] = counts.get(record.code, 0) + 1
        return counts

    # ----- recording -----

    def record(
        self, *, code: str, input_class: str, detail: str = ""
    ) -> InputValidationRecord:
        """Append one record. Persistence happens before the in-memory
        count moves, so a ledger that raises never leaves a count the
        caller believes was written."""
        record = InputValidationRecord(
            code=code, input_class=input_class, detail=detail
        )
        self._append(record)
        self._records.append(record)
        return record

    # ----- persistence -----

    def _append(self, record: InputValidationRecord) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "code": record.code,
                            "input_class": record.input_class,
                            "detail": record.detail,
                        },
                        sort_keys=True,
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        except OSError as exc:
            raise InputValidationLedgerUnwritableError(
                f"cannot append the input-validation record to "
                f"{self._path}: {type(exc).__name__}: {exc}"
            ) from exc

    def _replay(self, path: Path) -> None:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError) as exc:
            raise InputValidationLedgerCorruptError(
                f"cannot read the input-validation ledger at {path}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        for number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            self._records.append(self._parse_line(line, path, number))

    @staticmethod
    def _parse_line(
        line: str, path: Path, number: int
    ) -> InputValidationRecord:
        try:
            event = json.loads(line)
            return InputValidationRecord(
                code=str(event["code"]),
                input_class=str(event["input_class"]),
                detail=str(event.get("detail", "")),
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise InputValidationLedgerCorruptError(
                f"input-validation ledger {path} line {number} is not a "
                f"replayable record: {type(exc).__name__}: {exc}"
            ) from exc


def input_validation_ledger_from_env(
    env: Mapping[str, str] | None = None,
) -> InputValidationLedger:
    """Build the ledger the operator configured.

    ``FIN_INPUT_VALIDATION_LOG`` set to a path makes the count persist;
    unset or blank keeps it in memory.
    """
    source = os.environ if env is None else env
    configured = (source.get(INPUT_VALIDATION_LOG_ENV) or "").strip()
    if not configured:
        return InputValidationLedger()
    return InputValidationLedger(path=Path(configured))


def record_input_failure(
    exc: BaseException, *, ledger: InputValidationLedger, detail: str = ""
) -> InputValidationRecord:
    """Count ``exc`` as an input-validation failure.

    Refuses two shapes rather than counting them: a failure outside the
    four classes (:class:`UnclassifiedInputFailureError`) and one that
    declares ``degraded`` (:class:`InvalidInputMarkedDegradedError`).
    """
    input_class = classify_input_failure(exc)
    if input_class is None:
        raise UnclassifiedInputFailureError(
            f"{type(exc).__name__} (code "
            f"{getattr(exc, 'code', None)!r}) is not one of the "
            f"input-validation classes {list(INPUT_VALIDATION_CLASSES)}"
        )
    if degradation_signal_of_failure(exc).degraded:
        raise InvalidInputMarkedDegradedError(
            f"{type(exc).__name__} is invalid input yet declares "
            "degraded; §B2 第 5 条: 入参非法不是能力失败"
        )
    return ledger.record(
        code=str(getattr(exc, "code")),
        input_class=input_class,
        detail=detail or str(exc),
    )


def raise_input_failure(
    exc: InputValidationError, *, ledger: InputValidationLedger
) -> NoReturn:
    """Count ``exc`` and raise it — the count and the typed error are one
    act, so no validation site can name a failure without leaving the
    number SC42 asserts on."""
    record_input_failure(exc, ledger=ledger)
    raise exc


# ----- the validators -----


def validate_query(query: str, *, ledger: InputValidationLedger) -> None:
    """Raise :class:`EmptyQueryError` for an empty / whitespace-only query."""
    if not query or not query.strip():
        raise_input_failure(
            EmptyQueryError(
                f"query {query!r} is empty; an empty query is invalid "
                "input, not a query the corpus has no hits for"
            ),
            ledger=ledger,
        )


# The filter fields whose values a document carries exactly one of, so
# two conditions naming different values of the same field cannot both
# hold (ADR 0031's EDGAR-shaped filters).
SINGLE_VALUED_FILTER_FIELDS: Final[tuple[str, ...]] = (
    "cik",
    "form",
    "ticker",
    "filing_date",
)


def detect_filter_conflict(
    *,
    document_id: str | None = None,
    cik: str | None = None,
    form: str | None = None,
    ticker: str | None = None,
    filing_date: str | None = None,
    manifest_docs: Mapping[str, Mapping[str, Any]] | None,
) -> str | None:
    """Describe the mutual exclusion in this filter set, or ``None``.

    A conflict is proven, never guessed. Two bindings can prove one:

    * a known ``document_id`` pins exactly one row, so a metadata filter
      naming a different value for that row's field cannot also hold;
    * a known ``ticker`` is bound to one issuer's ``cik``, so a
      ``cik`` naming another issuer cannot also hold.

    Without ``manifest_docs`` fin holds no bindings and returns ``None``:
    the filter set may well be unsatisfiable, but the side that owns the
    metadata is the side that can say so (§B2 asks both sides for the
    same dichotomy, not for fin to invent a contradiction). An *unknown*
    value proves nothing either — that is a legal empty result.
    """
    if manifest_docs is None:
        return None

    supplied = {
        field: value
        for field, value in (
            ("cik", cik),
            ("form", form),
            ("ticker", ticker),
            ("filing_date", filing_date),
        )
        if value is not None and value != ""
    }
    if not supplied:
        return None

    if document_id is not None:
        meta = manifest_docs.get(document_id)
        if meta is not None:
            for field, value in supplied.items():
                bound = meta.get(field)
                if bound is not None and bound != value:
                    return (
                        f"document_id={document_id!r} is bound to "
                        f"{field}={bound!r}, which {field}={value!r} "
                        "excludes"
                    )

    if "ticker" in supplied and "cik" in supplied:
        bound_ciks = {
            str(meta.get("cik"))
            for meta in manifest_docs.values()
            if meta.get("ticker") == supplied["ticker"]
            and meta.get("cik") is not None
        }
        if bound_ciks and supplied["cik"] not in bound_ciks:
            return (
                f"ticker={supplied['ticker']!r} is bound to cik "
                f"{sorted(bound_ciks)}, which cik={supplied['cik']!r} "
                "excludes"
            )

    return None


def validate_filters(
    *,
    document_id: str | None = None,
    cik: str | None = None,
    form: str | None = None,
    ticker: str | None = None,
    filing_date: str | None = None,
    manifest_docs: Mapping[str, Mapping[str, Any]] | None,
    ledger: InputValidationLedger,
) -> None:
    """Raise :class:`ConflictingFiltersError` when the filter set is
    provably unsatisfiable (SC43)."""
    conflict = detect_filter_conflict(
        document_id=document_id,
        cik=cik,
        form=form,
        ticker=ticker,
        filing_date=filing_date,
        manifest_docs=manifest_docs,
    )
    if conflict is not None:
        raise_input_failure(
            ConflictingFiltersError(
                f"conflicting filter conditions: {conflict}"
            ),
            ledger=ledger,
        )


# ----- the dichotomy, as one observation -----

OUTCOME_INVALID_INPUT: Final[str] = "invalid_input"
OUTCOME_LEGAL_EMPTY: Final[str] = "legal_empty"
OUTCOME_HITS: Final[str] = "hits"
OUTCOME_OTHER_FAILURE: Final[str] = "other_failure"


@dataclass(frozen=True)
class InputOutcome:
    """Which side of the §B2 第 5 条 split one call landed on."""

    kind: str
    code: str = ""
    input_class: str = ""
    degraded: bool = False
    counted: bool = False
    result: Any = None


def is_empty_result(result: Any, *, result_key: str = "chunks") -> bool:
    """True for a success envelope carrying an empty result list.

    An ``{"error": ...}`` envelope is not an empty result — that is the
    named-failure shape, and calling it "empty" is the conflation §B2
    forbids.
    """
    if not isinstance(result, Mapping) or "error" in result:
        return False
    value = result.get(result_key)
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        return False
    return len(value) == 0


def observe_input_outcome(
    call: Callable[[], Any],
    *,
    ledger: InputValidationLedger,
    result_key: str = "chunks",
) -> InputOutcome:
    """Run ``call`` once and classify its answer against the dichotomy.

    This is the seam that makes 「两类不被混同」 one comparison: the same
    function judges an invalid input and a legal empty result, so a test
    asserts the two land on different kinds rather than eyeballing two
    unrelated shapes. A recognised invalid input is counted on ``ledger``
    as it passes through; an exception outside the four classes is
    re-raised untouched — this seam names failures, it never absorbs them.
    """
    try:
        result = call()
    except Exception as exc:
        input_class = classify_input_failure(exc)
        if input_class is None:
            raise
        record_input_failure(exc, ledger=ledger)
        return InputOutcome(
            kind=OUTCOME_INVALID_INPUT,
            code=str(getattr(exc, "code")),
            input_class=input_class,
            degraded=degradation_signal_of_failure(exc).degraded,
            counted=True,
        )

    if isinstance(result, Mapping) and "error" in result:
        code = str(result["error"])
        input_class = classify_input_failure_code(code)
        signal = read_degradation_signal(result)
        if input_class is None:
            return InputOutcome(
                kind=OUTCOME_OTHER_FAILURE,
                code=code,
                degraded=signal.degraded,
                result=result,
            )
        ledger.record(
            code=code, input_class=input_class, detail="error envelope"
        )
        return InputOutcome(
            kind=OUTCOME_INVALID_INPUT,
            code=code,
            input_class=input_class,
            degraded=signal.degraded,
            counted=True,
            result=result,
        )

    signal = read_degradation_signal(result)
    empty = is_empty_result(result, result_key=result_key)
    return InputOutcome(
        kind=OUTCOME_LEGAL_EMPTY if empty else OUTCOME_HITS,
        degraded=signal.degraded,
        result=result,
    )


__all__ = [
    "INPUT_CLASS_CONFLICTING_FILTERS",
    "INPUT_CLASS_EMPTY_BATCH",
    "INPUT_CLASS_EMPTY_DOCUMENT",
    "INPUT_CLASS_EMPTY_QUERY",
    "INPUT_VALIDATION_CLASSES",
    "INPUT_VALIDATION_LOG_ENV",
    "OUTCOME_HITS",
    "OUTCOME_INVALID_INPUT",
    "OUTCOME_LEGAL_EMPTY",
    "OUTCOME_OTHER_FAILURE",
    "SINGLE_VALUED_FILTER_FIELDS",
    "ConflictingFiltersError",
    "EmptyQueryError",
    "InputOutcome",
    "InputValidationError",
    "InputValidationLedger",
    "InputValidationLedgerCorruptError",
    "InputValidationLedgerUnwritableError",
    "InputValidationRecord",
    "InvalidInputMarkedDegradedError",
    "UnclassifiedInputFailureError",
    "classify_input_failure",
    "classify_input_failure_code",
    "codes_for_input_class",
    "degradation_signal_of_failure",
    "detect_filter_conflict",
    "input_validation_codes",
    "input_validation_ledger_from_env",
    "is_empty_result",
    "observe_input_outcome",
    "raise_input_failure",
    "record_input_failure",
    "validate_filters",
    "validate_query",
]
