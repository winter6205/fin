"""Boundary tests for the company read seam (ADR 0046 / ADR 0047, ADR 0088).

The serve path must not treat the local ``data/corpus/extracted`` tree as
the company-corpus SSOT, and the whole-page read seam is gone: there is
no pagination port and no compatibility path. What ``read_port``
holds now are the two authorities every pages-backed company read shares:

* ``validate_company_source_ref`` — the one place a company locator
  triple's shape and size are judged; both read legs resolve their
  locator through it.
* ``unit_text_from_pages`` — the in-memory ``{document_id: pages}``
  lookup of one frozen unit's text, which is what the fake serve path
  (:class:`fin.retrieve.fake_port.FakeRetrievePort`) reads its
  ``pages_by_doc`` manifest through.

The body a caller finally receives is that text put through the shared
bounded slicer (:mod:`fin.retrieve.source_read`), so the bound on a read
is a SIZE bound — the code-point and serialized-result caps — not a
page-count window. Those bounds are pinned here through the pages-backed
leg, because that is the seam ``read_port`` feeds.

Covers the five boundary classes: empty (no pages), negative (unit below
1, malformed keys), overflow (unit past the end, oversized locator, body
past the code-point and byte caps), concurrent (read-only shared state),
exception (nothing raises at the model — every miss is a typed answer).
"""

from __future__ import annotations

import concurrent.futures

import pytest

from fin.corpus.extracted import ExtractedPage, Locator
from fin.retrieve import mcp_adapter as mcp_adapter_module
from fin.retrieve import read_port as read_port_module
from fin.retrieve.fake_port import FakeRetrievePort
from fin.retrieve.read_port import unit_text_from_pages, validate_company_source_ref
from fin.retrieve.source_read import (
    SOURCE_READ_MAX_RESULT_BYTES,
    SOURCE_READ_MAX_TEXT_CODE_POINTS,
    serialized_result_bytes,
)


VERSION = "a" * 64
DOCUMENT_ID = "TEST-2023Q1"


# ----- synthetic unit fixture (no on-disk tree needed) -----


def _pages(document_id: str = DOCUMENT_ID, count: int = 3) -> tuple[ExtractedPage, ...]:
    return tuple(
        ExtractedPage(
            locator=Locator(document_id=document_id, version=VERSION, unit=n),
            text=f"PAGE {n} BODY",
            passages=(),
        )
        for n in range(1, count + 1)
    )


def _single_unit_table(text: str) -> dict[str, tuple[ExtractedPage, ...]]:
    """A one-unit manifest whose whole body is ``text``.

    Built from the caller's string so the expected slice is read off the
    fixture, not recomputed from what the read path returns.
    """
    return {
        DOCUMENT_ID: (
            ExtractedPage(
                locator=Locator(document_id=DOCUMENT_ID, version=VERSION, unit=1),
                text=text,
                passages=(),
            ),
        )
    }


def _company_ref(document_id: str = DOCUMENT_ID, unit: int = 1) -> dict:
    return {"document_id": document_id, "version": VERSION, "unit": unit}


# ===== the seam's surface: a locator gate and a lookup, nothing else =====


def test_the_read_seam_exposes_only_the_locator_gate_and_the_unit_lookup() -> None:
    """The frozen contract: ``read_port`` answers through exactly two
    functions. A re-introduced pagination port or page-limit helper would
    give one frozen unit a second read identity, so its absence is part
    of the contract, asserted by name."""
    assert sorted(read_port_module.__all__) == [
        "unit_text_from_pages",
        "validate_company_source_ref",
    ]
    for removed in (
        "PAGE_LIMIT",
        "PAGE_LIMIT_CAP",
        "ReadPort",
        "MissReadPort",
        "ExtractedTreeReadPort",
        "clamp_page_limit",
        "read_from_pages",
    ):
        assert not hasattr(read_port_module, removed), removed


@pytest.mark.parametrize(
    "module",
    [pytest.param(read_port_module, id="read-port"),
     pytest.param(mcp_adapter_module, id="mcp-read-leg")],
)
def test_a_read_leg_carries_no_local_tree_fallback(module: object) -> None:
    """ADR 0046 / ADR 0047: neither company read leg may fall back to the
    committed extracted tree on disk. With the tree-backed port deleted
    this is now structural — the loader is not reachable from a read leg
    at all, so a missing or malformed tree cannot be papered over by a
    silent second read path."""
    assert not hasattr(module, "load_extracted_tree")


