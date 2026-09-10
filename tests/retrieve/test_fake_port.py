"""Boundary tests for ``FakeRetrievePort``.

The fake port fills the seam between the host graph and the
retrieve capability (ADR 0030, T3). T3 acceptance: default
``uv run pytest -q`` runs without an MCP process or live BM25;
the fake port backs the host graph's tool bind so the test
surface is hermetic.

Boundary classes (mirrors ``RetrievePort`` semantics so tests
do not care which implementation backs a ``retrieve`` call):

* empty: empty / whitespace-only query → ``EmptyQueryError``
  (invalid input, T21); a query with no hits → ``{"chunks": []}``
* negative: unknown ``document_id`` (when ``known_document_ids``
  is set) → ``{"chunks": []}``
* overflow: ``limit > 20`` clamps to 20; ``limit <= 0`` falls
  back to 8
* concurrent: read-only access; thread-safe by construction
* exception: invalid input only — the fake port has no subprocess
  and no I/O to fail at, so the named ``EmptyQueryError`` is the
  one exception it produces.
"""

from __future__ import annotations

import concurrent.futures

import pytest

from fin.corpus.extracted import Locator
from fin.retrieve import (
    Chunk,
    EmptyQueryError,
    FakeRetrievePort,
    RetrievePort,
)


def _chunk(text: str, document_id: str = "DOC-2023Q1", page: int = 1) -> Chunk:
    return Chunk(
        text=text,
        source_ref=Locator(
            document_id=document_id,
            version="v-" + "a" * 60,
            page=page,
        ),
        score=1.0,
    )


# ----- empty -----


def test_empty_query_is_a_named_failure() -> None:
    """Boundary 'empty': an empty / whitespace-only query is invalid
    input (T21 / §B2 第 5 条), named and counted — not the miss shape,
    which belongs to a query the corpus simply has no hits for (the
    next test)."""
    port = FakeRetrievePort()
    for empty_query in ("", "   ", "\t\n", "\n"):
        with pytest.raises(EmptyQueryError):
            port.retrieve(empty_query)

    assert port.input_validation_failures.count() == 4


def test_unknown_query_returns_empty_chunks() -> None:
    """Boundary 'empty': a non-empty query that the fake has no
    hits for yields ``{"chunks": []}`` without raising."""
    port = FakeRetrievePort(
        hits_by_query={"alpha": [_chunk("hit")]},
    )
    assert port.retrieve("nonexistent query") == {"chunks": []}


# ----- negative -----


def test_unknown_document_id_returns_empty_chunks() -> None:
    """Boundary 'negative': unknown ``document_id`` yields
    ``{"chunks": []}`` without raising."""
    hits = {"foo": [_chunk("hit", document_id="DOC-2023Q1")]}
    port = FakeRetrievePort(
        hits_by_query=hits,
        known_document_ids={"DOC-2023Q1"},
    )
    assert port.retrieve("foo", document_id="UNKNOWN-9999") == {"chunks": []}


# ----- overflow -----


def test_limit_clamped_to_twenty() -> None:
    """Boundary 'overflow': ``limit > 20`` clamps to 20 (ADR 0009)."""
    chunks = [_chunk(f"hit {i}") for i in range(50)]
    port = FakeRetrievePort(
        hits_by_query={"foo": chunks},
        known_document_ids={"DOC-2023Q1"},
    )
    result = port.retrieve("foo", limit=999)
    assert len(result["chunks"]) == 20


def test_non_positive_limit_treated_as_default_eight() -> None:
    """Boundary 'overflow': ``limit <= 0`` falls back to 8."""
    chunks = [_chunk(f"hit {i}") for i in range(20)]
    port = FakeRetrievePort(
        hits_by_query={"foo": chunks},
        known_document_ids={"DOC-2023Q1"},
    )
    result = port.retrieve("foo", limit=0)
    assert len(result["chunks"]) == 8


