"""Structure-aware, citable units on top of the existing corpus path.

The parser boundary is intentionally small: an injected parser returns
``ParsedSection`` values and this module assigns deterministic locator
paths. The default PDF adapter uses the repository's existing
``pdfplumber`` dependency; it does not introduce a Docling/LlamaIndex
runtime or a second sliding-window splitter.

Parent text is assembled as a verbatim structure rendering and child text
is included in that rendering. Consequently every child is a substring of
its parent, and every top-level parent is a substring of the deterministic
document rendering.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from fin.corpus.structure_units_errors import (
    InvalidStructureInputError,
    StructureParseError,
    StructureUnitLimitError,
    StructureUnitsError,
    UnsupportedFormatError,
)


@dataclass(frozen=True)
class StructureLocator:
    """Stable cite identity for one structure-aware unit."""

    document_id: str
    version: str
    unit: str


@dataclass(frozen=True)
class ParsedSection:
    """Parser-neutral section data in document order."""

    title: str | None
    text: str
    children: tuple["ParsedSection", ...] = ()


@dataclass(frozen=True)
class StructureUnit:
    """One structure unit with a stable locator and parent path."""

    locator: StructureLocator
    text: str
    title: str | None = None
    parent_unit: str | None = None


class StructureParser(Protocol):
    """Minimal parser protocol usable by fakes and existing adapters."""

    def parse(self, source: Path) -> Iterable[ParsedSection]:
        """Parse ``source`` into sections in document order."""
        ...


_HEADING_RE = re.compile(
    r"^(?:"
    r"\d+(?:\.\d+)*[.)]?\s+.+"
    r"|(?i:item\s+\d+[a-z]?(?:[.):]|\s|$).+)"
    r"|[A-Z][A-Z0-9 &:/,'()\-]{3,}$"
    r")"
)


class PdfStructureParser:
    """Default PDF structure adapter using the existing PDF dependency."""

    def parse(self, source: Path) -> tuple[ParsedSection, ...]:
        try:
            import pdfplumber
        except Exception as exc:
            raise StructureParseError(
                "PDF structure parser dependency is unavailable"
            ) from exc

        try:
            with pdfplumber.open(str(source)) as pdf:
                page_text = [page.extract_text() or "" for page in pdf.pages]
        except Exception as exc:
            raise StructureParseError(
                f"unable to parse PDF {source}: {exc}"
            ) from exc
        return _sections_from_text("\n".join(page_text))


def _sections_from_text(text: str) -> tuple[ParsedSection, ...]:
    sections: list[tuple[str | None, str]] = []
    current_title: str | None = None
    current_lines: list[str] = []

    def flush() -> None:
        nonlocal current_title, current_lines
        body = "\n".join(current_lines).strip()
        if current_title is not None or body:
            sections.append((current_title, body))
        current_title = None
        current_lines = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if _HEADING_RE.match(line) and len(line) <= 160:
            if current_title is not None or current_lines:
                flush()
            current_title = line
        else:
            current_lines.append(line)
    if current_title is not None or current_lines:
        flush()

    return tuple(
        ParsedSection(title=title, text=body)
        for title, body in sections
        if title or body
    )


def _default_parser(source: Path) -> StructureParser:
    if source.suffix.lower() != ".pdf":
        raise UnsupportedFormatError(
            f"no default structure parser for "
            f"{source.suffix or '<no extension>'}"
        )
    return PdfStructureParser()


def _validate_identity(document_id: str, version: str) -> None:
    if not isinstance(document_id, str) or not document_id.strip():
        raise InvalidStructureInputError(
            "document_id must be a non-empty string"
        )
    if not isinstance(version, str) or not version.strip():
        raise InvalidStructureInputError(
            "version must be a non-empty string"
        )


def _coerce_sections(
    raw_sections: Iterable[ParsedSection],
) -> tuple[ParsedSection, ...]:
    try:
        sections = tuple(raw_sections)
    except Exception as exc:
        raise StructureParseError(
            "parser did not return an iterable of sections"
        ) from exc
    for section in sections:
        if not isinstance(section, ParsedSection):
            raise StructureParseError(
                "parser returned a value that is not ParsedSection"
            )
    return sections


def _own_section_text(section: ParsedSection) -> str:
    title = section.title.strip() if section.title is not None else ""
    text = section.text.strip()
    if not title and not text:
        raise InvalidStructureInputError(
            "a structure section cannot be empty"
        )
    if title and text:
        return f"{title}\n{text}"
    return title or text


def _render_section(section: ParsedSection) -> str:
    """Render a section and descendants as one contiguous text span."""
    parts = [_own_section_text(section)]
    parts.extend(_render_section(child) for child in section.children)
    return "\n".join(parts)


def _flatten_sections(
    sections: Sequence[ParsedSection],
    *,
    document_id: str,
    version: str,
    max_units: int,
) -> tuple[StructureUnit, ...]:
    units: list[StructureUnit] = []

    def visit(
        section: ParsedSection,
        *,
        path: str,
        parent_unit: str | None,
    ) -> None:
        if len(units) >= max_units:
            raise StructureUnitLimitError(
                f"document exceeds maximum structure units ({max_units})"
            )
        units.append(
            StructureUnit(
                locator=StructureLocator(
                    document_id=document_id,
                    version=version,
                    unit=path,
                ),
                text=_render_section(section),
                title=section.title.strip() if section.title else None,
                parent_unit=parent_unit,
            )
        )
        for child_index, child in enumerate(section.children, start=1):
            visit(
                child,
                path=f"{path}/section-{child_index:03d}",
                parent_unit=path,
            )

    for section_index, section in enumerate(sections, start=1):
        visit(
            section,
            path=f"section-{section_index:03d}",
            parent_unit=None,
        )
    return tuple(units)


def parse_structure_units(
    *,
    document_id: str,
    version: str,
    source: Path,
    parser: StructureParser | None = None,
    max_units: int = 1000,
) -> tuple[StructureUnit, ...]:
    """Parse ``source`` into deterministic structure-aware cite units."""
    _validate_identity(document_id, version)
    if (
        not isinstance(max_units, int)
        or isinstance(max_units, bool)
        or max_units <= 0
    ):
        raise StructureUnitLimitError(
            "max_units must be a positive integer"
        )

    selected_parser = parser or _default_parser(source)
    try:
        sections = _coerce_sections(selected_parser.parse(source))
    except StructureUnitsError:
        raise
    except Exception as exc:
        raise StructureParseError(
            f"unable to parse {source}: {exc}"
        ) from exc

    return _flatten_sections(
        sections,
        document_id=document_id,
        version=version,
        max_units=max_units,
    )


def parse_document(
    *,
    document_id: str,
    version: str,
    source: Path,
    parser: StructureParser | None = None,
    max_units: int = 1000,
) -> tuple[StructureUnit, ...]:
    """Compatibility alias for callers that use document terminology."""
    return parse_structure_units(
        document_id=document_id,
        version=version,
        source=source,
        parser=parser,
        max_units=max_units,
    )


__all__ = [
    "ParsedSection",
    "PdfStructureParser",
    "StructureLocator",
    "StructureParser",
    "StructureUnit",
    "StructureUnitLimitError",
    "StructureUnitsError",
    "parse_document",
    "parse_structure_units",
]
