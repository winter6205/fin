"""Tests for what the UA-gated live EDGAR fetch actually writes.

``tests/corpus/test_ingest_local.py`` owns the UA gate itself (unset
``FIN_EDGAR_USER_AGENT`` opens no socket; UA set + HTTP fail is the
``sec_http_fail`` typed EXIT). This module owns the other half of the
contract — the bytes the live path chooses and the tree it leaves
behind:

* the hashed ``version`` must be the official **primary iXBRL
  ``.htm``**, not the accession's directory listing (spec § locator,
  ADR 0028);
* writing one accession must not delete the rest of ``_index.json``
  (ADR 0020: the extracted tree is the query SSOT for every document,
  not just the one this run touched);
* the frozen unit is the HTML's stripped text, and it ships with the
  ``passages-NNN.json`` artifact ``load_extracted_tree`` requires.

Every SEC call is scripted through ``fin.corpus.live_fetch.urlopen``;
nothing here opens ``www.sec.gov`` (spec § Success Criteria: default
pytest does not call SEC hosts).
"""

from __future__ import annotations

import hashlib
import json
import urllib.error
from pathlib import Path
from unittest import mock

import pytest

from fin.corpus.errors import IngestMissingError
from fin.corpus import live_fetch
from fin.corpus.catalog import list_filings
from fin.corpus.live_fetch import (
    SecHttpFailError,
    SecPrimaryDocUnresolvedError,
    fetch_and_ingest,
)


_CIK = "0000320193"
_ACCESSION = "0000320193-23-000077"
_BASE = "https://www.sec.gov/Archives/edgar/data/320193/000032019323000077/"
_INDEX_URL = f"{_BASE}index.json"
_PRIMARY_URL = f"{_BASE}aapl-20230701.htm"

_PRIMARY_HTML = b"""<!DOCTYPE html>
<html><head><title>10-Q</title>
<style>.hide { display: none; }</style>
<script>var ix = 1;</script>
</head>
<body>
<ix:header><ix:hidden><ix:nonNumeric name="dei:AmendmentFlag">false\
</ix:nonNumeric></ix:hidden></ix:header>
<p>For the quarterly period ended&#160;July 1, 2023</p>
<table><tr><td>Products</td><td>$ 60,584</td><td>$ 63,355</td></tr></table>
</body></html>
"""


def _listing(names_and_sizes: list[tuple[str, int | str]]) -> bytes:
    """Serialise an EDGAR accession ``index.json`` directory listing."""
    return json.dumps(
        {
            "directory": {
                "name": f"/Archives/edgar/data/320193/{_ACCESSION}",
                "item": [
                    {"name": name, "type": "text.gif", "size": size}
                    for name, size in names_and_sizes
                ],
            }
        }
    ).encode("utf-8")


# The real AAPL 2023 Q3 listing shape: the primary iXBRL document sits
# among exhibits, rendered ``R*.htm`` report fragments, and the two
# ``-index`` wrappers.
_AAPL_LISTING = _listing(
    [
        (f"{_ACCESSION}-index-headers.html", ""),
        (f"{_ACCESSION}-index.html", ""),
        (f"{_ACCESSION}.txt", ""),
        ("a10-qexhibit31107012023.htm", 10527),
        ("a10-qexhibit32107012023.htm", 8336),
        ("aapl-20230701.htm", 963209),
        ("aapl-20230701_htm.xml", 892947),
        ("FilingSummary.xml", 33287),
        ("R1.htm", 77899),
        ("R10.htm", 154132),
    ]
)


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _scripted_sec(
    bodies: dict[str, bytes], calls: list[str]
) -> "mock._patch[mock.MagicMock]":
    """Patch ``live_fetch.urlopen`` with a URL-routed fake.

    Any URL not in ``bodies`` answers 404, which is how a test proves
    the resolver did not go somewhere it should not have.
    """

    def _urlopen(request: object, *args: object, **kwargs: object) -> _FakeResponse:
        url = request.full_url  # type: ignore[attr-defined]
        calls.append(url)
        if url not in bodies:
            raise urllib.error.HTTPError(
                url=url, code=404, msg="Not Found", hdrs={}, fp=None
            )
        return _FakeResponse(bodies[url])

    return mock.patch("fin.corpus.live_fetch.urlopen", side_effect=_urlopen)


