"""Boundary tests for the read seam (ADR 0046 / ADR 0047, T1).

The serve path must not treat the local ``data/corpus/extracted``
tree as the company-corpus SSOT. ``read_file`` therefore reads through
an injected **read port** — the same port idea as the retrieve port:

* ``ReadPort`` — structural protocol; ``read(document_id, version,
  page, page_limit)`` returns the read_file contract dict or the
  ``{"error": "unknown_locator"}`` miss dict.
* ``read_from_pages`` — the shared boundary logic (page bounds, ADR
  0009 clamp, version match, ``\\n\\n`` join) every pages-backed
  implementation reuses.
* ``ExtractedTreeReadPort`` — the legacy tree-backed adapter. It stays
  importable for offline / committed-corpus tests, but NO product
  compile path builds it (the graph injects the retrieve adapter or a
  typed-miss port instead).
* ``MissReadPort`` — the explicit "this backend cannot read objects"
  adapter: every read is the typed miss.
* ``FakeRetrievePort.read`` — the fake answers reads from its own
  ``pages_by_doc`` manifest, so a scripted serve test can give the
  model a readable body without any on-disk tree.
* ``MCPRetrieveAdapter.read`` — iknow-memory exposes no read tool
  (handoff 2026-09-04), so the product MCP read is the named
  ``read_unavailable`` failure — never a silent success, never a
  fallback to the local tree.

Covers the five boundary classes: empty (no pages), negative (page /
page_limit below 1), overflow (page past the end, page_limit above the
ADR 0009 cap), concurrent (read-only shared state), exception (missing
tree → typed miss, never raised).
"""

from __future__ import annotations

import concurrent.futures
import json
from pathlib import Path

import pytest

from fin.corpus.errors import IngestMissingError
from fin.corpus.extracted import ExtractedPage, Locator, load_extracted_tree
from fin.retrieve import (
    ExtractedTreeReadPort,
    FakeRetrievePort,
    MCPRetrieveAdapter,
    MissReadPort,
    ReadPort,
    read_from_pages,
)


VERSION = "a" * 64


# ----- synthetic pages fixture (no on-disk tree needed) -----


def _pages(document_id: str = "TEST-2023Q1", count: int = 3) -> tuple[ExtractedPage, ...]:
    return tuple(
        ExtractedPage(
            locator=Locator(document_id=document_id, version=VERSION, page=n),
            text=f"PAGE {n} BODY",
            passages=(),
        )
        for n in range(1, count + 1)
    )


# ----- protocol shape -----


def test_fake_port_with_pages_satisfies_read_port_protocol() -> None:
    """The fake's ``read`` makes it structurally a ``ReadPort`` — that
    is the seam ``compile_graph`` injects into the ``read_file`` tool."""
    assert isinstance(FakeRetrievePort(pages_by_doc={}), ReadPort)


def test_miss_port_satisfies_read_port_protocol() -> None:
    assert isinstance(MissReadPort(), ReadPort)


def test_tree_port_satisfies_read_port_protocol(tmp_path: Path) -> None:
    assert isinstance(
        ExtractedTreeReadPort(tmp_path / "manifest.json", tmp_path / "extracted"),
        ReadPort,
    )


# ===== read_from_pages: the shared boundary logic =====


def test_read_from_pages_happy_path_joins_range() -> None:
    result = read_from_pages(
        {"TEST-2023Q1": _pages()}, "TEST-2023Q1", VERSION, page=2, page_limit=2
    )
    assert result == {
        "document_id": "TEST-2023Q1",
        "version": VERSION,
        "page": 2,
        "text": "PAGE 2 BODY\n\nPAGE 3 BODY",
        "page_count": 3,
    }


def test_read_from_pages_empty_page_table_is_miss() -> None:
    """No pages at all (the default fake's shape) → typed miss."""
    assert read_from_pages({}, "TEST-2023Q1", VERSION, 1, 1) == {
        "error": "unknown_locator"
    }


def test_read_from_pages_unknown_document_is_miss() -> None:
    assert read_from_pages(
        {"TEST-2023Q1": _pages()}, "BOGUS-2099Q9", VERSION, 1, 1
    ) == {"error": "unknown_locator"}


def test_read_from_pages_version_mismatch_is_miss() -> None:
    assert read_from_pages(
        {"TEST-2023Q1": _pages()}, "TEST-2023Q1", "b" * 64, 1, 1
    ) == {"error": "unknown_locator"}


