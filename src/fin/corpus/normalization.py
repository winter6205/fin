"""Frozen-text normalization: the single rule authority (SC17).

Spec SC17: the normalization of frozen text — whitespace, Unicode
compatibility, hyphens, soft line breaks — is produced by **one** rule
outlet, and frozen-unit text and chunk text share it. The citation gate
(``fin.host.citation_gate``) matches spans verbatim (a plain ``in``
substring search with no normalization of its own), so this module is
the gate's compatibility surface: both sides of every substring
assertion must come from here.

The outlet is the pure function :func:`normalize_frozen_text`. Its
output is a fixed point (the function is idempotent), which is what
makes the two consumption paths share one rule:

* **Frozen-unit path** (T6): a parser's raw extractor output is frozen
  through this function — the result *is* the frozen-unit text.
* **Chunk path** (T11): chunks are contiguous slices of the frozen-unit
  text, so they are already in normal form; re-running the outlet on a
  chunk is a no-op (test-proven). No second replacement logic may exist
  anywhere else in the repo.

The four categories and what each does (applied in this order):

1. **Unicode** — NFKC compatibility decomposition: full-width forms
   (U+FF0D full-width hyphen included), ligatures (``ﬁ`` → ``fi``),
   circled/compatibility digits, and space variants that have NFKC
   decompositions (U+00A0 NBSP, U+2000–U+200A, U+202F, U+3000) map to
   their ASCII equivalents.
2. **Invisible break-control characters** (part of the hyphen / break
   family) — U+00AD soft hyphen, U+200B zero-width space, U+FEFF
   zero-width no-break space, U+200E/U+200F bidi marks are removed
   outright: they are layout hints, not content.
3. **Hyphens** — hyphen-like variants U+2010 HYPHEN, U+2011
   NON-BREAKING HYPHEN and U+FE63 SMALL HYPHEN-MINUS unify to ASCII
   U+002D. Em/en dashes (U+2013/U+2014) are deliberately left alone —
   they are punctuation with distinct meaning, not hyphenation.
4. **Soft line breaks + whitespace** — the whole line-break family
   (CRLF, CR, VT U+000B, FF U+000C, NEL U+0085, LS U+2028,
   PS U+2029, and the U+001C–U+001E group) normalizes to LF U+000A;
   within each line, runs of whitespace collapse to a single space and
   line edges are trimmed; blank lines are dropped (paragraph
   segmentation belongs to the parser, pre-freeze — the frozen text
   keeps one line per source line).

``NORMALIZATION_RULE_VERSION`` is the version stamp that travels with
every frozen unit / chunk (the idempotent-key content hash mixes it
in, and a version bump triggers an in-place full rebuild per the
spec's "就地全量重建" rule).
"""

from __future__ import annotations

import re
import unicodedata
from typing import Final

NORMALIZATION_RULE_VERSION: Final[str] = "norm-1"

# Invisible break-control characters (category 2): removed, never
# converted — they carry no content and would silently break verbatim
# span matching if left to vary.
_INVISIBLE_CHARS_RE: Final[re.Pattern[str]] = re.compile(
    "[\u00ad\u200b\u200e\u200f\ufeff]"
)

# Hyphen-like variants (category 3): unified to ASCII HYPHEN-MINUS.
# U+FF0D and the other full-width forms need no entry here — NFKC
# (category 1) already maps them to U+002D.
_HYPHEN_TABLE: Final[dict[int, str]] = {
    0x2010: "\u002d",  # HYPHEN
    0x2011: "\u002d",  # NON-BREAKING HYPHEN
    0xFE63: "\u002d",  # SMALL HYPHEN-MINUS
}

# Intra-line whitespace runs (category 4): any whitespace except the
# line separator itself collapses to one space. ``\s`` covers the
# remaining Unicode spaces not handled by NFKC and the U+001C–U+001F
# control group.
_INTRA_LINE_WS_RE: Final[re.Pattern[str]] = re.compile(r"[^\S\n]+")


def normalize_frozen_text(text: str) -> str:
    """Freeze raw extractor text into the single normalized form.

    This is the only character-replacement rule for frozen text in the
    repo (SC17). Both the frozen-unit path (T6 parsers) and the chunk
    path (T11 slices of frozen-unit text) go through it; the function
    is idempotent, so a chunk sliced from frozen text is a fixed point.

    Raises
    ------
    TypeError
        If ``text`` is not a ``str`` (defensive: a non-string from a
        parser bug must fail loudly, not coerce).
    """
    if not isinstance(text, str):
        raise TypeError(
            f"normalize_frozen_text expects str, got {type(text).__name__}"
        )

    # 1. Unicode compatibility (NFKC).
    text = unicodedata.normalize("NFKC", text)
    # 2. Invisible break-control characters out.
    text = _INVISIBLE_CHARS_RE.sub("", text)
    # 3. Hyphen variants to ASCII HYPHEN-MINUS.
    text = text.translate(_HYPHEN_TABLE)
    # 4. Soft line breaks to LF + intra-line whitespace collapse +
    #    blank-line drop, line by line.
    lines = (
        _INTRA_LINE_WS_RE.sub(" ", line).strip()
        for line in text.splitlines()
    )
    return "\n".join(line for line in lines if line)


__all__ = [
    "NORMALIZATION_RULE_VERSION",
    "normalize_frozen_text",
]
