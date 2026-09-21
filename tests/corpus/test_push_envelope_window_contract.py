"""The 抽图窗 / in-window children contract on the push envelope (plan
spec SC6 / ADR 0060 / 0063 / 0077).

The envelope already carries the frozen body plus ``chunks[]``; this
module locks the three facts the joint extract-input contract states
about that shape, so a later change cannot silently break them:

* every wire chunk is a contiguous substring of **its own parent
  window** (抽图窗) and the window text is a contiguous substring of
  the envelope body — the parent-child relation holds transitively on
  the wire, in the multi-window (overflow) geometry, not just in the
  one-window case. iknow's ``z.strictObject`` refuses a
  ``parent_block_id`` / ``parent_text`` wire key, so containment is
  the ONLY spelling of 父子 that can cross the repo boundary, and it
  must be proven per chunk.
* the push surface contains no second document cut: the envelope's
  chunks are the ONE chunking's children verbatim (same count, same
  order, same bytes), and no push-surface module even imports a
  splitter — Fast's co-occurrence scope is these same in-window
  children, so a second 切分 in fin would have to show up as new
  chunk content and this file's verbatim lock refuses it.
* no ranking symbol reaches the push surface (this increment's "Fin
  must not re-rank" boundary, asserted structurally like the OCR
  guards in ``tests/corpus/test_window_extract_contract.py``).
"""

from __future__ import annotations

import ast
from pathlib import Path

from fin.corpus.chunking import split_document, split_unit
from fin.corpus.parsers import parse_document
from fin.corpus.parsers._common import build_unit
from fin.corpus.push import envelope_of_unit

DOCUMENT_ID = "TEST-2023Q1"
VERSION = "a" * 64
VALID_FROM = "2026-01-01T00:00:00.000Z"
VALID_UNTIL = "9999-12-31T23:59:59.999Z"

# The push surface these locks apply to: the corpus-side
# envelope builder plus the three retrieve-side ports.
_PUSH_SURFACE_MODULES = (
    Path("src/fin/corpus/push.py"),
    Path("src/fin/retrieve/push_port.py"),
    Path("src/fin/retrieve/fake_push_port.py"),
    Path("src/fin/retrieve/mcp_push_port.py"),
)


def _sentences(n: int, start: int = 0) -> str:
    return "".join(
        f"Sentence {i:03d} reports that operating revenue grew in the "
        "quarter and the gross margin improved again. "
        for i in range(start, start + n)
    )


def _multi_window_unit():
    """One frozen unit whose body overflows into >= 2 抽图窗.

    The ``# Alpha`` heading scopes the windows; the 60-sentence body is
    the same overflow geometry ``test_chunking.py`` uses for its
    cross-window monotonicity locks, so the chain test below runs in
    the geometry where chunk-in-body alone would NOT prove
    chunk-in-its-window.
    """
    return split_unit(
        build_unit(
            fmt="txt",
            source_sha256=VERSION,
            document_id=DOCUMENT_ID,
            version=VERSION,
            unit=1,
            text="# Alpha\n" + _sentences(60),
        ),
        doc_type="narrative",
    )


def _envelope(unit):
    return envelope_of_unit(
        unit, valid_from=VALID_FROM, valid_until=VALID_UNTIL
    )


# ---------------------------------------------------------------------------
# Acceptance 1: per-chunk containment in ITS parent window, transitively in
# the body — on the envelope, in the multi-window geometry.
# ---------------------------------------------------------------------------


def test_envelope_unit_has_the_multi_window_precondition() -> None:
    unit = _multi_window_unit()
    assert len(unit.parents) >= 2, (
        "precondition: the fixture must overflow into >= 2 windows, "
        "otherwise the per-window lock below degrades to the "
        "chunk-in-body check test_push_idempotency already has"
    )


def test_every_wire_chunk_is_a_contiguous_substring_of_its_parent_window() -> None:
    unit = _multi_window_unit()
    envelope = _envelope(unit)
    payloads = unit.payloads()
    assert len(payloads) == len(envelope.chunks)
    for payload, chunk in zip(payloads, envelope.chunks):
        # the wire chunk is the child verbatim…
        assert chunk.content == payload.text
        # …a contiguous substring of its own 抽图窗…
        assert payload.text in payload.parent_text
        # …and the window text is a contiguous substring of the body,
        # so the 父子 chain holds transitively on the wire.
        assert payload.parent_text in envelope.body