@pytest.fixture(autouse=True)
def _ua_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FIN_EDGAR_USER_AGENT", "fin test@example.com")


def _roots(tmp_path: Path) -> dict[str, Path]:
    return {
        "manifest_path": tmp_path / "manifest.json",
        "extracted_root": tmp_path / "extracted",
        "facts_root": tmp_path / "facts",
    }


# ---------- atomic publish + catalog registration ----------


def test_successful_live_fetch_registers_accession_in_manifest_and_catalog(
    tmp_path: Path,
) -> None:
    """A successful live fetch publishes one catalog-visible accession."""
    roots = _roots(tmp_path)
    calls: list[str] = []

    with _scripted_sec({_INDEX_URL: _AAPL_LISTING, _PRIMARY_URL: _PRIMARY_HTML}, calls):
        fetch_and_ingest(
            cik=_CIK,
            accession=_ACCESSION,
            primary_doc="aapl-20230701.htm",
            **roots,
        )

    manifest = json.loads(roots["manifest_path"].read_text())
    row = next(
        document
        for document in manifest["documents"]
        if document["document_id"] == _ACCESSION
    )
    assert row["accession"] == _ACCESSION

    index = json.loads(
        (roots["extracted_root"] / "_index.json").read_text()
    )
    assert _ACCESSION in index["documents"]

    result = list_filings(
        manifest_path=roots["manifest_path"],
        extracted_root=roots["extracted_root"],
    )
    assert [filing["document_id"] for filing in result["filings"]] == [
        _ACCESSION
    ]


