"""T3-E: the retrieved-hit ↔ bounded ``read_source`` same-unit contract.

The plan line (``plans/permanent-corpus-upload.md`` T3 acceptance):
「a retrieved hit and exact bounded ``read_source`` resolve that same
source unit and text」. The chain exists in production code — the push
side freezes one envelope per unit (``fin.corpus.push.envelopes_of_document``),
retrieve maps the wire ``source_ref`` back to a fin locator
(``fin.retrieve.hit_mapping.map_chunks_to_fin_chunks``), and
``MCPRetrieveAdapter.read_slice`` fetches that unit through ``read_source``
— but until now the whole chain was chained only inside scattered live
e2e files. This file pins it hermetically against the scripted
``_call_mcp`` seam (the documented transport seam of
``test_mcp_adapter.py``), driven by the real product pipeline over a
committed representative corpus fixture:

1. push side — parse → 切分 → one envelope per frozen unit; the
   envelope ``body`` is the frozen unit text recorded BEFORE any read;
2. retrieve side — the wire chunks carry exactly the pushed envelopes'
   ``source_ref`` strings; every mapped hit must deserialize
   (:func:`fin.corpus.push.deserialize_source_ref`) to the canonical
   ``{document_id, version, unit}`` triple of a pushed unit;
3. read side — every ``read_source`` payload the adapter puts on the
   wire must carry one of those SAME canonical strings (byte-identical,
   asserted against the push-side artifact, not a fresh re-serialization),
   and the resolved text must be the frozen body of exactly the unit the
   hit pointed at;
4. miss shapes — a non-canonical (extra-key / string-unit /
   non-sorted-key) reference never becomes a locator or a body: the hit
   is dropped and counted, the read folds to the typed
   ``{"error": "unknown_locator"}``, never an empty or partial success;
5. ordering / bound — units resolve in unit order whatever order the
   hits arrived in, a read answer carries exactly the slice shape (no
   window or page fields), and one locator costs exactly one round trip:
   a read never fans out to a window of neighbouring units.

The hop the tests build explicitly: ``read_slice`` takes the locator the
caller already holds, so the same-unit promise closes by handing the
mapped hit's own triple to the read. These tests derive that triple from
the retrieved hits rather than from the push side, so a read that
silently re-addressed a different unit still has to prove it.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from fin.corpus.chunking import split_document, split_unit
from fin.corpus.extracted import Locator
from fin.corpus.parsers import parse_document
from fin.corpus.parsers._common import UnitLocator, build_unit
from fin.corpus.push import (
    PushEnvelope,
    deserialize_source_ref,
    envelope_of_unit,
    envelopes_of_document,
    serialize_source_ref,
)
from fin.retrieve.hit_mapping import (
    MAPPING_FAILURE_MALFORMED_HIT,
    MappingFailureLedger,
    map_chunks_to_fin_chunks,
)
from fin.retrieve.mcp_adapter import MCPRetrieveAdapter
from fin.retrieve.source_read import (
    SOURCE_READ_MAX_TEXT_CODE_POINTS,
    is_slice_result,
)


_PROJECT_ROOT = Path(__file__).resolve().parents[2]

# The committed representative fixture the live ingest test already
# uses (``test_live_corpus_ingest.py``): a real corpus document with
# several frozen units, parsed through the real product pipeline.
_FIXTURE = (
    _PROJECT_ROOT
    / "data/corpus/dataset/documents/md/AAPL-2022Q3__table_dense__md__md.md"
)
_DOC_ID = "T3X-AAPL-2022Q3"
_DOC_TYPE = "narrative"  # same documented choice as the live ingest test


def _pushed_envelopes() -> tuple[PushEnvelope, str]:
    """Run the real push-side pipeline over the committed fixture.

    Returns the envelopes (one per frozen unit) plus the version. The
    envelope ``body`` is the frozen unit text — recorded here, before
    any read, as the ground truth the read path must reproduce.
    """
    data = _FIXTURE.read_bytes()
    version = hashlib.sha256(data).hexdigest()
    parsed = parse_document(
        data, fmt="md", document_id=_DOC_ID, version=version
    )
    chunking = split_document(parsed, doc_type=_DOC_TYPE)
    envelopes = envelopes_of_document(
        chunking,
        valid_from=version,
        valid_until="9999-12-31T23:59:59.999Z",
    )
    return envelopes, version


def _source_ref_of(chunk: Any) -> dict[str, Any]:
    """The read request for one retrieved hit: the hit's own locator
    triple, taken field by field off the mapped ``Chunk``."""
    locator = chunk.source_ref
    return {
        "document_id": locator.document_id,
        "version": locator.version,
        "unit": locator.unit,
    }


def _read_unit(adapter: MCPRetrieveAdapter, source_ref: dict[str, Any]) -> str:
    """One unit's text as the caller reassembles it: follow the cursor
    to null, each step a bounded slice of the same locator."""
    collected = ""
    position: int | None = 0
    while position is not None:
        result = adapter.read_slice(source_ref, position)
        assert is_slice_result(result), result
        collected += result["text"]
        position = result["next_position"]
    return collected


# ----- scripted transport: the same-server fake for retrieve + read -----


class _SameUnitServerAdapter(MCPRetrieveAdapter):
    """Scripted ``_call_mcp`` answering both ``retrieve`` and
    ``read_source`` off a dict of pushed envelopes, in the real wire
    shapes (canonical MCP content blocks), while recording every
    payload the adapter put on the wire.

    ``bodies`` maps the exact pushed ``source_ref`` string to the
    frozen unit text; a locator whose ref is absent gets the named
    ``read_source_not_found`` miss envelope — the real server's wire
    shape for a unit it never stored. A stored unit comes back as one
    bounded slice at the requested cursor, the way the real backend
    cuts at the code-point cap.
    """

    def __init__(
        self,
        *,
        envelopes: tuple[PushEnvelope, ...],
        known_document_ids: set[str] | None = None,
        query: str = "operating revenue",
    ) -> None:
        # Skip the parent __init__ seam contract: mirror the fields the
        # parent would set (same documented pattern as test_mcp_adapter).
        self._command = None
        self._locator_map: dict[str, Locator] = {}
        self._known_doc_ids = (
            frozenset(known_document_ids)
            if known_document_ids is not None
            else None
        )
        self._timeout = 5.0
        self._mapping_ledger = None
        self._input_validation_ledger = None
        self._session = None
        self.bodies = {env.source_ref: env.body for env in envelopes}
        self.query = query
        self.payloads: list[dict] = []

    async def _call_mcp(self, payload: dict) -> dict:  # type: ignore[override]
        self.payloads.append(payload)
        tool = payload["params"]["name"]
        if tool == "retrieve":
            chunks = [
                {
                    "source_ref": source_ref,
                    "content": content,
                    "score": 1.0,
                }
                for source_ref, content in self._wire_hits()
            ]
            inner = {"chunks": chunks}
            return {
                "result": {
                    "content": [
                        {"type": "text", "text": json.dumps(inner)}
                    ]
                }
            }
        if tool == "read_source":
            arguments = payload["params"]["arguments"]
            source_ref = arguments["source_ref"]
            position = int(arguments.get("position", 0))
            body = self.bodies.get(source_ref)
            if body is None:
                return {
                    "result": {
                        "isError": True,
                        "content": [
                            {
                                "type": "text",
                                "text": json.dumps(
                                    {
                                        "error": "read_source_not_found",
                                        "source_ref": source_ref,
                                    }
                                ),
                            }
                        ],
                    }
                }
            end = min(position + SOURCE_READ_MAX_TEXT_CODE_POINTS, len(body))
            inner = {
                "id": source_ref,
                "source_ref": source_ref,
                "content": body[position:end],
                "next_position": end if end < len(body) else None,
            }
            return {
                "result": {
                    "content": [
                        {"type": "text", "text": json.dumps(inner)}
                    ]
                }
            }
        raise AssertionError(f"unexpected tool call: {tool!r}")

    def _wire_hits(self) -> list[tuple[str, str]]:
        raise NotImplementedError  # subclasses choose the hit set

    def read_payload_refs(self) -> list[str]:
        """The exact ``source_ref`` strings the read path sent to
        ``read_source``, in wire order."""
        return [
            p["params"]["arguments"]["source_ref"]
            for p in self.payloads
            if p["params"]["name"] == "read_source"
        ]


class _AllUnitsServerAdapter(_SameUnitServerAdapter):
    """Retrieve answers with one wire chunk per pushed envelope, the
    content being that unit's first child slice."""

    def __init__(
        self,
        *,
        envelopes: tuple[PushEnvelope, ...],
        **kwargs: Any,
    ) -> None:
        super().__init__(envelopes=envelopes, **kwargs)
        self._hits = [
            (env.source_ref, env.chunks[0].content)
            for env in envelopes
        ]

    def _wire_hits(self) -> list[tuple[str, str]]:
        return list(self._hits)


