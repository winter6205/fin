"""Boundary tests for ``FakeRetrievePort``.

The fake port fills the seam between the host graph and the
retrieve capability (ADR 0030, T3). T3 acceptance: default
``uv run pytest -q`` runs without an MCP process or live BM25;
the fake port backs the host graph's tool bind so the test
surface is hermetic.

Boundary classes (mirrors ``RetrievePort`` semantics so tests
do not care which implementation backs a ``retrieve`` call):

* empty: empty / whitespace-only query → ``{"chunks": []}``
* negative: unknown ``document_id`` (when ``known_document_ids``
  is set) → ``{"chunks": []}``
* overflow: ``limit > 20`` clamps to 20; ``limit <= 0`` falls
  back to 8
* concurrent: read-only access; thread-safe by construction
* exception: N/A — the fake port cannot raise on construction
  or retrieval (no subprocess, no I/O).
"""

from __future__ import annotations

import concurrent.futures

from fin.corpus.extracted import Locator
from fin.retrieve import Chunk, FakeRetrievePort, RetrievePort


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


def test_empty_query_returns_empty_chunks() -> None:
    """Boundary 'empty': empty / whitespace-only query yields
    ``{"chunks": []}`` without raising."""
    port = FakeRetrievePort()
    for empty_query in ("", "   ", "\t\n", "\n"):
        assert port.retrieve(empty_query) == {"chunks": []}


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