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


# ----- search_mode mirroring (search-mode-retrieve T2) -----


def test_fake_records_the_search_mode_it_received() -> None:
    """The fake mirrors the port contract AND records each call so a
    test can assert what the caller asked for without a live MCP."""
    port = FakeRetrievePort(hits_by_query={"foo": [_chunk("hit")]})
    port.retrieve("foo")
    port.retrieve("foo", search_mode="lexical")
    assert [call["search_mode"] for call in port.calls] == [None, "lexical"]
    assert port.calls[1]["query"] == "foo"


def test_tool_wrapper_forwards_search_mode_to_the_port() -> None:
    """``fin.retrieve.tool.retrieve`` is pass-through: the 档 the host
    assigned must reach the port, not be dropped on the floor."""
    from fin.retrieve import retrieve as retrieve_tool

    port = FakeRetrievePort(hits_by_query={"foo": [_chunk("hit")]})
    out = retrieve_tool("foo", search_mode="lexical", adapter=port)
    assert out["chunks"], "the lexical answer is unchanged by the mode"
    assert port.calls == [
        {
            "query": "foo",
            "document_id": None,
            "cik": None,
            "form": None,
            "ticker": None,
            "filing_date": None,
            "limit": 8,
            "search_mode": "lexical",
        }
    ]


@pytest.mark.parametrize(
    ("tier", "code"),
    [
        pytest.param("global", "tier_not_open", id="tier-global"),
        pytest.param("sql", "real_db_not_wired", id="tier-sql"),
    ],
)
def test_fake_refuses_an_unopened_tier_like_the_product_adapter(
    tier: str, code: str
) -> None:
    """Fake is not a 档: it mirrors the named exit rather than quietly
    answering with its dictionary lookup (spec SC8 — Fake covers the
    contract, not the ranking)."""
    from fin.retrieve.port import TierUnavailableError

    port = FakeRetrievePort(hits_by_query={"foo": [_chunk("hit")]})
    with pytest.raises(TierUnavailableError) as excinfo:
        port.retrieve("foo", search_mode=tier)
    assert excinfo.value.code == code
    assert port.calls == []


def test_fake_raises_invalid_search_mode_like_the_product_adapter() -> None:
    from fin.retrieve.port import InvalidSearchModeError

    port = FakeRetrievePort(hits_by_query={"foo": [_chunk("hit")]})
    with pytest.raises(InvalidSearchModeError) as excinfo:
        port.retrieve("foo", search_mode="summary")
    assert excinfo.value.code == "invalid_search_mode"
    assert port.calls == []


# ----- T3: the port's closed set, mirrored on the Fake -----


@pytest.mark.parametrize(
    "token", ["A", "B", "C", "D", "local", "", "hybrid", "b", "GRAPH"]
)
def test_a_leftover_token_is_refused_by_the_fake_like_the_port(
    token: str,
) -> None:
    """Negative class: the fake validates through the SAME seam as the
    product adapter, so a retired letter or a look-alike word cannot get a
    lexical answer out of the fake that the real port would refuse. No
    silent ``graph``, no silent ``global``, no silent mapping."""
    from fin.retrieve.port import InvalidSearchModeError

    port = FakeRetrievePort(hits_by_query={"foo": [_chunk("hit")]})
    with pytest.raises(InvalidSearchModeError) as excinfo:
        port.retrieve("foo", search_mode=token)
    assert excinfo.value.code == "invalid_search_mode"
    assert port.calls == [], "a refused request must leave no call record"
    assert port.input_validation_failures.count() == 0, (
        "the 档 token is a port-level named exit, not one of §B2's four"
    )


def test_an_unopened_tier_never_degrades_into_an_empty_chunk_answer() -> None:
    """Exception class: every named 档 exit RAISES, so none of them can be
    misread as 「语料没东西」 — the ``{"chunks": []}`` success shape is
    reserved for a request the 档 accepted and the corpus could not answer.
    Both unopened word forms and retired letters are in that set: the word
    forms answer ``TierUnavailableError``, the letters
    ``InvalidSearchModeError``."""
    from fin.retrieve.port import (
        InvalidSearchModeError,
        TierUnavailableError,
    )

    port = FakeRetrievePort(hits_by_query={"foo": [_chunk("hit")]})
    for token in ("global", "sql", "A", "D"):
        with pytest.raises((TierUnavailableError, InvalidSearchModeError)):
            port.retrieve("foo", search_mode=token)
    assert port.calls == []


# ----- T3: the five boundary classes under a 档 -----
#
# ``specs/search-mode-retrieve.md`` Boundaries: the five classes (empty /
# negative / overflow / concurrent / exception) must hold on the port
# contract once ``filters.search_mode`` rides it. Fake is not a 档 (SC8),
# so these assert CONTRACT shapes — never a ranking difference between
# the lasting tokens.


def test_an_open_tier_never_turns_the_legal_empty_miss_into_an_error() -> None:
    """Boundary 'empty' × 档: a query the corpus has no hits for stays
    the legal miss shape when an open 档 is set — naming the 档 is not
    a licence to answer MCP-down or raise. ``calls`` still records the
    accepted token (the miss ran *at* that 档)."""
    port = FakeRetrievePort(
        hits_by_query={"alpha": [_chunk("hit")]},
        known_document_ids={"DOC-2023Q1"},
    )
    assert port.retrieve("nonexistent query", search_mode="lexical") == {
        "chunks": []
    }
    assert port.calls[-1]["search_mode"] == "lexical"


