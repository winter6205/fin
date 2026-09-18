"""The extract-record contract on the window layer (SC6 / SC7).

``test_window_tree.py`` locks the window rule itself (SC2–SC5 / SC8);
``test_chunking.py`` locks the two-level block shape and the child
overlap identity (SC4). This module locks the two success criteria
that are stated about the **whole 切分 product** and the **extract
source**, not about one window:

* SC6 — two 切分 calls on the same (frozen bytes + rule version +
  parameter-table version) produce **byte-identical** window ids/text
  and child ids/text. The spec's Boundaries name the **concurrent**
  class explicitly ("two 切分 runs on the same frozen bytes yield
  identical trees"), so the determinism is asserted from several
  threads at once, not only from two sequential calls. The
  parameter-table version stamp is asserted to be the one constant
  across every run, so a per-call stamp cannot hide behind "the two
  calls happened to agree".
* SC7 — extract-record derivation reads **frozen text**: an HTML /
  Markdown fixture's heading is the title source; an OCR-write-back
  fixture with no heading tags yields title-less windows that still
  share the one frozen unit; and the split path must not call OCR or
  a vision heading model (asserted, not assumed). Conversely the
  ingest OCR EXIT stays the single typed ``ocr_required_unavailable``
  — extract invents no second OCR EXIT.

Why the negative lock is a subprocess
-------------------------------------
Default pytest imports ``fin.corpus.ingest`` / ``fin.corpus.parsers``
in sibling modules, which pulls ``fin.corpus.ocr`` into
``sys.modules`` before this file runs. An in-process
``monkeypatch.delitem(sys.modules, ...)`` would be undone by the next
sibling import and would measure import bookkeeping, not the split
path. The subprocess gets a fresh ``sys.modules`` (the established
pattern of ``tests/test_ask_path_contract.py``) and the poison module
below is installed *before* the split path is imported, so a real OCR
call — import or attribute — trips it. The assertion is deliberately
specific: an OCR call anywhere on the split path fails this test, and
nothing else does.

A known gap this module records rather than hides
-------------------------------------------------
SC7 says HTML headings are the title source. A frozen HTML unit
carries the heading **text** only (``parse_html`` flattens the tags —
``_html.py``'s heading block contributes its text as the block's first
line), so the window rule sees a bare line like ``Revenue``. The
window rule's classifier accepts markdown ATX and uppercase-only
lines, so ``<h2>Revenue</h2>`` lands on ``title is None`` while
``<h2>REVENUE</h2>`` lands on ``"REVENUE"``. Every committed HTML
dataset fixture is of the first shape (``<h2>Unit 1</h2>``), so the
gap is load-bearing, not hypothetical.

That is a real contract gap, reported and not fixed here (the fix
touches ``src/fin/corpus/window.py`` / the parsers, owned by another
worker). The HTML test below uses an uppercase heading fixture, so it
locks the *convention* — heading text becomes the title and tags never
leak into the frozen text — and
``test_sc7_html_title_case_heading_becomes_the_window_title`` states
the SC7 claim positively under a strict ``xfail``, so a later fix
flips it to XPASS loudly instead of changing a title silently.
"""

from __future__ import annotations

import ast
import concurrent.futures
import subprocess
import sys
import textwrap
from dataclasses import replace
from pathlib import Path

import pytest

from fin.corpus.chunking import split_document, split_unit
from fin.corpus.determinism import document_chunking_product, unit_chunking_product
from fin.corpus.extracted import ExtractedPage, Locator
from fin.corpus.normalization import normalize_frozen_text
from fin.corpus.ocr import OcrRequiredUnavailableError
from fin.corpus.ocr_writeback import selective_ocr_write_back
from fin.corpus.params import TABLE_VERSION
from fin.corpus.parsers import parse_document
from fin.corpus.parsers._common import build_unit
from fin.corpus.window import (
    EXTRACT_WINDOW_CHARS,
    count_window_tokens,
    is_heading_line,
    window_spans,
)

DOCUMENT_ID = "WINDOW-CONTRACT"
VERSION = "a" * 64