@pytest.mark.parametrize("bad_page", [0, -1, 4])
def test_read_from_pages_page_out_of_range_is_miss(bad_page: int) -> None:
    """``page < 1`` (negative boundary) and ``page > page_count``
    (overflow boundary) are both the typed miss."""
    assert read_from_pages(
        {"TEST-2023Q1": _pages()}, "TEST-2023Q1", VERSION, bad_page, 1
    ) == {"error": "unknown_locator"}


@pytest.mark.parametrize("bad_limit", [0, -1])
def test_read_from_pages_page_limit_below_one_is_miss(bad_limit: int) -> None:
    assert read_from_pages(
        {"TEST-2023Q1": _pages()}, "TEST-2023Q1", VERSION, 1, bad_limit
    ) == {"error": "unknown_locator"}


def test_read_from_pages_page_limit_above_cap_clamps_to_three() -> None:
    """``page_limit > 3`` is silently clamped (ADR 0009 cap)."""
    clamped = read_from_pages(
        {"TEST-2023Q1": _pages()}, "TEST-2023Q1", VERSION, 1, 10
    )
    full = read_from_pages({"TEST-2023Q1": _pages()}, "TEST-2023Q1", VERSION, 1, 3)
    assert clamped == full


def test_read_from_pages_range_clamps_to_page_count() -> None:
    result = read_from_pages(
        {"TEST-2023Q1": _pages()}, "TEST-2023Q1", VERSION, 3, 3
    )
    assert result["text"] == "PAGE 3 BODY"


# ===== FakeRetrievePort.read: the fake manifest answers reads =====


def test_fake_port_read_serves_body_from_pages_by_doc() -> None:
    """The open-point answer: a scripted serve test gives the fake a
    ``pages_by_doc`` manifest and ``read_file`` bodies come from it —
    no extracted tree on disk anywhere."""
    fake = FakeRetrievePort(pages_by_doc={"TEST-2023Q1": _pages()})
    result = fake.read("TEST-2023Q1", VERSION, page=1, page_limit=1)
    assert result["text"] == "PAGE 1 BODY"
    assert result["page_count"] == 3


def test_fake_port_without_pages_manifest_reads_miss() -> None:
    """Default fake (no manifest): every read is the typed miss — the
    fake corpus is empty, and that is a named answer, not a crash."""
    assert FakeRetrievePort().read("TEST-2023Q1", VERSION) == {
        "error": "unknown_locator"
    }


def test_fake_port_read_shares_read_only_state_across_threads() -> None:
    fake = FakeRetrievePort(pages_by_doc={"TEST-2023Q1": _pages()})

    def _call() -> dict:
        return fake.read("TEST-2023Q1", VERSION, page=1, page_limit=1)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: _call(), range(8)))
    assert all(r == results[0] for r in results)


def test_fake_port_pages_by_doc_is_copied_not_aliased() -> None:
    """Mutating the caller's mapping after construction cannot change
    what the fake serves (same defensive-copy rule as hits_by_query)."""
    pages = {"TEST-2023Q1": _pages()}
    fake = FakeRetrievePort(pages_by_doc=pages)
    pages["INJECTED-2024Q1"] = _pages("INJECTED-2024Q1")
    assert fake.read("INJECTED-2024Q1", VERSION) == {"error": "unknown_locator"}


# ===== MCPRetrieveAdapter.read: MCP dispatch, no local fallback =====


def test_mcp_adapter_read_unknown_without_locator_map() -> None:
    """A document with no unit locator map is a typed miss and never
    silently consults the local extracted tree."""
    adapter = MCPRetrieveAdapter(command=None)
    assert adapter.read("TEST-2023Q1", VERSION) == {"error": "unknown_locator"}


