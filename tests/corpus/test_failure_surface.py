"""T8 / SC9 / SC46: the named-failure surface for abnormal inputs.

SC9: encrypted, corrupt, empty-file, 0-byte, and non-target-MIME
inputs each return a **named** failure — not an empty string, not a
silently skipped absence, not an uncaught exception. T6 already named
the corrupt / empty / 0-byte shapes; T8 closes the remaining gaps and
makes the five classes pairwise distinguishable:

===========  ==================================  ==================
input class  named failure                       error code
===========  ==================================  ==================
0-byte       ``EmptySourceParseError``           ``empty_source``
empty file   ``NoUnitsParseError``               ``no_units``
corrupt      ``MalformedSourceParseError``       ``malformed_source``
encrypted    ``EncryptedSourceParseError``       ``encrypted_source``
wrong MIME   ``FormatMismatchParseError``        ``format_mismatch``
===========  ==================================  ==================

SC46: no bare ``except`` swallows a failure and no failure shape
becomes an empty success — the no-silent rule is proven in-process by
an AST scan over ``src/fin`` (same shape ``rg`` proves at the shell).

"Non-target MIME" reading (plan T8 headroom): ``parse_document``'s
format is named explicitly by the caller, so the class is realized as
"declared format conflicts with the bytes' content signature" — a
PDF's magic fed as ``xlsx``, an image fed as ``txt``. The signature
check is **failure classification only**: it never redirects or
replaces parsing, and absence of a known signature never fails (the
per-format parser stays the sole judge of "malformed").
"""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path

import pytest

from fin.corpus.errors import (
    EmptySourceParseError,
    EncryptedSourceParseError,
    FormatMismatchParseError,
    MalformedSourceParseError,
    NoUnitsParseError,
    ParseError,
    UnsupportedFormatParseError,
)
from fin.corpus.parsers import parse_document

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SRC_FIN = _PROJECT_ROOT / "src" / "fin"
_PDF_SAMPLE = _PROJECT_ROOT / "data" / "corpus" / "pdf" / "2023_Q3_AAPL.pdf"

# The Compound File Binary magic: ECMA-376 Standard / Agile encryption
# wraps the OOXML package in a CFB container, so a "docx/xlsx/pptx"
# whose bytes start with this magic is encrypted, not merely corrupt.
_CFB_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


# ---------------------------------------------------------------------------
# Deterministic fixtures (hand-assembled, stdlib only — no new deps).
# ---------------------------------------------------------------------------


def _build_encrypted_pdf() -> bytes:
    """A minimal PDF whose trailer carries a standard security handler.

    Same xref-exact hand-assembly style as ``test_ocr_branch.py``; the
    ``/Encrypt`` dict (``/Filter /Standard``) makes pdfminer terminate
    with ``PDFPasswordIncorrect`` (a ``PDFEncryptionError``) at open
    time. The bytes are never decryptable — that is the point.
    """
    o_value = "00" * 32
    u_value = "00" * 32
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [4 0 R] /Count 1 >>",
        b"<< /Base14 /Helvetica /Type /Font /Subtype /Type1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 3 0 R >> >> /Contents 5 0 R >>"
        ),
        None,  # content stream, filled below
        (
            f"<< /Filter /Standard /V 1 /R 2 /O <{o_value}> "
            f"/U <{u_value}> /P -3904 >>"
        ).encode(),
    ]
    content = b"BT /F1 12 Tf 72 720 Td (Encrypted fixture.) Tj ET"
    objects[4] = (
        f"<< /Length {len(content)} >>\nstream\n".encode()
        + content
        + b"\nendstream"
    )
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for num, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{num} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R /Encrypt 6 0 R "
        f"/ID [<0123456789abcdef0123456789abcdef> "
        f"<0123456789abcdef0123456789abcdef>] >>\n"
        f"startxref\n{xref_pos}\n%%EOF\n"
    ).encode()
    return bytes(out)


def _parse(data: bytes, fmt: str) -> None:
    """Run one parse_document call through the dispatch layer."""
    parse_document(
        data,
        fmt=fmt,
        document_id="T8-DOC",
        version="ver-1",
    )


# ---------------------------------------------------------------------------
# Each of the five SC9 classes returns its own named failure.
# ---------------------------------------------------------------------------