# The walk in the negative lock imports every fin.corpus module; keep
# the poison scan to the libraries an OCR / vision call would need, so
# an unrelated module appearing in sys.modules does not fail it.
_OCR_VISION_ROOTS = (
    "pytesseract",
    "PIL",
    "cv2",
    "easyocr",
    "paddleocr",
    "torch",
    "transformers",
)


def _sentences(n: int, start: int = 0) -> str:
    """n deterministic ~100-char sentences, each ending in a period."""
    return "".join(
        f"Sentence {i:03d} reports that operating revenue grew in the "
        "quarter and the gross margin improved again. "
        for i in range(start, start + n)
    )


def _parse_md(source: bytes):
    return parse_document(
        source, fmt="md", document_id=DOCUMENT_ID, version=VERSION
    )


def _parse_html(source: bytes):
    return parse_document(
        source, fmt="html", document_id=DOCUMENT_ID, version=VERSION
    )


# ---------------------------------------------------------------------------
# SC6: concurrent 切分 on the same frozen bytes is byte-identical
# ---------------------------------------------------------------------------


def _sc6_document_fixture():
    """A two-unit markdown document whose first unit overflows.

    The overflow matters: it exercises the window path (more than one
    window per unit, adjacent overlap, cross-window child packing), so
    a race in the overflow branch cannot pass behind the single-window
    path. The second unit is small, so the tree carries both shapes.
    """
    source = (
        "## Alpha\n"
        + _sentences(60)
        + "\n\nA short standalone paragraph about the filing.\n\n"
        + "## Beta\n"
        + _sentences(20)
    ).encode("utf-8")
    return _parse_md(source)


def _tree_snapshot(chunking) -> list[tuple]:
    """Every identity-bearing field of a ``DocumentChunking`` as plain
    tuples.

    ``UnitChunking.__eq__`` is dataclass equality; this list is the
    explicit, readable surface SC6 names — window ids/text and child
    ids/text plus the ordinals and the parent linkage that tie them
    together. Two snapshots compare equal iff the trees are
    byte-identical in every one of those fields.
    """
    snapshot: list[tuple] = []
    for uc in chunking.units:
        snapshot.append(
            (
                uc.locator.document_id,
                uc.locator.version,
                uc.locator.unit,
                uc.unit_text,
                uc.params_table_version,
                tuple(
                    (p.block_id, p.text, p.ordinal, p.parent_block_id, p.title)
                    for p in uc.parents
                ),
                tuple(
                    (c.block_id, c.text, c.ordinal, c.parent_ordinal,
                     c.parent_block_id)
                    for c in uc.children
                ),
            )
        )
    return snapshot


def test_sc6_concurrent_splits_are_byte_identical() -> None:
    # SC6 / spec Boundaries "concurrent": two 切分 runs on the same
    # frozen bytes yield identical trees. Same frozen bytes + same rule
    # version + same parameter-table version (the fixture is parsed
    # once and split many times), several threads at once — the
    # determinism must not depend on a single-threaded call order or on
    # a warm per-process cache.
    parsed = _sc6_document_fixture()
    assert len(parsed.units) >= 2, "precondition: a multi-unit document"
    baseline = split_document(parsed, doc_type="narrative")
    assert any(len(uc.parents) >= 2 for uc in baseline.units), (
        "precondition: the fixture exercises the window overflow path, "
        "not only the single-window path"
    )
    assert all(uc.children for uc in baseline.units)

    def split_again(_: int):
        return split_document(parsed, doc_type="narrative")

    workers, tasks = 4, 8
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(split_again, range(tasks)))
    assert len(results) == tasks

    expected = _tree_snapshot(baseline)
    for index, result in enumerate(results):
        assert result == baseline, f"task {index}: UnitChunking equality broke"
        assert _tree_snapshot(result) == expected, (
            f"task {index}: a window/child id, text, ordinal or parent "
            "link differs from the baseline tree"
        )
        # The stamp that rides the product is the params-table constant,
        # not a per-call value: SC6's "same parameter-table version" is
        # only meaningful if the version is the table's own.
        for uc in result.units:
            assert uc.params_table_version == TABLE_VERSION
            assert uc.parse_rule_version == parsed.parse_rule_version


