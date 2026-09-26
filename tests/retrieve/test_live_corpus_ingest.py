"""SC2: live corpus ingest reaches the real MCP server with >= 1 chunk.

The default ``uv run pytest -q`` never runs this file — it is marked
``mcp`` and ``tests/retrieve/conftest.py`` deselects that mark unless
``-m mcp`` names it, so the default suite stays hermetic (SC6). The
opt-in command documented in ``plans/first-closed-loop.md`` is::

    uv run pytest -q -m mcp tests/retrieve/test_live_corpus_ingest.py

What the ticket covers, on the wire
-----------------------------------

The product ingest chain already exists (``fin.host.workspace_http``
builds :class:`~fin.corpus.ingest_endpoint.IngestEndpoint` with
``MCPPushPort`` for the ``mcp`` adapter kind). What was missing is
committed evidence that the chain is reachable end to end: this test
drives one real repo fixture through ``ingest`` — real parse, real
切分, real MCP push — and then reads it back through the **same live
session** with the product retrieve adapter, asserting spec SC2:

* ``chunks`` length >= 1;
* every chunk's ``source_ref`` is the canonical fin serialization —
  the mapped ``Locator`` must equal a triple obtained by parsing a
  pushed record's ``source_ref`` through
  :func:`fin.corpus.push.deserialize_source_ref`, so a service-side id
  wearing the field name cannot pass;
* every retrieved chunk text is **byte-identical** to one of the
  records the endpoint pushed (the ADR 0037 no-re-chunk contract,
  asserted in the style of ``test_live_mcp_t2.py``).

The endpoint's own accounting invariant is asserted too:
``block_count == written + duplicates`` and ``written >= 1``.

Why the harness session is threaded in by hand
----------------------------------------------

``_build_default_ingest_service`` lets ``MCPPushPort()`` spawn its
own subprocess, and that process would own its own in-memory store —
a retrieve on the harness session would then see nothing. The
production wiring and this test differ only in *which* session the
port calls; ``MCPPushPort(session=...)`` routes ``_call_mcp`` through
the shared session instead of spawning.

SC6 (attach != push) is pinned elsewhere and is deliberately not
restated here: ``tests/host/test_thread_attach.py``
``test_attach_and_send_never_ingest_or_push`` and
``test_attach_does_not_trigger_mcp_read_source`` are the tripwires.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

import pytest

from fin.corpus.ingest_endpoint import IngestEndpoint
from fin.corpus.parsers._common import UnitLocator
from fin.corpus.push import deserialize_source_ref
from fin.retrieve.mcp_push_port import MCPPushPort

from live_iknow_support import IKNOW_CORPUS_READY_ROOT, live_iknow


pytestmark = pytest.mark.mcp

_PROJECT_ROOT = Path(__file__).resolve().parents[2]

# The fixture the ingest-route tests use
# (``tests/host/test_workspace_http_ingest.py``), pushed through the
# real parse + 切分 chain rather than a hand-built envelope.
_FIXTURE = (
    _PROJECT_ROOT
    / "data/corpus/dataset/documents/md/AAPL-2022Q3__table_dense__md__md.md"
)

# The fixture's dataset label is ``table_dense``, but that axis is the
# *scene* selector for 切分 parameters — ``"md"`` (the file format) is
# not in the params table's vocabulary and would raise
# ``IngestInvalidDocTypeError``. ``narrative`` is the documented
# choice for prose extraction of this file.
_DOC_TYPE = "narrative"

# Observed on a live run of this same fixture (2026-09-16, corpus-ready
# iknow worktree): this query is one the fixture's text ranks for, and
# the same call returned ``{"chunks": []}`` **before** the ingest below
# and 20 chunks after it. A live caveat measured while writing this
# test: the live envelope fills with chunks even for a token absent
# from the corpus, so chunk-count alone does not discriminate on the
# query — the pre-ingest baseline below is what pins「retrieve became
# non-empty *because of* the ingest」.
_QUERY = "operating revenue"
_LIMIT = 8


@pytest.fixture
def corpus_ready_live(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """A live session against the corpus-ready iknow worktree build.

    The checkout is machine-local and its branch carries the pushed-slice
    persistence this test needs. The operator-console build that
    ``IKNOW_WORKTREE_ROOT`` points at predates it: its ``push`` answers
    one chunk id per envelope, so the ingest endpoint's own accounting
    raises ``PushProtocolError`` (14 ids for 61 pushed chunks) before a
    retrieve could even run — see the worktree note in
    ``live_iknow_support``. An absent build skips cleanly, the same way
    ``test_live_mcp_read_source.py`` guards its own worktree.
    """

    entry = IKNOW_CORPUS_READY_ROOT / "dist" / "index.js"
    if not entry.is_file():
        pytest.skip(
            f"{entry} is missing; run `npm run build` in "
            f"{IKNOW_CORPUS_READY_ROOT} to exercise the live corpus "
            "ingest path"
        )
    with live_iknow(monkeypatch, root=IKNOW_CORPUS_READY_ROOT) as live:
        yield live


def _pushed_locator_triples(
    result: Any,
) -> set[UnitLocator]:
    """The canonical locator triples behind the ingest's own records.

    Each pushed record's ``source_ref`` is the fin-side canonical
    serialization (:func:`fin.corpus.push.serialize_source_ref`), so
    parsing it here gives the ground truth a retrieved chunk's
    ``source_ref`` must match. Comparing against a freshly parsed
    pushed record — rather than re-serializing the retrieved value —
    is what makes「canonical」non-circular: garbage could round-trip
    through itself, but it cannot equal a locator derived from the
    record that was actually pushed.
    """

    return {
        deserialize_source_ref(record.source_ref)
        for record in result.records
    }


def test_live_corpus_ingest_then_retrieve_returns_byte_identical_chunks(
    corpus_ready_live: Any,
) -> None:
    """SC2: ingest a repo fixture, then retrieve >= 1 canonical chunk.

    A retrieve on the same query runs first as the empty-store
    baseline (SC3's shape, measured {"chunks": []}): without it a
    passing chunk count would also be consistent with an ingest that
    did nothing. Then the ingest runs and the same retrieve must
    return >= 1 canonical, byte-identical chunk.
    """

    live = corpus_ready_live

    # The empty-store baseline, same query and limit: measured
    # ``{"chunks": []}`` on the live server before the ingest. It is the
    # load-bearing half of SC2 — without it a passing chunk count would
    # also be consistent with the ingest having done nothing.
    before = live.retrieve.retrieve(_QUERY, limit=_LIMIT)
    assert "error" not in before, before
    assert before["chunks"] == [], (
        "the live store is not empty before this test's ingest; the "
        f"chunk assertions below would not be attributable: {before!r}"
    )

    endpoint = IngestEndpoint(
        push_port=MCPPushPort(session=live.session, timeout_seconds=20.0),
        invalidation_port=live.invalidation,
    )
    result = endpoint.ingest(
        _FIXTURE.read_bytes(),
        filename=_FIXTURE.name,
        document_id="AAPL-2022Q3",
        doc_type=_DOC_TYPE,
    )

    # Endpoint accounting: the outcome's own invariant, and a real push.
    assert result.document_id
    assert result.version
    assert result.unit_count >= 1
    assert result.written >= 1
    assert result.block_count == result.written + result.duplicates
    assert len(result.block_ids) == result.block_count
    assert result.records, "ingest pushed no records"

    answer = live.retrieve.retrieve(_QUERY, limit=_LIMIT)
    assert "error" not in answer, answer
    chunks = answer["chunks"]
    assert len(chunks) >= 1, (
        f"SC2: ingest of {_FIXTURE.name} pushed {result.written} chunks "
        f"but retrieve({_QUERY!r}) returned none: {answer!r}"
    )

    pushed_locators = _pushed_locator_triples(result)
    pushed_texts = {record.text for record in result.records}
    for chunk in chunks:
        locator = chunk.source_ref
        assert locator.document_id == result.document_id
        assert locator.version == result.version
        # SC2's「canonical source_ref」: the retrieved reference must be
        # one of the triples the ingest actually pushed, so the chunk
        # resolves back to a fin locator rather than to a service id.
        assert (
            UnitLocator(
                document_id=locator.document_id,
                version=locator.version,
                unit=locator.page,
            )
            in pushed_locators
        ), f"retrieved chunk points at {locator!r}, not a pushed 资料"
        assert chunk.text in pushed_texts, (
            "retrieved chunk is not byte-identical to any pushed record "
            "text (ADR 0037 forbids a second 切分 on the read path)"
        )
