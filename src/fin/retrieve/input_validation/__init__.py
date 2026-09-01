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

from fin.retrieve.input_validation.classification import (
    classify_input_failure,
    classify_input_failure_code,
    codes_for_input_class,
    degradation_signal_of_failure,
    input_validation_codes,
)
from fin.retrieve.input_validation.errors import (
    INPUT_CLASS_CONFLICTING_FILTERS,
    INPUT_CLASS_EMPTY_BATCH,
    INPUT_CLASS_EMPTY_DOCUMENT,
    INPUT_CLASS_EMPTY_QUERY,
    INPUT_VALIDATION_CLASSES,
    INPUT_VALIDATION_LOG_ENV,
    ConflictingFiltersError,
    EmptyQueryError,
    InputValidationError,
    InputValidationLedgerCorruptError,
    InputValidationLedgerUnwritableError,
    InvalidInputMarkedDegradedError,
    UnclassifiedInputFailureError,
)
from fin.retrieve.input_validation.ledger import (
    InputValidationLedger,
    InputValidationRecord,
    input_validation_ledger_from_env,
    raise_input_failure,
    record_input_failure,
)
from fin.retrieve.input_validation.observe import (
    OUTCOME_HITS,
    OUTCOME_INVALID_INPUT,
    OUTCOME_LEGAL_EMPTY,
    OUTCOME_OTHER_FAILURE,
    InputOutcome,
    is_empty_result,
    observe_input_outcome,
)
from fin.retrieve.input_validation.validators import (
    SINGLE_VALUED_FILTER_FIELDS,
    detect_filter_conflict,
    validate_filters,
    validate_query,
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
