"""PDF parser: physical pages -> frozen page units (T6 granularity).

Reuses the repo's existing PDF dependency (``pdfplumber``, already a
hard dependency of ``fin.corpus.ingest``) so the multi-format path and
the committed extracted tree agree on one extraction stack. Each
physical page becomes one frozen unit; the unit ordinal equals the
1-based page number. Empty pages (no extractable text layer) yield no
unit — SC6's non-empty floor is enforced document-wide by the caller.

``max_units`` bounds the number of physical pages parsed. It exists so
bounded contexts (the default pytest suite) can cap work on the frozen
10-Qs; the default ``None`` parses every page. The truncation is a
deterministic prefix (pages 1..N), so the same ``max_units`` always
yields the same units.
"""

from __future__ import annotations

from pathlib import Path

from fin.corpus.errors import (
    MalformedSourceParseError,
    NoUnitsParseError,
    ParseError,
)
from fin.corpus.parsers._common import ParsedUnit, build_unit


def parse_pdf(
    *,
    source_bytes: bytes,
    source_sha256: str,
    document_id: str,
    version: str,
    max_units: int | None = None,
) -> tuple[ParsedUnit, ...]:
    """Parse ``source_bytes`` as PDF into page-level frozen units."""
    import io

    import pdfplumber

    if max_units is not None and max_units < 1:
        raise NoUnitsParseError(
            f"max_units must be >= 1 or None, got {max_units}"
        )
    units: list[ParsedUnit] = []
    try:
        with pdfplumber.open(io.BytesIO(source_bytes)) as pdf:
            for page_num, page in enumerate(pdf.pages, start=1):
                if max_units is not None and page_num > max_units:
                    break
                # Lazy import already done; extraction only — the OCR
                # branch is T7's deliverable and is deliberately absent
                # here (a scanned page yields no unit on this path).
                text = page.extract_text() or ""
                if not text.strip():
                    continue
                units.append(
                    build_unit(
                        fmt="pdf",
                        source_sha256=source_sha256,
                        document_id=document_id,
                        version=version,
                        unit=page_num,
                        text=text,
                    )
                )
    except MalformedSourceParseError:
        raise
    except ParseError:
        # Metadata / no-units failures from ``build_unit`` / this
        # function must propagate verbatim, not be re-wrapped.
        raise
    except Exception as exc:
        raise MalformedSourceParseError(
            f"PDF source for {document_id!r} cannot be parsed: {exc}"
        ) from exc
    if not units:
        raise NoUnitsParseError(
            f"PDF source for {document_id!r} produced no non-empty "
            "frozen units"
        )
    return tuple(units)


__all__ = ["parse_pdf"]