def test_default_limit_is_eight() -> None:
    """Default ``limit`` (no argument) is 8 per ADR 0009."""
    chunks = [_chunk(f"hit {i}") for i in range(20)]
    port = FakeRetrievePort(
        hits_by_query={"foo": chunks},
        known_document_ids={"DOC-2023Q1"},
    )
    default = port.retrieve("foo")
    explicit = port.retrieve("foo", limit=8)
    assert [c.text for c in default["chunks"]] == [
        c.text for c in explicit["chunks"]
    ]


# ----- concurrent -----


def test_overlapping_retrieve_calls_independent() -> None:
    """Boundary 'concurrent': 16 parallel ``retrieve`` calls on
    one shared ``FakeRetrievePort`` return identical hit lists.

    The fake port holds a read-only dict; thread-safety is by
    construction (no shared mutable state).
    """
    chunks = [_chunk(f"hit {i}") for i in range(5)]
    port = FakeRetrievePort(
        hits_by_query={"foo": chunks},
        known_document_ids={"DOC-2023Q1"},
    )

    def _go() -> list[str]:
        return [c.text for c in port.retrieve("foo", limit=5)["chunks"]]

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(lambda _: _go(), range(16)))
    reference = results[0]
    for i, hit_texts in enumerate(results[1:], start=1):
        assert hit_texts == reference, (
            f"call #{i} returned a different hit list: {hit_texts!r} vs {reference!r}"
        )


# ----- protocol conformance -----


def test_fake_port_satisfies_retrieve_port() -> None:
    """``FakeRetrievePort`` must satisfy the ``RetrievePort``
    Protocol structurally (runtime ``isinstance`` check)."""
    port = FakeRetrievePort()
    assert isinstance(port, RetrievePort)


# ----- locator mapping -----


def test_chunks_carry_locator_as_source_ref() -> None:
    """Every chunk's ``source_ref`` is a ``Locator`` (ADR 0006) —
    the citation gate reads ``source_ref.document_id / .page``."""
    chunks = [
        Chunk(
            text="hit text",
            source_ref=Locator(
                document_id="DOC-2023Q1",
                version="v-" + "a" * 60,
                page=3,
            ),
            score=2.5,
        ),
    ]
    port = FakeRetrievePort(
        hits_by_query={"foo": chunks},
        known_document_ids={"DOC-2023Q1"},
    )
    out = port.retrieve("foo")
    assert out["chunks"]
    for chunk in out["chunks"]:
        assert isinstance(chunk.source_ref, Locator)
        assert chunk.source_ref.document_id == "DOC-2023Q1"
        assert chunk.source_ref.page == 3
        assert chunk.score == 2.5

# ----- EDGAR-shaped filter parity (T2 / T3 hand-off) -----


def test_empty_cik_filter_returns_empty_chunks() -> None:
    """Empty EDGAR-shaped filter values are miss shapes per spec."""
    port = FakeRetrievePort(
        hits_by_query={"foo": [_mk_chunk("DOC-1", 1, "hit")]},
        known_document_ids={"DOC-1"},
    )
    assert port.retrieve("foo", cik="") == {"chunks": []}


def test_empty_form_filter_returns_empty_chunks() -> None:
    port = FakeRetrievePort(hits_by_query={"foo": [_mk_chunk("DOC-1", 1, "hit")]})
    assert port.retrieve("foo", form="") == {"chunks": []}


def test_empty_ticker_filter_returns_empty_chunks() -> None:
    port = FakeRetrievePort(hits_by_query={"foo": [_mk_chunk("DOC-1", 1, "hit")]})
    assert port.retrieve("foo", ticker="") == {"chunks": []}


def test_empty_filing_date_filter_returns_empty_chunks() -> None:
    port = FakeRetrievePort(hits_by_query={"foo": [_mk_chunk("DOC-1", 1, "hit")]})
    assert port.retrieve("foo", filing_date="") == {"chunks": []}


def test_nonempty_edgar_filters_are_noops_on_fake() -> None:
    """The fake has no manifest metadata table — non-empty filters pass
    through to the dict lookup (parity with BM25/MCP when no manifest
    metadata is wired)."""
    chunks = [_mk_chunk("DOC-1", 1, "hit")]
    port = FakeRetrievePort(hits_by_query={"foo": chunks})
    out = port.retrieve(
        "foo",
        cik="0000320193",
        form="10-Q",
        ticker="AAPL",
        filing_date="2023-08-04",
    )
    assert out == {"chunks": chunks}


