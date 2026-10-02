# 0096. A text region is a count, not a box; page text is carried by the page/OCR layer

Date: 2026-10-03
Status: accepted
Amends: 0089 (region vocabulary only; the layout pin itself is unchanged)

## Context

`g4-scanned-001` authors `region_kinds_expected: ["text", "table"]` on all three
of its pages. Its pages rasterise AAPL-2023Q3 pages 6, 9 and 10, so they do carry
text — and they do carry tables, which the pinned layout stage returns as 1 / 1 / 2
boxes. The `text` component is what cannot be satisfied, and the reason is a
contract fact rather than a measurement gap.

## Decision

**A `text` entry in a gold page's `region_kinds_expected` states what the rendered
page contains. It is not a box expectation, and it is not scored as one. A page's
text is carried where the product consumes it: the page/OCR layer, its character
counts, and the per-page quote.**

## Basis

The product's region vocabulary cannot hold a text box, and says so in the code:

- `src/fin/host/attachment_layout.py:85` —
  `_BOXED_KINDS: Final[tuple[str, ...]] = ("table", "figure")`, carrying the
  reason in its own comment: *"It counts text regions too, but a text region
  arrives as a count with no box, so it cannot become a Region."*
- `_regions_of` (`attachment_layout.py:963-981`) builds the `Region` list from
  `_BOXED_KINDS` alone, so no `text` box reaches a consumer.
- The measurement driver charges Docling's text items with `boxed=False`
  (`scripts/g4_pipeline/driver.py`, `region_counts_from_document`), so a text
  region exists only as a number on the wire.

A count cannot be cropped, sent to a vision call, or extracted from. Requiring a
`text` **box** on a raster page therefore asks for something the product's region
set cannot hold, and scoring it as unmet would record a product defect where
there is a vocabulary boundary.

**Where the product reads a page's text instead** — `src/fin/corpus/parsers/_pages.py`:

- `_one_pdf_page` takes `native_text` from the page source port
  (`source.native_text(page_no=...)`) and asks `detect_ocr_required` about it;
- on a collapsed text layer the page's frozen unit is built from the OCR read,
  and `_page` states the precedence: *"Native text wins: a citation is quoted from
  the layer that exists in the document, and what OCR read stays additive beside
  it."*
- `ParsedPage` carries `native_text_char_count` and `scanned_text`, and
  `ocr_applied` / `ocr_empty` name the difference between a read that produced
  nothing and a read that never happened.

So the text on a scanned page reaches the product as characters and as a frozen
unit — never as a layout box.

## Consequences

- The `text` entries in `region_kinds_expected` are **kept**. They remain the
  human-verified statement of what the page contains, and `known_gaps[3]` already
  states that the field is a presence/absence floor, not box-level ground truth.
- **No floor is lowered and nothing is deleted.** For `g4-scanned-001` the text
  floor is discharged through the channel that carries it: `native_text_chars 0`,
  OCR applied on all three pages, and all three `expected_quote` values present
  verbatim in the run's `page_NNNN.ocr.txt` sidecars.
- The boxed half of the same floor is unchanged and still measured: `table`
  1 / 1 / 2.
- A scorer reading a `text` entry must score it against the page/OCR layer
  (character counts, `expected_quote`), never against `region_counts.text`, which
  counts Docling's text *items* and is not a presence assertion about a box.
- `region_counts.text` stays in the record as what it is: a count of layout text
  items. This ADR does not change what that number means.

## Rejected alternative

Adding `"text"` to `_BOXED_KINDS` was considered and rejected. It is a
product-contract change with a wide blast radius: every consumer of `Region`
(`attachment_layout._regions_of`, the crop lane, the vision crop budget, and
`tests/host/test_attachment_layout.py`) would begin receiving regions it has
never been shaped for, and a text region on a page whose text layer is already
read would charge a vision call for pixels the page rule has covered. Nothing in
the product's two-rule OCR contract needs it: page text comes from the text
layer, chart text comes from figure crops (`crop_is_legible` / `crop_png`).
