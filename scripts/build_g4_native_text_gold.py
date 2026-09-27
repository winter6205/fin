#!/usr/bin/env python3
"""Build the G4 attachment gold native-text fixture, ``g4-native-text-001.pdf``.

Plan ``plans/thread-attachment-source-read.md`` section 8 names four gold
shapes for ``data/corpus/g4_attachment_gold/``: native-text, mixed, scanned,
and standalone-image. The manifest shipped the last three; this script builds
the missing one: a one-page PDF whose entire payload is a native text layer,
so the parser path must extract it without running OCR.

Determinism (the fixture is sha256-pinned in ``manifest.json``):

* every content string is embedded in this file verbatim; nothing is read
  from another PDF at build time (the provenance below records where the
  words came from and how they were verified, not a runtime dependency);
* the writer emits PDF 1.4 with an uncompressed content stream, base-14
  Helvetica/Helvetica-Bold with WinAnsiEncoding, and no document ID, no
  CreationDate/ModDate, and no traversal-order randomness, so the same
  interpreter produces byte-identical output on every host;
* line breaks are pure ``textwrap`` word wrapping, so a re-wrap can never
  split a word: quotes survive as substrings of the whitespace-normalised
  ``pdftotext`` read of the page.

Provenance of the embedded words (verified against the committed filing with
poppler 26.01.0 before being typed in here):

* the running heads ``Apple Inc.`` and ``Notes to Condensed Consolidated
  Financial Statements (Unaudited)`` are the native text layer of
  ``data/corpus/pdf/2023_Q3_AAPL.pdf`` pages 9-13 (same heading the
  ``g4-scanned-001`` case quotes from page 9);
* the section headings ``Accounts Receivable``, ``Trade Receivables`` and
  ``Vendor Non-Trade Receivables`` and both prose paragraphs are page 13 of
  that filing, read with ``pdftotext -layout -f 13 -l 13``; paragraph lines
  were joined at spaces before wrapping. No table and no image exists on that
  page's quoted region, and none is carried by this fixture.

Usage::

    uv run python scripts/build_g4_native_text_gold.py [output-path]

Default output: ``data/corpus/g4_attachment_gold/g4-native-text-001.pdf``.
"""

from __future__ import annotations

import hashlib
import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = REPO_ROOT / "data/corpus/g4_attachment_gold/g4-native-text-001.pdf"

PAGE_WIDTH_PT = 612.0
PAGE_HEIGHT_PT = 792.0
MARGIN_PT = 72.0
TEXT_WIDTH_PT = PAGE_WIDTH_PT - 2 * MARGIN_PT

RUNNING_HEAD = "Apple Inc."
RUNNING_HEAD_2 = "Notes to Condensed Consolidated Financial Statements (Unaudited)"
SECTION_HEAD = "Accounts Receivable"
SUBHEAD_1 = "Trade Receivables"
SUBHEAD_2 = "Vendor Non-Trade Receivables"

# Verbatim prose of Apple Inc. Form 10-Q (AAPL-2023Q3) page 13, paragraph
# lines joined at spaces (see module docstring for the verification tool).
PARAGRAPH_1 = (
    "The Company has considerable trade receivables outstanding with its "
    "third-party cellular network carriers, wholesalers, retailers, resellers, "
    "small and mid-sized businesses and education, enterprise and government "
    "customers. The Company generally does not require collateral from its "
    "customers; however, the Company will require collateral or third-party "
    "credit support in certain instances to limit credit risk. In addition, "
    "when possible, the Company attempts to limit credit risk on trade "
    "receivables with credit insurance for certain customers or by requiring "
    "third-party financing, loans or leases to support credit exposure. These "
    "credit-financing arrangements are directly between the third-party "
    "financing company and the end customer. As such, the Company generally "
    "does not assume any recourse or credit risk sharing related to any of "
    "these arrangements."
)
PARAGRAPH_1_CONT = (
    "As of September 24, 2022, the Company had one customer that represented "
    "10% or more of total trade receivables, which accounted for 10%. The "
    "Company\u2019s cellular network carriers accounted for 44% of total trade "
    "receivables as of September 24, 2022."
)
PARAGRAPH_2 = (
    "The Company has non-trade receivables from certain of its manufacturing "
    "vendors resulting from the sale of components to these vendors who "
    "manufacture subassemblies or assemble final products for the Company. "
    "The Company purchases these components directly from suppliers. As of "
    "July 1, 2023, the Company had two vendors that individually represented "
    "10% or more of total vendor non-trade receivables, which accounted for "
    "54% and 14%. As of September 24, 2022, the Company had two vendors that "
    "individually represented 10% or more of total vendor non-trade "
    "receivables, which accounted for 54% and 13%."
)


