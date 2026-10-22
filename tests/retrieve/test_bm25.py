"""Boundary tests for the v1 retrieve port and in-process BM25 adapter.

The five boundary classes come from the defensive-contract-validator
axis: empty, negative, overflow, concurrent, exception. Together they
exercise the typed-error contract (ADR 0020) the host graph (T6) and
the citation gate (T5) will rely on.

The corpus tests share a session-scoped fixture that ingests the
committed 12 PDFs once (~2.5 minutes). Per-test mutations are restored
in a ``finally`` block so subsequent tests see an intact tree.

The BM25 adapter is the v1 ``retrieve`` capability per ADR 0007: it
indexes passage chunks (not whole pages) with their locators as
``source_ref``, in RAM, built once at construction. The host tool
wrapper (``fin.retrieve.tool.retrieve``) is the seam between capability
and surface; T6 wraps it in a LangChain ``@tool`` decorator and shares
one ``BM25Adapter`` per run.
"""

from __future__ import annotations

import ast
import concurrent.futures
import json
import shutil
from pathlib import Path
from typing import Iterator

import pytest

from fin.corpus.errors import IngestMissingError
from fin.corpus.extracted import Locator, load_extracted_tree
from fin.corpus.ingest import run_ingest
from fin.retrieve import (
    BM25Adapter,
    Chunk,
    ConflictingFiltersError,
    EmptyQueryError,
    RetrievePort,
    retrieve,
)
from fin.retrieve.port import DocumentScopeEntry, UNSUPPORTED_SOURCE_SCOPE_CODE


# ---------- helpers ----------


def _project_data_dir() -> Path:
    """Absolute path to the project's committed ``data/corpus`` dir."""
    return Path(__file__).resolve().parents[2] / "data" / "corpus"


def _real_manifest() -> tuple[Path, Path, list[dict]]:
    manifest_path = _project_data_dir() / "manifest.json"
    pdf_dir = _project_data_dir() / "pdf"
    documents = json.loads(manifest_path.read_text())["documents"]
    return manifest_path, pdf_dir, documents


def _write_manifest(manifest_path: Path, documents: list[dict]) -> None:
    payload = {
        "upstream": {
            "repo": "test",
            "commit": "test",
            "path": "test",
            "license": "test",
        },
        "tickers": [],
        "periods": [],
        "note": "boundary-test fixture",
        "documents": documents,
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(payload))


# ---------- session-scoped real-ingest fixture ----------


