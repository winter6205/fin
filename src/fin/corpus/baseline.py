"""Cross-repo baseline-section verifier (plan T4 / spec §B6).

The "跨仓对齐基线" section of ``specs/corpus-retrieval.md`` (fin side)
and of the retrieval-service spec (``retrieval-stack.md`` in the
iknow-memory repo) must be byte-identical (§B6, SC41). This module is
the one-command verifier:

    uv run python -m fin.corpus.baseline <fin_spec> <retrieval_spec>

Section-extraction rule, verbatim from §B6: the section body starts
AFTER the ``## 跨仓对齐基线`` heading line (the heading itself is not
hashed), ends before the next ``## `` heading line, with leading and
trailing blank lines removed. Each side's body is hashed with sha256;
equal hashes exit 0, unequal hashes exit 1 with BOTH hashes printed to
stdout (never just "mismatch").

Named failures (nonzero exit + explicit stderr message, no silent
skips):

* ``BaselineFileMissingError``  — a side file does not exist (exit 2).
* ``BaselineSectionMissingError`` — a side file has no baseline
  section (exit 3).

The module is stdlib-only (``hashlib`` / ``pathlib`` / ``argparse``):
no network access, no service processes — safe for the default
``uv run pytest -q`` suite.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

# §B6 names the section by its heading. The fin spec's heading carries a
# parenthetical suffix ("## 跨仓对齐基线（共享章节，两仓逐字一致）"), so
# the heading line is matched by this prefix; h3 lines ("### ...") never
# match because their third character is "#", not the space in "## ".
_BASELINE_HEADING_PREFIX = "## 跨仓对齐基线"
_NEXT_H2_PREFIX = "## "


class BaselineFileMissingError(FileNotFoundError):
    """A spec file given on the command line does not exist."""


class BaselineSectionMissingError(ValueError):
    """A spec file contains no ``## 跨仓对齐基线`` heading line."""


def extract_baseline_section(text: str) -> str:
    """Extract the B6 baseline-section body from one spec's text.

    Rule (§B6, verbatim): the body starts on the line AFTER the
    ``## 跨仓对齐基线`` heading line, ends before the next ``## ``
    heading line (h3 ``### ...`` subsections belong to the body), with
    leading and trailing blank lines removed. The heading line itself is
    never part of the returned body.

    Raises ``BaselineSectionMissingError`` when the heading is absent.
    """
    lines = text.splitlines()
    start: int | None = None
    for i, line in enumerate(lines):
        if line.startswith(_BASELINE_HEADING_PREFIX):
            start = i
            break
    if start is None:
        raise BaselineSectionMissingError(
            f"no {_BASELINE_HEADING_PREFIX!r} heading line found in spec text"
        )
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].startswith(_NEXT_H2_PREFIX):
            end = j
            break
    body = lines[start + 1 : end]
    while body and not body[0].strip():
        body.pop(0)
    while body and not body[-1].strip():
        body.pop()
    return "\n".join(body)


def section_sha256(spec_path: Path) -> str:
    """sha256 of the baseline-section body of one spec file."""
    if not spec_path.is_file():
        raise BaselineFileMissingError(f"spec file not found: {spec_path}")
    text = spec_path.read_text(encoding="utf-8")
    return hashlib.sha256(extract_baseline_section(text).encode("utf-8")).hexdigest()


def main(argv: list[str] | None = None) -> int:
    """CLI entry: compare the baseline sections of two spec files.

    Exit codes: 0 = hashes equal; 1 = mismatch (both hashes printed to
    stdout); 2 = a side file is missing; 3 = a side file lacks the
    baseline section. Failures print ``error: ...`` on stderr.
    """
    parser = argparse.ArgumentParser(
        prog="python -m fin.corpus.baseline",
        description=(
            "Verify that the '跨仓对齐基线' sections of two spec files "
            "are byte-identical (spec §B6): extract each section body, "
            "sha256 both, exit 0 on equality."
        ),
    )
    parser.add_argument("fin_spec", help="path to the fin-side spec file")
    parser.add_argument(
        "retrieval_spec", help="path to the retrieval-service-side spec file"
    )
    args = parser.parse_args(argv)

    fin_path = Path(args.fin_spec)
    retrieval_path = Path(args.retrieval_spec)
    try:
        fin_sha = section_sha256(fin_path)
    except BaselineFileMissingError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except BaselineSectionMissingError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    try:
        retrieval_sha = section_sha256(retrieval_path)
    except BaselineFileMissingError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except BaselineSectionMissingError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3

    print(f"fin spec sha256:       {fin_sha} ({fin_path})")
    print(f"retrieval spec sha256: {retrieval_sha} ({retrieval_path})")
    if fin_sha != retrieval_sha:
        print("status: mismatch", file=sys.stderr)
        return 1
    print("status: match")
    return 0


if __name__ == "__main__":
    sys.exit(main())