def _pdf_string(text: str) -> bytes:
    """Encode one display string as a PDF literal string, WinAnsi bytes.

    The base-14 fonts are declared with WinAnsiEncoding, so cp1252 is that
    encoding byte for byte; only ASCII string delimiters and non-printable
    bytes need octal escapes.
    """
    raw = text.encode("cp1252")
    out = bytearray(b"(")
    for byte in raw:
        if byte in (0x28, 0x29, 0x5C) or byte < 0x20 or byte > 0x7E:
            out += b"\\%03o" % byte
        else:
            out.append(byte)
    out += b")"
    return bytes(out)


def _wrap(text: str, font_size: float) -> list[str]:
    """Word-wrap one paragraph to the text column at the given font size.

    The character budget uses Helvetica's ~0.5 em average advance; the exact
    breaks are cosmetic, and word wrapping guarantees no line is a partial
    word, so the whitespace-normalised text is break-independent.
    """
    width_chars = int(TEXT_WIDTH_PT / (font_size * 0.5))
    return textwrap.wrap(text, width=width_chars, break_long_words=False)


def _content_stream() -> bytes:
    """Lay the page top-to-bottom and return the single uncompressed stream."""
    # (font, size, leading, gap-after, wrapped-lines) blocks; fonts F1 =
    # Helvetica, F2 = Helvetica-Bold. Sizes sit between the filing's own
    # running heads and its body text; none affects extraction semantics.
    blocks = [
        ("F2", 11.0, 13.0, 6.0, [RUNNING_HEAD]),
        ("F2", 9.5, 12.0, 26.0, [RUNNING_HEAD_2]),
        ("F2", 10.0, 13.0, 14.0, [SECTION_HEAD]),
        ("F2", 9.5, 12.0, 4.0, [SUBHEAD_1]),
        ("F1", 9.5, 12.0, 12.0, _wrap(PARAGRAPH_1, 9.5)),
        ("F1", 9.5, 12.0, 22.0, _wrap(PARAGRAPH_1_CONT, 9.5)),
        ("F2", 9.5, 12.0, 4.0, [SUBHEAD_2]),
        ("F1", 9.5, 12.0, 0.0, _wrap(PARAGRAPH_2, 9.5)),
    ]
    y = PAGE_HEIGHT_PT - MARGIN_PT
    parts: list[bytes] = []
    for font, size, leading, gap, lines in blocks:
        parts.append(
            b"BT /%s %.1f Tf %.1f TL 1 0 0 1 %.2f %.2f Tm "
            % (font.encode(), size, leading, MARGIN_PT, y)
        )
        for index, line in enumerate(lines):
            if index:
                parts.append(b"T* ")
            parts.append(_pdf_string(line) + b" Tj\n")
        parts.append(b"ET\n")
        y -= leading * len(lines) + gap
    return b"".join(parts)


def build_pdf() -> bytes:
    """Assemble the whole PDF file: catalog, one page, two fonts, one stream."""
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R /F2 5 0 R >> >> /Contents 6 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
        b"/Encoding /WinAnsiEncoding >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold "
        b"/Encoding /WinAnsiEncoding >>",
    ]
    stream = _content_stream()
    objects.append(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream))

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    xref_at = len(out)
    out += b"xref\n0 %d\n" % (len(objects) + 1)
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += (
        b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
        % (len(objects) + 1, xref_at)
    )
    return bytes(out)


def main(argv: list[str]) -> int:
    output = Path(argv[1]) if len(argv) > 1 else DEFAULT_OUTPUT
    payload = build_pdf()
    output.write_bytes(payload)
    print(
        "wrote %s: %d bytes, sha256 %s"
        % (output, len(payload), hashlib.sha256(payload).hexdigest())
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
