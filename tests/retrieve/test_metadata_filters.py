"""The four-key generic metadata filter at the public port boundary.

Spec ``specs/qa-chain-corpus-retrieval.md`` §"Retrieve request and metadata
filters" + success criterion CR-1: the public metadata-filter object has
exactly the keys ``title`` / ``doc_type`` / ``company`` / ``period``; values
normalize by NFC and Unicode-whitespace trim, and every remaining value is a
string of 1–256 UTF-8 bytes with no Unicode ``Cc`` control and no ``*`` /
``?`` wildcard. Any other state — unknown or reserved key (the four legacy
``cik`` / ``form`` / ``ticker`` / ``filing_date`` names included), a
non-object filter, more than four members, a non-string value, an overlong
or control-containing value, a wildcard value — is the canonical named
input failure ``{"error": "invalid_filter"}``, WITHOUT a ``chunks`` key and
WITHOUT starting any I/O.

The validation runs at the port seam (``MCPRetrieveAdapter.retrieve`` via
the scripted-transport seam, and ``FakeRetrievePort.retrieve``), before
anything reaches the transport: a scripted response here proves the call
never arrived.

The allowlist is catalog-owned (CR-1: one allowlist supplies every
consumer) — :data:`fin.corpus.catalog.METADATA_FILTER_KEYS` — so the read
model and the filter contract cannot drift apart.
"""

from __future__ import annotations

import json
import unicodedata
from typing import Any

import pytest

from fin.corpus.catalog import METADATA_FILTER_KEYS
from fin.corpus.extracted import Locator
from fin.corpus.parsers._common import UnitLocator
from fin.corpus.push import serialize_source_ref
from fin.retrieve import Chunk
from fin.retrieve.fake_port import FakeRetrievePort
from fin.retrieve.mcp_adapter import MCPRetrieveAdapter


# The canonical named failure, spelled once.
INVALID_FILTER: dict[str, Any] = {"error": "invalid_filter"}


def _canonical_ref() -> str:
    """The B4 canonical fin ``source_ref`` serialization for one locator."""
    return serialize_source_ref(
        UnitLocator(
            document_id="DOC-1",
            version="v-" + "a" * 60,
            unit=1,
        )
    )


class _RecordingAdapter(MCPRetrieveAdapter):
    """Scripted adapter that records whether the transport was reached.

    Mirrors the documented ``_ScriptedAdapter`` seam (skip ``__init__``,
    set the fields it would set). ``transport_reached`` proves the
    no-I/O half of the contract: a validation refusal must answer
    before ``_call_mcp`` is ever called.
    """

    def __init__(self, scripted: dict[str, Any]) -> None:
        self._command = None
        self._locator_map = {}
        self._known_doc_ids = None
        self._timeout = 5.0
        self._scripted_response = scripted
        self._scripted_exception = None
        self.transport_reached = False
        self.payloads: list[dict[str, Any]] = []

    async def _call_mcp(self, payload: dict[str, Any]) -> dict[str, Any]:  # type: ignore[override]
        self.transport_reached = True
        self.payloads.append(payload)
        assert self._scripted_response is not None
        return self._scripted_response


def _hit_adapter() -> _RecordingAdapter:
    """An adapter scripted to answer one hit if (and only if) it is reached."""
    return _RecordingAdapter(
        {
            "result": {
                "chunks": [
                    {
                        "content": "t",
                        "source_ref": _canonical_ref(),
                        "score": 1.0,
                    }
                ]
            }
        }
    )


def _hit_fake_port() -> FakeRetrievePort:
    """A fake port scripted to answer one hit under the "revenue" query."""
    return FakeRetrievePort(
        hits_by_query={
            "revenue": [
                Chunk(
                    text="t",
                    source_ref=Locator(
                        document_id="DOC-1",
                        version="v-" + "a" * 60,
                        unit=1,
                    ),
                    score=1.0,
                )
            ]
        }
    )


# ----- the allowlist is catalog-owned and exactly four keys -----