def test_mcp_adapter_read_touches_no_local_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An MCP read with no known document cannot fall through to the
    extracted tree loader."""

    def _boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("MCP read consulted the local tree")

    monkeypatch.setattr(
        "fin.retrieve.read_port.load_extracted_tree", _boom, raising=False
    )
    adapter = MCPRetrieveAdapter(command=["iknow-memory"])
    assert adapter.read("TEST-2023Q1", VERSION) == {"error": "unknown_locator"}


# ===== ExtractedTreeReadPort: legacy adapter, typed miss on broken tree =====


def _write_synthetic_tree(tmp_path: Path) -> tuple[Path, Path]:
    extracted_root = tmp_path / "extracted"
    doc_dir = extracted_root / "TEST-2023Q1"
    doc_dir.mkdir(parents=True)
    (doc_dir / "page-001.json").write_text(
        json.dumps(
            {
                "document_id": "TEST-2023Q1",
                "version": VERSION,
                "page": 1,
                "text": "PAGE 1 BODY",
            }
        )
    )
    (doc_dir / "passages-001.json").write_text(
        json.dumps(
            {
                "passages": [
                    {
                        "text": "PAGE 1 BODY",
                        "char_start": 0,
                        "char_end": 12,
                    }
                ]
            }
        )
    )
    (extracted_root / "_index.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "documents": {
                    "TEST-2023Q1": {"page_count": 1, "version": VERSION}
                },
            }
        )
    )
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "upstream": {
                    "repo": "test",
                    "commit": "test",
                    "path": "test",
                    "license": "test",
                },
                "tickers": ["TEST"],
                "periods": ["2023 Q1"],
                "note": "synthetic test fixture",
                "documents": [
                    {
                        "document_id": "TEST-2023Q1",
                        "source_id": "TEST",
                        "company": "TEST",
                        "period": "2023 Q1",
                        "doc_type": "10-Q",
                        "filename": "test.pdf",
                        "upstream_name": "test.pdf",
                        "bytes": 0,
                        "sha256": VERSION,
                    }
                ],
            }
        )
    )
    return manifest_path, extracted_root


def test_tree_port_happy_path_matches_tree_loader(tmp_path: Path) -> None:
    manifest_path, extracted_root = _write_synthetic_tree(tmp_path)
    port = ExtractedTreeReadPort(manifest_path, extracted_root)
    result = port.read("TEST-2023Q1", VERSION, page=1, page_limit=1)
    assert result["text"] == "PAGE 1 BODY"

    tree = load_extracted_tree(manifest_path, extracted_root)
    assert result["page_count"] == len(tree["TEST-2023Q1"])


def test_tree_port_missing_tree_is_typed_miss_not_exception(
    tmp_path: Path,
) -> None:
    """SC3: with the local extracted tree absent, the tree-backed port
    answers the typed miss dict — it never raises ``IngestMissingError``
    at the model and never silently succeeds."""
    port = ExtractedTreeReadPort(
        tmp_path / "manifest.json", tmp_path / "extracted"
    )
    assert (tmp_path / "extracted").exists() is False
    assert port.read("TEST-2023Q1", VERSION) == {"error": "unknown_locator"}


def test_tree_port_broken_tree_is_typed_miss(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed tree raises ``IngestMissingError`` from the loader;
    the port converts it to the model-facing miss dict (ADR 0009 +
    ADR 0020 contract preserved from the old ``read_file``)."""

    def _raise(*args: object, **kwargs: object) -> None:
        raise IngestMissingError("simulated missing tree")

    monkeypatch.setattr("fin.retrieve.read_port.load_extracted_tree", _raise)
    port = ExtractedTreeReadPort(
        tmp_path / "manifest.json", tmp_path / "extracted"
    )
    assert port.read("TEST-2023Q1", VERSION) == {"error": "unknown_locator"}


# ===== MissReadPort =====


def test_miss_port_always_misses() -> None:
    port = MissReadPort()
    assert port.read("TEST-2023Q1", VERSION) == {"error": "unknown_locator"}
    assert port.read("ANY-2024Q2", "b" * 64, page=2, page_limit=3) == {
        "error": "unknown_locator"
    }


# ===== validate_company_source_ref: the shared locator gate (T4) =====


def test_validator_resolves_a_well_formed_triple() -> None:
    from fin.retrieve.read_port import validate_company_source_ref

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
    from fin.retrieve.read_port import validate_company_source_ref

    assert validate_company_source_ref(bad_ref) == {
        "error": "invalid_locator",
        "reason": reason,
    }


@pytest.mark.parametrize(
    "junk", ["x", 42, None, [], {}, object(), b"bytes", 3.5, True]
)
def test_validator_never_raises_on_junk(junk: object) -> None:
    """The boundary contract: garbage in, a typed failure dict out."""
    from fin.retrieve.read_port import validate_company_source_ref

    result = validate_company_source_ref(junk)
    assert isinstance(result, dict)
    assert result["error"] == "invalid_locator"