def _mk_chunk(document_id: str, page: int, text: str):
    from fin.retrieve.port import Chunk
    return Chunk(
        text=text,
        source_ref=Locator(
            document_id=document_id,
            version="v-" + "a" * 60,
            page=page,
        ),
        score=1.0,
    )


# ----- spec SC: retrieve envelope shape (no full 资料 body) -----


def test_fake_retrieve_empty_hits_yield_chunks_only_no_body() -> None:
    """Spec SC: Fake retrieve still maps empty hits to
    ``{ chunks: [] }`` and the retrieve envelope carries no full 资料
    body field.

    The retrieve envelope is the contract shape ADR 0009 / ADR 0031
    pin — ``{"chunks": [...]}`` plus the §B3 degradation fields — and
    the spec adds that it MUST NOT carry a full 资料 body field
    (``body`` / ``content``) on the envelope or on the chunks
    themselves. The Fake mirrors the canonical MCP adapter's envelope
    shape exactly: an empty hit answer is the same dict shape, just
    with an empty ``chunks`` list.

    This test pins three invariants on the empty-hit answer:

    * the envelope has exactly the ``chunks`` key (plus, optionally,
      ``source_ref`` — see :class:`FakeRetrievePort.retrieve`);
    * the envelope has NO ``body`` or ``content`` key at the top
      level;
    * every chunk in the envelope has only the fields the §B3 / ADR
      0009 chunk schema declares (``text`` / ``source_ref`` /
      ``score``) and no ``body`` / ``content`` / full-text keys.

    A future change that smuggles a full 资料 body into the retrieve
    envelope would (a) leak the full unit text past the cite gate's
    per-slice boundary and (b) break the canonical envelope parity
    with the MCP adapter. Both are caught here.
    """
    port = FakeRetrievePort(
        hits_by_query={"alpha": [_chunk("hit")]},
        known_document_ids={"DOC-2023Q1"},
    )

    envelope = port.retrieve("nonexistent query")

    # 1. Empty hits → ``{"chunks": []}`` (possibly plus source_ref
    #    carried by the fake); no ``body`` / ``content`` keys.
    assert envelope["chunks"] == []
    forbidden_top_level = {"body", "content"}
    present_top_level = set(envelope)
    assert not (forbidden_top_level & present_top_level), (
        f"retrieve envelope carries forbidden full-body fields: "
        f"{forbidden_top_level & present_top_level}"
    )


def test_fake_retrieve_with_hits_has_no_full_body_field_per_chunk() -> None:
    """The non-empty retrieve envelope is the same shape: ``chunks``
    plus optional ``source_ref``, never a full 资料 body field.

    The full unit body rides the **push** wire, not the retrieve
    response. The retrieve response carries only per-slice fields
    (``text`` / ``source_ref`` / ``score``) so the citation gate can
    quote a slice without leaking the whole 资料.
    """
    chunks = [_chunk("slice-1"), _chunk("slice-2", page=2)]
    port = FakeRetrievePort(
        hits_by_query={"alpha": chunks},
        known_document_ids={"DOC-2023Q1"},
    )

    envelope = port.retrieve("alpha")

    assert "chunks" in envelope
    assert envelope["chunks"], "fixture should yield hits"
    forbidden_top_level = {"body", "content"}
    assert not (forbidden_top_level & set(envelope)), (
        f"non-empty retrieve envelope carries forbidden fields: "
        f"{forbidden_top_level & set(envelope)}"
    )
    forbidden_chunk_keys = {"body", "content"}
    for chunk in envelope["chunks"]:
        # The chunk's ``text`` is the per-slice text — that's allowed.
        # The forbidden keys are the full 资料 body carriers, which
        # would let the cite gate bypass the per-slice boundary.
        from dataclasses import asdict
        chunk_keys = set(asdict(chunk))
        leaked = forbidden_chunk_keys & chunk_keys
        assert not leaked, (
            f"chunk carries forbidden full-body fields: {leaked} "
            f"(chunk keys: {sorted(chunk_keys)})"
        )
