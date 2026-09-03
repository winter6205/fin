"""Plain-text parser: blank-line paragraphs -> frozen units (T6
granularity, plan "已决": 纯文本以空行段).

Each paragraph (a maximal run of lines not separated by a blank line)
becomes exactly one unit — no cap merge: the decided granularity for
plain text is the blank-line paragraph itself. Splitting happens on
the *raw* text (blank lines are layout, and they vanish at freeze
time anyway — the normalization outlet drops blank lines).
"""

from __future__ import annotations

import re

from fin.corpus.errors import (
    MalformedSourceParseError,
    NoUnitsParseError,
)
from fin.corpus.parsers._common import ParsedUnit, build_unit

# A blank line: a line containing only whitespace. Two or more blank
# lines behave identically to one (deterministic).
_BLANK_LINE_RE = re.compile(r"^[^\S\n]*$")


def _split_paragraphs(raw: str) -> list[str]:
    paragraphs: list[str] = []
    current: list[str] = []
    for line in raw.splitlines():
        if _BLANK_LINE_RE.match(line):
            if current:
                paragraphs.append("\n".join(current))
                current = []
        else:
            current.append(line)
    if current:
        paragraphs.append("\n".join(current))
    return paragraphs


def parse_plain_text(
    *,
    source_bytes: bytes,
    source_sha256: str,
    document_id: str,
    version: str,
) -> tuple[ParsedUnit, ...]:
    """Parse ``source_bytes`` as plain text into blank-line-paragraph
    units."""
    try:
        raw = source_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MalformedSourceParseError(
            f"text source for {document_id!r} is not valid UTF-8: {exc}"
        ) from exc
    units = [
        build_unit(
            fmt="txt",
            source_sha256=source_sha256,
            document_id=document_id,
            version=version,
            unit=ordinal,
            text=paragraph,
        )
        for ordinal, paragraph in enumerate(
            _split_paragraphs(raw), start=1
        )
    ]
    if not units:
        raise NoUnitsParseError(
            f"text source for {document_id!r} produced no non-empty "
            "frozen units"
        )
    return tuple(units)


__all__ = ["parse_plain_text"]