def test_sc6_concurrent_split_unit_results_are_byte_identical() -> None:
    # The same lock one level down: ``split_unit`` on one frozen unit
    # from many threads. ``UnitChunking`` equality plus the explicit
    # id/text/ordinal/parent lists, so a nondeterministic block id can
    # never pass as "equal objects".
    unit = _sc6_document_fixture().units[0]
    baseline = split_unit(unit, doc_type="narrative")
    assert len(baseline.parents) >= 2, "precondition: an overflow unit"
    product = unit_chunking_product(baseline)

    def split_again(_: int):
        return split_unit(unit, doc_type="narrative")

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(split_again, range(8)))

    for index, result in enumerate(results):
        assert result == baseline, f"task {index}: UnitChunking equality broke"
        assert unit_chunking_product(result) == product, (
            f"task {index}: the canonical product (text / ids / order / "
            "metadata) differs from the baseline"
        )
        assert [p.block_id for p in result.parents] == [
            p.block_id for p in baseline.parents
        ]
        assert [p.text for p in result.parents] == [
            p.text for p in baseline.parents
        ]
        assert [c.block_id for c in result.children] == [
            c.block_id for c in baseline.children
        ]
        assert [c.text for c in result.children] == [
            c.text for c in baseline.children
        ]


def test_sc6_sequential_determinism_asserts_ids_and_text_not_only_equality() -> None:
    # The sequential half of SC6, with the explicit lists: ids and text
    # are asserted directly (not only ``first == second``), so the test
    # still names what byte-identity means if ``__eq__`` ever loosens.
    parsed = _sc6_document_fixture()
    first = split_document(parsed, doc_type="narrative")
    second = split_document(parsed, doc_type="narrative")
    assert document_chunking_product(first) == document_chunking_product(second)
    assert _tree_snapshot(first) == _tree_snapshot(second)
    # The params-table stamp is the table's constant on every unit.
    assert {uc.params_table_version for uc in first.units} == {TABLE_VERSION}


def test_sc6_ids_follow_the_version_stamps() -> None:
    # SC6 names the version triple as the determinism input: a params
    # bump (a different parameter-table version with the same bytes)
    # MUST re-key the blocks, otherwise an in-place rebuild after a
    # table bump could serve stale ids. This is the negative side of
    # "same bytes + same versions → same ids": same bytes + a DIFFERENT
    # version → different ids.
    unit = _sc6_document_fixture().units[0]
    current = split_unit(unit, doc_type="narrative")
    bumped = split_unit(
        replace(unit, params_table_version="params-99"), doc_type="narrative"
    )
    assert bumped.params_table_version == "params-99"
    assert [p.text for p in bumped.parents] == [p.text for p in current.parents]
    assert [p.block_id for p in bumped.parents] != [
        p.block_id for p in current.parents
    ]
    assert [c.block_id for c in bumped.children] != [
        c.block_id for c in current.children
    ]


# ---------------------------------------------------------------------------
# SC7: extract-record derivation reads frozen text (HTML / Markdown)
# ---------------------------------------------------------------------------


def _big_body(sentence_count: int = 60) -> str:
    """A body past the window cap, so overflow windows exist too."""
    return _sentences(sentence_count)


def test_sc7_markdown_heading_becomes_the_window_title() -> None:
    # SC7: extract-record derivation on a frozen Markdown fixture uses
    # the heading as title. The fixture goes through the REAL parser
    # entry point (``parse_document``), so the title's source is
    # genuinely the md heading line, not a hand-built string.
    parsed = _parse_md(("## Revenue\n" + _big_body()).encode("utf-8"))
    assert len(parsed.units) == 1
    unit = parsed.units[0]
    assert unit.text.startswith("## Revenue")
    chunked = split_unit(unit, doc_type="narrative")
    assert chunked.parents, "the unit must produce windows"
    assert {p.title for p in chunked.parents} == {"Revenue"}
    # every window of the heading shares the title; later overflow
    # windows do not re-emit the heading line as a second body cutter
    assert len(chunked.parents) >= 2, "precondition: the body overflows"
    assert chunked.parents[0].text.startswith("## Revenue")
    assert all(not p.text.startswith("## Revenue") for p in chunked.parents[1:])