def test_catalog_allowlist_is_exactly_the_four_keys() -> None:
    """CR-1: one catalog-owned allowlist; its members are exactly the
    four public metadata keys, in contract order."""
    assert METADATA_FILTER_KEYS == (
        "title",
        "doc_type",
        "company",
        "period",
    )


# ----- happy paths through the port boundary -----


@pytest.mark.parametrize(
    "adapter_factory",
    [
        pytest.param(_hit_adapter, id="mcp-adapter"),
        pytest.param(_hit_fake_port, id="fake-port"),
    ],
)
def test_normalized_values_reach_the_boundary_unchanged(
    adapter_factory: Any,
) -> None:
    """A well-formed map answers as a retrieve, not a refusal."""
    port = adapter_factory()
    result = port.retrieve("revenue", filters={"title": "Annual Report"})
    assert len(result["chunks"]) == 1
    chunk = result["chunks"][0]
    assert chunk.text == "t"


def test_nfc_normalization_trims_and_accepts() -> None:
    """Values normalize by NFC and Unicode-whitespace trim before they
    are judged: a composed-equivalent, padded value is legal."""
    adapter = _hit_adapter()
    raw = "  ホワイト書式ａ　"  # NFD-spelling + trailing ideographic space
    nfc = unicodedata.normalize("NFC", raw).strip()
    result = adapter.retrieve("revenue", filters={"title": raw})
    assert "chunks" in result
    # The normalized form is what rides the wire.
    sent = adapter.payloads[0]["params"]["arguments"]["filters"]
    assert sent["metadata.title"] == nfc


def test_whitespace_only_value_is_omitted() -> None:
    """A whitespace-only value disappears; the remaining key survives."""
    adapter = _hit_adapter()
    result = adapter.retrieve(
        "revenue", filters={"company": "  　 ", "period": "2025"}
    )
    assert "chunks" in result
    sent = adapter.payloads[0]["params"]["arguments"]["filters"]
    assert sent == {"metadata.period": "2025"}


def test_all_omitted_values_is_an_empty_filter() -> None:
    """Every value omitted: the request is equivalent to an empty filter
    object — no ``filters`` block rides the wire."""
    adapter = _hit_adapter()
    result = adapter.retrieve("revenue", filters={"company": " \t "})
    assert "chunks" in result
    assert "filters" not in adapter.payloads[0]["params"]["arguments"]


def test_empty_filters_dict_searches_the_whole_scope() -> None:
    """``filters={}`` is the absent form: whole authorized scope, no
    ``filters`` block on the wire, no refusal."""
    adapter = _hit_adapter()
    result = adapter.retrieve("revenue", filters={})
    assert "chunks" in result
    assert "filters" not in adapter.payloads[0]["params"]["arguments"]


# ----- invalid_filter, refused before any I/O -----


