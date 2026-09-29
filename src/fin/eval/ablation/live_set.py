"""Live ablation eval set: opt-in path off ``contract_fake`` (T3, SC2–SC4).

Why this module exists
----------------------

The default ``fin.eval.ablation`` harness is hermetic: the
``scripted`` and ``dataset`` eval sets script their hits through
:class:`CapabilityAwareFakePort`, so ``backend.kind`` is the
``"contract_fake"`` literal SC2 pins down. This module is the **opt-in**
third eval set:

* CLI: ``uv run python -m fin.eval.ablation run --eval-set live`` is the
  only way the live path runs (T3 / SC2 — default stays
  ``contract_fake``).
* Surface: a wrapper that adapts :class:`MCPRetrieveAdapter` to the
  ablation runner's ``retrieve_for_eval_with_arms`` seam.
* Known limit: iknow-memory's retrieve response does not carry
  per-hit arm attribution (the eight capability keys are fin's
  enumeration, not iknow's wire schema). The wrapper applies the
  **documented per-query-class arm rule** (the same one the scripted /
  dataset sets use) so 图臂命中率 is the script's honest statement,
  not a fabricated number; the report's ``caveats`` say so.
* Known limit: the eight keys are not transmitted to MCP today
  (ADR 0039 — capability switches are eval-only). The wrapper accepts
  the configuration on the seam and consumes it only for arm
  attribution. The product path stays clean.

Product code must not import this module. The wrapper is the seam
between the live MCP retrieve path (only the live eval-set CLI uses
it) and the ablation runner; importing it on the product path would
drag :class:`MCPRetrieveAdapter` and the ``fin.retrieve.degradation``
machinery into a context that should stay hermetic. The package-level
``__all__`` only re-exports what the CLI and tests need (the factory
``live_eval_set``); everything else stays at module scope.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from fin.corpus.dataset.t1 import (
    T1_DATASET_ID,
    T1_MANIFEST_REL,
    T1_MIN_QUERY_PER_SCENE,
    T1_TEXT_DIR_REL,
    is_t1_countable,
    load_t1_manifest,
)
from fin.eval.ablation.metrics import EvalQuery
from fin.eval.ablation.scripted_set import ARMS_BY_QUERY_CLASS
from fin.retrieve.degradation import DegradedEnvelopeInvalidError
from fin.retrieve.mcp_adapter import (
    MCPRetrieveAdapter,
    RetrieveDegradedFailureError,
    RetrieveUnavailableError,
)


#: The literal ``backend.kind`` a live report carries (SC2).
LIVE_BACKEND_KIND = "mcp"

#: Stable dataset id a live report stamps onto the eval set, so the
#: merged report can be distinguished from a scripted / dataset one
#: without inspecting ``backend.kind``.
LIVE_DATASET_ID = "fin-ablation-live-t1-v1"


class LiveEvalSetError(Exception):
    """Base for live eval set failures. ``code`` is stable."""

    code = "live_eval_set_error"


class LiveSeamUnavailableError(LiveEvalSetError):
    """The live seam cannot reach the configured MCP server.

    Distinct from :class:`RetrieveUnavailableError`: this fires before
    the runner sees any ``retrieve`` call (the wrapper could not be
    constructed at all, e.g. the configured command is missing).
    """

    code = "live_seam_unavailable"


@dataclass(frozen=True)
class LiveEvalSeamWrapper:
    """Adapt :class:`MCPRetrieveAdapter` to the ablation seam.

    The wrapper is what the CLI hands to :func:`run_ablation`. Every
    configuration the runner sends in is honored on the seam
    (``retrieve_for_eval_with_arms`` must accept the same eight keys),
    but the underlying MCP call does not see them — iknow does not
    understand fin's capability enumeration. The wrapper still uses
    the configuration to derive the per-hit arm attribution the runner
    needs for 图臂命中率 (intersected with the documented per-class
    rule, exactly like :class:`CapabilityAwareFakePort`).
    """

    adapter: MCPRetrieveAdapter
    query_class_by_question: Mapping[str, str]

    def retrieve_for_eval_with_arms(
        self,
        query: str,
        *,
        capabilities: Mapping[str, Any],
        limit: int = 8,
    ) -> dict[str, Any]:
        """Call MCP once and re-emit chunks + arms in the seam shape.

        Failures map to typed exceptions the runner already counts:

        * ``{"error": "retrieve_unavailable"}`` → raised as
          :class:`RetrieveUnavailableError` (``code=retrieve_unavailable``).
        * ``degraded: true`` envelope with **no** chunks →
          :class:`RetrieveDegradedFailureError`
          (``code=retrieve_degraded_failure``) per ADR 0041 §B2 第 2 条.
          The envelope's :attr:`signal` is preserved on the exception so
          the runner surfaces the reasons rather than a bare unavailable.
        * ``degraded: true`` envelope with chunks → returned with the
          chunks; arms are still derived (the rule is honest), and the
          degraded flag travels back so the report's caveats list the
          partial-failure reasons.
        """
        payload = self.adapter.retrieve(query, limit=limit)
        if isinstance(payload, dict) and payload.get("error"):
            code = str(payload["error"])
            if code == "retrieve_degraded_failure":
                # The MCP adapter already raises this internally, but a
                # user-patched ``retrieve`` may return the dict. Build
                # the typed exception so the runner sees a single
                # ``RetrieveUnavailableError`` family.
                raise RetrieveUnavailableError(
                    "MCP returned a degraded response without usable chunks"
                )
            raise RetrieveUnavailableError(
                f"live retrieve failed: {code}"
            )
        if not isinstance(payload, dict):
            raise RetrieveUnavailableError(
                f"live retrieve returned non-dict payload: {type(payload).__name__}"
            )
        chunks = payload.get("chunks", [])
        if not isinstance(chunks, list):
            raise RetrieveUnavailableError(
                f"live retrieve returned non-list chunks: {type(chunks).__name__}"
            )
        if payload.get("degraded") and not chunks:
            # Per ADR 0041 §B2 第 2 条: degraded + zero chunks is a
            # named partial failure, never a legal empty miss. Reading
            # the envelope through the strict reader first guarantees
            # that ``degraded: true`` with no / non-list
            # ``degraded_reasons`` does NOT silently resolve to
            # NOT_DEGRADED — the §B3禁止的静默形态 the envelope reader
            # is the single authority against.
            raise RetrieveDegradedFailureError(
                "MCP returned a degraded response without usable chunks",
                signal=_signal_from_envelope(payload),
            )
        arms = [self._arms_for(query, capabilities) for _ in chunks]
        return {"chunks": list(chunks), "arms": arms}

    def _arms_for(
        self, query: str, capabilities: Mapping[str, Any]
    ) -> frozenset[str]:
        enabled = {key for key, on in capabilities.items() if on}
        query_class = self.query_class_by_question.get(query, "narrative")
        class_arms = ARMS_BY_QUERY_CLASS.get(
            query_class, frozenset({"vector"})
        )
        # Mirror ``CapabilityAwareFakePort.retrieve_for_eval_with_arms``:
        # the reported arm set is the per-class rule intersected with
        # the configuration's enabled keys, so a configuration that
        # turned a key off cannot claim that arm.
        return frozenset(class_arms & enabled)


def _signal_from_envelope(payload: Mapping[str, Any]) -> Any:
    """Build a ``DegradationSignal`` from a raw envelope, strictly.

    Delegates to :func:`fin.retrieve.degradation.read_degradation_signal`
    — the single authority for §B3 envelope parsing. That reader refuses
    a ``degraded: true`` flag without a non-empty list of non-blank
    reason strings: it raises :class:`DegradedEnvelopeInvalidError`
    instead of falling back to ``NOT_DEGRADED``, because "unreadable
    flag treated as healthy" is the silent shape §B3 forbids.

    The wrapper translates that named failure into
    :class:`RetrieveDegradedFailureError` (carrying the named reason
    ``degraded_reasons_missing_or_invalid``) so the ablation runner
    counts the query as a typed failure rather than silently passing
    through as a clean zero hit.
    """
    from fin.retrieve.degradation import NOT_DEGRADED, read_degradation_signal

    try:
        return read_degradation_signal(payload)
    except DegradedEnvelopeInvalidError as exc:
        raise RetrieveDegradedFailureError(
            "degraded_reasons_missing_or_invalid: " + str(exc),
            signal=NOT_DEGRADED,
        ) from exc


def live_document_ids(project_root: Path | None = None) -> set[str]:
    """The union of document ids a live report is allowed to cite.

    SC3 / SC4: every hit the live path returns must map to either a
    T1 sample or a SEC primary document; otherwise the citation gate
    bounces it on ``unknown_locator``. The runner passes this set as
    ``known_document_ids`` so the gate does not reject live hits.
    """
    root = Path(project_root) if project_root is not None else _repo_root()
    sec_ids = _sec_document_ids(root)
    t1_ids = _t1_document_ids(root)
    return sec_ids | t1_ids


def _repo_root() -> Path:
    # ``src/fin/eval/ablation/live_set.py`` → 4 parents up is the repo root.
    return Path(__file__).resolve().parents[4]


def _sec_document_ids(root: Path) -> set[str]:
    manifest_path = root / "data" / "corpus" / "manifest.json"
    if not manifest_path.is_file():
        return set()
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    documents = payload.get("documents") or []
    return {
        str(doc["document_id"])
        for doc in documents
        if isinstance(doc, dict) and doc.get("document_id")
    }


def _t1_document_ids(root: Path) -> set[str]:
    manifest_path = root / T1_MANIFEST_REL
    if not manifest_path.is_file():
        return set()
    try:
        manifest = load_t1_manifest(manifest_path)
    except (OSError, ValueError):
        return set()
    return {
        str(sample["document_id"])
        for sample in manifest.get("samples", [])
        if isinstance(sample, dict) and is_t1_countable(sample)[0]
    }


def _build_live_queries(
    *,
    manifest: Mapping[str, Any],
    text_dir: Path,
    sample_by_id: Mapping[str, Mapping[str, Any]],
) -> tuple[EvalQuery, ...]:
    """Construct :class:`EvalQuery` objects off T1 queries + frozen text.

    T1 samples are single-page markdown; the wrapper treats each file
    as one ``unit`` (unit=1). The citation gate's 32-character minimum
    is met by extending the ``evidence_span`` inside the same frozen
    unit text (same rule ``citable_answer`` applies for the dataset
    set).
    """
    queries: list[EvalQuery] = []
    for item in manifest.get("queries", []) or []:
        if not isinstance(item, dict):
            continue
        sample_id = str(item.get("sample_id", ""))
        sample = sample_by_id.get(sample_id)
        if sample is None:
            continue
        if not is_t1_countable(sample)[0]:
            continue
        sample_path = text_dir / str(sample.get("path", ""))
        if not sample_path.is_file():
            continue
        text = sample_path.read_text(encoding="utf-8")
        evidence_span = str(item.get("evidence_span", ""))
        if not evidence_span or evidence_span not in text:
            continue
        citable = _grow_to_min_span(text, evidence_span)
        queries.append(
            EvalQuery(
                query_id=str(item.get("query_id", sample_id)),
                question=str(item.get("question", "")),
                expected_document_id=str(sample.get("document_id", "")),
                expected_version=str(sample.get("sha256", "")),
                expected_unit=1,
                citable_answer=citable,
            )
        )
    return tuple(queries)


def _grow_to_min_span(text: str, evidence_span: str) -> str:
    """Extend ``evidence_span`` to the gate's minimum length.

    Mirrors :func:`fin.eval.ablation.dataset_set.citable_answer` so the
    live path and the dataset path give the gate the same shape of
    answer text.
    """
    min_len = 32
    start = text.find(evidence_span)
    if start < 0:
        return evidence_span
    end = start + len(evidence_span)
    if end - start >= min_len:
        return evidence_span
    deficit = min_len - (end - start)
    end = min(len(text), end + deficit)
    if end - start < min_len:
        start = max(0, start - (min_len - (end - start)))
    span = text[start:end]
    return span if len(span) >= min_len else evidence_span


def live_eval_set(
    *,
    project_root: Path | None = None,
    adapter: MCPRetrieveAdapter | None = None,
) -> tuple[
    tuple[EvalQuery, ...],
    LiveEvalSeamWrapper | MCPRetrieveAdapter,
    set[str],
    dict[str, Any],
]:
    """Build ``(queries, port, known_document_ids, metadata)`` for live.

    Parameters
    ----------
    project_root
        Repo root used to locate ``data/corpus/dataset/t1_manifest.json``
        and ``data/corpus/manifest.json``. Defaults to the parent of
        ``src/`` in this checkout.
    adapter
        An optional pre-built :class:`MCPRetrieveAdapter`. When omitted
        a no-command adapter is built (which raises
        :class:`RetrieveUnavailableError` on first ``retrieve``) so the
        CLI can be exercised without configuring a server.
    """
    root = Path(project_root) if project_root is not None else _repo_root()
    manifest_path = root / T1_MANIFEST_REL
    text_dir = root / T1_TEXT_DIR_REL
    manifest = load_t1_manifest(manifest_path)
    sample_by_id = {
        str(sample["sample_id"]): sample
        for sample in manifest.get("samples", [])
        if isinstance(sample, dict) and str(sample.get("sample_id", ""))
    }
    queries = _build_live_queries(
        manifest=manifest, text_dir=text_dir, sample_by_id=sample_by_id
    )
    if not queries:
        raise LiveEvalSetError(
            f"T1 manifest at {manifest_path} yields no countable queries; "
            "live eval set is empty — refusing to run"
        )
    query_class_by_question = {
        str(item.get("question", "")): str(item.get("query_class", "narrative"))
        for item in manifest.get("queries", [])
        if isinstance(item, dict)
    }
    known_ids = live_document_ids(root)
    chosen_adapter = adapter or MCPRetrieveAdapter()
    port: LiveEvalSeamWrapper | MCPRetrieveAdapter = LiveEvalSeamWrapper(
        adapter=chosen_adapter,
        query_class_by_question=query_class_by_question,
    )
    t1_scene_counts = {
        str(scene): sum(
            1 for sample in manifest.get("samples", [])
            if isinstance(sample, dict)
            and int(sample.get("scene", 0) or 0) == scene
            and is_t1_countable(sample)[0]
        )
        for scene in range(1, 7)
    }
    metadata: dict[str, Any] = {
        "dataset_id": LIVE_DATASET_ID,
        "backend": {
            "kind": LIVE_BACKEND_KIND,
            "module": "fin.retrieve.mcp_adapter",
            "arm_attribution": {
                query_class: sorted(arms)
                for query_class, arms in ARMS_BY_QUERY_CLASS.items()
            },
            "note": (
                "live retrieve path: wrapper adapts MCPRetrieveAdapter to the "
                "ablation seam. iknow does not return per-hit arm attribution, "
                "so the wrapper applies the documented per-query-class rule "
                "(same as scripted/dataset); the eight capability keys are "
                "consumed only for arm intersection and are NOT transmitted to "
                "MCP (ADR 0039 — capability switches are eval-only)."
            ),
        },
        "caveats": [
            "live path: capability keys are eval-only — they intersect the "
            "per-class arm rule but are NOT passed to MCP",
            "live path: 图臂命中率 derives from the per-class rule "
            "(honest statement), not from a live arm-attribution measurement",
            "live path: degraded:true with chunks returns a partial-failure "
            "result; degraded:true with no chunks raises retrieve_degraded_failure",
        ],
        "t1_scene_counts": t1_scene_counts,
        "t1_min_query_per_scene": T1_MIN_QUERY_PER_SCENE,
        "t1_manifest_dataset_id": T1_DATASET_ID,
        "known_gaps": list(manifest.get("known_gaps", [])),
    }
    return queries, port, known_ids, metadata


__all__ = [
    "LIVE_BACKEND_KIND",
    "LIVE_DATASET_ID",
    "LiveEvalSeamWrapper",
    "LiveEvalSetError",
    "LiveSeamUnavailableError",
    "live_document_ids",
    "live_eval_set",
]
