"""Plan D0 fin-side T7: opt-in-style byte-identical round-trip through the
MCP transport (§B4 「不二次切分」 + ADR 0037).

The §B4 contract is unambiguous: the retrieval service stores the text
verbatim and recalls it byte-identical. fin does NOT pass the text
through any second chunker, normalizer, or sliding window between the
:func:`MCPPushPort._to_push_item` projection and the
:func:`map_chunks_to_fin_chunks` re-read — both adapters are byte-
level projections on a wire schema they don't own.

This module asserts that contract at the adapter level by closing a
round-trip in-process: the push side's script returns chunk_ids; the
retrieve side's script returns chunks whose ``content`` is exactly
the ``text`` that was pushed. No subprocess is spawned — the
scripted-transport seam is the same hermetic pattern used by
``test_mcp_adapter.py`` / ``test_mcp_push_port.py`` / the rest of the
default suite (ADR 0032 / SC6).

What the test covers
--------------------

* :class:`MCPPushPort` projects ``PushRecord.text`` to the wire
  ``content`` field byte-identically (no normalization, no slicing,
  no trimming).
* :class:`MCPRetrieveAdapter` (via ``map_chunks_to_fin_chunks``)
  reads chunk ``content`` byte-identically.
* The two adapters close the round-trip: every pushed record's
  ``text`` appears verbatim in some retrieved chunk.

The real-subprocess opt-in counterpart (push + retrieve against a live
``iknow-memory`` process) lives in Stage 2 (Plan D) and is excluded
from the default ``uv run pytest -q``.
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

from fin.corpus.extracted import Locator
from fin.corpus.parsers._common import UnitLocator
from fin.corpus.push import (
    REQUIRED_PUSH_METADATA_FIELDS,
    PushRecord,
    serialize_source_ref,
)
from fin.retrieve.mcp_adapter import MCPRetrieveAdapter
from fin.retrieve.mcp_push_port import MCPPushPort


def _build_records(n: int) -> tuple[PushRecord, ...]:
    """Build ``n`` push records with deterministic, distinctive text.

    Each record's ``text`` contains the block id and a UTF-8 line that
    covers the cases §B4 forbids the service to rewrite (newlines,
    unicode, surrounding whitespace, ASCII punctuation). If the
    round-trip slices, normalizes or trims anything, the assertion
    below catches it.

    ``source_ref`` is the canonical B4 locator serialization
    (``{document_id, version, page}`` via
    :func:`fin.corpus.push.serialize_source_ref`), so the retrieve
    side's ``_locator_of_chunk`` can deserialize it back to a
    :class:`UnitLocator` without consulting ``locator_map``.
    """
    records: list[PushRecord] = []
    for i in range(n):
        text = (
            f"BLOCK-{i:03d}\n"
            "  indented line (NBSP) followed\n"
            "\ttab-indented line\n"
            "末行 — punctuation: <>&\"'\n"
            "  trailing whitespace   \n"
        )
        source_ref = serialize_source_ref(
            UnitLocator(
                document_id="DOC-2023Q1",
                version=f"v{i:03d}",
                unit=i + 1,
            )
        )
        metadata = {field: f"v-{i:03d}" for field in REQUIRED_PUSH_METADATA_FIELDS}
        records.append(
            PushRecord(
                block_id=f"b-{i:03d}",
                source_ref=source_ref,
                content_id=f"c-{i:03d}",
                text=text,
                parent_block_id="b-parent",
                parent_text="parent text",
                metadata=metadata,
            )
        )
    return tuple(records)


# ----- scripted MCP transport overrides -----


class _ScriptedPushPort(MCPPushPort):
    """An ``MCPPushPort`` whose ``_call_mcp`` answers a canned response.

    Skips the parent ``__init__`` so the test does not allocate a
    subprocess. The script returns ``{"chunk_ids": [...]}`` and
    remembers what was sent, so a paired retrieve can echo the text.
    """

    def __init__(self) -> None:
        self._command: list[str] | None = ["iknow-memory"]
        self._timeout: float = 5.0
        self._ledger = None  # type: ignore[assignment]
        self._seen_keys: dict[tuple[str, str], str] = {}
        self.sent_items: list[dict[str, Any]] = []

    async def _call_mcp(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.sent_items = list(payload["params"]["arguments"]["items"])
        # One chunk_id per item, in order, mirroring the iknow push
        # tool's contract (``{"chunk_ids": [...]}`` envelope).
        chunk_ids = [f"chunk-{index}" for index in range(len(self.sent_items))]
        body = {"chunk_ids": chunk_ids}
        return {
            "result": {
                "content": [
                    {"type": "text", "text": json.dumps(body)}
                ]
            }
        }


class _ScriptedRetrieveAdapter(MCPRetrieveAdapter):
    """An ``MCPRetrieveAdapter`` whose ``_call_mcp`` echoes the
    pushed items as canonical §B4 chunks.

    The echo's ``content`` is taken **byte-identical** from what was
    pushed — the same fields the test asserts against on the push
    side. Any re-slicing in the adapter path would surface here as a
    mismatch, because the round-trip text has to come back
    unaltered.
    """

    def __init__(self) -> None:
        self._command: list[str] | None = ["iknow-memory"]
        self._timeout: float = 5.0
        self._locator_map = {}
        self._known_doc_ids = None  # type: ignore[assignment]
        self._mapping_ledger = None  # type: ignore[assignment]
        self._input_validation_ledger = None  # type: ignore[assignment]
        self._items: list[dict[str, Any]] = []

    def load_pushed_items(self, items: Sequence[Mapping[str, Any]]) -> None:
        """Snapshot the items the push side sent so the retrieve side
        can echo them as canonical chunks."""
        self._items = list(items)

    async def _call_mcp(self, payload: dict[str, Any]) -> dict[str, Any]:
        chunks: list[dict[str, Any]] = []
        for index, item in enumerate(self._items):
            chunks.append(
                {
                    "id": f"chunk-{index}",
                    "content": item["content"],
                    "source_ref": item["source_ref"],
                    "valid_window": ["2025-01-01T00:00:00.000Z", None],
                    "score": 1.0,
                }
            )
        body = {"chunks": chunks}
        return {
            "result": {
                "content": [
                    {"type": "text", "text": json.dumps(body)}
                ]
            }
        }


# ----- contract assertions -----


def test_push_projects_text_to_content_byte_identically() -> None:
    """``MCPPushPort`` projects ``PushRecord.text`` to the wire
    ``content`` field byte-identically — no normalization, no
    trimming, no Unicode round-trip loss.

    This is the « push leg → wire » side of §B4 「不二次切分」. The
    full round-trip is exercised below.
    """
    push = _ScriptedPushPort()
    records = _build_records(3)
    push.push([record.to_wire() for record in records])

    assert len(push.sent_items) == len(records)
    for sent, record in zip(push.sent_items, records):
        # The wire ``content`` equals ``record.text`` byte-for-byte
        # — the adapter does not pass through any slicer / normalizer.
        assert sent["content"] == record.text
        # The §B4 metadata keys are verbatim the schema field names.
        for field in REQUIRED_PUSH_METADATA_FIELDS:
            assert field in sent["metadata"]


def test_retrieve_adapter_reads_chunk_content_byte_identically() -> None:
    """``MCPRetrieveAdapter`` (via ``map_chunks_to_fin_chunks``)
    reads chunk ``content`` byte-identically — the « wire → retrieve
    leg » side of §B4 「不二次切分」.
    """
    adapter = _ScriptedRetrieveAdapter()
    items = [
        {
            "content": "  leading whitespace\n末行 — punctuation: <>&\"'\n  trailing   \n",
            "source_ref": serialize_source_ref(
                UnitLocator(document_id="DOC-2023Q1", version="v000", unit=1)
            ),
        }
    ]
    adapter.load_pushed_items(items)
    response = adapter.retrieve("query")
    chunks = response["chunks"]
    assert len(chunks) == 1
    assert chunks[0].text == items[0]["content"]


def test_push_then_retrieve_round_trip_is_byte_identical() -> None:
    """The full round-trip: push via ``MCPPushPort`` then retrieve via
    ``MCPRetrieveAdapter`` preserves ``PushRecord.text`` byte-for-byte.

    The §B4 invariant is that the bytes that leave fin equal the
    bytes that come back, and that fin never inserts any pass over
    the text in between. A round-trip assertion closes that loop
    and catches any future change that would, for example, normalize
    whitespace on the push side or trim on the retrieve side.
    """
    push = _ScriptedPushPort()
    records = _build_records(5)
    push.push([record.to_wire() for record in records])

    adapter = _ScriptedRetrieveAdapter()
    adapter.load_pushed_items(push.sent_items)
    response = adapter.retrieve("any query")
    chunks = response["chunks"]

    # Every pushed record's text reappears in some retrieved chunk,
    # byte-for-byte, in the same order.
    assert len(chunks) == len(records)
    retrieved_texts = [chunk.text for chunk in chunks]
    expected_texts = [record.text for record in records]
    assert retrieved_texts == expected_texts


def test_push_does_not_insert_a_chunker_between_fin_and_service() -> None:
    """A wider guard: no record's text is ever sliced into multiple
    wire items, and no wire item aggregates multiple records' text.

    §B4 + ADR 0037 mean one child block maps to one ``content``
    field, in order. The round-trip assertion above already catches
    the count mismatch indirectly; this test pins the 1:1
    correspondence on the push leg.
    """
    push = _ScriptedPushPort()
    records = _build_records(7)
    push.push([record.to_wire() for record in records])

    assert len(push.sent_items) == len(records)
    # Each ``content`` is the original record's text exactly — no
    # concatenation, no slicing.
    for sent, record in zip(push.sent_items, records):
        assert sent["content"] == record.text


def test_push_is_idempotent_under_replay() -> None:
    """Pushing the same batch twice (the §B1「幂等基准」shape: 3
    consecutive pushes) yields no fresh writes and no chunk-id drift.

    The adapter's ``(source_ref, content_id)`` idempotency ledger
    recognizes the replay and reports ``duplicates`` rather than a
    fresh ``written`` batch.
    """
    push = _ScriptedPushPort()
    records = _build_records(3)
    wire_records = [record.to_wire() for record in records]

    outcomes = [push.push(wire_records) for _ in range(3)]

    # First push writes; subsequent pushes are all duplicates.
    assert outcomes[0]["written"] == 3
    assert outcomes[0]["duplicates"] == 0
    assert outcomes[1]["written"] == 0
    assert outcomes[1]["duplicates"] == 3
    assert outcomes[2]["written"] == 0
    assert outcomes[2]["duplicates"] == 3

    # The chunk ids do not drift across replays — a replay returns
    # the same ids as the original write.
    assert outcomes[0]["block_ids"] == outcomes[1]["block_ids"]
    assert outcomes[1]["block_ids"] == outcomes[2]["block_ids"]