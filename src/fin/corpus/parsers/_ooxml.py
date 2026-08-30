"""Shared OOXML (zip + XML) plumbing for the docx / xlsx / pptx
parsers (T6).

All three Office formats are ZIP containers of XML parts. They are
parsed with the standard library only (``zipfile`` +
``xml.etree.ElementTree``) — no new dependency is warranted for read-
only text extraction, and the ACR restraint rule ("优先纯 stdlib")
rules out python-docx / openpyxl / python-pptx here.

Every failure to open / decode the container is a named
``MalformedSourceParseError`` — no bare exception escapes to the
caller (error-handling-enforcer).
"""

from __future__ import annotations

import re
import zipfile
from xml.etree import ElementTree

from fin.corpus.errors import EncryptedSourceParseError, MalformedSourceParseError
from fin.corpus.parsers._common import CFB_MAGIC

# Numeric suffix of OOXML part names like ``slide7.xml`` /
# ``sheet12.xml`` — sorted numerically, never lexically, so slide/sheet
# order is deterministic across machines.
_PART_NUMBER_RE = re.compile(r"(\d+)\.xml$")


def open_zip(source_bytes: bytes, *, document_id: str) -> zipfile.ZipFile:
    """Open ``source_bytes`` as a zip container, failing named.

    A Compound File Binary container is not a broken zip — it is an
    ECMA-376 **encrypted** package (Standard / Agile encryption), so it
    is named as encrypted, not merely malformed (T8: the remediations
    differ — credentials vs. repair). ``parse_document`` already gates
    this shape for the dispatch path; the check is repeated here so
    direct ``parse_docx`` / ``parse_xlsx`` / ``parse_pptx`` callers get
    the same named surface.
    """
    import io

    if source_bytes.startswith(CFB_MAGIC):
        raise EncryptedSourceParseError(
            f"OOXML source for {document_id!r} is an encrypted package "
            "(Compound File Binary container) and no credentials were "
            "supplied"
        )
    try:
        return zipfile.ZipFile(io.BytesIO(source_bytes))
    except Exception as exc:
        raise MalformedSourceParseError(
            f"OOXML source for {document_id!r} is not a readable zip "
            f"container: {exc}"
        ) from exc


def parse_xml(zip_file: zipfile.ZipFile, name: str, *, document_id: str) -> ElementTree.Element:
    """Read and parse one XML part, failing named."""
    try:
        return ElementTree.fromstring(zip_file.read(name))
    except KeyError as exc:
        raise MalformedSourceParseError(
            f"OOXML source for {document_id!r} is missing part {name!r}: "
            f"{exc}"
        ) from exc
    except Exception as exc:
        raise MalformedSourceParseError(
            f"OOXML part {name!r} of {document_id!r} is malformed XML: "
            f"{exc}"
        ) from exc


def part_number(name: str) -> int:
    """Extract the numeric suffix of an OOXML part name (0 fallback
    keeps the sort total and deterministic for non-numbered names)."""
    match = _PART_NUMBER_RE.search(name)
    return int(match.group(1)) if match else 0


__all__ = ["open_zip", "parse_xml", "part_number"]
