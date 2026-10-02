#!/usr/bin/env python3
"""Re-author g4-mixed-chart-001 page 2 as the RULED table its source page is.

Why this exists
---------------
Fixture page 2 reproduces AAPL-2023Q3 page 4 (Condensed Consolidated Statements
of Operations) as *text only*: 98 positioned ``Tj`` runs and zero path
operators. The source page it is modelled on is a ruled table — pdfminer
reports 154 thin horizontal rules, 76 column separators and 240 grey band
fills on that page (measured; recorded in
``docs/validation/2026-10-03-c1-gold-residual.md``). The pinned layout stage
returns a ``table`` region for the source page and ``table 0`` for the
borderless reproduction, so the fixture was not representative of the document
it claims to be, and no gold expectation could be scored against it.

What this writes
----------------
The same 98 text runs, unchanged, plus the source page's rule geometry placed
on the fixture's own row positions:

* a rule under the two column-group captions and under the four date captions,
* single rules above and below each subtotal row, a paired rule under the
  double-ruled totals, and the double rule over the net-income block,
* the source's alternating grey row bands, and
* column separators between the four numeric columns.

Coordinates are the fixture's own: every rule is placed relative to a text row
already in the page, so this adds the geometry the source carries without
moving, deleting, or re-wording a single run.

The rewrite is deterministic — no timestamps, no randomness, no external
state — and ``--check`` proves two builds agree byte for byte.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path

# The fixture's own table extent, in points, from the text runs already in the
# page. Rules are drawn in the same user space as the text (y up).
LEFT_EDGE_PT = 40.0
RIGHT_EDGE_PT = 594.0
COLUMN_SEPARATORS_PT = (376.0, 451.0, 526.0)

ROW_HEIGHT_PT = 15.0  # the fixture's own row pitch
ROW_PITCH_PT = 15.0  # baseline-to-baseline distance between rows

# Rules named by the text of the row they bracket, and the gap the rule sits
# in. ``above``/``below`` place the rule half a row pitch clear of the named
# row's baseline, which is the gap between two rows rather than a line through
# one. The source underlines the column captions, brackets every subtotal, and
# double-rules the net-income block; this reproduces that shape on the rows
# the fixture already carries.
#
# The baseline is named rather than inferred because two rows share a label on
# this page (``Products`` heads both Net sales and Cost of sales) and the
# column captions sit to the right of the label column, so a label alone does
# not identify a row.
ROW_RULES: tuple[tuple[str, float, str], ...] = (
    ("Three Months Ended", 646.0, "below"),
    ("July 1, 2023", 632.0, "below"),
    ("Total net sales", 561.0, "above"),
    ("Total net sales", 561.0, "below"),
    ("Total cost of sales", 499.0, "above"),
    ("Total cost of sales", 499.0, "below"),
    ("Gross margin", 484.0, "above"),
    ("Gross margin", 484.0, "below"),
    ("Total operating expenses", 422.0, "above"),
    ("Total operating expenses", 422.0, "below"),
    ("Operating income", 407.0, "above"),
    ("Operating income", 407.0, "below"),
    ("Income before provision for income taxes", 377.0, "above"),
    ("Net income", 347.0, "above"),
    ("Net income", 347.0, "below"),
)

# The four numeric columns' own x spans, from the page's cell runs. The source
# shades each cell block separately rather than one band across the row, so
# the gaps between columns stay unshaded.
COLUMN_BANDS_PT: tuple[tuple[float, float], ...] = (
    (299.0, 374.0),
    (379.0, 449.0),
    (454.0, 524.0),
    (529.0, 594.0),
)

# The rows the source page shades, reproduced over the same rows here, named by
# their label and baseline for the same reason as the rules above.
SHADED_ROWS: tuple[tuple[str, float], ...] = (
    ("Products", 591.0),
    ("Total net sales", 561.0),
    ("Services", 576.0),
    ("Gross margin", 484.0),
    ("Selling, general and administrative", 437.0),
    ("Operating income", 407.0),
    ("Other income/\\(expense\\), net", 392.0),
    ("Provision for income taxes", 362.0),
    ("Basic", 315.0),
    ("Diluted", 300.0),
)

# The two rows the column separators run between, named as the rules are.
SEPARATOR_TOP_ROW = "Products"
SEPARATOR_TOP_BASELINE_PT = 591.0
SEPARATOR_BOTTOM_ROW = "Diluted"
SEPARATOR_BOTTOM_BASELINE_PT = 300.0


def _row_baselines(content: str) -> dict[tuple[str, float], float]:
    """Every ``(label, baseline)`` the page carries, from its own text runs.

    A row is identified by its label *and* its baseline, because this page has
    two rows labelled ``Products`` and its column captions sit to the right of
    the label column; either alone would name the wrong row.
    """
    rows: dict[tuple[str, float], float] = {}
    for match in re.finditer(
        r"1 0 0 1 ([\d.]+) ([\d.]+) Tm \((.*?)\) Tj ET", content
    ):
        y, text = float(match.group(2)), match.group(3)
        rows[(text, y)] = y
    return rows


def _require(rows: dict[tuple[str, float], float], text: str, baseline: float) -> float:
    """The baseline of one named row, or a refusal naming what is missing."""
    if (text, baseline) not in rows:
        raise SystemExit(
            f"page 2 carries no row {text!r} at baseline {baseline}; "
            "this builder is pinned to the committed fixture's own rows"
        )
    return baseline


def _rule_operators(content: str) -> str:
    """Path operators for the source page's rule geometry on this page's rows.

    Two details of the source page are reproduced rather than approximated,
    because both are what makes it read as a table to a layout model:

    * the rules sit in the gap *between* rows, never through a row's text. The
      fixture's row pitch is 15 pt and its 8 pt text is about 9 pt tall, so a
      rule is placed at the midpoint between two baselines (or half a gap
      outside the first/last row of a block) instead of at a fixed offset.
    * the grey bands are per-column blocks, not one band across the whole
      row. The source shades each numeric cell separately, leaving the gaps
      between columns unshaded.
    """
    rows = _row_baselines(content)

    # Every numeric column's own x span, read off the page's own text runs: a
    # cell's x is its left edge and the column runs to the separator after it.
    out: list[str] = ["0.6 w"]
    for text, baseline, where in ROW_RULES:
        y = _require(rows, text, baseline) + (ROW_PITCH_PT / 2 if where == "above" else -ROW_PITCH_PT / 2)
        out.append(f"{LEFT_EDGE_PT:.1f} {y:.1f} m {RIGHT_EDGE_PT:.1f} {y:.1f} l S")

    for text, baseline in SHADED_ROWS:
        y = _require(rows, text, baseline) - ROW_PITCH_PT / 2 + 1.0
        for x0, x1 in COLUMN_BANDS_PT:
            out.append("0.92 g")
            out.append(f"{x0:.1f} {y:.1f} {x1 - x0:.1f} {ROW_HEIGHT_PT:.1f} re f")
    out.append("0 g")  # hand the page back with black ink, as it was

    top = _require(rows, SEPARATOR_TOP_ROW, SEPARATOR_TOP_BASELINE_PT) - 3.0
    bottom = _require(rows, SEPARATOR_BOTTOM_ROW, SEPARATOR_BOTTOM_BASELINE_PT) + 8.0
    for x in COLUMN_SEPARATORS_PT:
        out.append(f"{x:.1f} {top:.1f} m {x:.1f} {bottom:.1f} l S")
    return "\n".join(out) + "\n"


def _split_document(raw: bytes) -> tuple[bytes, int, int, bytes, bytes]:
    """Split the file into (pre-xref body, xref offset, trailer dict, tail).

    Returns the body up to ``xref``, the old ``startxref`` value, the trailer
    dictionary, and the bytes after ``%%EOF`` (empty for a well-formed file).
    """
    xref_at = raw.find(b"xref")
    if xref_at < 0:
        raise SystemExit("fixture has no xref table")
    startxref = re.search(rb"startxref\s+(\d+)", raw)
    if startxref is None:
        raise SystemExit("fixture has no startxref")
    trailer = re.search(rb"trailer\s*(<<.*?>>)", raw, re.S)
    if trailer is None:
        raise SystemExit("fixture has no trailer dictionary")
    return raw[:xref_at], int(startxref.group(1)), trailer.group(1), b""


def _rebuild(src: Path, dst: Path) -> None:
    raw = src.read_bytes()
    body, _, trailer, _ = _split_document(raw)

    stream = re.search(rb"9 0 obj\s*<< /Length (\d+) >>\s*stream\r?\n", body)
    if stream is None:
        raise SystemExit(f"{src} has no page-2 content stream in the expected shape")
    start = stream.end()
    end = body.find(b"endstream", start)
    content = body[start:end].decode("latin-1")

    # Rules are prepended so the text runs stay the page's own last word: a
    # reader extracting text is unaffected by anything the graphics add.
    new_content = (_rule_operators(content) + content).encode("latin-1")
    new_body = (
        body[: stream.start()]
        + f"9 0 obj\n<< /Length {len(new_content)} >>\nstream\n".encode("latin-1")
        + new_content
        + b"endstream\nendobj\n"
        + body[end + len(b"endstream") :]
    )

    # The stream grew, so every offset after it moved: the table is rebuilt
    # from the file's real object positions rather than patched.
    offsets: dict[int, int] = {}
    for match in re.finditer(rb"(?m)^(\d+) 0 obj", new_body):
        offsets[int(match.group(1))] = match.start()
    if not offsets:
        raise SystemExit("fixture has no objects")
    highest = max(offsets)

    table = [b"xref\n", f"0 {highest + 1}\n".encode("latin-1"), b"0000000000 65535 f \n"]
    for number in range(1, highest + 1):
        offset = offsets.get(number)
        table.append(
            b"0000000000 65535 f \n"
            if offset is None
            else f"{offset:010d} 00000 n \n".encode("latin-1")
        )
    table_bytes = b"".join(table)

    trailer = re.sub(
        rb"/Size \d+", b"/Size " + str(highest + 1).encode("latin-1"), trailer, count=1
    )
    startxref_at = len(new_body)
    dst.write_bytes(
        new_body
        + table_bytes
        + b"trailer\n"
        + trailer
        + f"\nstartxref\n{startxref_at}\n%%EOF\n".encode("latin-1")
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("dest", type=Path, nargs="?")
    parser.add_argument(
        "--check", action="store_true", help="fail unless two builds agree byte for byte"
    )
    args = parser.parse_args()

    dest = args.dest or args.source.with_suffix(".ruled.pdf")
    _rebuild(args.source, dest)
    print(f"{dest} {dest.stat().st_size} bytes sha256={hashlib.sha256(dest.read_bytes()).hexdigest()}")

    if args.check:
        again = dest.with_suffix(".again.pdf")
        _rebuild(args.source, again)
        if again.read_bytes() != dest.read_bytes():
            print("NOT REPRODUCIBLE", file=sys.stderr)
            return 1
        again.unlink()
        print("reproducible: two builds agree byte for byte")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