def test_sc7_html_heading_tag_becomes_the_window_title() -> None:
    # SC7: same lock for HTML. ``parse_html`` turns the ``<h2>`` into
    # the unit's first line (the tag itself never reaches the frozen
    # text — SC7's contiguity rule), and the window rule reads that
    # line as the governing heading.
    #
    # Scope honesty: this fixture is UPPERCASE on purpose — the
    # classifier's uppercase arm is the only HTML heading shape that
    # resolves a title today. It does NOT claim the dataset's shape:
    # every committed HTML fixture is ``<h2>Unit N</h2>`` (title
    # case), and that shape is the documented SC7 gap pinned by the
    # strict xfail below — the title stays None there until the
    # spec-side classifier decision lands.
    source = (
        "<html><body><main>"
        "<h2>REVENUE</h2><p>" + _big_body() + "</p>"
        "<h2>COSTS</h2><p>Cost of sales were flat.</p>"
        "</main></body></html>"
    ).encode("utf-8")
    parsed = _parse_html(source)
    # The oversized REVENUE section stands alone (a block past the
    # merge cap is never merged); the small COSTS section becomes the
    # next unit. Read each heading's own unit, so the merge behaviour
    # is not what this test pins.
    revenue_unit = next(
        candidate
        for candidate in parsed.units
        if candidate.text.startswith("REVENUE")
    )
    assert "<h2>" not in revenue_unit.text, "tags never enter the frozen text"
    revenue = split_unit(revenue_unit, doc_type="narrative")
    revenue_titles = [p.title for p in revenue.parents]
    assert set(revenue_titles) == {"REVENUE"}, revenue_titles
    # the revenue heading's body overflows, so several windows share it
    assert len(revenue_titles) >= 2, revenue_titles
    assert is_heading_line("REVENUE"), "precondition: the classifier arm"
    # the second heading's own unit carries its own title
    costs_unit = next(
        candidate
        for candidate in parsed.units
        if candidate.text.startswith("COSTS")
    )
    costs = split_unit(costs_unit, doc_type="narrative")
    assert [p.title for p in costs.parents] == ["COSTS"]


@pytest.mark.xfail(
    strict=True,
    reason=(
        "SC7 gap (reported, not fixed here): HTML heading tags are the "
        "title source, but the frozen unit drops the tag and the window "
        "classifier does not recognize a title-case line, so "
        "<h2>Revenue</h2> yields title None. Remove this mark when the "
        "fix lands (strict: it will XPASS the moment it does)."
    ),
)
def test_sc7_html_title_case_heading_becomes_the_window_title() -> None:
    # The SC7 contract stated positively: a reverse-case HTML heading
    # yields the heading text as the title. It does not hold today —
    # the frozen HTML unit drops the tags (``_html.py`` contributes
    # only the heading text as the block's first line), so the window
    # rule sees a bare ``Revenue`` line, and its classifier accepts
    # markdown ATX and UPPERCASE-only lines but not a title-case line.
    # Every committed HTML dataset fixture is this shape
    # (``<h2>Unit 1</h2>``), so the gap is load-bearing.
    source = (
        "<html><body><main>"
        "<h2>Revenue</h2><p>Total net sales increased.</p>"
        "</main></body></html>"
    ).encode("utf-8")
    parsed = _parse_html(source)
    unit = parsed.units[0]
    assert unit.text.startswith("Revenue")
    windows = window_spans(unit.text)
    assert [w.title for w in windows] == ["Revenue"]
    assert is_heading_line("Revenue")


