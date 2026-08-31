"""A tiny in-code eval set for the default test lane (T22).

The dataset-backed run (:mod:`fin.eval.ablation.dataset_set`) reads the
234-sample corpus and is opt-in. The structural half of T22's
acceptance — 10 configurations, every metric present, contributions
complete, no bare percentage — does not need a big corpus, only *some*
run to assert over. This module is that run: four questions, four short
passages, zero file reads, zero processes, zero network.

Arm attribution is scripted per query class, mirroring the rule the
dataset-backed set applies to the real annotations, so the two sets
produce comparable shapes.
"""

from __future__ import annotations

from fin.corpus.extracted import Locator
from fin.eval.ablation.metrics import EvalQuery
from fin.retrieve.fake_capability_port import CapabilityAwareFakePort, CapabilityHit
from fin.retrieve.port import Chunk


#: Which capability arms can produce the gold passage of each query
#: class. Six of the eight keys produce hits; ``rrf`` / ``rerank`` only
#: reorder, and ``cjk_segmentation`` produces nothing on an
#: English-language 10-Q corpus — a real 0, not a hidden gap.
ARMS_BY_QUERY_CLASS: dict[str, frozenset[str]] = {
    "factoid": frozenset({"keyword_bm25", "vector", "field_boost"}),
    "narrative": frozenset({"vector", "graph_ppr"}),
    "phrase": frozenset({"phrase", "keyword_bm25"}),
    "cross_period": frozenset({"vector", "keyword_bm25", "graph_ppr"}),
}

DISTRACTOR_ARMS: tuple[frozenset[str], ...] = (
    frozenset({"vector"}),
    frozenset({"keyword_bm25"}),
)

_VERSION_A = "a" * 64
_VERSION_B = "b" * 64

_SCRIPT: tuple[dict, ...] = (
    {
        "query_id": "s-factoid-001",
        "query_class": "factoid",
        "question": "total net sales for the quarter",
        "document_id": "AAPL-2022Q3",
        "version": _VERSION_A,
        "unit": 4,
        "text": "Total net sales 82,959 81,434 304,182 282,457 for the periods presented.",
        "answer_span": "Total net sales 82,959 81,434 304,182 282,457",
    },
    {
        "query_id": "s-narr-001",
        "query_class": "narrative",
        "question": "why the effective tax rate moved",
        "document_id": "AAPL-2022Q3",
        "version": _VERSION_A,
        "unit": 22,
        "text": (
            "The effective tax rate was higher because of a higher effective tax "
            "rate on foreign earnings during the period."
        ),
        "answer_span": "higher because of a higher effective tax rate on foreign earnings",
    },
    {
        "query_id": "s-phrase-001",
        "query_class": "phrase",
        "question": "forward-looking statements safe harbour wording",
        "document_id": "MSFT-2022Q3",
        "version": _VERSION_B,
        "unit": 30,
        "text": (
            "Statements are made within the meaning of the Private Securities "
            "Litigation Reform Act of 1995."
        ),
        "answer_span": "within the meaning of the Private Securities Litigation Reform Act of 1995",
    },
    {
        "query_id": "s-cross-001",
        "query_class": "cross_period",
        "question": "how revenue compares with the prior-year quarter",
        "document_id": "MSFT-2022Q3",
        "version": _VERSION_B,
        "unit": 3,
        "text": (
            "Total revenue 50,122 45,317 compared with the same quarter of the "
            "prior fiscal year."
        ),
        "answer_span": "Total revenue 50,122 45,317 compared with the same quarter",
    },
)


def scripted_eval_set() -> tuple[
    tuple[EvalQuery, ...], CapabilityAwareFakePort, set[str]
]:
    """Return ``(queries, port, known_document_ids)`` for the default lane.

    Each query's scripted result holds two distractors ahead of the gold
    passage, so fusion (``rrf``, which reorders by score) has something
    real to improve rather than a pre-sorted list.
    """
    queries: list[EvalQuery] = []
    hits_by_query: dict[str, list[CapabilityHit]] = {}
    known_document_ids = {entry["document_id"] for entry in _SCRIPT}

    for index, entry in enumerate(_SCRIPT):
        gold_locator = Locator(
            document_id=entry["document_id"],
            version=entry["version"],
            page=entry["unit"],
        )
        distractors = [
            _SCRIPT[(index + offset) % len(_SCRIPT)] for offset in (1, 2)
        ]
        hits = [
            CapabilityHit(
                chunk=Chunk(
                    text=distractor["text"],
                    source_ref=Locator(
                        document_id=distractor["document_id"],
                        version=distractor["version"],
                        # A neighbouring unit of the same document: a
                        # plausible near-miss, not the gold unit.
                        page=distractor["unit"] + 100,
                    ),
                    score=0.4 - 0.1 * offset_index,
                ),
                produced_by=DISTRACTOR_ARMS[offset_index],
            )
            for offset_index, distractor in enumerate(distractors)
        ]
        hits.append(
            CapabilityHit(
                chunk=Chunk(
                    text=entry["text"],
                    source_ref=gold_locator,
                    score=0.9,
                ),
                produced_by=ARMS_BY_QUERY_CLASS[entry["query_class"]],
            )
        )
        hits_by_query[entry["question"]] = hits
        queries.append(
            EvalQuery(
                query_id=entry["query_id"],
                question=entry["question"],
                expected_document_id=entry["document_id"],
                expected_version=entry["version"],
                expected_unit=entry["unit"],
                citable_answer=entry["answer_span"],
            )
        )

    return tuple(queries), CapabilityAwareFakePort(hits_by_query), known_document_ids


SCRIPTED_METADATA: dict[str, object] = {
    "dataset_id": "fin-ablation-scripted-v1",
    "backend": {
        "kind": "contract_fake",
        "module": "fin.retrieve.fake_capability_port",
        "note": (
            "in-code scripted passages; absolute values describe the script, "
            "not a retrieval service"
        ),
    },
    "caveats": [
        "scripted set: 4 questions, below the §B1 20–40 band, structural only",
    ],
}


__all__ = ["ARMS_BY_QUERY_CLASS", "SCRIPTED_METADATA", "scripted_eval_set"]