@pytest.fixture(scope="module")
def pushed_and_hits() -> dict[str, Any]:
    """One module-level run: push-side envelopes plus the mapped
    retrieve hits of the scripted round trip."""
    envelopes, version = _pushed_envelopes()
    adapter = _AllUnitsServerAdapter(
        envelopes=envelopes,
        known_document_ids={_DOC_ID},
    )
    answer = adapter.retrieve(adapter.query, limit=len(envelopes))
    assert "error" not in answer, answer
    return {
        "envelopes": envelopes,
        "version": version,
        "hits": answer["chunks"],
        "retrieve_payloads": list(adapter.payloads),
    }


# ----- (a) hit source_ref deserializes to the canonical triple -------


def test_every_retrieved_hit_source_ref_is_a_pushed_canonical_triple(
    pushed_and_hits: dict[str, Any],
) -> None:
    """(a) Each mapped hit's locator equals a pushed envelope's
    ``{document_id, version, unit}`` triple — derived by deserializing
    the envelope's own ``source_ref``, not by re-serializing the hit,
    so garbage cannot round-trip through itself."""
    envelopes = pushed_and_hits["envelopes"]
    hits = pushed_and_hits["hits"]
    assert len(hits) == len(envelopes)

    pushed_triples = {
        deserialize_source_ref(env.source_ref) for env in envelopes
    }
    seen: set[UnitLocator] = set()
    for chunk in hits:
        locator = chunk.source_ref
        triple = UnitLocator(
            document_id=locator.document_id,
            version=locator.version,
            unit=locator.unit,
        )
        assert triple in pushed_triples, (
            f"retrieved hit points at {triple!r}, not a pushed unit"
        )
        assert triple.document_id == _DOC_ID
        assert triple.version == pushed_and_hits["version"]
        seen.add(triple)
    assert seen == pushed_triples


