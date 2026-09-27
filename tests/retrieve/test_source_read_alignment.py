"""T3-E: the retrieved-hit ↔ bounded ``read_source`` same-unit contract.

The plan line (``plans/permanent-corpus-upload.md`` T3 acceptance):
「a retrieved hit and exact bounded ``read_source`` resolve that same
source unit and text」. The chain exists in production code — the push
side freezes one envelope per unit (``fin.corpus.push.envelopes_of_document``),
retrieve maps the wire ``source_ref`` back to a fin locator
(``fin.retrieve.hit_mapping.map_chunks_to_fin_chunks``), and
``MCPRetrieveAdapter.read`` fetches each unit through ``read_source``
(mcp_adapter ``read`` / ``_read_locators`` / ``_fetch_unit_body``) —
but until now the whole chain was chained only inside scattered live
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
5. ordering / window — units are read in unit order, the page window
   of a retrieved unit contains that unit's frozen text, and the
   promised 3-unit ``read_source`` bound is pinned: the clamp lives in
   ``_read_locators`` and reaches the fetch window via its returned
   bound (see
   :func:`test_read_page_limit_is_clamped_to_three_units`).

The host seam the tests build explicitly (``_locator_map_from_hits``):
retrieve maps a canonical ref WITHOUT the locator map (ADR 0047), but
``read`` discovers a document's unit locators FROM the locator map.
So the same-unit hop is only closed when the host carries the retrieved
chunks' locators into the read adapter's map — these tests pin that
seam's contract; a host boot that forgets the carry gets
``unknown_locator`` (named, never silent).
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
from fin.retrieve import mcp_adapter as mcp_adapter_module
from fin.retrieve.mcp_adapter import MCPRetrieveAdapter
from fin.retrieve.read_port import PAGE_LIMIT_CAP, clamp_page_limit


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


# ----- scripted transport: the same-server fake for retrieve + read -----


class _SameUnitServerAdapter(MCPRetrieveAdapter):
    """Scripted ``_call_mcp`` answering both ``retrieve`` and
    ``read_source`` off a dict of pushed envelopes, in the real wire
    shapes (canonical MCP content blocks), while recording every
    payload the adapter put on the wire.

    ``bodies`` maps the exact pushed ``source_ref`` string to the
    frozen unit text; a locator whose ref is absent gets the named
    ``read_source_not_found`` miss envelope — the real server's wire
    shape for a unit it never stored.
    """

    def __init__(
        self,
        *,
        envelopes: tuple[PushEnvelope, ...],
        locator_map: dict[str, Locator] | None = None,
        known_document_ids: set[str] | None = None,
        query: str = "operating revenue",
    ) -> None:
        # Skip the parent __init__ seam contract: mirror the fields the
        # parent would set (same documented pattern as test_mcp_adapter).
        self._command = None
        self._locator_map = dict(locator_map or {})
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
            source_ref = payload["params"]["arguments"]["source_ref"]
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
            inner = {
                "id": f"srv-{abs(hash(source_ref))}",
                "source_ref": source_ref,
                "content": body,
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


def _locator_map_from_hits(
    chunks: Any,
) -> dict[str, Locator]:
    """The host seam: carry the retrieved chunks' locators into the
    read adapter's locator map, keyed by the canonical serialization
    of each locator triple.

    This is the explicit same-unit hop the plan line promises: the read
    side may ONLY know the units the retrieve side actually resolved,
    so the tests derive the map from the hits rather than from the push
    side.
    """
    return {
        serialize_source_ref(
            UnitLocator(
                document_id=chunk.source_ref.document_id,
                version=chunk.source_ref.version,
                unit=chunk.source_ref.page,
            )
        ): chunk.source_ref
        for chunk in chunks
    }


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
            unit=locator.page,
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

    One unit at a time (window of one): the resolved text must equal
    that single envelope's body — no folding across units, no
    re-chunking (ADR 0037).

    NOTE on addressing: ``read``'s ``page`` is a 1-based position in
    the ORDERED located-unit list, not the unit number — the same-unit
    promise is that the located set comes from the hits and each
    window member is fetched under its own unit's canonical ref."""
    envelopes = pushed_and_hits["envelopes"]
    hits = pushed_and_hits["hits"]
    body_by_ref = {env.source_ref: env.body for env in envelopes}

    for position, (chunk, env) in enumerate(zip(hits, envelopes), 1):
        locator = chunk.source_ref
        ref = serialize_source_ref(
            UnitLocator(locator.document_id, locator.version, locator.page)
        )
        assert ref == env.source_ref  # sanity: hit unit == this envelope
        read_adapter = _AllUnitsServerAdapter(
            envelopes=envelopes,
            locator_map=_locator_map_from_hits(hits),
            known_document_ids={_DOC_ID},
        )
        result = read_adapter.read(
            locator.document_id, locator.version, page=position, page_limit=1
        )
        assert read_adapter.read_payload_refs() == [env.source_ref], (
            "the read wire must carry the hit's own pushed source_ref "
            "string, byte-identical"
        )
        assert result["text"] == body_by_ref[env.source_ref]
        assert result["text"] == env.body
        # The hit's chunk text is a contiguous substring of the frozen
        # unit body the same locator resolves to (SC11) — the read may
        # not have re-cut the unit.
        assert chunk.text in env.body


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
    """(d, read side) A locator for a unit that was never pushed folds
    to the typed ``{"error": "unknown_locator"}`` — never an empty text
    or a success envelope.

    Two shapes, both named:
    * the located-window walk never reaches past the units the map
      carries — a page past the count is refused at the seam (and a
      page window whose LAST unit is not stored still folds the whole
      read to the miss, never a partial success);
    * a carried locator whose ref the server never stored rides the
      wire and the server's named ``read_source_not_found`` miss is
      folded at the seam.
    """
    envelopes = pushed_and_hits["envelopes"]
    version = pushed_and_hits["version"]
    ghost_page = len(envelopes) + 1
    ghost_ref = serialize_source_ref(
        UnitLocator(_DOC_ID, version, unit=ghost_page)
    )

    # A locator the map does not carry: refused at the seam, no round
    # trip, no empty page.
    uncarried = _AllUnitsServerAdapter(
        envelopes=envelopes,
        locator_map=_locator_map_from_hits(pushed_and_hits["hits"]),
        known_document_ids={_DOC_ID},
    )
    assert uncarried.read(_DOC_ID, version, page=ghost_page, page_limit=1) == {
        "error": "unknown_locator"
    }
    assert uncarried.read_payload_refs() == []

    # A carried ghost extends the located count to itself, so the
    # window that ENDS on the ghost rides the wire: units
    # (count-2, count-1, ghost). The two real bodies resolve, the ghost
    # answers the named miss — and the WHOLE read is the named
    # unknown_locator, never the two-thirds of it that succeeded.
    carried = _AllUnitsServerAdapter(
        envelopes=envelopes,
        locator_map={
            **_locator_map_from_hits(pushed_and_hits["hits"]),
            ghost_ref: Locator(_DOC_ID, version, ghost_page),
        },
        known_document_ids={_DOC_ID},
    )
    assert carried.read(
        _DOC_ID, version, page=ghost_page - 2, page_limit=1
    ) != {"error": "unknown_locator"}  # sanity: the real window resolves
    assert carried.read(
        _DOC_ID, version, page=ghost_page, page_limit=3
    ) == {"error": "unknown_locator"}
    assert carried.read_payload_refs()[-1] == ghost_ref


