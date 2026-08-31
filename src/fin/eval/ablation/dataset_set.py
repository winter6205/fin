"""The dataset-backed eval subset: real questions, real frozen text (T22).

§B1「消融矩阵」asks for a **20–40 题的脚本化子集**. This module takes
that subset deterministically off the committed T10 dataset
(``data/corpus/dataset/manifest.json``, 108 annotated questions over the
frozen tier-1 filings) and scripts each question's result set out of the
real extracted tree — the passage text is the frozen unit text, and the
locator is the dataset's ``expected_locator``.

What is real here and what is not
---------------------------------
* **Real**: the questions, the gold locators, the passage text, the
  citation-gate verdicts (fin's own gate, run over these passages), the
  dataset's own known gaps.
* **Scripted**: which capability arm produces which hit. The retrieval
  service owns ranking (ADR 0030 / 0037) and 线 B (iknow-memory) is not
  wired yet, so the arms come from a documented per-class rule rather
  than a live retriever. The report says so in ``backend`` and
  ``caveats``: these absolutes describe the contract fake, not a
  service benchmark, and they will be re-run against the real service
  once it is reachable.

The subset is stratified by query class and ordered by ``query_id``, so
the same dataset always yields the same subset.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fin.corpus.dataset.build import default_paths
from fin.corpus.extracted import Locator, load_extracted_tree
from fin.eval.ablation.matrix import B1_QUERY_SUBSET_MAX, validate_query_subset_size
from fin.eval.ablation.metrics import CITATION_MIN_SPAN_LEN, EvalQuery
from fin.eval.ablation.scripted_set import ARMS_BY_QUERY_CLASS, DISTRACTOR_ARMS
from fin.retrieve.fake_capability_port import CapabilityAwareFakePort, CapabilityHit
from fin.retrieve.port import Chunk


#: How many distractor passages precede the gold passage in a scripted
#: result set. Two keeps P@5 meaningfully below 1.0 without swamping it.
DISTRACTORS_PER_QUERY = 2


class AblationEvalSetError(Exception):
    """Base for eval-set construction failures. ``code`` is stable."""

    code = "ablation_eval_set_error"


class EvalSetSourceMissingError(AblationEvalSetError):
    """The dataset manifest or the extracted tree is not there."""

    code = "ablation_eval_set_source_missing"


class EvalSetAnnotationError(AblationEvalSetError):
    """A dataset annotation does not line up with the frozen text."""

    code = "ablation_eval_set_annotation_invalid"


def citable_answer(unit_text: str, evidence_span: str) -> str:
    """Grow ``evidence_span`` inside ``unit_text`` to a citable length.

    The citation gate needs a >= 32-character contiguous span
    (ADR 0021). Some dataset annotations pin a shorter phrase, so the
    span is extended forwards (then backwards) *within the same frozen
    unit* — still one verbatim substring of the real text, never
    stitched from two places.
    """
    start = unit_text.find(evidence_span)
    if start < 0:
        raise EvalSetAnnotationError(
            f"evidence span is not a substring of its frozen unit: {evidence_span!r}"
        )
    end = start + len(evidence_span)
    if len(evidence_span) >= CITATION_MIN_SPAN_LEN:
        return evidence_span
    deficit = CITATION_MIN_SPAN_LEN - len(evidence_span)
    end = min(len(unit_text), end + deficit)
    if end - start < CITATION_MIN_SPAN_LEN:
        start = max(0, start - (CITATION_MIN_SPAN_LEN - (end - start)))
    span = unit_text[start:end]
    if len(span) < CITATION_MIN_SPAN_LEN:
        raise EvalSetAnnotationError(
            "frozen unit is shorter than the citation gate's minimum span: "
            f"{len(unit_text)} chars"
        )
    return span


def _stratified_subset(items: list[dict], size: int) -> list[dict]:
    """Round-robin over query classes, each class ordered by query_id."""
    by_class: dict[str, list[dict]] = {}
    for item in sorted(items, key=lambda q: str(q.get("query_id", ""))):
        by_class.setdefault(str(item.get("query_class", "")), []).append(item)
    picked: list[dict] = []
    cursor = 0
    class_names = sorted(by_class)
    while len(picked) < size:
        progressed = False
        for name in class_names:
            bucket = by_class[name]
            if cursor < len(bucket):
                picked.append(bucket[cursor])
                progressed = True
                if len(picked) == size:
                    break
        if not progressed:
            break
        cursor += 1
    return picked


def dataset_eval_set(
    *,
    size: int = B1_QUERY_SUBSET_MAX,
    corpus_dir: Path | None = None,
    dataset_dir: Path | None = None,
) -> tuple[tuple[EvalQuery, ...], CapabilityAwareFakePort, set[str], dict[str, Any]]:
    """Build ``(queries, port, known_document_ids, metadata)`` off the dataset."""
    default_corpus, extracted_dir, default_dataset = default_paths()
    corpus_root = Path(corpus_dir) if corpus_dir is not None else default_corpus
    dataset_root = Path(dataset_dir) if dataset_dir is not None else default_dataset
    manifest_path = dataset_root / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise EvalSetSourceMissingError(
            f"dataset manifest unreadable: {manifest_path}: {exc}"
        ) from exc

    tree = load_extracted_tree(corpus_root / "manifest.json", extracted_dir)
    text_by_unit: dict[tuple[str, int], str] = {
        (page.locator.document_id, page.locator.page): page.text
        for pages in tree.values()
        for page in pages
    }
    known_document_ids = {document_id for document_id in tree}

    items = manifest.get("queries", {}).get("items", [])
    if not isinstance(items, list) or not items:
        raise EvalSetSourceMissingError(
            f"dataset manifest carries no queries: {manifest_path}"
        )
    subset = _stratified_subset(items, size)
    validate_query_subset_size(len(subset))

    queries: list[EvalQuery] = []
    hits_by_query: dict[str, list[CapabilityHit]] = {}
    for index, item in enumerate(subset):
        locator_payload = item["expected_locator"]
        document_id = str(locator_payload["document_id"])
        unit = int(locator_payload["unit"])
        unit_text = text_by_unit.get((document_id, unit))
        if not unit_text:
            raise EvalSetAnnotationError(
                f"{item.get('query_id')}: {document_id} unit {unit} has no frozen text"
            )
        gold = Locator(
            document_id=document_id,
            version=str(locator_payload["version"]),
            page=unit,
        )
        question = str(item["question"])
        arms = ARMS_BY_QUERY_CLASS[str(item["query_class"])]
        hits: list[CapabilityHit] = []
        for offset in range(1, DISTRACTORS_PER_QUERY + 1):
            neighbour = subset[(index + offset) % len(subset)]
            neighbour_locator = neighbour["expected_locator"]
            neighbour_id = str(neighbour_locator["document_id"])
            neighbour_unit = int(neighbour_locator["unit"])
            neighbour_text = text_by_unit.get((neighbour_id, neighbour_unit))
            if not neighbour_text:
                raise EvalSetAnnotationError(
                    f"{neighbour.get('query_id')}: {neighbour_id} unit "
                    f"{neighbour_unit} has no frozen text"
                )
            hits.append(
                CapabilityHit(
                    chunk=Chunk(
                        text=neighbour_text,
                        source_ref=Locator(
                            document_id=neighbour_id,
                            version=str(neighbour_locator["version"]),
                            page=neighbour_unit,
                        ),
                        score=0.5 - 0.1 * offset,
                    ),
                    produced_by=DISTRACTOR_ARMS[(offset - 1) % len(DISTRACTOR_ARMS)],
                )
            )
        hits.append(
            CapabilityHit(
                chunk=Chunk(text=unit_text, source_ref=gold, score=0.9),
                produced_by=arms,
            )
        )
        # Two dataset questions may share wording only if their ids
        # differ; the fake keys results by question text, so a repeat
        # would silently overwrite a scripted result set.
        if question in hits_by_query:
            raise EvalSetAnnotationError(
                f"{item.get('query_id')}: duplicate question text in the subset"
            )
        hits_by_query[question] = hits
        queries.append(
            EvalQuery(
                query_id=str(item["query_id"]),
                question=question,
                expected_document_id=document_id,
                expected_version=str(locator_payload["version"]),
                expected_unit=unit,
                citable_answer=citable_answer(
                    unit_text, str(item["evidence_span"])
                ),
            )
        )

    metadata: dict[str, Any] = {
        "dataset_id": str(manifest.get("dataset_id", "unspecified")),
        "backend": {
            "kind": "contract_fake",
            "module": "fin.retrieve.fake_capability_port",
            "arm_attribution": {
                query_class: sorted(arms)
                for query_class, arms in ARMS_BY_QUERY_CLASS.items()
            },
            "note": (
                "the retrieval service owns ranking (ADR 0037) and 线 B is not "
                "wired yet; hits and arms are scripted from the dataset's real "
                "annotations, so these absolutes describe the contract fake, "
                "not a service benchmark"
            ),
        },
        "known_gaps": manifest.get("known_gaps", []),
        "caveats": [
            "capability arms are scripted per query class, not measured on a "
            "live retriever — re-run once 线 B (iknow-memory) is reachable",
            "cjk_segmentation produces no hit on this English-language corpus, "
            "so its contribution is a real 0 rather than a missing measurement",
        ],
    }
    return tuple(queries), CapabilityAwareFakePort(hits_by_query), known_document_ids, metadata


__all__ = [
    "DISTRACTORS_PER_QUERY",
    "AblationEvalSetError",
    "EvalSetAnnotationError",
    "EvalSetSourceMissingError",
    "citable_answer",
    "dataset_eval_set",
]