@pytest.mark.parametrize(
    "adapter_factory",
    [
        pytest.param(_hit_adapter, id="mcp-adapter"),
        pytest.param(lambda: FakeRetrievePort(), id="fake-port"),
    ],
)
@pytest.mark.parametrize(
    "bad_filters",
    [
        pytest.param({"issuer": "acme"}, id="unknown-key"),
        pytest.param({"metadata.title": "x"}, id="reserved-prefixed-key"),
        pytest.param({"source_ref": "DOC-1"}, id="upstream-scope-key"),
        pytest.param({"search_mode": "lexical"}, id="internal-control-key"),
        pytest.param({"cik": "0000320193"}, id="legacy-cik"),
        pytest.param({"form": "10-Q"}, id="legacy-form"),
        pytest.param({"ticker": "AAPL"}, id="legacy-ticker"),
        pytest.param({"filing_date": "2023-09-30"}, id="legacy-filing-date"),
        pytest.param("not-an-object", id="non-object-string"),
        pytest.param(["10-Q"], id="non-object-list"),
        pytest.param(
            {"title": "a", "doc_type": "b", "company": "c", "period": "d",
             "extra": "e"},
            id="five-keys",
        ),
        pytest.param({"title": 7}, id="non-string-value"),
        pytest.param({"title": ["a"]}, id="array-value"),
        pytest.param({"title": {"v": "a"}}, id="object-value"),
        pytest.param({"title": "x" * 257}, id="overlong-value"),
        # ``None`` is the port's established "argument absent" marker
        # (the same convention ``search_mode=None`` holds): it searches
        # the whole scope and is NOT a refusal. The refusal cases below
        # name the non-object TYPES a caller can actually send.
        pytest.param({"title": "ok\u0007bell"}, id="control-char-value"),
        pytest.param({"title": "a*"}, id="wildcard-star"),
        pytest.param({"title": "a?b"}, id="wildcard-question"),
        pytest.param({"title": ""}, id="empty-value"),
        pytest.param({"title": "   "}, id="blank-value-with-unknown-sibling"),
    ],
)
def test_invalid_filters_are_refused_before_io(
    adapter_factory: Any, bad_filters: Any
) -> None:
    """Every out-of-contract state is the canonical refusal, with no
    ``chunks`` key and no transport start.

    The two blank-value entries: a blank value alone is the omit rule
    (tested above), but a blank value whose map ALSO carries an
    out-of-contract member must still refuse — so ``{"title": "   "}``
    is paired here with a sibling unknown key to make the refusal the
    only expected answer. Both entries carry that sibling.
    """
    filters = bad_filters
    if bad_filters in ({"title": ""}, {"title": "   "}):
        filters = {**bad_filters, "issuer": "acme"}
    port = adapter_factory()
    result = port.retrieve("revenue", filters=filters)
    assert result == INVALID_FILTER
    if isinstance(port, _RecordingAdapter):
        assert port.transport_reached is False


@pytest.mark.parametrize(
    "adapter_factory",
    [
        pytest.param(_hit_adapter, id="mcp-adapter"),
        pytest.param(lambda: FakeRetrievePort(), id="fake-port"),
    ],
)
def test_invalid_filters_carry_no_chunks_key(
    adapter_factory: Any,
) -> None:
    """The refusal envelope has NO ``chunks`` key — an empty hit list
    would read as "the corpus has nothing", which is a different fact."""
    port = adapter_factory()
    result = port.retrieve("revenue", filters={"cik": "0000320193"})
    assert result == INVALID_FILTER
    assert "chunks" not in result


# ----- wire mapping: the four keys become metadata.<key> -----


def test_generic_keys_ride_the_wire_as_metadata_prefixed() -> None:
    """The adapter maps the four public keys to the upstream exact-match
    vocabulary: ``metadata.<key>`` per the upstream ``METADATA_FILTER_PREFIX``."""
    adapter = _hit_adapter()
    adapter.retrieve(
        "revenue",
        filters={
            "title": "Whitepaper",
            "doc_type": "whitepaper",
            "company": "Acme",
            "period": "FY2025",
        },
    )
    sent = adapter.payloads[0]["params"]["arguments"]["filters"]
    assert sent == {
        "metadata.title": "Whitepaper",
        "metadata.doc_type": "whitepaper",
        "metadata.company": "Acme",
        "metadata.period": "FY2025",
    }


def test_legacy_arguments_still_ride_the_wire_unchanged() -> None:
    """Compatibility window: the four legacy arguments keep their
    existing upstream financial field spelling (bare keys, pinned by
    ``test_supplied_filters_are_forwarded_in_the_request``) — they are
    never conflated with the generic map."""
    adapter = _hit_adapter()
    adapter.retrieve(
        "revenue", cik="0000320193", form="10-Q", ticker="AAPL",
        filing_date="2023-09-30",
    )
    sent = adapter.payloads[0]["params"]["arguments"]["filters"]
    assert sent == {
        "cik": "0000320193",
        "form": "10-Q",
        "ticker": "AAPL",
        "filing_date": "2023-09-30",
    }


def test_legacy_and_generic_apply_conjunctively() -> None:
    """Old args + generic filters are both sent: conjunctive predicates
    during the migrate/expand window."""
    adapter = _hit_adapter()
    adapter.retrieve(
        "revenue",
        cik="0000320193",
        filters={"doc_type": "10-Q"},
    )
    sent = adapter.payloads[0]["params"]["arguments"]["filters"]
    assert sent == {"cik": "0000320193", "metadata.doc_type": "10-Q"}