def test_sc7_ocr_write_back_windows_share_the_one_frozen_unit() -> None:
    # SC7: an OCR-write-back fixture has no heading tags, so title may
    # be empty and overflow windows still share that 冻结单元 plus
    # 资料主体. The frozen text IS the body: extract reads it, it does
    # not OCR again — no engine is passed anywhere below.
    ocr_text = "\n".join(
        f"Consolidated statement line {i:03d} reports net sales of "
        f"{i * 7} million dollars for the quarter and operating income "
        f"of {i * 3} million."
        for i in range(1, 121)
    )
    page = ExtractedPage(
        locator=Locator(DOCUMENT_ID, VERSION, 7), text="", passages=()
    )

    class _OneShotEngine:
        """Returns the write-back text once; never called by 切分."""

        def recognize(self, *, source: Path, page: int) -> str:
            return ocr_text

    written = selective_ocr_write_back(
        page,
        source=Path("filing.pdf"),
        image_count=1,
        engine=_OneShotEngine(),
    )
    assert written.applied is True
    # the write-back preserves the locator (the 冻结单元 identity)
    assert written.page.locator == page.locator

    unit = build_unit(
        fmt="pdf",
        source_sha256=VERSION,
        document_id=DOCUMENT_ID,
        version=VERSION,
        unit=7,
        text=written.page.text,
    )
    assert unit.locator.unit == 7
    chunked = split_unit(unit, doc_type="narrative")
    assert chunked.unit_text == unit.text, "extract reads this frozen text"
    assert len(chunked.parents) >= 2, "precondition: the OCR page overflows"
    # SC7: no heading tags on the page -> title may be empty, and
    # EVERY window still resolves to the same frozen unit.
    assert {p.title for p in chunked.parents} == {None}
    assert all(p.text in chunked.unit_text for p in chunked.parents)
    assert all(c.text in chunked.unit_text for c in chunked.children)
    # the 冻结单元 identity (locator) rides every child's payload, so a
    # window that overflowed still cites the one shared unit — the
    # SC7 "windows still share that 冻结单元 plus 资料主体" clause.
    payloads = chunked.payloads()
    assert len(payloads) == len(chunked.children)
    assert {payload.locator for payload in payloads} == {unit.locator}
    assert all(payload.source_sha256 == VERSION for payload in payloads)
    # the windows of the unit partition the frozen body: every
    # non-whitespace character is reachable from some window, so the
    # overflow branch drops no text
    covered: set[int] = set()
    for window in window_spans(chunked.unit_text):
        covered.update(range(window.body_start, window.body_end))
    needed = {
        index
        for index, char in enumerate(chunked.unit_text)
        if not char.isspace()
    }
    assert covered & needed == needed
    # the body really is past the cap, so the shared-unit claim is not
    # vacuously satisfied by a single window
    assert count_window_tokens(chunked.unit_text) > 1200
    assert len(chunked.unit_text) > EXTRACT_WINDOW_CHARS
    # the frozen text is a normalization fixed point — the write-back
    # path froze it once, and 切分 re-normalizes nothing
    assert normalize_frozen_text(chunked.unit_text) == chunked.unit_text


def test_sc7_plain_text_unit_with_no_heading_yields_titleless_windows() -> None:
    # The OCR page's frozen shape on the plain-text door: no heading
    # tag and no ATX / uppercase line anywhere. Every window is
    # title-less and every child stays inside the one unit text.
    body = "\n".join(
        f"Line {i:03d} reports net sales of {i} million dollars for the "
        "quarter and operating income growth."
        for i in range(120)
    )
    parsed = parse_document(
        body.encode("utf-8"),
        fmt="txt",
        document_id=DOCUMENT_ID,
        version=VERSION,
    )
    assert len(parsed.units) == 1
    unit = parsed.units[0]
    assert not any(is_heading_line(line) for line in unit.text.split("\n"))
    chunked = split_unit(unit, doc_type="narrative")
    assert len(chunked.parents) >= 2
    assert {p.title for p in chunked.parents} == {None}
    assert all(c.text in chunked.unit_text for c in chunked.children)


# ---------------------------------------------------------------------------
# SC7: the split path never reaches OCR or a vision heading model
# ---------------------------------------------------------------------------

