"""T24: one executable catalog for every corpus/retrieve failure path."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest

from fin.corpus.errors import EmptySourceParseError
from fin.corpus.extracted import Locator
from fin.corpus.ocr import OcrRequiredUnavailableError
from fin.corpus.parsers import parse_document
from fin.corpus.parsers._pdf import parse_pdf
from fin.corpus.push import PushRecord
from fin.retrieve.failure_catalog import (
    FAILURE_CATALOG,
    FAILURE_CATEGORY_BACKGROUND,
    FAILURE_CATEGORY_INVALIDATION,
    FAILURE_CATEGORY_JOURNAL,
    FAILURE_CATEGORY_MAPPING,
    FAILURE_CATEGORY_OCR,
    FAILURE_CATEGORY_PARSE,
    FAILURE_CATEGORY_PROTOCOL,
    FAILURE_CATEGORY_PUSH_UNAVAILABLE,
    untraced_failure_categories,
)
from fin.retrieve.hit_mapping import MappingFailureLedger, map_hits_to_chunks
from fin.retrieve.invalidation_port import (
    InvalidationLedger,
    InvalidationTarget,
    InvalidationServiceUnavailableError,
    invalidate_targets,
)
from fin.retrieve.push_batching import (
    PushJournal,
    PushJournalUnwritableError,
    plan_batches,
    push_in_batches,
)
from fin.retrieve.push_port import (
    PushBatchFailedError,
    PushProtocolError,
    PushServiceUnavailableError,
    push_records,
)


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SRC_FIN = _PROJECT_ROOT / "src" / "fin"


def _build_scanned_pdf() -> bytes:
    image = b"\x00" * 16
    content = b"q 400 0 0 600 100 100 cm /Im0 Do Q"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /XObject << /Im0 4 0 R >> >> "
            b"/Contents 5 0 R >>"
        ),
        (
            b"<< /Type /XObject /Subtype /Image /Width 4 /Height 4 "
            b"/ColorSpace /DeviceGray /BitsPerComponent 8 "
            + f"/Length {len(image)} >>\nstream\n".encode()
            + image
            + b"\nendstream"
        ),
        f"<< /Length {len(content)} >>\nstream\n".encode()
        + content
        + b"\nendstream",
    ]
    result = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(result))
        result += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(result)
    result += f"xref\n0 {len(objects) + 1}\n".encode()
    result += b"0000000000 65535 f \n"
    for offset in offsets:
        result += f"{offset:010d} 00000 n \n".encode()
    result += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n"
    ).encode()
    return bytes(result)


def _record() -> PushRecord:
    return PushRecord(
        block_id="block-1",
        source_ref='{"document_id":"DOC-1","unit":1,"version":"v1"}',
        content_id="content-1",
        text="revenue rose",
        parent_block_id="parent-1",
        parent_text="revenue rose in the quarter",
        metadata={
            "fmt": "txt",
            "doc_type": "narrative",
            "section": "body",
            "source_sha256": "a" * 64,
            "parse_rule_version": "parse-1",
            "params_table_version": "params-1",
        },
    )


class _UnavailablePushPort:
    def push(self, records: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
        raise ConnectionRefusedError("service unavailable")


class _ProtocolPushPort:
    def push(self, records: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
        return {"block_ids": [], "written": 0, "duplicates": 0}


class _UnavailableInvalidationPort:
    def invalidate(
        self, targets: Sequence[Mapping[str, Any]]
    ) -> Mapping[str, Any]:
        raise ConnectionRefusedError("service unavailable")


class _FailedPushPort:
    def push(self, records: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
        raise ValueError("background payload failed")


def _catalog_entry(category: str):
    return next(entry for entry in FAILURE_CATALOG if entry.category == category)


def test_failure_catalog_names_exactly_the_seven_required_paths() -> None:
    assert {entry.category for entry in FAILURE_CATALOG} == {
        FAILURE_CATEGORY_PARSE,
        FAILURE_CATEGORY_OCR,
        FAILURE_CATEGORY_PUSH_UNAVAILABLE,
        FAILURE_CATEGORY_PROTOCOL,
        FAILURE_CATEGORY_MAPPING,
        FAILURE_CATEGORY_INVALIDATION,
        FAILURE_CATEGORY_BACKGROUND,
        FAILURE_CATEGORY_JOURNAL,
    }
    assert len(FAILURE_CATALOG) == 8


def test_failure_catalog_has_zero_untraced_paths() -> None:
    assert untraced_failure_categories() == ()


@pytest.mark.parametrize(
    "category",
    [
        FAILURE_CATEGORY_PARSE,
        FAILURE_CATEGORY_OCR,
        FAILURE_CATEGORY_PUSH_UNAVAILABLE,
        FAILURE_CATEGORY_PROTOCOL,
        FAILURE_CATEGORY_MAPPING,
        FAILURE_CATEGORY_INVALIDATION,
        FAILURE_CATEGORY_BACKGROUND,
        FAILURE_CATEGORY_JOURNAL,
    ],
)
def test_every_catalog_code_is_published_by_its_real_failure_surface(
    category: str,
) -> None:
    entry = _catalog_entry(category)
    assert entry.code == entry.code_source()
    assert entry.code


def test_parse_catalog_code_is_triggerable() -> None:
    entry = _catalog_entry(FAILURE_CATEGORY_PARSE)
    with pytest.raises(EmptySourceParseError) as excinfo:
        parse_document(b"", fmt="txt", document_id="T24", version="v1")
    assert excinfo.value.code == entry.code


def test_ocr_catalog_code_is_triggerable() -> None:
    entry = _catalog_entry(FAILURE_CATEGORY_OCR)

    class _UnavailableEngine:
        def recognize(self, *, source: Path, page: int) -> str:
            raise OcrRequiredUnavailableError("OCR engine unavailable")

    with pytest.raises(OcrRequiredUnavailableError) as excinfo:
        parse_pdf(
            source_bytes=_build_scanned_pdf(),
            source_sha256="a" * 64,
            document_id="T24",
            version="v1",
            ocr_engine=_UnavailableEngine(),
        )
    assert excinfo.value.code == entry.code


def test_push_unavailable_catalog_code_is_triggerable() -> None:
    entry = _catalog_entry(FAILURE_CATEGORY_PUSH_UNAVAILABLE)
    with pytest.raises(PushServiceUnavailableError) as excinfo:
        push_records(_UnavailablePushPort(), (_record(),))
    assert excinfo.value.code == entry.code


def test_protocol_catalog_code_is_triggerable() -> None:
    entry = _catalog_entry(FAILURE_CATEGORY_PROTOCOL)
    with pytest.raises(PushProtocolError) as excinfo:
        push_records(_ProtocolPushPort(), (_record(),))
    assert excinfo.value.code == entry.code


def test_mapping_catalog_code_is_triggerable() -> None:
    entry = _catalog_entry(FAILURE_CATEGORY_MAPPING)
    ledger = MappingFailureLedger()
    map_hits_to_chunks(
        [{"source_ref": "missing-ref", "text": "revenue"}],
        locator_map={},
        limit=1,
        ledger=ledger,
    )
    assert ledger.count_by_code()[entry.code] == 1


def test_invalidation_catalog_code_is_triggerable() -> None:
    entry = _catalog_entry(FAILURE_CATEGORY_INVALIDATION)
    target = InvalidationTarget(
        source_ref="source-1",
        content_id="content-1",
        block_id="block-1",
        document_id="DOC-1",
        version="v1",
        unit=1,
        reason="unit_version_changed",
    )
    ledger = InvalidationLedger()
    with pytest.raises(InvalidationServiceUnavailableError) as excinfo:
        invalidate_targets(
            _UnavailableInvalidationPort(), (target,), ledger=ledger
        )
    assert ledger.failures()[0].code == entry.code
    assert excinfo.value.code == entry.code


def test_background_catalog_code_is_triggerable() -> None:
    entry = _catalog_entry(FAILURE_CATEGORY_BACKGROUND)
    journal = PushJournal()
    with pytest.raises(PushBatchFailedError) as excinfo:
        push_in_batches(
            _FailedPushPort(), (_record(),), journal=journal, batch_size=1
        )
    assert excinfo.value.code == entry.code
    assert journal.failures()[0].code == entry.code


def test_journal_persistence_catalog_code_is_triggerable(tmp_path) -> None:
    entry = _catalog_entry(FAILURE_CATEGORY_JOURNAL)
    journal_parent = tmp_path / "journal-parent"
    journal_parent.write_text("not a directory", encoding="utf-8")
    journal_path = journal_parent / "journal.jsonl"
    journal = PushJournal(path=journal_path)
    plan = plan_batches((_record(),), batch_size=1)[0]

    with pytest.raises(PushJournalUnwritableError) as excinfo:
        journal.record_failure(
            plan,
            code=PushBatchFailedError.code,
            message="background payload failed",
        )

    assert excinfo.value.code == entry.code
    assert journal.failures() == ()


def test_no_empty_except_handler_exists_in_the_full_fin_source_tree() -> None:
    offenders: list[str] = []
    for path in sorted(_SRC_FIN.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler) and all(
                isinstance(statement, ast.Pass) for statement in node.body
            ):
                offenders.append(
                    f"{path.relative_to(_PROJECT_ROOT)}:{node.lineno}"
                )
    assert offenders == []


def test_catalog_does_not_enable_live_services_or_network() -> None:
    source = (
        _PROJECT_ROOT
        / "src"
        / "fin"
        / "retrieve"
        / "failure_catalog.py"
    ).read_text(encoding="utf-8")
    assert "subprocess" not in source
    assert "urlopen" not in source
    assert "httpx" not in source