# ----- (e) unit ordering and page-window clamp -----


def test_read_window_is_unit_ordered(
    pushed_and_hits: dict[str, Any],
) -> None:
    """(e, ordering) ``read`` walks units in unit order regardless of
    the locator map's insertion order (deliberately reversed here),
    the resolved text is the unit-ordered join of those units' frozen
    bodies, and ``page_count`` reports the full located count."""
    envelopes = pushed_and_hits["envelopes"]
    version = pushed_and_hits["version"]
    assert len(envelopes) >= 4

    hits = pushed_and_hits["hits"]
    shuffled = list(reversed(hits))
    adapter = _AllUnitsServerAdapter(
        envelopes=envelopes,
        locator_map=_locator_map_from_hits(shuffled),
        known_document_ids={_DOC_ID},
    )
    page = 2
    result = adapter.read(_DOC_ID, version, page=page, page_limit=3)
    refs = adapter.read_payload_refs()
    assert refs == [env.source_ref for env in envelopes[page - 1 : page + 2]], (
        "the window must be the 3-unit slice in unit order"
    )
    assert result["text"] == "\n\n".join(
        env.body for env in envelopes[page - 1 : page + 2]
    )
    assert result["page_count"] == len(envelopes)
    assert result["page"] == page

    # A full-document walk with the in-contract window reads every
    # unit exactly once, in unit order.
    walk = _AllUnitsServerAdapter(
        envelopes=envelopes,
        locator_map=_locator_map_from_hits(shuffled),
        known_document_ids={_DOC_ID},
    )
    walked_refs: list[str] = []
    cursor = 1
    while cursor <= len(envelopes):
        page_result = walk.read(_DOC_ID, version, page=cursor, page_limit=3)
        assert "error" not in page_result, page_result
        batch = walk.read_payload_refs()[len(walked_refs):]
        walked_refs.extend(batch)
        cursor += 3
    assert walked_refs == [env.source_ref for env in envelopes]