def test_canonical_ref_is_exactly_the_three_key_sorted_shape(
    pushed_and_hits: dict[str, Any],
) -> None:
    """The locator triple's serialized shape: exactly the keys
    ``document_id`` / ``unit`` / ``version``, sorted, no spaces — the
    deserialize/serialize pair is an exact inverse."""
    env = pushed_and_hits["envelopes"][0]
    payload = json.loads(env.source_ref)
    assert set(payload) == {"document_id", "version", "unit"}
    assert env.source_ref == json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    )
    locator = deserialize_source_ref(env.source_ref)
    assert serialize_source_ref(locator) == env.source_ref


# ----- (b)+(c) the read wire carries the hit's own ref and the read
# body is the frozen unit text -----


def test_read_carries_the_hits_own_source_ref_and_returns_frozen_text(
    pushed_and_hits: dict[str, Any],
) -> None:
    """(b)+(c) For a retrieved hit, the adapter's read_source call
    carries EXACTLY the hit's canonical ``source_ref`` string (the same
    string the push envelope carried — byte-identical, spied on the
    wire), and the resolved body is the frozen unit text of exactly
    that unit.

    One locator per read: the resolved text is that single envelope's
    body — no folding across units, no re-chunking (ADR 0037).
    """
    envelopes = pushed_and_hits["envelopes"]
    hits = pushed_and_hits["hits"]
    body_by_ref = {env.source_ref: env.body for env in envelopes}

    for chunk, env in zip(hits, envelopes):
        locator = chunk.source_ref
        ref = serialize_source_ref(
            UnitLocator(locator.document_id, locator.version, locator.unit)
        )
        assert ref == env.source_ref  # sanity: hit unit == this envelope
        adapter = _AllUnitsServerAdapter(
            envelopes=envelopes,
            known_document_ids={_DOC_ID},
        )
        assert _read_unit(adapter, _source_ref_of(chunk)) == env.body
        assert adapter.read_payload_refs() == [env.source_ref], (
            "the read wire must carry the hit's own pushed source_ref "
            "string, byte-identical, and read that unit alone"
        )
        # The hit's chunk text is a contiguous substring of the frozen
        # unit body the same locator resolves to (SC11) — the read may
        # not have re-cut the unit.
        assert chunk.text in body_by_ref[env.source_ref]


# ----- (d) non-canonical shapes fold to named misses -----