def test_windows_tile_the_body_and_each_chunk_lies_inside_its_window_slice() -> None:
    """The positional form of the same containment (iknow's freeze
    walk only sees ``body`` + ordered ``chunks``, never the parent
    text): every window sits in the body at a non-decreasing offset,
    and every chunk is found INSIDE the byte span of its own window.
    """
    unit = _multi_window_unit()
    envelope = _envelope(unit)
    windows = {parent.block_id: parent.text for parent in unit.parents}
    window_start: dict[str, int] = {}
    last_start = 0
    for parent in unit.parents:
        start = envelope.body.find(parent.text, last_start)
        assert start != -1, "windows appear in body order"
        window_start[parent.block_id] = start
        last_start = start
    for payload, chunk in zip(unit.payloads(), envelope.chunks):
        start = window_start[payload.parent_block_id]
        window_end = start + len(windows[payload.parent_block_id])
        found = envelope.body.find(chunk.content, start, window_end)
        assert found != -1, (
            f"chunk {chunk.index} lies outside the byte span of its "
            "own window in the body"
        )
        assert found + len(chunk.content) <= window_end


# ---------------------------------------------------------------------------
# Acceptance 2: one document cut — the envelope is the ONE chunking's
# children verbatim, and no push-surface module even imports a splitter.
# ---------------------------------------------------------------------------


def test_envelope_chunks_are_the_single_chunkings_children_verbatim() -> None:
    unit = _multi_window_unit()
    envelope = _envelope(unit)
    children = unit.children
    assert len(envelope.chunks) == len(children)
    for index, (chunk, child) in enumerate(zip(envelope.chunks, children)):
        assert chunk.index == index
        assert chunk.content == child.text
        assert chunk.id == child.block_id


def test_document_envelopes_follow_the_same_single_cut() -> None:
    """The document-level path is the unit-level one composed: the
    envelope of every unit equals ``envelope_of_unit`` of that unit,
    so there is exactly one 切分 feeding every push shape.
    """
    result = parse_document(
        (_sentences(20) + "\n\n" + _sentences(20, start=20)).encode("utf-8"),
        fmt="txt",
        document_id=DOCUMENT_ID,
        version=VERSION,
    )
    document = split_document(result, doc_type="narrative")
    for unit in document.units:
        envelope = _envelope(unit)
        assert envelope.body == unit.unit_text
        assert tuple(c.content for c in envelope.chunks) == tuple(
            p.text for p in unit.payloads()
        )


def _imported_names(tree: ast.AST) -> list[str]:
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module:
                found.append(node.module)
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
    return found


def test_push_surface_imports_no_splitter_authority() -> None:
    """``fin.corpus.chunking``'s split entry points (and the window
    rule it delegates to) are the single 切分 authority; no module on
    the push surface imports ``split_unit`` / ``split_document`` /
    ``window_spans``, so no push-side code path owns a second cut.
    Types (``UnitChunking`` …) may be imported — only callables that
    CREATE blocks are refused.
    """
    root = Path(__file__).resolve().parents[2]
    offenders: dict[str, list[str]] = {}
    for relative in _PUSH_SURFACE_MODULES:
        source = (root / relative).read_text(encoding="utf-8")
        bad = [
            name
            for name in _imported_names(ast.parse(source))
            if name in {"split_unit", "split_document", "window_spans"}
        ]
        if bad:
            offenders[str(relative)] = bad
    assert offenders == {}, offenders


# ---------------------------------------------------------------------------
# Acceptance 3: no ranking on this surface (fin must not re-rank; the
# ordering on the wire is the monotonic chunk index, nothing else).
# ---------------------------------------------------------------------------


def _ranking_identifier_hits(tree: ast.AST) -> list[str]:
    """Every identifier / attribute / literal-key name on the push
    surface that spells ranking or scoring. AST-scanned (not substring)
    so prose in docstrings — which is where "must not re-rank" is
    DOCUMENTED — does not trip the guard.
    """
    hits: list[str] = []
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Name):
            names = [node.id]
        elif isinstance(node, ast.Attribute):
            names = [node.attr]
        elif isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            names = [node.name]
        elif isinstance(node, ast.keyword) and node.arg:
            names = [node.arg]
        hits.extend(n for n in names if "rank" in n.lower() or "score" in n.lower())
    return hits


def test_push_surface_names_no_ranking_symbol() -> None:
    root = Path(__file__).resolve().parents[2]
    found: dict[str, list[str]] = {}
    for relative in _PUSH_SURFACE_MODULES:
        source = (root / relative).read_text(encoding="utf-8")
        hits = _ranking_identifier_hits(ast.parse(source))
        if hits:
            found[str(relative)] = hits
    assert found == {}, found