def test_read_page_limit_is_clamped_to_three_units(
    pushed_and_hits: dict[str, Any],
) -> None:
    """(e, clamp) An over-sized ``page_limit`` must fetch at most the
    three-unit bounded window the read contract promises."""
    envelopes = pushed_and_hits["envelopes"]
    version = pushed_and_hits["version"]
    assert len(envelopes) > 3

    adapter = _AllUnitsServerAdapter(
        envelopes=envelopes,
        locator_map=_locator_map_from_hits(pushed_and_hits["hits"]),
        known_document_ids={_DOC_ID},
    )
    adapter.read(_DOC_ID, version, page=1, page_limit=99)
    assert len(adapter.read_payload_refs()) <= 3, (
        "read_source fan-out exceeded the bounded 3-unit window"
    )


def test_page_limit_cap_value_is_three() -> None:
    """The ADR 0009 bound's VALUE is pinned via the public symbol."""
    assert PAGE_LIMIT_CAP == 3


@pytest.mark.parametrize(
    ("page_limit", "expected"),
    [(1, 1), (2, 2), (3, 3), (4, 3), (99, 3)],
)
def test_clamp_page_limit_bounds_only_the_upper_edge(
    page_limit: int, expected: int
) -> None:
    """The clamp RULE (not just its value) is the shared helper:
    at/below the cap passes through, above it is bounded. Lower-bound
    rejection stays at the callers, so the helper never invents a
    limit from an invalid one."""
    assert clamp_page_limit(page_limit) == expected


def test_read_paths_share_the_one_clamp_helper() -> None:
    """Both bounded-read call sites use the public helper; the private
    symbol the adapter used to import is gone (single source of the
    rule, no private-name coupling)."""
    assert "clamp_page_limit" in mcp_adapter_module.__dict__
    assert not hasattr(mcp_adapter_module, "_PAGE_LIMIT_CAP")


def test_read_page_beyond_the_units_is_unknown_locator(
    pushed_and_hits: dict[str, Any],
) -> None:
    """(e boundary) A page past the last unit is the named miss, not
    an empty page."""
    envelopes = pushed_and_hits["envelopes"]
    version = pushed_and_hits["version"]
    adapter = _AllUnitsServerAdapter(
        envelopes=envelopes,
        locator_map=_locator_map_from_hits(pushed_and_hits["hits"]),
        known_document_ids={_DOC_ID},
    )
    assert adapter.read(
        _DOC_ID, version, page=len(envelopes) + 1, page_limit=1
    ) == {"error": "unknown_locator"}
    assert adapter.read(_DOC_ID, version, page=1, page_limit=0) == {
        "error": "unknown_locator"
    }


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
                UnitLocator(locator.document_id, locator.version, locator.page)
                in pushed_triples
            ), locator

        # (b)+(c) read the first hit's unit on the same session.
        chunk = hits[0]
        locator = chunk.source_ref
        hit_ref = serialize_source_ref(
            UnitLocator(
                locator.document_id, locator.version, locator.page
            )
        )
        assert hit_ref in body_by_ref, hit_ref
        read_adapter = MCPRetrieveAdapter(
            session=live.session,
            timeout_seconds=20.0,
            locator_map={
                hit_ref: Locator(
                    locator.document_id, locator.version, locator.page
                )
            },
        )
        result = read_adapter.read(
            locator.document_id,
            locator.version,
            page=locator.page,
            page_limit=1,
        )
        assert result["text"] == body_by_ref[hit_ref]

        # (d) a page whose unit the real server never stored is the
        # named miss — folded to unknown_locator, never an empty page.
        # The ghost unit sits INSIDE the carried locator count so the
        # window's last page rides the real read_source wire.
        ghost_page = len(envelopes) + 1
        ghost_ref = serialize_source_ref(
            UnitLocator(document_id, version, unit=ghost_page)
        )
        ghost_adapter = MCPRetrieveAdapter(
            session=live.session,
            timeout_seconds=20.0,
            locator_map={
                **{
                    env.source_ref: Locator(
                        unit["locator"].document_id,
                        unit["locator"].version,
                        unit["locator"].unit,
                    )
                    for unit, env in zip(units, envelopes)
                },
                ghost_ref: Locator(document_id, version, ghost_page),
            },
        )
        assert ghost_adapter.read(
            document_id, version, page=ghost_page, page_limit=3
        ) == {"error": "unknown_locator"}