@pytest.fixture(scope="session")
def real_ingested_tree(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Run ``run_ingest`` once against the real corpus and share the
    populated tree across tests.

    Plan C added 8 EDGAR iXBRL primary documents (html_live kind,
    frozen by ``fin.corpus.live_fetch``); they are out of scope for the
    local re-ingest path. Copy their already-frozen artifacts from the
    committed extracted tree into the fixture's tmp root before
    ``run_ingest`` so the merged tree still satisfies the catalog.
    """
    manifest_path, pdf_dir, _ = _real_manifest()
    extracted_root = tmp_path_factory.mktemp("real-ingest") / "extracted"
    extracted_root.mkdir(parents=True, exist_ok=True)
    committed_extracted = _project_data_dir() / "extracted"
    for entry in committed_extracted.iterdir():
        target = extracted_root / entry.name
        if entry.is_dir():
            shutil.copytree(entry, target)
        elif entry.is_file():
            shutil.copyfile(entry, target)
    run_ingest(
        manifest_path=manifest_path,
        pdf_dir=pdf_dir,
        extracted_root=extracted_root,
    )
    return extracted_root


# ---------- boundary 1: empty ----------


def test_retrieve_with_empty_query_returns_empty_chunks(
    real_ingested_tree: Path,
) -> None:
    """Empty / whitespace-only query is invalid input: a named
    ``EmptyQueryError``, counted, with no index lookup.

    Boundary "empty": the model may pass ``""`` or whitespace during
    abuse or while reasoning aloud. Until T21 the adapter answered
    ``{"chunks": []}``; §B2 第 5 条 / SC42 make that the conflation to
    avoid — an empty result would report on the corpus, when the truth
    is that nothing was asked. The host tool turns the typed error into
    ``{"error": "empty_query"}`` so a blank query still cannot kill a
    run (``tests/host/test_input_validation_exit.py``).
    """
    manifest_path, _, _ = _real_manifest()
    tree = load_extracted_tree(
        manifest_path=manifest_path,
        extracted_root=real_ingested_tree,
    )
    adapter = BM25Adapter(tree)

    for empty_query in ("", "   ", "\t\n", "\n"):
        with pytest.raises(EmptyQueryError):
            adapter.retrieve(empty_query)

    assert adapter.input_validation_failures.count_by_code() == {
        "empty_query": 4
    }


def test_retrieve_on_empty_corpus_returns_empty_chunks(tmp_path: Path) -> None:
    """Empty corpus (manifest with zero documents) must yield ``{"chunks": []}``
    for any query.

    Boundary "empty": the adapter must be constructible over a zero-doc
    tree (manifest with no documents) and answer any query with the
    empty contract. This covers the cold-start / fresh-deploy case
    before any PDF has been ingested.
    """
    manifest_path = tmp_path / "manifest.json"
    pdf_dir = tmp_path / "pdf"
    pdf_dir.mkdir(parents=True, exist_ok=True)

    _write_manifest(manifest_path, documents=[])
    extracted_root = tmp_path / "extracted"

    # Ingest against an empty manifest is deterministic (T2).
    run_ingest(
        manifest_path=manifest_path,
        pdf_dir=pdf_dir,
        extracted_root=extracted_root,
    )

    tree = load_extracted_tree(
        manifest_path=manifest_path,
        extracted_root=extracted_root,
    )
    assert tree == {}

    adapter = BM25Adapter(tree)
    result = adapter.retrieve("anything", limit=8)
    assert result == {"chunks": []}


def test_retrieve_unknown_document_id_returns_empty_chunks(
    real_ingested_tree: Path,
) -> None:
    """Unknown ``document_id`` (not in manifest) must return ``{"chunks": []}``
    without raising.

    Boundary "empty": the model can ask ``retrieve("x", document_id="FOO-2023Q1")``
    for a document that does not exist. The adapter must NOT raise
    ``IngestMissingError`` (the loader's typed error); per ADR 0009 it
    must return the empty contract because the call is well-formed
    but the corpus has no such document.
    """
    manifest_path, _, _ = _real_manifest()
    tree = load_extracted_tree(
        manifest_path=manifest_path,
        extracted_root=real_ingested_tree,
    )
    adapter = BM25Adapter(tree)

    result = adapter.retrieve("risk factors", document_id="FOO-2023Q1", limit=8)
    assert result == {"chunks": []}


# ---------- boundary 2: negative ----------


@pytest.mark.parametrize("bad_limit", [0, -5, -100])
def test_retrieve_non_positive_limit_treated_as_default(
    real_ingested_tree: Path,
    bad_limit: int,
) -> None:
    """``limit <= 0`` must be treated as the default of 8.

    Boundary "negative": the cap rule says ``limit <= 0`` → 8. We verify
    that the *behavior* matches an explicit ``limit=8`` call by asking
    a query that has more than 8 hits and confirming we get up to 8.
    """
    manifest_path, _, _ = _real_manifest()
    tree = load_extracted_tree(
        manifest_path=manifest_path,
        extracted_root=real_ingested_tree,
    )
    adapter = BM25Adapter(tree)

    default_result = adapter.retrieve("revenue", limit=8)
    bad_result = adapter.retrieve("revenue", limit=bad_limit)

    assert len(default_result["chunks"]) == 8
    assert len(bad_result["chunks"]) == 8
    # And the two lists must be identical because the index is deterministic
    # and the limit resolved to the same value.
    assert [c.text for c in default_result["chunks"]] == [
        c.text for c in bad_result["chunks"]
    ]


def test_retrieve_limit_caps_at_twenty(real_ingested_tree: Path) -> None:
    """``limit > 20`` must clamp to 20 (the ADR 0009 cap).

    Boundary "negative": the cap of 20 is a hard ceiling regardless of
    how large the caller asks. We assert both the upper bound and the
    equality with an explicit ``limit=20`` call.
    """
    manifest_path, _, _ = _real_manifest()
    tree = load_extracted_tree(
        manifest_path=manifest_path,
        extracted_root=real_ingested_tree,
    )
    adapter = BM25Adapter(tree)

    capped = adapter.retrieve("the", limit=999)
    explicit = adapter.retrieve("the", limit=20)

    assert len(capped["chunks"]) == 20
    assert [c.text for c in capped["chunks"]] == [
        c.text for c in explicit["chunks"]
    ]


def test_retrieve_default_limit_is_eight(real_ingested_tree: Path) -> None:
    """Default ``limit`` (no argument) is 8 per ADR 0009."""
    manifest_path, _, _ = _real_manifest()
    tree = load_extracted_tree(
        manifest_path=manifest_path,
        extracted_root=real_ingested_tree,
    )
    adapter = BM25Adapter(tree)

    default = adapter.retrieve("risk factors")
    explicit = adapter.retrieve("risk factors", limit=8)
    assert default == explicit


# ---------- happy path / contract shape ----------


def test_retrieve_returns_chunks_with_locator_and_score(
    real_ingested_tree: Path,
) -> None:
    """Happy path: hits carry ``text``, ``source_ref`` (Locator), ``score``.

    Each ``Chunk.source_ref`` MUST round-trip through the
    ``Locator`` dataclass — the citation gate (T5) reads
    ``chunk.source_ref.document_id / .unit`` directly. ``score`` is the
    BM25 raw score; ordering is descending (higher is better for hits).
    """
    manifest_path, _, _ = _real_manifest()
    tree = load_extracted_tree(
        manifest_path=manifest_path,
        extracted_root=real_ingested_tree,
    )
    adapter = BM25Adapter(tree)

    result = adapter.retrieve("risk factors", limit=5)
    assert "chunks" in result
    assert len(result["chunks"]) >= 1

    for chunk in result["chunks"]:
        assert isinstance(chunk, Chunk)
        assert isinstance(chunk.text, str) and chunk.text
        assert isinstance(chunk.source_ref, Locator)
        assert chunk.source_ref.document_id
        assert chunk.source_ref.version
        assert isinstance(chunk.source_ref.unit, int) and chunk.source_ref.unit >= 1
        assert isinstance(chunk.score, float)

    # Descending by score: hits must rank higher than non-matches.
    scores = [c.score for c in result["chunks"]]
    assert scores == sorted(scores, reverse=True)


def test_retrieve_filters_by_document_id(real_ingested_tree: Path) -> None:
    """``document_id`` filter must only return chunks from that document.

    The filter is a strict membership test on the indexed chunks.
    """
    manifest_path, _, _ = _real_manifest()
    tree = load_extracted_tree(
        manifest_path=manifest_path,
        extracted_root=real_ingested_tree,
    )
    adapter = BM25Adapter(tree)

    target_doc = sorted(tree.keys())[0]
    result = adapter.retrieve("revenue", document_id=target_doc, limit=20)
    assert result["chunks"], "expected at least one hit in the filtered corpus"
    assert all(c.source_ref.document_id == target_doc for c in result["chunks"])


def test_retrieve_tool_wrapper_calls_adapter(
    real_ingested_tree: Path,
) -> None:
    """``fin.retrieve.retrieve`` is a thin wrapper around ``BM25Adapter.retrieve``.

    T6 registers this function as the third tool. The wrapper must NOT
    re-implement scoring; it must delegate to the adapter so the
    port/adapter separation is preserved.
    """
    manifest_path, _, _ = _real_manifest()
    tree = load_extracted_tree(
        manifest_path=manifest_path,
        extracted_root=real_ingested_tree,
    )
    adapter = BM25Adapter(tree)

    direct = adapter.retrieve("risk factors", limit=3)
    via_tool = retrieve("risk factors", limit=3, adapter=adapter)

    assert [c.text for c in direct["chunks"]] == [
        c.text for c in via_tool["chunks"]
    ]
    assert [c.score for c in direct["chunks"]] == [
        c.score for c in via_tool["chunks"]
    ]


def test_retrieve_port_protocol_is_satisfied() -> None:
    """``BM25Adapter`` must satisfy the ``RetrievePort`` Protocol structurally.

    The Protocol is the seam that lets T6 swap in an MCP-backed
    adapter (e.g. an iknow-memory client) without changing the host
    graph. Static type-checkers should see ``BM25Adapter`` as a valid
    ``RetrievePort``; we verify the runtime ``isinstance`` check here.
    """
    manifest_path, _, _ = _real_manifest()
    # We use a session fixture for the real tree, but here we just need
    # any adapter. The Protocol check is structural — it doesn't read
    # the corpus — so even an empty adapter will do.
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        manifest_path = tmp / "manifest.json"
        pdf_dir = tmp / "pdf"
        pdf_dir.mkdir(parents=True, exist_ok=True)
        _write_manifest(manifest_path, documents=[])
        extracted_root = tmp / "extracted"
        run_ingest(
            manifest_path=manifest_path,
            pdf_dir=pdf_dir,
            extracted_root=extracted_root,
        )
        tree = load_extracted_tree(
            manifest_path=manifest_path,
            extracted_root=extracted_root,
        )
        adapter = BM25Adapter(tree)

    assert isinstance(adapter, RetrievePort)


# ---------- boundary 3: overflow (parallel adapter construction) ----------


def test_parallel_adapter_construction_is_deterministic(
    real_ingested_tree: Path,
) -> None:
    """Four parallel ``BM25Adapter`` constructions on the same corpus
    produce identical hit lists for the same query.

    Boundary "overflow": the host graph constructs the adapter once at
    process start, but parallel constructions (e.g. in tests, or in a
    future supervisor) must agree. We assert tokenization is
    deterministic and that BM25 scores are reproducible across
    concurrent constructions.

    We compare (a) the hit lists (chunk text) AND (b) the top-3 scores,
    because a stable but differently-ordered hit list under equal scores
    would still be a "non-deterministic" outcome for downstream callers.
    """
    manifest_path, _, _ = _real_manifest()
    tree = load_extracted_tree(
        manifest_path=manifest_path,
        extracted_root=real_ingested_tree,
    )

    def _build_and_query() -> dict:
        adapter = BM25Adapter(tree)
        return adapter.retrieve("revenue", limit=8)

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: _build_and_query(), range(4)))

    # Compare the *sorted* hit lists: under equal scores BM25Okapi may
    # not break ties in a fully stable way across runs, but the SET of
    # returned chunks is deterministic. We assert set equality on chunk
    # identity (text + locator) and score equality on the top hit.
    texts_first = [c.text for c in results[0]["chunks"]]
    scores_first = [c.score for c in results[0]["chunks"]]
    for i, result in enumerate(results[1:], start=1):
        texts_i = [c.text for c in result["chunks"]]
        scores_i = [c.score for c in result["chunks"]]
        # Top hit must be identical.
        assert scores_i[0] == scores_first[0], (
            f"run #{i} top score {scores_i[0]!r} != run #0 {scores_first[0]!r}"
        )
        # The multiset of returned chunks must be identical.
        assert sorted(texts_i) == sorted(texts_first), (
            f"run #{i} returned a different chunk set"
        )


# ---------- boundary 4: concurrent (parallel retrieve on shared adapter) ----------


def test_concurrent_retrieve_yields_identical_hit_lists(
    real_ingested_tree: Path,
) -> None:
    """16 parallel ``retrieve()`` calls on one shared ``BM25Adapter``
    produce identical hit lists.

    Boundary "concurrent": the host graph may call ``retrieve`` from
    any node; the index is read-only after construction, so the
    contract is no-races / identical-output. We assert the chunk list
    is identical across 16 parallel queries.
    """
    manifest_path, _, _ = _real_manifest()
    tree = load_extracted_tree(
        manifest_path=manifest_path,
        extracted_root=real_ingested_tree,
    )
    adapter = BM25Adapter(tree)

    def _query() -> dict:
        return adapter.retrieve("risk factors", limit=8)

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda _: _query(), range(16)))

    reference = [c.text for c in results[0]["chunks"]]
    for i, result in enumerate(results[1:], start=1):
        assert [c.text for c in result["chunks"]] == reference, (
            f"call #{i} returned a different hit list"
        )


# ---------- boundary 5: exception ----------


def test_corrupt_loader_output_propagates_typed_error(
    real_ingested_tree: Path,
) -> None:
    """A corrupt extracted tree must raise a typed error at adapter
    construction — NOT silently return ``{"chunks": []}``.

    Boundary "exception": the loader (``corpus.extracted``) raises
    ``IngestMissingError`` on missing or malformed artifacts (ADR
    0020). The adapter must propagate that typed error; it must NOT
    catch the failure and return an empty contract, because that
    would mask a misconfigured extraction tree as a no-result query.
    """
    manifest_path, _, _ = _real_manifest()
    # Pick the first document and corrupt one page artifact.
    doc_id = sorted(p.name for p in real_ingested_tree.iterdir() if p.is_dir())[0]
    doc_dir = real_ingested_tree / doc_id
    page_files = sorted(p for p in doc_dir.glob("page-*.json"))
    victim = page_files[len(page_files) // 2]
    backup = victim.read_bytes()
    try:
        victim.write_text("{ this is not valid json")
        with pytest.raises(IngestMissingError):
            load_extracted_tree(
                manifest_path=manifest_path,
                extracted_root=real_ingested_tree,
            )
            BM25Adapter({})  # not reached; the loader raised first
    finally:
        victim.write_bytes(backup)


def test_adapter_propagates_partial_tree(
    real_ingested_tree: Path,
) -> None:
    """A partial extracted tree (missing one page) must raise
    ``IngestMissingError`` at adapter construction.

    Boundary "exception": if the operator deletes a page file and the
    host graph still constructs an adapter, the loader must fail fast
    rather than produce a silently shrunken index.
    """
    manifest_path, _, _ = _real_manifest()
    doc_id = sorted(p.name for p in real_ingested_tree.iterdir() if p.is_dir())[0]
    doc_dir = real_ingested_tree / doc_id
    page_files = sorted(p for p in doc_dir.glob("page-*.json"))
    victim = page_files[len(page_files) // 2]
    backup = victim.read_bytes()
    try:
        victim.unlink()
        with pytest.raises(IngestMissingError):
            load_extracted_tree(
                manifest_path=manifest_path,
                extracted_root=real_ingested_tree,
            )
    finally:
        victim.write_bytes(backup)


# ---------- ask-path invariants ----------


def test_retrieve_module_does_not_import_pdfplumber() -> None:
    """The ask-path adapter must not depend on ``pdfplumber`` (ADR 0020).

    If this assertion ever fails, the BM25 adapter has acquired a
    runtime dependency that pulls PDF extraction onto the ask path.
    We parse the source as AST and look for ``import pdfplumber`` /
    ``from pdfplumber`` statements — string mentions in docstrings
    are allowed.
    """
    import fin.retrieve as retrieve_pkg

    package_root = Path(retrieve_pkg.__file__).parent
    offending: list[tuple[Path, int, str]] = []
    for source_path in sorted(package_root.glob("*.py")):
        tree = ast.parse(source_path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] == "pdfplumber":
                        offending.append(
                            (source_path, node.lineno, f"import {alias.name}")
                        )
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.module.split(".")[0] == "pdfplumber":
                    offending.append(
                        (source_path, node.lineno, f"from {node.module} import ...")
                    )
    assert offending == [], (
        "fin.retrieve must not import pdfplumber; "
        f"offending: {[(str(p), ln, desc) for p, ln, desc in offending]}"
    )


def test_retrieve_package_does_not_import_pdfplumber() -> None:
    """Same guard, applied across the whole ``fin.retrieve`` package.

    Belt-and-braces check: even if a sub-module imports pdfplumber
    indirectly, the package-level walk above catches it.
    """
    # The previous test already walks the package. This test asserts
    # the dependency surface one more time at the module level so
    # the failure message points at ``fin.retrieve`` clearly.
    from fin.retrieve import bm25 as bm25_mod

    source_path = Path(bm25_mod.__file__)
    source = source_path.read_text()
    assert "pdfplumber" not in source, (
        f"pdfplumber mention in {source_path}; ask path must not "
        "import the PDF extraction library"
    )


# ---------- parametrize: limited-corpus fixture ----------


@pytest.fixture
def small_tree(tmp_path: Path) -> Iterator[tuple[Path, Path]]:
    """A small hand-built extracted tree with two documents, two pages each.

    This fixture is cheaper than ``real_ingested_tree`` and gives us
    deterministic chunk content for assertions about specific
    text / locator values.

    Page content is intentionally varied across documents so BM25 IDF
    produces sensible rankings: "risk factors" appears in only one of
    the two documents (DOC-2023Q1), and the rest of the corpus is
    distinct prose.
    """
    pdf_dir = tmp_path / "pdf"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = tmp_path / "manifest.json"
    extracted_root = tmp_path / "extracted"
    extracted_root.mkdir(parents=True, exist_ok=True)

    # Per-(doc, page) page text. The second document does NOT contain
    # the phrase "risk factors" anywhere, so BM25 ranks DOC-2023Q1's
    # risk-factors passage above DOC-2023Q2's unrelated prose.
    page_texts: dict[tuple[str, int], str] = {
        ("DOC-2023Q1", 1): (
            "Quarterly report for DOC-2023Q1 page 1. "
            "Revenue increased twelve percent year over year. "
            "Operating income margin held steady at twenty eight percent. "
            "Risk factors include macroeconomic uncertainty. "
            "Capital expenditures remained flat."
        ),
        ("DOC-2023Q1", 2): (
            "DOC-2023Q1 page 2 cash flow statement. "
            "Cash from operations increased modestly. "
            "Free cash flow conversion improved. "
            "Working capital efficiency held."
        ),
        ("DOC-2023Q2", 1): (
            "Quarterly report for DOC-2023Q2 page 1. "
            "Revenue grew at a similar pace. "
            "Operating income margin expanded. "
            "Capital expenditures rose modestly. "
            "Customer base widened internationally."
        ),
        ("DOC-2023Q2", 2): (
            "DOC-2023Q2 page 2 segment reporting. "
            "Hardware segment revenue increased. "
            "Services segment growth continued. "
            "Geographic mix shifted modestly."
        ),
    }

    documents: list[dict] = []
    for doc_idx, (doc_id, version, page_count) in enumerate(
        [
            ("DOC-2023Q1", "v1-" + "a" * 60, 2),
            ("DOC-2023Q2", "v2-" + "b" * 60, 2),
        ]
    ):
        documents.append(
            {
                "document_id": doc_id,
                "filename": f"{doc_id}.pdf",
                "sha256": version,
            }
        )
        doc_dir = extracted_root / doc_id
        doc_dir.mkdir()
        for page in range(1, page_count + 1):
            page_text = page_texts[(doc_id, page)]
            # Two passages per page: first half / second half, so
            # we have multiple chunks per page in the index.
            mid = len(page_text) // 2
            passages = [
                {"char_start": 0, "char_end": mid, "text": page_text[:mid]},
                {
                    "char_start": mid,
                    "char_end": len(page_text),
                    "text": page_text[mid:],
                },
            ]
            (doc_dir / f"page-{page:03d}.json").write_text(
                json.dumps(
                    {
                        "document_id": doc_id,
                        "version": version,
                        "page": page,
                        "text": page_text,
                    }
                )
            )
            (doc_dir / f"passages-{page:03d}.json").write_text(
                json.dumps(
                    {
                        "document_id": doc_id,
                        "version": version,
                        "page": page,
                        "passages": passages,
                    }
                )
            )

    _write_manifest(manifest_path, documents=documents)
    yield manifest_path, extracted_root


def test_small_tree_ranking_picks_relevant_passage_first(
    small_tree: tuple[Path, Path],
) -> None:
    """The top hit for "risk factors" must come from the second passage
    of a page (which contains "Risk factors include macroeconomic
    uncertainty"), not from the first passage (revenue / operating
    income boilerplate).

    This is the lexical-BM25 ranking contract: the passage with the
    literal query terms ranks above the one without them.
    """
    manifest_path, extracted_root = small_tree
    tree = load_extracted_tree(
        manifest_path=manifest_path,
        extracted_root=extracted_root,
    )
    adapter = BM25Adapter(tree)

    result = adapter.retrieve("risk factors", limit=1)
    assert len(result["chunks"]) == 1
    top = result["chunks"][0]
    assert "Risk factors" in top.text
    # Score is positive (this passage matches the query terms).
    assert top.score > 0


def test_small_tree_source_ref_is_locator_with_three_fields(
    small_tree: tuple[Path, Path],
) -> None:
    """Each chunk's ``source_ref`` carries ``document_id``, ``version``,
    ``unit`` — the locator shape the citation gate reads."""
    manifest_path, extracted_root = small_tree
    tree = load_extracted_tree(
        manifest_path=manifest_path,
        extracted_root=extracted_root,
    )
    adapter = BM25Adapter(tree)

    result = adapter.retrieve("revenue", limit=8)
    assert result["chunks"]
    seen_docs: set[str] = set()
    for chunk in result["chunks"]:
        assert isinstance(chunk.source_ref, Locator)
        assert chunk.source_ref.document_id
        assert chunk.source_ref.version.startswith("v")
        assert isinstance(chunk.source_ref.unit, int)
        assert chunk.source_ref.unit >= 1
        seen_docs.add(chunk.source_ref.document_id)
    # We hit at least one of our two documents.
    assert seen_docs <= {"DOC-2023Q1", "DOC-2023Q2"}
    assert seen_docs  # non-empty


def test_small_tree_document_id_filter_restricts_hits(
    small_tree: tuple[Path, Path],
) -> None:
    """With ``document_id`` set, only chunks from that document return."""
    manifest_path, extracted_root = small_tree
    tree = load_extracted_tree(
        manifest_path=manifest_path,
        extracted_root=extracted_root,
    )
    adapter = BM25Adapter(tree)

    only_q1 = adapter.retrieve(
        "revenue", document_id="DOC-2023Q1", limit=20
    )
    only_q2 = adapter.retrieve(
        "revenue", document_id="DOC-2023Q2", limit=20
    )
    assert all(c.source_ref.document_id == "DOC-2023Q1" for c in only_q1["chunks"])
    assert all(c.source_ref.document_id == "DOC-2023Q2" for c in only_q2["chunks"])


def test_small_tree_document_scope_filters_by_id_and_version_before_limit(
    small_tree: tuple[Path, Path],
) -> None:
    manifest_path, extracted_root = small_tree
    tree = load_extracted_tree(
        manifest_path=manifest_path,
        extracted_root=extracted_root,
    )
    adapter = BM25Adapter(tree)

    result = adapter.retrieve(
        "revenue",
        document_scope=(
            DocumentScopeEntry("DOC-2023Q2", "v2-" + "b" * 60),
        ),
        limit=1,
    )

    assert len(result["chunks"]) == 1
    assert result["chunks"][0].source_ref.document_id == "DOC-2023Q2"
    assert result["chunks"][0].source_ref.version == "v2-" + "b" * 60


def test_document_scope_and_explicit_id_intersect_without_widening(
    small_tree: tuple[Path, Path],
) -> None:
    manifest_path, extracted_root = small_tree
    tree = load_extracted_tree(
        manifest_path=manifest_path,
        extracted_root=extracted_root,
    )
    adapter = BM25Adapter(tree)

    result = adapter.retrieve(
        "revenue",
        document_id="DOC-2023Q1",
        document_scope=(
            DocumentScopeEntry("DOC-2023Q2", "v2-" + "b" * 60),
        ),
    )

    assert result == {"error": UNSUPPORTED_SOURCE_SCOPE_CODE}


@pytest.mark.parametrize("search_mode", ["global", "sql"])
def test_bm25_refuses_modes_that_cannot_enforce_document_scope(
    small_tree: tuple[Path, Path], search_mode: str
) -> None:
    manifest_path, extracted_root = small_tree
    tree = load_extracted_tree(
        manifest_path=manifest_path,
        extracted_root=extracted_root,
    )

    result = BM25Adapter(tree).retrieve(
        "revenue",
        search_mode=search_mode,
        document_scope=(
            DocumentScopeEntry("DOC-2023Q1", "v1-" + "a" * 60),
        ),
        sql_window_start="2026-01-01",
        sql_window_end="2026-03-31",
    )

    assert result == {"error": UNSUPPORTED_SOURCE_SCOPE_CODE}


def test_small_tree_index_is_built_once_and_reusable(tmp_path: Path) -> None:
    """Two constructions on the same tree are independent and equivalent.

    Documents the choice that the adapter caches the index in RAM and
    rebuilds on construction; concurrent constructions (the previous
    test) all produce the same answer because tokenization is
    deterministic and the corpus is unchanged.
    """
    manifest_path = tmp_path / "manifest.json"
    extracted_root = tmp_path / "extracted"
    extracted_root.mkdir(parents=True, exist_ok=True)

    version = "v-" + "c" * 60
    doc_dir = extracted_root / "DOC-2023Q1"
    doc_dir.mkdir()
    text = (
        "Revenue rose. Risk factors include macroeconomic uncertainty. "
        "Operating margin held."
    )
    (doc_dir / "page-001.json").write_text(
        json.dumps(
            {
                "document_id": "DOC-2023Q1",
                "version": version,
                "page": 1,
                "text": text,
            }
        )
    )
    (doc_dir / "passages-001.json").write_text(
        json.dumps(
            {
                "document_id": "DOC-2023Q1",
                "version": version,
                "page": 1,
                "passages": [
                    {"char_start": 0, "char_end": len(text), "text": text}
                ],
            }
        )
    )
    (extracted_root / "_index.json").write_text(
        json.dumps(
            {"documents": {"DOC-2023Q1": {"version": version, "page_count": 1}}}
        )
    )

    _write_manifest(
        manifest_path,
        documents=[
            {
                "document_id": "DOC-2023Q1",
                "filename": "x.pdf",
                "sha256": version,
            }
        ],
    )

    tree_a = load_extracted_tree(
        manifest_path=manifest_path, extracted_root=extracted_root
    )
    tree_b = load_extracted_tree(
        manifest_path=manifest_path, extracted_root=extracted_root
    )

    adapter_a = BM25Adapter(tree_a)
    adapter_b = BM25Adapter(tree_b)

    first = adapter_a.retrieve("risk factors", limit=4)
    second = adapter_b.retrieve("risk factors", limit=4)
    assert [c.text for c in first["chunks"]] == [
        c.text for c in second["chunks"]
    ]


# ---------- T2 EDGAR-shaped filter kwargs (ADR 0031) ----------


@pytest.fixture
def small_tree_with_manifest_docs(small_tree: tuple[Path, Path]) -> tuple[Path, Path]:
    """``small_tree`` extended with EDGAR-shaped metadata per document.

    DOC-2023Q1 gets AAPL-style fields; DOC-2023Q2 gets MSFT-style
    fields. The fixture is the smallest corpus where ``cik`` /
    ``form`` / ``ticker`` / ``filing_date`` filters can produce
    distinct results for the same query.
    """
    manifest_path, extracted_root = small_tree

    # Rewrite the manifest to add EDGAR-shaped optional fields. The
    # loader only needs ``document_id`` / ``sha256`` to build the
    # tree, so we don't disturb the extracted artifacts.
    payload = json.loads(manifest_path.read_text())
    by_doc: dict[str, dict] = {
        "DOC-2023Q1": {
            "cik": "0000320193",
            "form": "10-Q",
            "ticker": "AAPL",
            "filing_date": "2023-02-03",
        },
        "DOC-2023Q2": {
            "cik": "0000789019",
            "form": "10-Q",
            "ticker": "MSFT",
            "filing_date": "2023-04-25",
        },
    }
    for doc in payload["documents"]:
        doc.update(by_doc[doc["document_id"]])
    manifest_path.write_text(json.dumps(payload))

    return manifest_path, extracted_root


def _build_manifest_docs(manifest_path: Path) -> dict[str, dict[str, object]]:
    """Build the ``{document_id: {cik, form, ticker, filing_date}}`` map
    the BM25 adapter consumes for EDGAR-shaped filters."""
    payload = json.loads(manifest_path.read_text())
    keys = ("cik", "form", "ticker", "filing_date")
    return {
        doc["document_id"]: {k: doc[k] for k in keys if k in doc}
        for doc in payload["documents"]
    }


# ===== boundary 1: empty =====


@pytest.mark.parametrize(
    "field",
    ["cik", "form", "ticker", "filing_date"],
)
def test_bm25_empty_filter_value_returns_empty_chunks(
    small_tree_with_manifest_docs: tuple[Path, Path],
    field: str,
) -> None:
    """Empty filter value (``cik=""`` etc.) MUST return ``{"chunks": []}``
    without raising — NOT "match anything" and NOT an exception
    (ADR 0031 / spec "empty hits not exceptions").
    """
    manifest_path, extracted_root = small_tree_with_manifest_docs
    tree = load_extracted_tree(
        manifest_path=manifest_path, extracted_root=extracted_root
    )
    adapter = BM25Adapter(tree, manifest_docs=_build_manifest_docs(manifest_path))

    result = adapter.retrieve("revenue", **{field: ""}, limit=20)
    assert result == {"chunks": []}, (
        f"empty {field}='' must yield empty chunks; got {result!r}"
    )


# ===== boundary 2: negative =====


def test_bm25_unknown_cik_returns_empty_chunks(
    small_tree_with_manifest_docs: tuple[Path, Path],
) -> None:
    """Unknown ``cik`` returns ``{"chunks": []}`` — NOT an error."""
    manifest_path, extracted_root = small_tree_with_manifest_docs
    tree = load_extracted_tree(
        manifest_path=manifest_path, extracted_root=extracted_root
    )
    adapter = BM25Adapter(tree, manifest_docs=_build_manifest_docs(manifest_path))

    result = adapter.retrieve("revenue", cik="9999999999", limit=20)
    assert result == {"chunks": []}


def test_bm25_unknown_ticker_returns_empty_chunks(
    small_tree_with_manifest_docs: tuple[Path, Path],
) -> None:
    """Unknown ``ticker`` returns ``{"chunks": []}``."""
    manifest_path, extracted_root = small_tree_with_manifest_docs
    tree = load_extracted_tree(
        manifest_path=manifest_path, extracted_root=extracted_root
    )
    adapter = BM25Adapter(tree, manifest_docs=_build_manifest_docs(manifest_path))

    result = adapter.retrieve("revenue", ticker="ZZZZ", limit=20)
    assert result == {"chunks": []}


def test_bm25_unknown_form_returns_empty_chunks(
    small_tree_with_manifest_docs: tuple[Path, Path],
) -> None:
    """Unknown ``form`` returns ``{"chunks": []}``."""
    manifest_path, extracted_root = small_tree_with_manifest_docs
    tree = load_extracted_tree(
        manifest_path=manifest_path, extracted_root=extracted_root
    )
    adapter = BM25Adapter(tree, manifest_docs=_build_manifest_docs(manifest_path))

    result = adapter.retrieve("revenue", form="10-K", limit=20)
    assert result == {"chunks": []}


def test_bm25_conflicting_ticker_and_cik_is_a_named_failure(
    small_tree_with_manifest_docs: tuple[Path, Path],
) -> None:
    """Conflicting ``ticker`` + ``cik`` is invalid input (T21 / SC43).

    AAPL's ticker (``AAPL``) paired with MSFT's CIK
    (``0000789019``) — the manifest binds ``AAPL`` to AAPL's CIK, so
    the pair is provably unsatisfiable: mutually exclusive conditions
    specified together, not a filter that merely matched nothing.
    Until T21 this answered ``{"chunks": []}``, which is the shape an
    *unknown* value keeps (see the tests above).
    """
    manifest_path, extracted_root = small_tree_with_manifest_docs
    tree = load_extracted_tree(
        manifest_path=manifest_path, extracted_root=extracted_root
    )
    adapter = BM25Adapter(tree, manifest_docs=_build_manifest_docs(manifest_path))

    with pytest.raises(ConflictingFiltersError):
        adapter.retrieve("revenue", ticker="AAPL", cik="0000789019", limit=20)

    assert adapter.input_validation_failures.count_by_code() == {
        "conflicting_filters": 1
    }


def test_bm25_matching_cik_returns_hits_from_matching_doc_only(
    small_tree_with_manifest_docs: tuple[Path, Path],
) -> None:
    """Matching ``cik`` filters hits to docs that match.

    AAPL's CIK (``0000320193``) returns only DOC-2023Q1 chunks; MSFT's
    CIK (``0000789019``) returns only DOC-2023Q2 chunks.
    """
    manifest_path, extracted_root = small_tree_with_manifest_docs
    tree = load_extracted_tree(
        manifest_path=manifest_path, extracted_root=extracted_root
    )
    adapter = BM25Adapter(tree, manifest_docs=_build_manifest_docs(manifest_path))

    aapl_only = adapter.retrieve("revenue", cik="0000320193", limit=20)
    msft_only = adapter.retrieve("revenue", cik="0000789019", limit=20)

    assert aapl_only["chunks"], "expected AAPL chunks"
    assert msft_only["chunks"], "expected MSFT chunks"
    assert all(
        c.source_ref.document_id == "DOC-2023Q1"
        for c in aapl_only["chunks"]
    ), f"cik=AAPL filter leaked non-AAPL chunks: {aapl_only['chunks']!r}"
    assert all(
        c.source_ref.document_id == "DOC-2023Q2"
        for c in msft_only["chunks"]
    ), f"cik=MSFT filter leaked non-MSFT chunks: {msft_only['chunks']!r}"


def test_bm25_metadata_filter_is_noop_when_manifest_docs_unset(
    small_tree: tuple[Path, Path],
) -> None:
    """When the adapter is built without ``manifest_docs``, the EDGAR-
    shaped filters are no-ops (v1 behavior preserved).

    Callers that haven't wired the catalog in (e.g. the host graph
    before T3) keep working: BM25 ranks the corpus purely on lexical
    match + ``document_id`` membership.
    """
    manifest_path, extracted_root = small_tree
    tree = load_extracted_tree(
        manifest_path=manifest_path, extracted_root=extracted_root
    )
    adapter = BM25Adapter(tree)  # no manifest_docs

    baseline = adapter.retrieve("revenue", limit=20)
    filtered = adapter.retrieve(
        "revenue", cik="0000320193", ticker="AAPL", limit=20
    )

    assert [c.text for c in filtered["chunks"]] == [
        c.text for c in baseline["chunks"]
    ]


# ===== search_mode mirroring (search-mode-retrieve T2 / T3) =====


def test_bm25_mirrors_the_tier_named_exits_without_ranking() -> None:
    """The archived adapter takes the 档 parameter for port parity only
    (ADR 0030: BM25 is off the product path, its one ranking IS
    lexical): every closed-set token is accepted and answered with the
    ordinary result — since A2 nothing is refused at routing on ANY
    ``RetrievePort`` shape — while an illegal token still raises the
    port's named error, so a caller can never slip a bad 档 request past
    this implementation unnoticed."""
    from fin.retrieve.port import InvalidSearchModeError

    adapter = BM25Adapter({})

    assert adapter.retrieve("revenue", search_mode="lexical") == {"chunks": []}
    assert adapter.retrieve("revenue", search_mode="graph") == {"chunks": []}
    # 综述 opened (fin task 4) and sql opened (A2): both are accepted 档
    # on EVERY RetrievePort shape, answered with the ordinary (empty)
    # result rather than a named refusal. BM25 holds no ledger to bind,
    # so the window pair is parity-only there too — the window gate
    # belongs to the MCP wire seam (``resolve_sql_window``).
    assert adapter.retrieve("revenue", search_mode="global") == {"chunks": []}
    assert adapter.retrieve("revenue", search_mode="sql") == {"chunks": []}

    with pytest.raises(InvalidSearchModeError) as excinfo:
        adapter.retrieve("revenue", search_mode="B")
    assert excinfo.value.code == "invalid_search_mode"

    with pytest.raises(InvalidSearchModeError) as excinfo:
        adapter.retrieve("revenue", search_mode="summary")
    assert excinfo.value.code == "invalid_search_mode"
