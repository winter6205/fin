"""Plan D0 fin-side T7: opt-in-style byte-identical round-trip through the
MCP transport (§B4 「不二次切分」 + ADR 0037; spec
`operator-console-host-contract` T1).

The §B4 contract is unambiguous: the retrieval service stores the
body and slices verbatim and recalls them byte-identical. fin does
NOT pass the text through any second chunker, normalizer, or sliding
window between the :func:`MCPPushPort._to_push_item` projection and
the :func:`map_chunks_to_fin_chunks` re-read — both adapters are
byte-level projections on a wire schema they don't own.

After T1 the wire shape is **one envelope per 资料** (``source_ref``
+ body + ``chunks[]``); the byte-identical assertion is now about
the body and the chunk contents that ride inside one envelope.

This module asserts that contract at the adapter level by closing a
round-trip in-process: the push side's script returns chunk_ids; the
retrieve side's script returns chunks whose ``content`` is exactly
the body / slice text that was pushed. No subprocess is spawned —
the scripted-transport seam is the same hermetic pattern used by
``test_mcp_adapter.py`` / ``test_mcp_push_port.py`` / the rest of
the default suite (ADR 0032 / SC6).

What the test covers
--------------------

* :class:`MCPPushPort` projects envelope ``body`` + ``chunks`` to
  the wire ``content`` / ``chunks`` fields byte-identically (no
  normalization, no slicing, no trimming).
* :class:`MCPRetrieveAdapter` (via ``map_chunks_to_fin_chunks``)
  reads chunk ``content`` byte-identically.
* The two adapters close the round-trip: every pushed envelope's
  body and chunk contents reappear verbatim in some retrieved
  chunk.

The real-subprocess opt-in counterpart (push + retrieve against a
live ``iknow-memory`` process) lives in Stage 2 (Plan D) and is
excluded from the default ``uv run pytest -q``.
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

from fin.corpus.parsers._common import UnitLocator
from fin.corpus.push import (
    REQUIRED_PUSH_METADATA_FIELDS,
    PushChunk,
    PushEnvelope,
    PushRecord,
    content_identity,
    envelope_of_unit,
    serialize_source_ref,
)
from fin.corpus.chunking import split_unit
from fin.corpus.normalization import NORMALIZATION_RULE_VERSION
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
    (``{document_id, version, unit}`` via
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
        metadata["normalization_rule_version"] = NORMALIZATION_RULE_VERSION
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


def _build_envelopes(n: int) -> tuple[PushEnvelope, ...]:
    """Build one envelope per ``n`` frozen unit (post-T1 wire shape)."""
    envelopes: list[PushEnvelope] = []
    for i in range(n):
        body = (
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
        metadata["normalization_rule_version"] = NORMALIZATION_RULE_VERSION
        envelopes.append(
            PushEnvelope(
                source_ref=source_ref,
                body=body,
                chunks=(PushChunk(index=0, content=body),),
                metadata=metadata,
                valid_from="0001-01-01T00:00:00.000Z",
                valid_until="9999-12-31T23:59:59.999Z",
                body_content_id=content_identity(
                    body,
                    normalization_rule_version=NORMALIZATION_RULE_VERSION,
                    parse_rule_version="v-{0:03d}".format(i),
                    params_table_version="v-{0:03d}".format(i),
                ),
            )
        )
    return tuple(envelopes)


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
        self._seen_keys: dict[tuple[str, str], bool] = {}
        self.sent_items: list[dict[str, Any]] = []

    async def _call_mcp(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.sent_items = list(payload["params"]["arguments"]["items"])
        # One chunk_id per chunk across all envelopes (the new wire
        # shape: chunk_ids is parallel to chunks, not envelopes).
        chunk_count = sum(len(item["chunks"]) for item in self.sent_items)
        chunk_ids = [f"chunk-{index}" for index in range(chunk_count)]
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
        # After T1 the wire chunk carries ``content``; one chunk per
        # pushed slice, in order across all envelopes.
        chunks: list[dict[str, Any]] = []
        chunk_index = 0
        for item in self._items:
            for chunk in item["chunks"]:
                chunks.append(
                    {
                        "id": f"chunk-{chunk_index}",
                        "content": chunk["content"],
                        "source_ref": item["source_ref"],
                        "valid_window": ["2025-01-01T00:00:00.000Z", None],
                        "score": 1.0,
                    }
                )
                chunk_index += 1
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
    """``MCPPushPort`` projects envelope ``body`` and ``chunks`` to
    the wire ``content`` / ``chunks`` fields byte-identically — no
    normalization, no trimming, no Unicode round-trip loss.

    This is the « push leg → wire » side of §B4 「不二次切分」. The
    full round-trip is exercised below.
    """
    push = _ScriptedPushPort()
    envelopes = _build_envelopes(3)
    push.push([envelope.to_wire() for envelope in envelopes])

    assert len(push.sent_items) == len(envelopes)
    for sent, envelope in zip(push.sent_items, envelopes):
        # The wire ``content`` equals ``envelope.body`` byte-for-byte
        # — the adapter does not pass through any slicer / normalizer.
        assert sent["content"] == envelope.body
        # The §B4 metadata keys are verbatim the schema field names.
        for field in REQUIRED_PUSH_METADATA_FIELDS:
            assert field in sent["metadata"]


def test_retrieve_adapter_reads_chunk_content_byte_identically() -> None:
    """``MCPRetrieveAdapter`` (via ``map_chunks_to_fin_chunks``)
    reads chunk ``content`` byte-identically — the « wire → retrieve
    leg » side of §B4 「不二次切分」.
    """
    adapter = _ScriptedRetrieveAdapter()
    body_text = (
        "  leading whitespace\n末行 — punctuation: <>&\"'\n  trailing   \n"
    )
    items = [
        {
            "content": body_text,
            "source_ref": serialize_source_ref(
                UnitLocator(document_id="DOC-2023Q1", version="v000", unit=1)
            ),
            "chunks": [{"content": body_text, "index": 0}],
        }
    ]
    adapter.load_pushed_items(items)
    response = adapter.retrieve("query")
    chunks = response["chunks"]
    assert len(chunks) == 1
    assert chunks[0].text == body_text


def test_push_then_retrieve_round_trip_is_byte_identical() -> None:
    """The full round-trip: push via ``MCPPushPort`` then retrieve via
    ``MCPRetrieveAdapter`` preserves envelope body and slice text
    byte-for-byte.

    The §B4 invariant is that the bytes that leave fin equal the
    bytes that come back, and that fin never inserts any pass over
    the text in between. A round-trip assertion closes that loop
    and catches any future change that would, for example, normalize
    whitespace on the push side or trim on the retrieve side.
    """
    push = _ScriptedPushPort()
    envelopes = _build_envelopes(5)
    push.push([envelope.to_wire() for envelope in envelopes])

    adapter = _ScriptedRetrieveAdapter()
    adapter.load_pushed_items(push.sent_items)
    response = adapter.retrieve("any query")
    chunks = response["chunks"]

    # Every pushed envelope's chunk content reappears in some
    # retrieved chunk, byte-for-byte, in the same order.
    expected_texts = [
        chunk.content
        for envelope in envelopes
        for chunk in envelope.chunks
    ]
    retrieved_texts = [chunk.text for chunk in chunks]
    assert retrieved_texts == expected_texts


def test_push_does_not_insert_a_chunker_between_fin_and_service() -> None:
    """A wider guard: no envelope's body is ever sliced into multiple
    wire items, and no wire item aggregates multiple envelopes'
    bodies.

    §B4 + ADR 0037 mean one envelope maps to one ``content`` field
    (the body) and its ``chunks[]`` is the canonical slice list.
    The round-trip assertion above already catches the count
    mismatch indirectly; this test pins the 1:1 correspondence on
    the push leg.
    """
    push = _ScriptedPushPort()
    envelopes = _build_envelopes(7)
    push.push([envelope.to_wire() for envelope in envelopes])

    assert len(push.sent_items) == len(envelopes)
    # Each ``content`` is the original envelope's body exactly — no
    # concatenation, no slicing.
    for sent, envelope in zip(push.sent_items, envelopes):
        assert sent["content"] == envelope.body


def test_push_is_idempotent_under_replay() -> None:
    """Pushing the same batch twice (the §B1「幂等基准」shape: 3
    consecutive pushes) yields no fresh writes and no chunk-id drift.

    The adapter's ``(source_ref, body_content_id)`` idempotency
    ledger recognizes the replay and reports ``duplicates`` rather
    than a fresh ``written`` batch.
    """
    push = _ScriptedPushPort()
    envelopes = _build_envelopes(3)
    wires = [envelope.to_wire() for envelope in envelopes]
    push.push(wires)
    # Replay with the same scripted push adapter (its
    # ``_seen_keys`` ledger keys on (source_ref, body_content_id)).
    outcome = push.push(wires)
    assert outcome["written"] == 0
    assert outcome["duplicates"] == sum(
        len(envelope.chunks) for envelope in envelopes
    )