def test_failure_after_staged_page_write_leaves_visible_publish_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A page-write failure cannot expose an unregistered accession."""
    roots = _roots(tmp_path)
    roots["extracted_root"].mkdir(parents=True)
    original_index = b'{"schema_version": 1, "documents": {}}\n'
    (roots["extracted_root"] / "_index.json").write_bytes(original_index)
    original_manifest = b'{"documents": []}\n'
    roots["manifest_path"].write_bytes(original_manifest)

    real_atomic_write = live_fetch._atomic_write

    def fail_after_page_write(path: Path, payload: bytes) -> None:
        real_atomic_write(path, payload)
        if path.name == "page-001.json":
            raise RuntimeError("injected page-write failure")

    monkeypatch.setattr(live_fetch, "_atomic_write", fail_after_page_write)

    with _scripted_sec({_PRIMARY_URL: _PRIMARY_HTML}, []):
        with pytest.raises(RuntimeError, match="injected page-write failure"):
            fetch_and_ingest(
                cik=_CIK,
                accession=_ACCESSION,
                primary_doc="aapl-20230701.htm",
                **roots,
            )

    assert not (roots["extracted_root"] / _ACCESSION).exists()
    assert (roots["extracted_root"] / "_index.json").read_bytes() == original_index
    assert roots["manifest_path"].read_bytes() == original_manifest


# ---------- primary document resolution ----------


def test_version_hashes_the_primary_htm_not_the_directory_listing(
    tmp_path: Path,
) -> None:
    """The live path resolves the accession's primary iXBRL ``.htm``
    and hashes **those** bytes as the locator ``version``.

    Hashing the directory listing would pin the cite identity to a
    page of hyperlinks that changes whenever EDGAR re-renders it, and
    would never contain the quoted span the citation verifier checks.
    """
    calls: list[str] = []
    bodies = {_INDEX_URL: _AAPL_LISTING, _PRIMARY_URL: _PRIMARY_HTML}
    roots = _roots(tmp_path)

    with _scripted_sec(bodies, calls):
        fetch_and_ingest(cik=_CIK, accession=_ACCESSION, **roots)

    assert calls == [_INDEX_URL, _PRIMARY_URL]
    index = json.loads((roots["extracted_root"] / "_index.json").read_text())
    entry = index["documents"][_ACCESSION]
    assert entry["version"] == hashlib.sha256(_PRIMARY_HTML).hexdigest()
    assert entry["version"] != hashlib.sha256(_AAPL_LISTING).hexdigest()
    assert entry["source_url"] == _PRIMARY_URL


def test_primary_doc_override_skips_the_listing_request(tmp_path: Path) -> None:
    """``primary_doc`` names the document outright, so the resolver
    makes one GET instead of two."""
    calls: list[str] = []
    roots = _roots(tmp_path)

    with _scripted_sec({_PRIMARY_URL: _PRIMARY_HTML}, calls):
        fetch_and_ingest(
            cik=_CIK,
            accession=_ACCESSION,
            primary_doc="aapl-20230701.htm",
            **roots,
        )

    assert calls == [_PRIMARY_URL]


def test_exhibits_and_rendered_fragments_are_not_mistaken_for_the_filing(
    tmp_path: Path,
) -> None:
    """Only ``aapl-20230701.htm`` is a candidate: the ``-index``
    wrappers, the ``R*.htm`` rendered fragments and the exhibits are
    all excluded even though they are ``.htm`` too."""
    calls: list[str] = []
    bodies = {_INDEX_URL: _AAPL_LISTING, _PRIMARY_URL: _PRIMARY_HTML}

    with _scripted_sec(bodies, calls):
        fetch_and_ingest(cik=_CIK, accession=_ACCESSION, **_roots(tmp_path))

    assert calls[1] == _PRIMARY_URL


def test_falls_back_to_the_largest_document_when_naming_is_not_ixbrl(
    tmp_path: Path,
) -> None:
    """Older filings do not use the ``ticker-YYYYMMDD.htm`` convention
    (MSFT ships ``msft-10q_20220930.htm``). The primary document is
    then the largest non-exhibit ``.htm`` in the accession."""
    listing = _listing(
        [
            (f"{_ACCESSION}-index.html", ""),
            ("msft-10q_20220930.htm", 4048693),
            ("msft-ex311_8.htm", 7865),
            ("msft-ex321_10.htm", 4776),
        ]
    )
    expected = f"{_BASE}msft-10q_20220930.htm"
    calls: list[str] = []

    with _scripted_sec({_INDEX_URL: listing, expected: _PRIMARY_HTML}, calls):
        fetch_and_ingest(cik=_CIK, accession=_ACCESSION, **_roots(tmp_path))

    assert calls[1] == expected


def test_listing_without_a_primary_document_is_a_typed_exit(
    tmp_path: Path,
) -> None:
    """An accession whose listing carries no candidate document is a
    typed EXIT, not a fetch of whatever happened to be first — and it
    marks nothing complete."""
    listing = _listing(
        [(f"{_ACCESSION}-index.html", ""), ("R1.htm", 100), ("primary.xml", 200)]
    )
    roots = _roots(tmp_path)
    calls: list[str] = []

    with _scripted_sec({_INDEX_URL: listing}, calls):
        with pytest.raises(SecPrimaryDocUnresolvedError):
            fetch_and_ingest(cik=_CIK, accession=_ACCESSION, **roots)

    assert calls == [_INDEX_URL]
    assert not roots["extracted_root"].exists()
    assert not roots["facts_root"].exists()


def test_listing_http_failure_marks_nothing_complete(tmp_path: Path) -> None:
    """A failed listing GET is still ``sec_http_fail``: typed EXIT
    before any write."""
    roots = _roots(tmp_path)

    with _scripted_sec({}, []):
        with pytest.raises(SecHttpFailError) as excinfo:
            fetch_and_ingest(cik=_CIK, accession=_ACCESSION, **roots)

    assert excinfo.value.status_code == 404
    assert not roots["extracted_root"].exists()


# ---------- _index.json merge semantics ----------


def _seed_index(extracted_root: Path) -> None:
    extracted_root.mkdir(parents=True, exist_ok=True)
    (extracted_root / "_index.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "documents": {
                    "AAPL-2023Q3": {
                        "version": "7b9b5483" + "0" * 56,
                        "page_count": 29,
                        "ocr_applied": True,
                        "ocr_applied_pages": [4],
                    }
                },
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def test_live_write_merges_into_the_existing_index(tmp_path: Path) -> None:
    """Writing one accession must leave every other document in
    ``_index.json`` intact, including the fields ingest put there.

    Replacing the file wholesale silently drops the local corpus from
    the query SSOT, so ``load_extracted_tree`` starts failing on
    documents whose artifacts are still on disk.
    """
    roots = _roots(tmp_path)
    _seed_index(roots["extracted_root"])
    bodies = {_INDEX_URL: _AAPL_LISTING, _PRIMARY_URL: _PRIMARY_HTML}

    with _scripted_sec(bodies, []):
        fetch_and_ingest(cik=_CIK, accession=_ACCESSION, **roots)

    documents = json.loads(
        (roots["extracted_root"] / "_index.json").read_text()
    )["documents"]
    assert set(documents) == {"AAPL-2023Q3", _ACCESSION}
    assert documents["AAPL-2023Q3"]["page_count"] == 29
    assert documents["AAPL-2023Q3"]["ocr_applied_pages"] == [4]


def test_refetching_an_accession_updates_its_own_entry_only(
    tmp_path: Path,
) -> None:
    """A second fetch of the same accession replaces that entry (new
    bytes → new ``version``) and still leaves its neighbours alone."""
    roots = _roots(tmp_path)
    _seed_index(roots["extracted_root"])
    revised = _PRIMARY_HTML.replace(b"60,584", b"60,585")

    with _scripted_sec({_INDEX_URL: _AAPL_LISTING, _PRIMARY_URL: _PRIMARY_HTML}, []):
        fetch_and_ingest(cik=_CIK, accession=_ACCESSION, **roots)
    with _scripted_sec({_INDEX_URL: _AAPL_LISTING, _PRIMARY_URL: revised}, []):
        fetch_and_ingest(cik=_CIK, accession=_ACCESSION, **roots)

    documents = json.loads(
        (roots["extracted_root"] / "_index.json").read_text()
    )["documents"]
    assert set(documents) == {"AAPL-2023Q3", _ACCESSION}
    assert documents[_ACCESSION]["version"] == hashlib.sha256(revised).hexdigest()


def test_local_reingest_keeps_the_live_accession_in_the_index(
    tmp_path: Path,
) -> None:
    """The merge has to hold in both directions: a local ``run_ingest``
    over the same tree rebuilds the index from the manifest, and must
    not drop the accession the live path put there."""
    from fin.corpus.ingest import run_ingest

    data_dir = Path(__file__).resolve().parents[2] / "data" / "corpus"
    manifest = json.loads((data_dir / "manifest.json").read_text())
    row = next(
        doc
        for doc in manifest["documents"]
        if doc["document_id"] == "AAPL-2023Q3"
    )
    local_manifest = tmp_path / "local-manifest.json"
    local_manifest.write_text(json.dumps({"documents": [row]}))

    roots = _roots(tmp_path)
    with _scripted_sec({_INDEX_URL: _AAPL_LISTING, _PRIMARY_URL: _PRIMARY_HTML}, []):
        fetch_and_ingest(cik=_CIK, accession=_ACCESSION, **roots)

    run_ingest(
        manifest_path=local_manifest,
        pdf_dir=data_dir / "pdf",
        extracted_root=roots["extracted_root"],
        facts_root=roots["facts_root"],
    )

    documents = json.loads(
        (roots["extracted_root"] / "_index.json").read_text()
    )["documents"]
    assert set(documents) == {"AAPL-2023Q3", _ACCESSION}


def test_malformed_existing_index_refuses_rather_than_overwrites(
    tmp_path: Path,
) -> None:
    """An unreadable ``_index.json`` is a typed EXIT. Treating it as
    "no index" would let the live write replace a damaged-but-present
    catalog with a single-entry one."""
    roots = _roots(tmp_path)
    roots["extracted_root"].mkdir(parents=True)
    damaged = roots["extracted_root"] / "_index.json"
    damaged.write_text("{ this is not json")
    bodies = {_INDEX_URL: _AAPL_LISTING, _PRIMARY_URL: _PRIMARY_HTML}

    with _scripted_sec(bodies, []):
        with pytest.raises(IngestMissingError):
            fetch_and_ingest(cik=_CIK, accession=_ACCESSION, **roots)

    assert damaged.read_text() == "{ this is not json"


# ---------- the frozen unit ----------


def test_frozen_unit_is_stripped_text_and_ships_its_passages(
    tmp_path: Path,
) -> None:
    """The unit text is the HTML's stripped text (spec § locator), and
    the ``passages-NNN.json`` artifact ``load_extracted_tree`` requires
    is written alongside it."""
    roots = _roots(tmp_path)
    bodies = {_INDEX_URL: _AAPL_LISTING, _PRIMARY_URL: _PRIMARY_HTML}

    with _scripted_sec(bodies, []):
        fetch_and_ingest(cik=_CIK, accession=_ACCESSION, **roots)

    doc_dir = roots["extracted_root"] / _ACCESSION
    page = json.loads((doc_dir / "page-001.json").read_text())
    assert "<p>" not in page["text"] and "<table>" not in page["text"]
    assert "var ix" not in page["text"], "script bodies are not filing text"
    assert ".hide" not in page["text"], "style bodies are not filing text"
    assert "dei:AmendmentFlag" not in page["text"] and "false" not in page["text"], (
        "the inline-XBRL header block is not readable filing text"
    )
    assert "For the quarterly period ended July 1, 2023" in page["text"]
    # One statement row freezes as one line, so a claimed span quoting
    # across columns is a contiguous substring of the unit.
    assert "Products $ 60,584 $ 63,355" in page["text"]

    passages = json.loads((doc_dir / "passages-001.json").read_text())
    assert passages["document_id"] == _ACCESSION
    assert passages["version"] == page["version"]
    assert passages["passages"], "a non-empty unit must carry passages"
    first = passages["passages"][0]
    assert page["text"][first["char_start"] : first["char_end"]] == first["text"]


def test_frozen_unit_is_loadable_as_an_extracted_document(
    tmp_path: Path,
) -> None:
    """End-to-end: the tree the live path writes satisfies the read
    side, so a live accession can be cited like any frozen document."""
    from fin.corpus.extracted import load_extracted_tree

    roots = _roots(tmp_path)
    bodies = {_INDEX_URL: _AAPL_LISTING, _PRIMARY_URL: _PRIMARY_HTML}
    with _scripted_sec(bodies, []):
        fetch_and_ingest(cik=_CIK, accession=_ACCESSION, **roots)

    version = hashlib.sha256(_PRIMARY_HTML).hexdigest()
    roots["manifest_path"].write_text(
        json.dumps(
            {
                "documents": [
                    {"document_id": _ACCESSION, "sha256": version},
                ]
            }
        )
    )

    tree = load_extracted_tree(
        roots["manifest_path"], roots["extracted_root"]
    )

    pages = tree[_ACCESSION]
    assert len(pages) == 1
    assert pages[0].locator.version == version
    assert "Products $ 60,584 $ 63,355" in pages[0].text