def test_wire_refs_with_extra_or_off_type_keys_never_become_locators() -> None:
    """(d, retrieve side) A wire chunk whose ``source_ref`` is a valid
    locator JSON plus an extra key, a string ``unit``, or non-sorted
    keys is NOT a canonical ref: the hit is dropped AND counted
    (SC22/T17), never mapped into a foreign locator and never served
    as an empty success."""
    envelopes, version = _pushed_envelopes()
    env = envelopes[0]
    payload = json.loads(env.source_ref)
    extra_key = json.dumps(
        {**payload, "block": 1}, sort_keys=True, separators=(",", ":")
    )
    string_unit = json.dumps(
        {**payload, "unit": "1"}, sort_keys=True, separators=(",", ":")
    )
    non_sorted = (
        '{"version":"' + version + '","document_id":"' + _DOC_ID + '","unit":1}'
    )

    ledger = MappingFailureLedger()
    chunks = map_chunks_to_fin_chunks(
        [
            {"source_ref": extra_key, "content": "x", "score": 1.0},
            {"source_ref": string_unit, "content": "y", "score": 1.0},
            {"source_ref": non_sorted, "content": "z", "score": 1.0},
        ],
        limit=10,
        ledger=ledger,
    )
    assert chunks == ()
    counts = ledger.count_by_code()
    assert counts.get(MAPPING_FAILURE_MALFORMED_HIT, 0) == 3, counts
    assert ledger.count() == 3


def test_read_of_a_locator_never_pushed_is_unknown_locator_not_empty_success(
    pushed_and_hits: dict[str, Any],
) -> None:
    """(d, read side) A locator for a unit that was never pushed rides
    the wire, and the server's named ``read_source_not_found`` folds to
    the typed ``{"error": "unknown_locator"}`` — never an empty text, a
    partial body, or a success envelope.

    An ordinal the locator gate itself refuses never reaches the wire at
    all; that shape is pinned where the gate is the unit under test.
    """
    envelopes = pushed_and_hits["envelopes"]
    version = pushed_and_hits["version"]
    ghost_unit = len(envelopes) + 1
    ghost_ref = serialize_source_ref(
        UnitLocator(_DOC_ID, version, unit=ghost_unit)
    )

    adapter = _AllUnitsServerAdapter(
        envelopes=envelopes,
        known_document_ids={_DOC_ID},
    )
    result = adapter.read_slice(
        {"document_id": _DOC_ID, "version": version, "unit": ghost_unit}
    )
    assert result == {"error": "unknown_locator"}
    assert "text" not in result, "a named miss must never hand back a body"
    assert adapter.read_payload_refs() == [ghost_ref]


# ----- (e) unit ordering and the one-call bound -----


def test_each_unit_reads_under_its_own_ref_in_unit_order(
    pushed_and_hits: dict[str, Any],
) -> None:
    """(e, ordering) A read addresses a unit by its own locator, so the
    units resolve in unit order whatever order the hits arrived in (the
    hit list is deliberately reversed here), each round trip carries that
    unit's canonical ref, and the reassembled text is that unit's frozen
    body — never a join across units."""
    envelopes = pushed_and_hits["envelopes"]
    assert len(envelopes) >= 4

    shuffled = list(reversed(pushed_and_hits["hits"]))
    ordered = sorted(shuffled, key=lambda chunk: chunk.source_ref.unit)
    assert [chunk.source_ref for chunk in ordered] != shuffled, (
        "fixture guard: the reversed hit list really differs from unit order"
    )

    walk = _AllUnitsServerAdapter(
        envelopes=envelopes,
        known_document_ids={_DOC_ID},
    )
    bodies = [_read_unit(walk, _source_ref_of(chunk)) for chunk in ordered]
    assert bodies == [env.body for env in envelopes]
    assert walk.read_payload_refs() == [env.source_ref for env in envelopes]


def test_one_read_never_fans_out_to_a_window_of_units(
    pushed_and_hits: dict[str, Any],
) -> None:
    """(e, bound) The bound that replaced the page window: one read of
    one locator touches that unit alone, so a neighbour's text cannot
    ride into the answer unasked and a document walk costs exactly one
    round trip per unit rather than an open-ended window."""
    envelopes = pushed_and_hits["envelopes"]
    hits = pushed_and_hits["hits"]
    assert all(
        len(env.body) <= SOURCE_READ_MAX_TEXT_CODE_POINTS for env in envelopes
    ), "fixture guard: each unit of the corpus fits one bounded slice"

    adapter = _AllUnitsServerAdapter(
        envelopes=envelopes,
        known_document_ids={_DOC_ID},
    )
    result = adapter.read_slice(_source_ref_of(hits[0]))
    assert is_slice_result(result), result
    assert set(result) == {"source_ref", "text", "next_position"}
    assert result["text"] == envelopes[0].body
    assert adapter.read_payload_refs() == [envelopes[0].source_ref]