# The poison installs itself as ``fin.corpus.ocr`` BEFORE the split
# path is imported, so any import or attribute the split path performs
# on that module raises. ``detect_ocr_required`` is the heuristic a
# real "do we need OCR here" call would use; ``TesseractEngine`` /
# ``OCREngine`` are the engine seams; the vision names cover a heading
# model smuggled through the same module.
_POISON_OCR_SNIPPET = """
    import sys
    import types

    class _PoisonModule(types.ModuleType):
        def __getattr__(self, name):
            if name.startswith("__"):
                raise AttributeError(name)
            raise AssertionError(
                f"the split path reached fin.corpus.ocr.{name}"
            )

    def _boom(*args, **kwargs):
        raise AssertionError("the split path called an OCR seam")

    poison = _PoisonModule("fin.corpus.ocr")
    for name in (
        "OCREngine",
        "OCRError",
        "TesseractEngine",
        "detect_ocr_required",
    ):
        setattr(poison, name, _boom)

    class _PoisonError(Exception):
        code = "ocr_required_unavailable"

    poison.OcrRequiredUnavailableError = _PoisonError

    import fin.corpus  # noqa: F401  (register the parent first)
    sys.modules["fin.corpus.ocr"] = poison
    setattr(fin.corpus, "ocr", poison)

    from fin.corpus.parsers import parse_document
    from fin.corpus.parsers._common import build_unit
    from fin.corpus.chunking import split_document, split_unit
    from fin.corpus.window import window_spans

    html = (
        b"<html><body><main><h2>REVENUE</h2><p>"
        + b"Total net sales increased during the quarter. " * 8
        + b"</p></main></body></html>"
    )
    md = (
        b"## Revenue\\n"
        + b"Total net sales increased during the quarter. " * 8
    )
    for fmt, source in (("html", html), ("md", md)):
        parsed = parse_document(
            source, fmt=fmt, document_id="NEG-LOCK", version="a" * 64
        )
        chunked = split_document(parsed, doc_type="narrative")
        assert chunked.units, f"{fmt}: split produced no units"
        for uc in chunked.units:
            window_spans(uc.unit_text)

    ocr_page = (
        "Consolidated statement line 001 reports net sales of 7 million "
        "dollars for the quarter.\\n"
    ) * 40
    text_unit = build_unit(
        fmt="txt",
        source_sha256="b" * 64,
        document_id="NEG-LOCK-OCR",
        version="b" * 64,
        unit=1,
        text=ocr_page,
    )
    split_unit(text_unit, doc_type="narrative")
    window_spans(text_unit.text)

    leaked = sorted(
        name
        for name in sys.modules
        if name.split(".")[0] in _OCR_VISION_ROOTS
    )
    assert leaked == [], leaked
    print("NEGATIVE-LOCK-OK")
"""


def _run_poison_subprocess() -> str:
    snippet = textwrap.dedent(_POISON_OCR_SNIPPET).replace(
        "_OCR_VISION_ROOTS", repr(_OCR_VISION_ROOTS)
    )
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(snippet)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        "the split path reached OCR / a vision import "
        f"(rc={result.returncode}):\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    return result.stdout


def test_sc7_split_path_never_reaches_ocr() -> None:
    # SC7: "Tests must not require extract to call OCR or a vision
    # heading model." This proves the stronger statement: the split /
    # window path can never call one. A fresh interpreter installs a
    # poison ``fin.corpus.ocr`` before the split path is imported and
    # runs parse → split → window over an HTML fixture, a Markdown
    # fixture and the OCR page shape. Any OCR import, engine
    # construction or heuristic call fails the subprocess; any OCR /
    # vision library left in ``sys.modules`` fails the leak scan.
    #
    # Load-bearing (verified by mutation on the real ``chunking.py``,
    # restored byte-exact afterwards): adding
    # ``from fin.corpus.ocr import detect_ocr_required`` and calling it
    # at the top of ``split_unit`` makes this test fail with "the
    # split path called an OCR seam".
    #
    # Honest limit, measured: the poison module is already in
    # ``sys.modules``, so a bare ``import fin.corpus.ocr`` with no
    # attribute access would NOT trip this half. That shape is the AST
    # test's job (``test_sc7_window_and_chunking_modules_import_no_ocr``).
    # The two halves cover each other: a call-site trips the poison, an
    # import-only reference trips the AST scan.
    assert "NEGATIVE-LOCK-OK" in _run_poison_subprocess()


