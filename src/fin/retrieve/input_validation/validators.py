"""Query and filter validators for the retrieve path."""

from __future__ import annotations

from typing import Any, Final, Mapping

from fin.retrieve.input_validation.errors import ConflictingFiltersError, EmptyQueryError
from fin.retrieve.input_validation.ledger import InputValidationLedger, raise_input_failure


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