# ===== unit_text_from_pages: the shared in-memory lookup =====


def test_unit_text_resolves_the_addressed_unit() -> None:
    assert (
        unit_text_from_pages({DOCUMENT_ID: _pages()}, DOCUMENT_ID, VERSION, 2)
        == "PAGE 2 BODY"
    )


def test_unit_text_of_an_empty_table_is_none() -> None:
    """No pages at all (the default fake's shape) is a miss, not an
    empty body."""
    assert unit_text_from_pages({}, DOCUMENT_ID, VERSION, 1) is None


def test_unit_text_of_an_unknown_document_is_none() -> None:
    assert (
        unit_text_from_pages({DOCUMENT_ID: _pages()}, "BOGUS-2099Q9", VERSION, 1)
        is None
    )


def test_unit_text_of_a_mismatched_version_is_none() -> None:
    """The rows carry the ingested sha256; a caller asking for a version
    the table does not hold gets a miss, never another version's text."""
    assert (
        unit_text_from_pages({DOCUMENT_ID: _pages()}, DOCUMENT_ID, "b" * 64, 1)
        is None
    )


@pytest.mark.parametrize("bad_unit", [0, -1, 4])
def test_unit_outside_the_table_is_none(bad_unit: int) -> None:
    """``unit < 1`` (negative boundary) and ``unit > len(pages)``
    (overflow boundary) are both the same named miss — no zero-based
    reinterpretation, no wrap-around to the first unit."""
    assert (
        unit_text_from_pages({DOCUMENT_ID: _pages()}, DOCUMENT_ID, VERSION, bad_unit)
        is None
    )


@pytest.mark.parametrize(
    ("table", "ref"),
    [
        pytest.param({}, _company_ref(), id="no-documents"),
        pytest.param({DOCUMENT_ID: ()}, _company_ref(), id="empty-document"),
        pytest.param({DOCUMENT_ID: _pages(count=1)}, _company_ref(unit=3), id="past-the-end"),
        pytest.param({DOCUMENT_ID: _pages()}, _company_ref(document_id="GONE"), id="no-such-document"),
    ],
)
def test_an_incomplete_table_is_a_typed_miss_never_an_exception(
    table: dict, ref: dict
) -> None:
    """SC3: with nothing readable behind it, the model still gets the
    typed ``unknown_locator`` dict — the lookup returns ``None`` and the
    leg folds that to a name, so no missing or short artifact can raise
    ``IngestMissingError`` at the model or answer an empty success."""
    assert FakeRetrievePort(pages_by_doc=table).read_slice(ref) == {
        "error": "unknown_locator"
    }


# ===== the surviving bound on a body: size, not a page-count window =====


def test_a_unit_longer_than_the_bound_is_read_in_capped_slices() -> None:
    """The bound that replaced the page window: an over-long unit never
    arrives whole. One read hands back at most
    ``SOURCE_READ_MAX_TEXT_CODE_POINTS`` code points and a cursor that
    advances to the remainder, which is the surviving form of the old
    "the range clamps to the cap" promise."""
    text = "A" * (SOURCE_READ_MAX_TEXT_CODE_POINTS + 7)
    port = FakeRetrievePort(pages_by_doc=_single_unit_table(text))

    first = port.read_slice(_company_ref())
    assert first["text"] == text[:SOURCE_READ_MAX_TEXT_CODE_POINTS]
    assert first["next_position"] == SOURCE_READ_MAX_TEXT_CODE_POINTS

    tail = port.read_slice(_company_ref(), first["next_position"])
    assert tail["text"] == text[SOURCE_READ_MAX_TEXT_CODE_POINTS:]
    assert tail["next_position"] is None
    assert first["text"] + tail["text"] == text


def test_a_multibyte_unit_is_shrunk_until_the_whole_result_fits_the_byte_cap() -> None:
    """The cap measures the complete serialized result, so a unit whose
    text costs four bytes a code point is cut below the code-point cap
    rather than handed over the byte bound. The cursor still strictly
    advances and the walk reassembles the stored text exactly."""
    text = "\U0001F600" * (SOURCE_READ_MAX_TEXT_CODE_POINTS + 100)
    port = FakeRetrievePort(pages_by_doc=_single_unit_table(text))

    collected = ""
    position = 0
    guard = 0
    while True:
        result = port.read_slice(_company_ref(), position)
        assert serialized_result_bytes(result) <= SOURCE_READ_MAX_RESULT_BYTES
        collected += result["text"]
        if result["next_position"] is None:
            break
        assert result["next_position"] > position
        position = result["next_position"]
        guard += 1
        assert guard < 100, "a non-advancing cursor must not spin the walk"
    assert collected == text


