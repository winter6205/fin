"""In-process BM25 adapter for v1 retrieve (ADR 0007).

The adapter is the v1 implementation behind ``RetrievePort``: a
lexical BM25 ranker over page-passage chunks, each carrying a
``Locator`` as ``source_ref``. No embeddings, no cosine, no vector
store — the index is built in RAM at process start from the
committed extracted tree (ADR 0020) and never re-tokenizes during
``retrieve()`` calls.

Tokenization
------------
v1 tokenization is intentionally simple:

* lowercase the text,
* split on the regex ``[a-z0-9]+`` (after lower-casing),
* drop a small hardcoded set of ~30 English stopwords.

This matches the v1 corpus: SEC 10-Q filings are dense English prose
where lexical overlap (e.g. "risk factors", "revenue increased") is a
strong ranking signal. Stopwords are stripped because they appear on
every page and would otherwise drown the IDF component.

The tokenization is deterministic — the same text always produces the
same token list — which is what makes parallel constructions (the
overflow boundary) and idempotent re-runs safe.

Construction
------------
The constructor walks the extracted tree once and builds a
``BM25Okapi`` index over every ``Passage`` on every page of every
document. The index is read-only after construction; ``retrieve()``
is ``O(query_tokens × corpus)`` and does not re-tokenize the corpus.

Filter semantics
----------------
``document_id`` is a strict membership test on the loaded corpus:

* In-corpus → only chunks from that document are considered.
* Not-in-corpus → empty contract (``{"chunks": []}``). We do NOT
  raise ``IngestMissingError`` because the call is well-formed; the
  loader's typed error is reserved for missing/malformed artifacts,
  not "no such document".

Limit cap
---------
Per ADR 0009: default ``limit`` is 8, hard cap is 20. ``limit <= 0``
falls back to the default of 8. ``limit > 20`` is clamped to 20. The
cap applies regardless of how many hits the corpus has.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from rank_bm25 import BM25Okapi  # type: ignore[import-untyped]

from fin.corpus.extracted import ExtractedPage, Locator
from fin.retrieve.port import Chunk


# A small hardcoded set of ~30 English stopwords. Stripped at
# tokenization to avoid drowning the IDF component.
_STOPWORDS: frozenset[str] = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "by", "for", "from",
        "has", "have", "in", "is", "it", "its", "of", "on", "or", "that",
        "the", "this", "to", "was", "were", "will", "with", "which", "but",
        "not", "their", "our",
    }
)

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# v1 limit cap per ADR 0009: default 8, hard cap 20.
_DEFAULT_LIMIT = 8
_LIMIT_CAP = 20


def _tokenize(text: str) -> list[str]:
    """Lowercase + regex word-split + stopword strip.

    Deterministic: the same input always produces the same token list,
    which is what makes parallel adapter constructions (overflow
    boundary) and idempotent re-builds safe.
    """
    return [t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOPWORDS]


@dataclass(frozen=True)
class _IndexedChunk:
    """A passage chunk that has been placed in the BM25 corpus."""

    chunk: Chunk
    document_id: str


class BM25Adapter:
    """In-process BM25 over page-passage chunks.

    The adapter is the v1 capability behind the ``RetrievePort``
    protocol. Construction is the only expensive step (one
    tokenization pass + BM25 index build). After that,
    ``retrieve(query, document_id, limit)`` is a deterministic lexical
    lookup.
    """

    def __init__(
        self,
        extracted_tree: dict[str, tuple[ExtractedPage, ...]],
    ) -> None:
        # Flatten the tree into a parallel list of indexed chunks and a
        # parallel list of tokenized passage texts. The two lists must
        # stay in lock-step — index ``i`` in ``_indexed`` corresponds
        # to the document and BM25 score vector position ``i``.
        self._indexed: list[_IndexedChunk] = []
        tokenized_corpus: list[list[str]] = []
        for document_id, pages in extracted_tree.items():
            for page in pages:
                for passage in page.passages:
                    self._indexed.append(
                        _IndexedChunk(
                            chunk=Chunk(
                                text=passage.text,
                                source_ref=page.locator,
                                score=0.0,
                            ),
                            document_id=document_id,
                        )
                    )
                    tokenized_corpus.append(_tokenize(passage.text))

        # Build the index once. An empty corpus is valid: the loader
        # may legitimately return ``{}`` (e.g. empty manifest before
        # any PDF has been ingested). ``BM25Okapi`` itself crashes on
        # an empty corpus (ZeroDivisionError on ``avgdl``), so we
        # gate construction on corpus size and let ``retrieve()``
        # short-circuit to the empty contract when there is nothing
        # to score.
        self._corpus_size: int = len(self._indexed)
        self._bm25: BM25Okapi | None = (
            BM25Okapi(tokenized_corpus) if self._corpus_size > 0 else None
        )

    # ----- helpers -----

    @staticmethod
    def _resolve_limit(limit: int) -> int:
        """Apply ADR 0009's limit rules: default 8, cap 20.

        * ``limit <= 0`` → 8 (treat as "use the default").
        * ``limit > 20`` → 20 (the hard cap).
        * otherwise → ``limit`` unchanged.
        """
        if limit <= 0:
            return _DEFAULT_LIMIT
        if limit > _LIMIT_CAP:
            return _LIMIT_CAP
        return limit

    def _document_ids(self) -> frozenset[str]:
        """All document_ids in the loaded corpus.

        Cached implicitly via the set — ``__init__`` walks the tree
        once, so this is just the keys of the input. We expose it via
        a method so tests can assert filter membership without poking
        private state.
        """
        return frozenset(ic.document_id for ic in self._indexed)

    # ----- RetrievePort -----

    def retrieve(
        self,
        query: str,
        document_id: str | None = None,
        limit: int = 8,
    ) -> dict[str, list[Chunk]]:
        """Return ``{"chunks": [...]}`` ranked descending by BM25 score.

        Parameters
        ----------
        query
            Lexical query. Empty / whitespace-only queries return
            ``{"chunks": []}`` without touching the index.
        document_id
            Optional corpus filter. Unknown document_id returns
            ``{"chunks": []}`` without raising.
        limit
            Default 8, hard cap 20 (see ``_resolve_limit``).

        Returns
        -------
        dict
            ``{"chunks": [Chunk, ...]}`` in descending score order.
        """
        # Empty / whitespace-only query: short-circuit before tokenization
        # or BM25 lookup. The model can pass ``""`` or whitespace during
        # abuse; we must NOT treat that as an error.
        if not query or not query.strip():
            return {"chunks": []}

        # Empty corpus: nothing to score against. Return the empty
        # contract without touching the (non-existent) BM25 index.
        if self._bm25 is None:
            return {"chunks": []}

        # Unknown document_id: the call is well-formed, but the corpus
        # has no such document. Return the empty contract without
        # raising — the loader's typed error is reserved for missing /
        # malformed artifacts.
        if document_id is not None and document_id not in self._document_ids():
            return {"chunks": []}

        effective_limit = self._resolve_limit(limit)

        # Tokenize the query and score the corpus. ``BM25Okapi.get_scores``
        # returns a numpy array with one score per indexed chunk, in the
        # same order as the corpus we passed at construction time.
        query_tokens = _tokenize(query)
        raw_scores = self._bm25.get_scores(query_tokens)

        # Enumerate (index, score) pairs, drop the document_id filter
        # case, and sort descending. ``rank_bm25`` returns 0-or-negative
        # scores for non-matches; that ordering is preserved naturally
        # because we sort high-to-low.
        scored: list[tuple[int, float]] = []
        for idx, indexed in enumerate(self._indexed):
            if (
                document_id is not None
                and indexed.document_id != document_id
            ):
                continue
            scored.append((idx, float(raw_scores[idx])))

        scored.sort(key=lambda pair: pair[1], reverse=True)

        chunks: list[Chunk] = []
        for idx, score in scored[:effective_limit]:
            base = self._indexed[idx].chunk
            chunks.append(
                Chunk(
                    text=base.text,
                    source_ref=base.source_ref,
                    score=score,
                )
            )
        return {"chunks": chunks}


__all__ = ["BM25Adapter"]