def _ocr_named_imports(tree: ast.AST) -> list[str]:
    """Every ocr/vision-named import reference reachable in ``tree``.

    ``ast.walk`` descends into function bodies, so a locally-scoped
    import counts. Both halves of an ``ImportFrom`` are inspected: the
    module (``from fin.corpus.ocr import x``) AND the imported names
    (``from fin.corpus import ocr as _x``, measured to slip past a
    module-only check). Relative imports (``from . import ocr``) are
    reported by their imported names for the same reason.
    """
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module:
                found.append(node.module)
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
    return [
        name for name in found if "ocr" in name.lower() or "vision" in name.lower()
    ]


def test_sc7_window_and_chunking_modules_import_no_ocr() -> None:
    # Structural half of the same lock: the two rule modules' IMPORT
    # graphs name no OCR / vision module. Checked via AST rather than a
    # substring scan, because their docstrings say "no OCR" on purpose
    # (that is where the non-call is documented) and a substring scan
    # would fail on the documentation.
    for module_name in ("fin.corpus.window", "fin.corpus.chunking"):
        module = sys.modules.get(module_name)
        if module is None:
            module = __import__(module_name, fromlist=["__file__"])
        source = Path(module.__file__ or "").read_text(encoding="utf-8")
        offending = _ocr_named_imports(ast.parse(source))
        assert offending == [], (module_name, offending)


def test_sc7_ocr_required_unavailable_stays_the_single_typed_exit() -> None:
    # SC7 / spec Boundaries "exception": the ingest OCR EXIT is
    # ``ocr_required_unavailable`` and extract invents no second OCR
    # EXIT. The canonical class still carries the code, and a scan of
    # the whole product source finds no other class exposing an
    # ocr-flavoured ``code`` — a second OCR EXIT anywhere would have to
    # show up in this set, so the assertion is a real lock rather than
    # a name check on one module. The scan is whole-``src/fin`` on
    # purpose: a new OCR code on a neighbouring (ingest / write-back)
    # path is out of this spec too, and the spec says a second OCR code
    # needs its own decision (ADR 0054 closed the existing one).
    assert OcrRequiredUnavailableError.code == "ocr_required_unavailable"

    found: set[tuple[str, str, str]] = set()
    corpus_root = Path(__file__).resolve().parents[2] / "src" / "fin"
    for path in sorted(corpus_root.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.ClassDef):
                continue
            for statement in node.body:
                if not isinstance(statement, ast.Assign):
                    continue
                targets = {
                    target.id
                    for target in statement.targets
                    if isinstance(target, ast.Name)
                }
                value = statement.value
                if (
                    "code" in targets
                    and isinstance(value, ast.Constant)
                    and isinstance(value.value, str)
                    and "ocr" in value.value.lower()
                ):
                    found.add(
                        (str(path.relative_to(corpus_root)), node.name, value.value)
                    )
    assert found == {
        ("corpus/ocr.py", "OcrRequiredUnavailableError", "ocr_required_unavailable")
    }, found


def test_sc7_window_and_chunking_modules_define_no_exception() -> None:
    # The rule modules raise nothing of their own: a typed OCR exit
    # invented "on the extract/window path" would have to live in one
    # of them (or be re-exported by them), and it is not there. Names
    # are checked too, so an OCR-flavoured class name cannot slip in
    # without a ``code``.
    import fin.corpus.chunking as chunking_module
    import fin.corpus.window as window_module

    for module in (window_module, chunking_module):
        classes = [
            name
            for name, value in vars(module).items()
            if isinstance(value, type)
        ]
        assert not [name for name in classes if "ocr" in name.lower()], (
            module.__name__,
            classes,
        )
        assert "OCR" not in getattr(module, "__all__", ()), module.__name__