# ===== FakeRetrievePort.read_slice: the pages-backed leg =====


def test_fake_read_slice_shares_read_only_state_across_threads() -> None:
    """Concurrent reads of one shared fake answer identically: the
    manifest is copied at construction and never mutated by a read."""
    port = FakeRetrievePort(pages_by_doc={DOCUMENT_ID: _pages()})

    def _call() -> dict:
        return port.read_slice(_company_ref(unit=1))

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: _call(), range(8)))
    assert all(r == results[0] for r in results)
    assert results[0]["text"] == "PAGE 1 BODY"


def test_fake_pages_by_doc_is_copied_not_aliased() -> None:
    """Mutating the caller's mapping after construction cannot change
    what the fake serves (same defensive-copy rule as hits_by_query)."""
    pages = {DOCUMENT_ID: _pages()}
    port = FakeRetrievePort(pages_by_doc=pages)
    pages["INJECTED-2024Q1"] = _pages("INJECTED-2024Q1")
    assert port.read_slice(_company_ref("INJECTED-2024Q1")) == {
        "error": "unknown_locator"
    }


# ===== validate_company_source_ref: the shared locator gate =====


def test_validator_resolves_a_well_formed_triple() -> None:
    located = validate_company_source_ref(
        {"document_id": "TEST-2023Q1", "version": VERSION, "unit": 3}
    )
    assert located == ("TEST-2023Q1", VERSION, 3)


@pytest.mark.parametrize(
    ("bad_ref", "reason"),
    [
        pytest.param("a serialized string", "locator_not_a_record", id="string"),
        pytest.param(None, "locator_not_a_record", id="none"),
        pytest.param([1, 2], "locator_not_a_record", id="list"),
        pytest.param({}, "locator_keys_malformed", id="empty-record"),
        pytest.param(
            {"document_id": "T", "version": VERSION}, "locator_keys_malformed", id="missing-unit"
        ),
        pytest.param(
            {"document_id": "T", "version": VERSION, "unit": 1, "page": 2},
            "locator_keys_malformed",
            id="extra-key",
        ),
        pytest.param(
            {"document_id": "", "version": VERSION, "unit": 1},
            "locator_fields_malformed",
            id="empty-doc-id",
        ),
        pytest.param(
            {"document_id": "T", "version": 5, "unit": 1},
            "locator_fields_malformed",
            id="non-string-version",
        ),
        pytest.param(
            {"document_id": "T", "version": VERSION, "unit": 0},
            "locator_fields_malformed",
            id="zero-unit",
        ),
        pytest.param(
            {"document_id": "T", "version": VERSION, "unit": -2},
            "locator_fields_malformed",
            id="negative-unit",
        ),
        pytest.param(
            {"document_id": "T", "version": VERSION, "unit": True},
            "locator_fields_malformed",
            id="bool-unit",
        ),
        pytest.param(
            {"document_id": "T", "version": VERSION, "unit": "1"},
            "locator_fields_malformed",
            id="string-unit",
        ),
        pytest.param(
            {"document_id": "D" * 1100, "version": VERSION, "unit": 1},
            "request_locator_oversized",
            id="oversized-serialization",
        ),
    ],
)
def test_validator_names_every_malformed_shape(bad_ref: object, reason: str) -> None:
    """The reason tokens are the closed vocabulary both read legs
    report; ``request_locator_oversized`` reuses the backend's own
    token so one oversized identity has one name."""
    assert validate_company_source_ref(bad_ref) == {
        "error": "invalid_locator",
        "reason": reason,
    }


@pytest.mark.parametrize(
    "junk", ["x", 42, None, [], {}, object(), b"bytes", 3.5, True]
)
def test_validator_never_raises_on_junk(junk: object) -> None:
    """The boundary contract: garbage in, a typed failure dict out."""
    result = validate_company_source_ref(junk)
    assert isinstance(result, dict)
    assert result["error"] == "invalid_locator"
