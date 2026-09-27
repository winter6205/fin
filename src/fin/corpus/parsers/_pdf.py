"""PDF parser: physical pages -> frozen page units (T6 granularity).

STATUS: retired from the corpus dispatch path — ``parse_document(fmt=
"pdf")`` now consumes the shared page/region core
(:class:`fin.corpus.parsers._pages.PageRegionParser`). This function
stays as the pinned behavior contract for its direct callers
(``tests/corpus/test_ocr_branch``, ``tests/corpus/test_ocr_region``,
``tests/retrieve/test_failure_catalog``) and as the comparison target
for the core-path equivalence tests.

Reuses the repo's existing PDF dependency (``pdfplumber``, already a
hard dependency of ``fin.corpus.ingest``) so the multi-format path and
the committed extracted tree agree on one extraction stack. Each
physical page becomes one frozen unit; the unit ordinal equals the
1-based page number. Genuinely blank pages (no text layer, no images)
yield no unit — SC6's non-empty floor is enforced document-wide by the
caller.

OCR branch (T7 / SC8): a page with **no text layer AND at least one
image** is a scanned page — it goes through the OCR branch instead of
being silently skipped. The engine is injected via ``ocr_engine``
(default ``None`` = no OCR capability, the default-pytest shape):

* engine present and ``recognize`` returns non-empty text -> the page
  becomes an :class:`OcrParsedUnit` (a ``ParsedUnit`` carrying
  ``ocr_applied=True``) whose text is frozen through the single
  normalization outlet (T5) by the shared ``build_unit`` constructor;
* engine missing, engine failure (any exception shape), or empty OCR
  text -> the unit terminates as the spec's named failure
  ``ocr_required_unavailable`` (:class:`~fin.corpus.ocr.OcrRequiredUnavailableError`).
  Every failing page is collected and named in one typed error; the
  product never contains an "empty text but recorded as successful"
  unit and no untyped exception escapes to the caller.

``max_units`` bounds the number of physical pages parsed. It exists so
bounded contexts (the default pytest suite) can cap work on the frozen
10-Qs; the default ``None`` parses every page. The truncation is a
deterministic prefix (pages 1..N), so the same ``max_units`` always
yields the same units.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from pathlib import Path

from fin.corpus.errors import (
    EncryptedSourceParseError,
    MalformedSourceParseError,
    NoUnitsParseError,
    ParseError,
)
from fin.corpus.ocr import (
    OCREngine,
    OCRError,
    OcrRequiredUnavailableError,
    detect_ocr_required,
)
from fin.corpus.parsers._common import ParsedUnit, build_unit


@dataclass(frozen=True)
class OcrParsedUnit(ParsedUnit):
    """A ``ParsedUnit`` whose text came from the OCR branch (SC8).

    Everything else (the eight SC10 metadata fields, frozen text via
    the normalization outlet) is identical to a plain unit — the only
    extension is the ``ocr_applied`` flag, the same field name the
    ingest write-back convention uses.
    """

    ocr_applied: bool = True

    @classmethod
    def from_unit(cls, unit: ParsedUnit) -> "OcrParsedUnit":
        """Re-stamp a plain unit as OCR-frozen, copying every base field.

        The copy iterates ``ParsedUnit``'s dataclass fields at call time,
        so a field added to the base later reaches the OCR unit by
        construction instead of being silently dropped at each call site.
        """
        return cls(
            **{f.name: getattr(unit, f.name) for f in fields(ParsedUnit)},
            ocr_applied=True,
        )


def _is_encryption_failure(exc: BaseException) -> bool:
    """Walk the cause chain looking for a pdfminer encryption error.

    pdfplumber wraps pdfminer exceptions (``PdfminerException`` with
    the original as ``__cause__``), so the chain must be walked to find
    ``PDFEncryptionError`` (including ``PDFPasswordIncorrect``). Used
    only to classify the failure as encrypted vs. corrupt (T8) — the
    parse itself is never retried or redirected.
    """
    from pdfminer.pdfdocument import PDFEncryptionError

    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, PDFEncryptionError):
            return True
        current = current.__cause__ or current.__context__
    return False


def parse_pdf(
    *,
    source_bytes: bytes,
    source_sha256: str,
    document_id: str,
    version: str,
    max_units: int | None = None,
    ocr_engine: OCREngine | None = None,
    source_path: Path | None = None,
) -> tuple[ParsedUnit, ...]:
    """Parse ``source_bytes`` as PDF into page-level frozen units.

    Parameters
    ----------
    ocr_engine:
        Injected :class:`~fin.corpus.ocr.OCREngine` for scanned pages.
        ``None`` (default) means no OCR capability: a scanned page then
        terminates the parse with the named
        ``OcrRequiredUnavailableError`` instead of silently skipping.
        Tests inject a fake engine; production pairs
        ``fin.corpus.ocr.TesseractEngine`` with a page renderer.
    source_path:
        Optional original filesystem path, forwarded to the engine as
        ``recognize(source=..., page=...)``. When parsing in-memory
        bytes only, a referential placeholder path named after the
        document is passed instead (fake/test engines ignore it; the
        production engine pair renders pages itself).
    """
    import io

    import pdfplumber

    if max_units is not None and max_units < 1:
        raise NoUnitsParseError(
            f"max_units must be >= 1 or None, got {max_units}"
        )
    engine_source = (
        source_path if source_path is not None else Path(f"{document_id}.pdf")
    )
    units: list[ParsedUnit] = []
    # (page number, reason) for every OCR-required page that could not
    # be produced — collected so one typed failure names them all.
    ocr_failures: list[tuple[int, str]] = []
    try:
        with pdfplumber.open(io.BytesIO(source_bytes)) as pdf:
            for page_num, page in enumerate(pdf.pages, start=1):
                if max_units is not None and page_num > max_units:
                    break
                text = page.extract_text() or ""
                if text.strip():
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
                    continue
                # No text layer. A page that also carries no image is
                # genuinely blank — skip (T6 behavior unchanged).
                # The OCR decision goes through the shared
                # ``detect_ocr_required`` heuristic (min_text_chars=1
                # narrows it to SC8's "no text layer" shape).
                if not detect_ocr_required(
                    text=text,
                    image_count=len(getattr(page, "images", []) or []),
                    min_text_chars=1,
                ):
                    continue
                # SC8 OCR branch: scanned page.
                if ocr_engine is None:
                    ocr_failures.append(
                        (
                            page_num,
                            "no OCR engine available (pass ocr_engine "
                            "or install the [ocr] extra)",
                        )
                    )
                    continue
                try:
                    text = ocr_engine.recognize(
                        source=engine_source, page=page_num
                    )
                except OcrRequiredUnavailableError as exc:
                    ocr_failures.append((page_num, f"OCR engine failed: {exc}"))
                    continue
                except OCRError as exc:
                    ocr_failures.append((page_num, f"OCR engine failed: {exc}"))
                    continue
                except Exception as exc:
                    # Any unexpected engine crash is converted, never
                    # wrapped as malformed-source and never re-thrown
                    # bare (SC8: named failure, not escaping exception).
                    ocr_failures.append(
                        (
                            page_num,
                            f"OCR engine raised {type(exc).__name__}: {exc}",
                        )
                    )
                    continue
                if not text or not text.strip():
                    ocr_failures.append(
                        (page_num, "OCR returned empty text")
                    )
                    continue
                # build_unit is the single construction point: the OCR
                # text is frozen through the normalization outlet (T5)
                # and SC10 metadata completeness is enforced here too.
                base = build_unit(
                    fmt="pdf",
                    source_sha256=source_sha256,
                    document_id=document_id,
                    version=version,
                    unit=page_num,
                    text=text,
                )
                units.append(
                    OcrParsedUnit(
                        fmt=base.fmt,
                        source_sha256=base.source_sha256,
                        locator=base.locator,
                        char_count=base.char_count,
                        language=base.language,
                        parse_rule_version=base.parse_rule_version,
                        params_table_version=base.params_table_version,
                        text=base.text,
                        ocr_applied=True,
                    )
                )
    except MalformedSourceParseError:
        raise
    except ParseError:
        # Metadata / no-units failures from ``build_unit`` / this
        # function must propagate verbatim, not be re-wrapped.
        raise
    except OCRError:
        # Named OCR failures must reach the caller as themselves, not
        # be re-wrapped as malformed source.
        raise
    except Exception as exc:
        if _is_encryption_failure(exc):
            # T8: an encrypted PDF is structurally intact — it is
            # named encrypted, not corrupt, because the remediation is
            # credentials, not repair.
            raise EncryptedSourceParseError(
                f"PDF source for {document_id!r} is encrypted and no "
                f"credentials were supplied: {exc}"
            ) from exc
        raise MalformedSourceParseError(
            f"PDF source for {document_id!r} cannot be parsed: {exc}"
        ) from exc
    if ocr_failures:
        # SC8 named failure: every OCR-required unit that could not be
        # written is named — no "empty text but successful" unit, no
        # silent skip, no bare exception.
        detail = "; ".join(f"page {num} ({reason})" for num, reason in ocr_failures)
        raise OcrRequiredUnavailableError(
            f"ocr_required_unavailable: scanned page(s) of {document_id!r} "
            f"could not be OCR-frozen — {detail}"
        )
    if not units:
        raise NoUnitsParseError(
            f"PDF source for {document_id!r} produced no non-empty "
            "frozen units"
        )
    return tuple(units)


__all__ = ["OcrParsedUnit", "parse_pdf"]