# ----- opt-in live alignment (real iknow-memory MCP) -----


def _live_units(document_id: str, version: str) -> list[dict[str, Any]]:
    """Three deterministic sentences: one document, three frozen units.

    Each names its own ordinal and carries distinct query-reachable
    words (「operating revenue」/「research and development」/
    「cash flow」) so retrieve ranks them, mirroring the per-unit
    sentence shape of ``test_live_mcp_read_source.py``.
    """
    sentences = [
        f"Unit 1 of {document_id} reports operating revenue for the "
        "T3-E alignment check.\n",
        f"Unit 2 of {document_id} reports research and development "
        "expense for the T3-E alignment check.\n",
        f"Unit 3 of {document_id} reports cash flow from operations "
        "for the T3-E alignment check.\n",
    ]
    units = []
    for ordinal, text in enumerate(sentences, 1):
        parsed = build_unit(
            fmt="txt",
            source_sha256=version,
            document_id=document_id,
            version=version,
            unit=ordinal,
            text=text,
        )
        split = split_unit(parsed, doc_type="narrative")
        units.append(
            {
                "locator": UnitLocator(document_id, version, ordinal),
                "envelope": envelope_of_unit(
                    split,
                    valid_from="2026-01-01T00:00:00.000Z",
                    valid_until="9999-12-31T23:59:59.999Z",
                ),
            }
        )
    return units


@pytest.mark.mcp
def test_live_hit_and_read_source_resolve_the_same_unit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Opt-in (``-m mcp``): push one small document through the real
    MCP ``push``, retrieve a hit through the product adapter, then read
    the hit's unit through the real ``read_source`` — the read wire
    carries the hit's own canonical ref and the returned text is the
    frozen unit body."""
    from live_iknow_support import IKNOW_WORKTREE_ROOT, live_iknow

    entry = IKNOW_WORKTREE_ROOT / "dist" / "index.js"
    if not entry.is_file():
        pytest.skip(
            f"{entry} is missing; run `npm run build` in "
            f"{IKNOW_WORKTREE_ROOT} to exercise the live alignment path"
        )

    document_id = "T3E-LIVE-ALIGN"
    version = "c" * 64
    units = _live_units(document_id, version)
    envelopes = [unit["envelope"] for unit in units]
    body_by_ref = {env.source_ref: env.body for env in envelopes}

    with live_iknow(monkeypatch, root=IKNOW_WORKTREE_ROOT) as live:
        pushed = live.push.push([env.to_wire() for env in envelopes])
        assert pushed["written"] == len(envelopes), pushed

        answer = live.retrieve.retrieve("operating revenue", limit=5)
        assert "error" not in answer, answer
        hits = answer["chunks"]
        assert hits, answer

        # (a) every hit is one of the pushed triples.
        pushed_triples = {unit["locator"] for unit in units}
        for chunk in hits:
            locator = chunk.source_ref
            assert (
                UnitLocator(
                    locator.document_id, locator.version, locator.unit
                )
                in pushed_triples
            ), locator

        # (b)+(c) read the first hit's unit on the same session.
        chunk = hits[0]
        locator = chunk.source_ref
        hit_ref = serialize_source_ref(
            UnitLocator(locator.document_id, locator.version, locator.unit)
        )
        assert hit_ref in body_by_ref, hit_ref
        read_adapter = MCPRetrieveAdapter(
            session=live.session,
            timeout_seconds=20.0,
        )
        assert _read_unit(read_adapter, _source_ref_of(chunk)) == body_by_ref[
            hit_ref
        ]

        # (d) a unit the real server never stored is the named miss —
        # folded to unknown_locator, never an empty success and never the
        # partial body of an earlier slice.
        ghost_adapter = MCPRetrieveAdapter(
            session=live.session,
            timeout_seconds=20.0,
        )
        assert ghost_adapter.read_slice(
            {
                "document_id": document_id,
                "version": version,
                "unit": len(envelopes) + 1,
            }
        ) == {"error": "unknown_locator"}
