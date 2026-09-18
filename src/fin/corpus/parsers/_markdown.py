"""Markdown parser: heading structure blocks -> frozen units (T6
granularity).

Rule (parse rule version ``parse-2``):

* ATX headings (``#``–``######`` at line start) start a new block; the
  heading line is the block's first line;
* blank lines drop out at freeze time (the normalization outlet's
  paragraph rule); content lines flow into the current block;
* consecutive blocks are merged under
  :data:`~fin.corpus.parsers._common.BLOCK_MERGE_CAP_CHARS` (the
  deterministic cap; a block larger than the cap is never split).

The heading regex is intentionally narrow (ATX only, leading ``#``
run of 1–6 followed by a space) so the split is deterministic and
code-fence ``#`` comments inside indented content are not headings.
"""

from __future__ import annotations

import re

from fin.corpus.errors import (
    MalformedSourceParseError,
    NoUnitsParseError,
)
from fin.corpus.parsers._common import (
    ParsedUnit,
    build_unit,
    merge_blocks,
)

_ATX_HEADING_RE = re.compile(r"^#{1,6} .*$")


def _split_blocks(raw: str) -> list[str]:
    """Split raw markdown into heading-scoped blocks (raw text, not
    yet frozen)."""
    blocks: list[str] = []
    current: list[str] = []
    for line in raw.splitlines():
        if _ATX_HEADING_RE.match(line):
            if current:
                blocks.append("\n".join(current))
                current = []
        current.append(line)
    if current:
        blocks.append("\n".join(current))
    return blocks


def parse_markdown(
    *,
    source_bytes: bytes,
    source_sha256: str,
    document_id: str,
    version: str,
) -> tuple[ParsedUnit, ...]:
    """Parse ``source_bytes`` as Markdown into structure-block units."""
    try:
        raw = source_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MalformedSourceParseError(
            f"markdown source for {document_id!r} is not valid UTF-8: "
            f"{exc}"
        ) from exc
    units = [
        build_unit(
            fmt="md",
            source_sha256=source_sha256,
            document_id=document_id,
            version=version,
            unit=ordinal,
            text=block,
        )
        for ordinal, block in enumerate(
            merge_blocks(_split_blocks(raw)), start=1
        )
    ]
    if not units:
        raise NoUnitsParseError(
            f"markdown source for {document_id!r} produced no non-empty "
            "frozen units"
        )
    return tuple(units)


__all__ = ["parse_markdown"]