def test_zero_byte_source_returns_empty_source_named_failure() -> None:
    with pytest.raises(EmptySourceParseError) as excinfo:
        _parse(b"", fmt="txt")
    assert excinfo.value.code == "empty_source"
    assert "T8-DOC" in str(excinfo.value)


def test_empty_file_returns_no_units_named_failure() -> None:
    # A file that exists but carries no parseable content (whitespace
    # only): the parse runs mechanically and then terminates named.
    with pytest.raises(NoUnitsParseError) as excinfo:
        _parse(b"   \n\t\n  \n", fmt="txt")
    assert excinfo.value.code == "no_units"


def test_corrupt_source_returns_malformed_named_failure() -> None:
    # Broken zip container (a "docx" that is not a zip) and a PDF with
    # no valid xref: both are the corrupt class, named as malformed.
    with pytest.raises(MalformedSourceParseError) as zip_exc:
        _parse(b"PK\x03\x04 definitely not a real zip", fmt="docx")
    assert zip_exc.value.code == "malformed_source"
    with pytest.raises(MalformedSourceParseError):
        _parse(b"%PDF-1.4\nno xref table follows here", fmt="pdf")


def test_encrypted_pdf_returns_encrypted_named_failure() -> None:
    with pytest.raises(EncryptedSourceParseError) as excinfo:
        _parse(_build_encrypted_pdf(), fmt="pdf")
    assert excinfo.value.code == "encrypted_source"
    assert "T8-DOC" in str(excinfo.value)
    # Distinguishable from the corrupt class: not its subclass, not its
    # parent — a sibling inside the ParseError family.
    assert not isinstance(excinfo.value, MalformedSourceParseError)
    assert not issubclass(EncryptedSourceParseError, MalformedSourceParseError)
    assert not issubclass(MalformedSourceParseError, EncryptedSourceParseError)


def test_encrypted_ooxml_returns_encrypted_named_failure() -> None:
    # ECMA-376 encrypted packages are CFB containers, not zips: the CFB
    # magic is the named encryption signal (not a generic corrupt zip).
    with pytest.raises(EncryptedSourceParseError) as excinfo:
        _parse(_CFB_MAGIC + b"\x00" * 64, fmt="xlsx")
    assert excinfo.value.code == "encrypted_source"
    assert not isinstance(excinfo.value, MalformedSourceParseError)


def test_non_target_mime_returns_format_mismatch_named_failure() -> None:
    # Declared format conflicts with the bytes' content signature.
    # A PDF's magic declared as xlsx; a zip declared as pdf; a PNG
    # declared as txt (the classic binary-fed-to-text-parser shape,
    # which would otherwise silently yield garbage "text" units).
    with pytest.raises(FormatMismatchParseError) as pdf_as_xlsx:
        _parse(_PDF_SAMPLE.read_bytes(), fmt="xlsx")
    assert pdf_as_xlsx.value.code == "format_mismatch"
    with pytest.raises(FormatMismatchParseError):
        _parse(b"PK\x03\x04" + b"\x00" * 32, fmt="pdf")
    with pytest.raises(FormatMismatchParseError) as png_as_txt:
        _parse(
            b"\x89PNG\r\n\x1a\n" + b"\x00" * 32,
            fmt="txt",
        )
    assert png_as_txt.value.code == "format_mismatch"
    # Distinguishable from the corrupt class in both directions.
    assert not issubclass(FormatMismatchParseError, MalformedSourceParseError)
    assert not issubclass(MalformedSourceParseError, FormatMismatchParseError)


def test_non_target_format_name_stays_unsupported_named_failure() -> None:
    # An fmt the caller names that is not one of the seven supported
    # formats remains T6's unsupported-format shape (the "caller asked
    # for a format we do not speak" half of non-target MIME).
    from fin.corpus.errors import UnsupportedFormatParseError

    with pytest.raises(UnsupportedFormatParseError) as excinfo:
        _parse(b"some bytes", fmt="rtf")
    assert excinfo.value.code == "unsupported_format"


# ---------------------------------------------------------------------------
# The five classes are pairwise distinguishable — type and error code.
# ---------------------------------------------------------------------------