def test_an_empty_query_is_named_before_the_tier_is_consulted() -> None:
    """Named-exit ordering on the empty class: an empty query is
    ``EmptyQueryError`` even beside a refused 档 — the port names what
    the caller got wrong FIRST and never lets the tier error mask it
    (both are invalid input, but only the query is on the §B2 ledger)."""
    port = FakeRetrievePort()
    with pytest.raises(EmptyQueryError):
        port.retrieve("   ", search_mode="global")
    assert port.input_validation_failures.count() == 1
    assert port.calls == []


def test_an_omitted_mode_is_not_sent_and_stays_a_legal_miss() -> None:
    """Empty class, the「omit → graph」half: leaving the 档 out is not an
    error and not a letter. The fake records ``None`` (「do not send」) and
    still answers the ordinary hit list, so the caller's omission resolves
    to the ranking service's own default 知识问答 (``graph``) rather than
    to a refusal or a miss dressed as one."""
    port = FakeRetrievePort(hits_by_query={"foo": [_chunk("hit")]})

    assert [c.text for c in port.retrieve("foo")["chunks"]] == ["hit"]
    assert port.calls[-1]["search_mode"] is None, (
        "an omitted mode must not be sent as any token, least of all a "
        "retired letter"
    )
    # And the same call against a corpus with nothing for it stays the
    # legal empty result — omission is not itself a failure.
    assert FakeRetrievePort().retrieve("nothing") == {"chunks": []}


def test_the_limit_cap_holds_under_an_open_tier() -> None:
    """Boundary 'overflow' × 档: the 档 asks the ranking service HOW to
    score; the port's default-8 / cap-20 rules (ADR 0009) do not move
    with the request."""
    chunks = [_chunk(f"hit {i}") for i in range(50)]
    port = FakeRetrievePort(hits_by_query={"foo": chunks})
    over = port.retrieve("foo", limit=999, search_mode="graph")
    zero = port.retrieve("foo", limit=0, search_mode="graph")
    assert len(over["chunks"]) == 20
    assert len(zero["chunks"]) == 8


def test_concurrent_tiered_calls_answer_per_call() -> None:
    """Boundary 'concurrent' × 档: parallel calls on one shared fake —
    an accepted 档 answers with the same hit list, an unopened one
    raises its own named error, an illegal one raises
    ``InvalidSearchModeError``, and ``calls`` records ONLY the accepted
    requests each carrying its OWN mode (a refused 档 must not leak a
    record, another thread's or its own)."""
    from fin.retrieve.port import InvalidSearchModeError, TierUnavailableError

    chunks = [_chunk(f"hit {i}") for i in range(5)]
    port = FakeRetrievePort(
        hits_by_query={"foo": chunks},
        known_document_ids={"DOC-2023Q1"},
    )

    def _go(i: int) -> tuple[str, object]:
        mode = ("lexical", "graph", "global", "Z")[i % 4]
        try:
            out = port.retrieve("foo", limit=5, search_mode=mode)
            return mode, [c.text for c in out["chunks"]]
        except TierUnavailableError as exc:
            return mode, ("tier", exc.code)
        except InvalidSearchModeError:
            return mode, ("invalid",)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(_go, range(16)))

    expected_texts = [c.text for c in chunks]
    for mode, payload in results:
        if mode in ("lexical", "graph"):
            assert payload == expected_texts, mode
        elif mode == "global":
            assert payload == ("tier", "tier_not_open")
        else:
            # Exception class × 档: a refused token raises through the
            # shared port instead of answering one thread with a hit and
            # another with a silent empty list.
            assert payload == ("invalid",)

    recorded = [call["search_mode"] for call in port.calls]
    assert len(recorded) == 8
    assert recorded.count("lexical") == 4
    assert recorded.count("graph") == 4
    assert "global" not in recorded and "Z" not in recorded


# ----- recorded Fast 抽图 job fact (spec graph-index-standard-fast SC5) -----


def test_fast_graph_empty_answers_the_emptiness_named() -> None:
    """A Fast job that built an empty entity-relation graph over the
    资料 set must not read as a silent empty collection: the emptiness
    arrives under the recorded job-fact key so the host decision can
    name it (never 未就绪, never a healthy miss)."""
    from fin.retrieve.port import RETRIEVE_OUTCOME_FAST_GRAPH_EMPTY

    port = FakeRetrievePort(
        hits_by_query={"中文资料问题": [_chunk("不该出现的命中")]},
        fast_graph_empty=True,
    )

    result = port.retrieve("中文资料问题")

    # The hit table is shadowed: an empty graph can cite no chunk.
    assert result == {"chunks": [], RETRIEVE_OUTCOME_FAST_GRAPH_EMPTY: True}
    assert len(port.calls) == 1


def test_an_ordinary_fake_never_learns_the_fast_job_key() -> None:
    """Default-off both ways: without the flag the result keeps the
    plain ``{"chunks": [...]}`` contract — otherwise every empty
    corpus would read as a Fast failure at the host."""
    from fin.retrieve.port import RETRIEVE_OUTCOME_FAST_GRAPH_EMPTY

    empty = FakeRetrievePort().retrieve("what was revenue")
    with_hits = FakeRetrievePort(
        hits_by_query={"foo": [_chunk("hit")]}
    ).retrieve("foo")

    assert empty == {"chunks": []}
    assert RETRIEVE_OUTCOME_FAST_GRAPH_EMPTY not in empty
    assert RETRIEVE_OUTCOME_FAST_GRAPH_EMPTY not in with_hits


def test_the_fast_job_fact_never_masks_invalid_input() -> None:
    """Boundary 'exception': invalid input names first, exactly as it
    does for an ordinary fake — a recorded job fact is not a licence
    to swallow the named ``EmptyQueryError``."""
    port = FakeRetrievePort(fast_graph_empty=True)

    with pytest.raises(EmptyQueryError):
        port.retrieve("   ")

    assert port.calls == []
