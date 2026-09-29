"""Tests for selective algorithmic OCR write-back."""

from __future__ import annotations

from pathlib import Path

import pytest

from fin.corpus.extracted import ExtractedPage, Locator
from fin.corpus.ocr_writeback import (
    OCRWriteBackResult,
    selective_ocr_write_back,
)
from fin.corpus.ocr_writeback_errors import (
    InvalidOCRWriteBackInputError,
    OCRWriteBackEmptyTextError,
    OCRWriteBackEngineError,
)
from fin.host.citation_gate import DoneResult, EvidenceItem, verify_citations


class FakeOCREngine:
    def __init__(self, text: str = "", error: Exception | None = None) -> None:
        self.text = text
        self.error = error
        self.calls: list[tuple[Path, int]] = []

    def recognize(self, *, source: Path, page: int) -> str:
        self.calls.append((source, page))
        if self.error is not None:
            raise self.error
        return self.text


def _page(text: str = "") -> ExtractedPage:
    return ExtractedPage(
        locator=Locator("AAPL-2022Q3", "v" * 64, 7),
        text=text,
        passages=(),
    )


def test_collapsed_image_page_writes_back_ocr_text_and_keeps_locator() -> None:
    source = Path("filing.pdf")
    engine = FakeOCREngine(
        "Consolidated statements of operations show net sales and operating income."
    )

    result = selective_ocr_write_back(
        _page(),
        source=source,
        image_count=1,
        engine=engine,
    )

    assert isinstance(result, OCRWriteBackResult)
    assert result.applied is True
    assert result.page.locator == _page().locator
    assert result.page.text == engine.text
    assert result.page.passages == (
        result.page.passages[0],
    )
    assert result.page.passages[0].text == engine.text
    assert result.page.passages[0].char_end == len(engine.text)
    assert (source, 7) in engine.calls


def test_non_candidate_pages_skip_ocr_without_mutation() -> None:
    for page, image_count in ((_page(), 0), (_page("text layer"), 1)):
        engine = FakeOCREngine("must not be used")
        result = selective_ocr_write_back(
            page,
            source=Path("filing.pdf"),
            image_count=image_count,
            engine=engine,
        )
        assert result.applied is False
        assert result.page is page
        assert engine.calls == []


def test_empty_ocr_result_is_a_named_failure() -> None:
    with pytest.raises(OCRWriteBackEmptyTextError):
        selective_ocr_write_back(
            _page(),
            source=Path("filing.pdf"),
            image_count=1,
            engine=FakeOCREngine("  "),
        )


def test_ocr_engine_failure_is_not_silently_skipped() -> None:
    failure = OCRWriteBackEngineError("OCR backend failed")

    with pytest.raises(OCRWriteBackEngineError, match="OCR backend failed"):
        selective_ocr_write_back(
            _page(),
            source=Path("filing.pdf"),
            image_count=1,
            engine=FakeOCREngine(error=failure),
        )


def test_write_back_text_is_citable_as_a_contiguous_span() -> None:
    page = _page()
    ocr_text = (
        "The company reported total operating expenses of 12.4 billion "
        "dollars for the fiscal year."
    )
    result = selective_ocr_write_back(
        page,
        source=Path("filing.pdf"),
        image_count=1,
        engine=FakeOCREngine(ocr_text),
    )

    span = "total operating expenses of 12.4 billion dollars"
    citation_result = verify_citations(
        f"The filing says: {span}.",
        [
            EvidenceItem(
                locator=result.page.locator,
                unit_text=result.page.text,
            )
        ],
        repair_count=0,
        known_document_ids={result.page.locator.document_id},
    )

    assert len(span) >= 32
    assert isinstance(citation_result, DoneResult)
    assert span in citation_result.citations[0].quote


def test_negative_image_count_is_rejected() -> None:
    with pytest.raises(InvalidOCRWriteBackInputError):
        selective_ocr_write_back(
            _page(),
            source=Path("filing.pdf"),
            image_count=-1,
            engine=FakeOCREngine("not used"),
        )