def test_five_classes_are_pairwise_distinguishable() -> None:
    encrypted_pdf = _build_encrypted_pdf()
    samples: list[tuple[str, bytes, str]] = [
        # (SC9 class label, bytes, declared fmt)
        ("0-byte", b"", "txt"),
        ("empty-file", b"   \n\t\n  \n", "txt"),
        ("corrupt", b"PK\x03\x04 definitely not a real zip", "docx"),
        ("encrypted", encrypted_pdf, "pdf"),
        ("non-target-MIME", _PDF_SAMPLE.read_bytes(), "xlsx"),
    ]
    seen_types: list[type[Exception]] = []
    seen_codes: list[str] = []
    for label, data, fmt in samples:
        with pytest.raises(ParseError) as excinfo:  # never a bare exception
            _parse(data, fmt)
        exc = excinfo.value
        assert type(exc) not in seen_types, (
            f"{label} collides with an earlier class: {type(exc).__name__}"
        )
        assert exc.code not in seen_codes, (
            f"{label} reuses code {exc.code!r}"
        )
        assert str(exc).strip() != ""  # failure names itself, not empty
        seen_types.append(type(exc))
        seen_codes.append(exc.code)
    assert len(set(seen_types)) == 5
    assert len(set(seen_codes)) == 5


def test_every_failure_is_in_the_parse_error_family_with_a_code() -> None:
    for exc_type in (
        EmptySourceParseError,
        NoUnitsParseError,
        UnsupportedFormatParseError,
        MalformedSourceParseError,
        EncryptedSourceParseError,
        FormatMismatchParseError,
    ):
        assert issubclass(exc_type, ParseError)
        assert issubclass(exc_type, RuntimeError)
        assert exc_type.code
        assert exc_type.code != ParseError.code


# ---------------------------------------------------------------------------
# SC46: no empty except, no "failure as empty success" — in-process
# proof (the same shape `rg` shows at the shell).
# ---------------------------------------------------------------------------


def test_no_empty_except_handler_in_the_corpus_surface() -> None:
    """SC46 in-process proof for T8's surface (the shape ``rg`` shows
    at the shell): in ``src/fin/corpus`` **any** all-``pass`` except
    handler is an offender — every failure here must be named, never
    absorbed. (A repo-wide silent-failure sweep is T24's deliverable;
    pre-existing narrow, documented ``except SomeError: pass`` sites
    outside ``corpus`` — e.g. the host stop path and the MCP process
    reap-then-reraise — are out of this ticket's scope.)
    """
    offenders: list[str] = []
    for path in sorted((_SRC_FIN / "corpus").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler) and all(
                isinstance(stmt, ast.Pass) for stmt in node.body
            ):
                offenders.append(
                    f"{path.relative_to(_PROJECT_ROOT)}:"
                    f"{node.lineno} except-pass"
                )
    assert offenders == []


def test_no_failure_returns_an_empty_success_result() -> None:
    # Every abnormal input terminates in an exception: parse_document
    # never returns a ParseResult for the five classes (no "skipped
    # absence", no empty units tuple masquerading as success).
    encrypted_pdf = _build_encrypted_pdf()
    cases: list[tuple[bytes, str]] = [
        (b"", "txt"),
        (b"   \n\t\n  \n", "txt"),
        (b"PK\x03\x04 definitely not a real zip", "docx"),
        (encrypted_pdf, "pdf"),
        (_PDF_SAMPLE.read_bytes(), "xlsx"),
    ]
    for data, fmt in cases:
        with pytest.raises(ParseError):
            _parse(data, fmt)


# ---------------------------------------------------------------------------
# The signature gate classifies failures only — the normal path is
# byte-for-byte unchanged (T6 behavior preserved).
# ---------------------------------------------------------------------------


def test_valid_pdf_still_parses_normally_through_the_gate() -> None:
    result = parse_document(
        _PDF_SAMPLE,
        fmt="pdf",
        document_id="T8-NORMAL",
        version="ver-1",
        max_units=2,
    )
    assert len(result.units) == 2
    assert result.units[0].text.strip() != ""


def test_signature_absence_never_fails_the_gate(tmp_path: Path) -> None:
    # No known magic signature is not a mismatch: a plain text file
    # (html/md/txt territory) has no signature and must pass through.
    doc = tmp_path / "note.txt"
    doc.write_text("Ordinary narrative text.\n\nSecond paragraph.", encoding="utf-8")
    result = parse_document(
        doc,
        fmt="txt",
        document_id="T8-PLAIN",
        version=hashlib.sha256(doc.read_bytes()).hexdigest(),
    )
    assert len(result.units) >= 1
    assert result.units[0].text.strip() != ""
