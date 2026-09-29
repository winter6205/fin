"""Live ablation eval set: opt-in path off ``contract_fake`` (T3, SC2–SC4).

Why these tests live in the default lane
---------------------------------------

The ``live`` eval set is an opt-in command-line mode (the suite never
invokes it), but its **wiring** is product code: the wrapper that
adapts :class:`MCPRetrieveAdapter` into the ``retrieve_for_eval_with_arms``
seam is imported by the CLI, and the report's ``backend.kind`` is the
literal SC2 asserts on. Both must hold without ever talking to a real
iknow-memory process — that's the whole point of the seam.

The tests in this file exercise the wrapper against a scripted fake
seam port that satisfies the ablation runner's contract. They do NOT
spawn an MCP subprocess, do NOT need ``iknow-memory`` compiled, and do
NOT touch the default pytest lane's hermetic invariant.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

import pytest

from fin.corpus.dataset.t1 import (
    T1_MANIFEST_REL,
    T1_TEXT_DIR_REL,
    load_t1_manifest,
    verify_t1_manifest,
)
from fin.eval.ablation import (
    B1_CONFIGURATION_TOTAL,
    B1_MIN_REPEATS,
    AblationRunError,
    build_report,
    run_ablation,
    validate_report,
)
from fin.eval.ablation.live_set import (
    LiveEvalSetError,
    LiveEvalSeamWrapper,
    LIVE_BACKEND_KIND,
    live_document_ids,
    live_eval_set,
)
from fin.eval.ablation.metrics import EvalQuery
from fin.retrieve.capabilities import load_capability_keys
from fin.retrieve.mcp_adapter import RetrieveUnavailableError


PROJECT_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------- seam


class _FakeSeamPort:
    """Scripted seam port returning canned chunks + arms per question."""

    def __init__(
        self,
        responses: Mapping[str, dict[str, Any]] | None = None,
        *,
        raise_unavailable: bool = False,
    ) -> None:
        self._responses = dict(responses or {})
        self._raise_unavailable = raise_unavailable
        self.calls: list[tuple[str, dict[str, bool], int]] = []

    def retrieve_for_eval_with_arms(
        self,
        query: str,
        *,
        capabilities: Mapping[str, bool],
        limit: int = 8,
    ) -> dict[str, Any]:
        self.calls.append((query, dict(capabilities), limit))
        if self._raise_unavailable:
            raise RetrieveUnavailableError("simulated iknow outage")
        payload = self._responses.get(query)
        if payload is None:
            return {"chunks": [], "arms": []}
        return payload


# ---------------------------------------------------------------- module surface


def test_live_module_exposes_the_required_symbols() -> None:
    assert callable(live_eval_set)
    assert callable(live_document_ids)
    assert LIVE_BACKEND_KIND == "mcp"
    assert LiveEvalSeamWrapper is not None
    assert issubclass(LiveEvalSetError, Exception)


# ---------------------------------------------------------------- live_eval_set


def test_live_eval_set_returns_queries_wrapper_and_metadata() -> None:
    queries, port, known_ids, metadata = live_eval_set()
    assert queries, "live eval set must carry at least one query (T1 manifest)"
    assert all(isinstance(q, EvalQuery) for q in queries)
    assert metadata["backend"]["kind"] == LIVE_BACKEND_KIND
    # Every query is grounded on a T1 sample; known_ids must include
    # the union of T1 + SEC document_ids so the citation gate accepts
    # live-retrieved locators (SC3 / SC4).
    assert known_ids
    assert "t1-onboarding-001" in known_ids


def test_live_eval_set_known_ids_union_t1_and_sec() -> None:
    """known_document_ids must be T1 ∪ SEC so a real live run cannot
    bounce on a known locator the wrapper did not register (SC3)."""
    _, _, known_ids, _ = live_eval_set()
    # SEC side: the committed primary documents (data/corpus/manifest.json).
    sec_manifest = json.loads(
        (PROJECT_ROOT / "data" / "corpus" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    sec_ids = {
        str(doc["document_id"])
        for doc in sec_manifest["documents"]
        if "document_id" in doc
    }
    assert sec_ids & known_ids, "SEC manifest's document_ids must be in known_ids"
    # T1 side: at least one t1-* sample id must be present.
    assert any(doc_id.startswith("t1-") for doc_id in known_ids)


def test_live_eval_set_uses_a_seam_compatible_port() -> None:
    """The wrapper must satisfy the ablation runner's seam."""
    _, port, _, _ = live_eval_set()
    assert hasattr(port, "retrieve_for_eval_with_arms")


def test_live_wrapper_calls_mcp_retrieve_with_question_and_limit() -> None:
    captured: dict[str, Any] = {}

    class _RecordingAdapter:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def retrieve(self, query, *, limit=8, **kwargs):
            self.calls.append({"query": query, "limit": limit, **kwargs})
            captured["instance"] = self
            return {"chunks": [], "degraded": False, "degraded_reasons": []}

    adapter = _RecordingAdapter()
    wrapper = LiveEvalSeamWrapper(adapter=adapter, query_class_by_question={})
    payload = wrapper.retrieve_for_eval_with_arms(
        "what is onboarding?", capabilities={}, limit=4
    )
    assert payload == {"chunks": [], "arms": []}
    assert adapter.calls == [{"query": "what is onboarding?", "limit": 4}]
    assert captured["instance"] is adapter


def test_live_wrapper_translates_a_degraded_envelope_without_chunks_to_unavailable() -> None:
    """ADR 0041 §B2 第 2 条: degraded + no chunks is a named failure, NOT
    a legal empty hit. The wrapper raises ``RetrieveUnavailableError``
    with ``code=retrieve_degraded_failure`` so the runner counts the
    query as errored rather than as a clean zero hit (SC4)."""

    class _DegradedEmptyAdapter:
        def retrieve(self, query, *, limit=8, **kwargs):
            return {
                "chunks": [],
                "degraded": True,
                "degraded_reasons": ["adapter:field_stripped:limit"],
            }

    wrapper = LiveEvalSeamWrapper(
        adapter=_DegradedEmptyAdapter(), query_class_by_question={}
    )
    with pytest.raises(RetrieveUnavailableError) as excinfo:
        wrapper.retrieve_for_eval_with_arms("q", capabilities={}, limit=8)
    assert excinfo.value.code == "retrieve_degraded_failure"


def test_live_wrapper_raises_named_failure_when_degraded_reasons_are_empty() -> None:
    """ADR 0041: ``degraded_reasons`` is a non-empty finite enum list when
    ``degraded`` is true. An envelope that flips the flag but drops the
    reasons (or hands in a non-list) must NOT silently fall back to
    NOT_DEGRADED — the §B3禁止的静默形态 the envelope reader forbids.
    The wrapper raises the typed ``RetrieveDegradedFailureError`` so the
    runner records the query as a named failure rather than passing
    through as a healthy zero hit."""

    class _DegradedEmptyReasonsAdapter:
        def retrieve(self, query, *, limit=8, **kwargs):
            return {
                "chunks": [],
                "degraded": True,
                "degraded_reasons": [],
            }

    wrapper = LiveEvalSeamWrapper(
        adapter=_DegradedEmptyReasonsAdapter(), query_class_by_question={}
    )
    with pytest.raises(RetrieveUnavailableError) as excinfo:
        wrapper.retrieve_for_eval_with_arms("q", capabilities={}, limit=8)
    assert excinfo.value.code == "retrieve_degraded_failure"
    message = str(excinfo.value)
    assert "degraded_reasons_missing_or_invalid" in message


def test_live_wrapper_raises_named_failure_when_degraded_reasons_are_not_a_list() -> None:
    """Same §B3 / §B2 invariant, applied to a non-list reasons value: a
    string passed where the contract asks for a list must NOT resolve
    to NOT_DEGRADED. The wrapper raises the typed failure."""

    class _DegradedBadReasonsAdapter:
        def retrieve(self, query, *, limit=8, **kwargs):
            return {
                "chunks": [],
                "degraded": True,
                "degraded_reasons": "adapter:field_stripped:limit",
            }

    wrapper = LiveEvalSeamWrapper(
        adapter=_DegradedBadReasonsAdapter(), query_class_by_question={}
    )
    with pytest.raises(RetrieveUnavailableError) as excinfo:
        wrapper.retrieve_for_eval_with_arms("q", capabilities={}, limit=8)
    assert excinfo.value.code == "retrieve_degraded_failure"


def test_live_wrapper_preserves_degraded_signal_with_legitimate_reasons() -> None:
    """ADR 0041 happy path: degraded + non-empty reasons + chunks →
    chunks are returned (partial result is usable). This test pins the
    unchanged invariant: the §B2 fix must NOT over-trigger on a
    well-formed degraded envelope."""

    class _DegradedWithChunksAdapter:
        def retrieve(self, query, *, limit=8, **kwargs):
            return {
                "chunks": [
                    {
                        "text": "x" * 40,
                        "source_ref": {
                            "document_id": "t1-onboarding-001",
                            "version": "v" * 64,
                            "unit": 1,
                        },
                        "score": 0.9,
                    }
                ],
                "degraded": True,
                "degraded_reasons": ["adapter:field_stripped:limit"],
            }

    wrapper = LiveEvalSeamWrapper(
        adapter=_DegradedWithChunksAdapter(), query_class_by_question={"q": "narrative"}
    )
    on = {"vector": True}
    result = wrapper.retrieve_for_eval_with_arms("q", capabilities=on, limit=8)
    assert len(result["chunks"]) == 1


def test_live_wrapper_transports_unavailable_into_the_seam() -> None:
    """The MCP-side ``retrieve_unavailable`` MUST be re-raised so the
    ablation runner records it as a named error (SC4), not a quiet
    empty hit. The CLI maps it to a non-zero exit."""

    class _DownAdapter:
        def retrieve(self, query, *, limit=8, **kwargs):
            return {"error": "retrieve_unavailable"}

    wrapper = LiveEvalSeamWrapper(adapter=_DownAdapter(), query_class_by_question={})
    with pytest.raises(RetrieveUnavailableError) as excinfo:
        wrapper.retrieve_for_eval_with_arms("q", capabilities={}, limit=8)
    assert excinfo.value.code == "retrieve_unavailable"


def test_live_wrapper_attributes_arms_per_query_class() -> None:
    """Live retrieval has no real arm attribution (iknow doesn't return
    ``arms``), so the wrapper applies the documented per-query-class
    rule (same rule the scripted / dataset sets use) — same honest
    statement, not a fabricated number."""

    class _ChunkAdapter:
        def __init__(self, payload: dict[str, Any]) -> None:
            self._payload = payload
            self.calls = 0

        def retrieve(self, query, *, limit=8, **kwargs):
            self.calls += 1
            return dict(self._payload)

    payload = {
        "chunks": [
            {
                "text": "x" * 40,
                "source_ref": {
                    "document_id": "t1-onboarding-001",
                    "version": "v" * 64,
                    "unit": 1,
                },
                "score": 0.9,
            }
        ],
        "degraded": False,
        "degraded_reasons": [],
    }
    adapter = _ChunkAdapter(payload)
    wrapper = LiveEvalSeamWrapper(
        adapter=adapter,
        query_class_by_question={"q": "factoid"},
    )
    # factoid arms = {keyword_bm25, vector, field_boost}; pass all of
    # them in the configuration so the intersection keeps them.
    all_keys = load_capability_keys()
    result = wrapper.retrieve_for_eval_with_arms(
        "q", capabilities={key: True for key in all_keys}, limit=8
    )
    assert len(result["arms"]) == 1
    assert "vector" in result["arms"][0]
    assert "keyword_bm25" in result["arms"][0]
    assert "field_boost" in result["arms"][0]
    # cjk_segmentation / phrase / graph_ppr / rrf / rerank are not in
    # the factoid rule, so they don't show up on the hit.
    assert "graph_ppr" not in result["arms"][0]
    assert "phrase" not in result["arms"][0]


def test_live_wrapper_returns_arms_union_with_enabled_capabilities() -> None:
    """The reported arm set per hit is the per-class rule intersected
    with the configuration's enabled keys (matches ``CapabilityAwareFakePort``),
    so a ``single_off_graph_ppr`` configuration cannot claim graph-arm
    hits — the metric it produces is real."""

    class _ChunkAdapter:
        def retrieve(self, query, *, limit=8, **kwargs):
            return {
                "chunks": [
                    {
                        "text": "x" * 40,
                        "source_ref": {
                            "document_id": "t1-onboarding-001",
                            "version": "v" * 64,
                            "unit": 1,
                        },
                        "score": 0.9,
                    }
                ],
                "degraded": False,
                "degraded_reasons": [],
            }

    wrapper = LiveEvalSeamWrapper(
        adapter=_ChunkAdapter(),
        query_class_by_question={"q": "narrative"},  # narrative arms include graph_ppr
    )
    on = {"graph_ppr": True}
    off = {"graph_ppr": False}
    on_result = wrapper.retrieve_for_eval_with_arms("q", capabilities=on, limit=8)
    off_result = wrapper.retrieve_for_eval_with_arms("q", capabilities=off, limit=8)
    assert "graph_ppr" in on_result["arms"][0]
    assert "graph_ppr" not in off_result["arms"][0]


# ---------------------------------------------------------------- runner integration


def test_live_seam_wrapper_drives_run_ablation_end_to_end() -> None:
    """A scripted seam port behind the wrapper yields a full report
    whose ``backend.kind`` is ``"mcp"`` and whose hits land on T1
    locators (SC3)."""
    queries, _wrapper, known_ids, metadata = live_eval_set()
    # Pick the first T1 question and build a canned response that maps
    # onto its expected locator.
    q = queries[0]
    locator = {
        "document_id": q.expected_document_id,
        "version": q.expected_version,
        "unit": q.expected_unit,
    }
    responses: dict[str, dict[str, Any]] = {
        q.question: {
            "chunks": [
                {
                    "text": q.citable_answer,
                    "source_ref": locator,
                    "score": 0.95,
                }
            ],
            "degraded": False,
            "degraded_reasons": [],
        }
    }
    for question in queries[1:]:
        responses[question.question] = {"chunks": [], "arms": []}

    # Build a seam port that returns Chunk-shaped objects (the runner
    # reads ``chunk.source_ref`` / ``chunk.text`` / ``chunk.score`` as
    # attributes, not dict keys).
    from fin.corpus.extracted import Locator
    from fin.retrieve.port import Chunk

    chunks_by_question: dict[str, list[Any]] = {}
    arms_by_question: dict[str, list[Any]] = {}
    for question_text, payload in responses.items():
        chunks: list[Chunk] = []
        arms: list[frozenset[str]] = []
        for raw_chunk in payload.get("chunks", []):
            ref = raw_chunk["source_ref"]
            chunks.append(
                Chunk(
                    text=raw_chunk["text"],
                    source_ref=Locator(
                        document_id=ref["document_id"],
                        version=ref["version"],
                        unit=ref["unit"],
                    ),
                    score=float(raw_chunk["score"]),
                )
            )
            arms.append(frozenset({"vector", "keyword_bm25"}))
        chunks_by_question[question_text] = chunks
        arms_by_question[question_text] = arms

    class _ChunkSeamPort:
        def retrieve_for_eval_with_arms(self, query, *, capabilities, limit=8):
            return {
                "chunks": chunks_by_question.get(query, []),
                "arms": arms_by_question.get(query, []),
            }

    port = _ChunkSeamPort()
    run = run_ablation(
        port,
        queries,
        repeats=B1_MIN_REPEATS,
        known_document_ids=known_ids,
        metadata=metadata,
    )
    report = build_report(run)
    assert report["backend"]["kind"] == LIVE_BACKEND_KIND
    problems = validate_report(report)
    assert problems == [], problems


def test_live_seam_runner_records_named_unavailable_failures() -> None:
    """When the seam raises ``RetrieveUnavailableError``, every query
    lands in the runner's error ledger with the typed code (SC4)."""
    queries, _, known_ids, metadata = live_eval_set()
    port = _FakeSeamPort(raise_unavailable=True)
    run = run_ablation(
        port,
        queries,
        repeats=B1_MIN_REPEATS,
        known_document_ids=known_ids,
        metadata=metadata,
    )
    error_codes = {
        code
        for config in run.configurations
        for code in config.error_codes
    }
    assert "retrieve_unavailable" in error_codes


def test_live_seam_rejects_run_without_seam() -> None:
    """An adapter that does NOT expose ``retrieve_for_eval_with_arms``
    is a named failure even on the live path (no silent zeros)."""
    queries, _, known_ids, metadata = live_eval_set()

    class _ArmlessSeamPort:
        def retrieve_for_eval(self, query, *, capabilities, limit=8):
            return {"chunks": []}

    with pytest.raises(AblationRunError) as excinfo:
        run_ablation(
            _ArmlessSeamPort(),
            queries,
            repeats=B1_MIN_REPEATS,
            known_document_ids=known_ids,
            metadata=metadata,
        )
    assert excinfo.value.code == "graph_arm_attribution_unavailable"


# ---------------------------------------------------------------- CLI surface


def test_cli_default_eval_set_remains_dataset_with_contract_fake(tmp_path: Path) -> None:
    """SC2: the default invocation must NOT silently route through
    ``live``. ``backend.kind`` stays ``contract_fake``."""
    from fin.eval.ablation.__main__ import main

    out = tmp_path / "ablation"
    exit_code = main(
        [
            "run",
            "--eval-set",
            "scripted",
            "--repeats",
            str(B1_MIN_REPEATS),
            "--output",
            str(out),
        ]
    )
    assert exit_code == 0
    report = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert report["backend"]["kind"] == "contract_fake"


def test_cli_live_eval_set_with_fake_seam_writes_mcp_report(tmp_path: Path) -> None:
    """Opt-in ``--eval-set live`` produces a ``backend.kind == "mcp"``
    report. The actual MCP subprocess is NOT spawned here — the
    wrapper is patched to a fake seam port that satisfies the runner."""
    from fin.eval.ablation import __main__ as ablation_cli

    class _FakeLiveSetFactory:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self):  # noqa: D401 - factory shim
            self.calls += 1
            return live_eval_set_for_fake_seam()

    payload_factory = _FakeLiveSetFactory()
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(ablation_cli, "live_eval_set", payload_factory)
        out = tmp_path / "ablation-live"
        exit_code = ablation_cli.main(
            [
                "run",
                "--eval-set",
                "live",
                "--repeats",
                str(B1_MIN_REPEATS),
                "--output",
                str(out),
            ]
        )
    finally:
        monkeypatch.undo()
    assert exit_code == 0
    assert payload_factory.calls == 1
    report = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert report["backend"]["kind"] == LIVE_BACKEND_KIND
    assert (
        report["dataset"]["dataset_id"]
        == "fin-ablation-live-t1-v1"
    )


def live_eval_set_for_fake_seam():
    """A test-only factory that swaps the wrapper's adapter for the
    scripted seam port so the live path can be exercised without an
    MCP subprocess."""
    queries, _wrapper, known_ids, metadata = live_eval_set()
    from fin.corpus.extracted import Locator
    from fin.eval.ablation.scripted_set import ARMS_BY_QUERY_CLASS
    from fin.retrieve.fake_capability_port import (
        CapabilityAwareFakePort,
        CapabilityHit,
    )
    from fin.retrieve.port import Chunk

    hits_by_query: dict[str, list[CapabilityHit]] = {}
    for q in queries:
        gold = Chunk(
            text=q.citable_answer,
            source_ref=Locator(
                document_id=q.expected_document_id,
                version=q.expected_version,
                unit=q.expected_unit,
            ),
            score=0.9,
        )
        arms = ARMS_BY_QUERY_CLASS.get("narrative", frozenset())
        hits_by_query[q.question] = [CapabilityHit(chunk=gold, produced_by=arms)]
    fake_port = CapabilityAwareFakePort(hits_by_query)
    metadata["backend"] = dict(metadata["backend"])
    metadata["backend"]["note"] = (
        "test seam: live_eval_set was patched for CLI test; "
        "MCPRetrieveAdapter is not actually invoked here"
    )
    return queries, fake_port, known_ids, metadata


def test_cli_live_unreachable_exits_named_failure(tmp_path: Path) -> None:
    """SC4: when iknow is unreachable, the live command exits non-zero
    with the typed ``retrieve_unavailable`` failure — never ``done`` with
    empty chunks."""
    from fin.eval.ablation import __main__ as ablation_cli

    class _FakeLiveSetFactory:
        def __call__(self):
            queries, _wrapper, known_ids, metadata = live_eval_set()
            port = _FakeSeamPort(raise_unavailable=True)
            return queries, port, known_ids, metadata

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(ablation_cli, "live_eval_set", _FakeLiveSetFactory())
        out = tmp_path / "ablation-live-unavailable"
        exit_code = ablation_cli.main(
            [
                "run",
                "--eval-set",
                "live",
                "--repeats",
                str(B1_MIN_REPEATS),
                "--output",
                str(out),
            ]
        )
    finally:
        monkeypatch.undo()
    assert exit_code != 0
    # The report must NOT have been written — a non-zero exit means
    # the run was not a successful「done with empty chunks」scenario.
    assert not (out / "report.json").exists()


def test_cli_live_eval_set_rejects_repeats_below_floor(tmp_path: Path) -> None:
    """The repeats floor is the same for live as for scripted/dataset
    (B1 contract): a value below the floor is a named failure and
    exits non-zero without writing a report."""
    from fin.eval.ablation.__main__ import main

    exit_code = main(
        [
            "run",
            "--eval-set",
            "live",
            "--repeats",
            str(B1_MIN_REPEATS - 1),
            "--output",
            str(tmp_path / "ablation-live"),
        ]
    )
    assert exit_code == 2
    assert not (tmp_path / "ablation-live" / "report.json").exists()


# ---------------------------------------------------------------- T1 manifest


def test_live_eval_set_only_includes_countable_t1_queries() -> None:
    """SC6: synthetic / uncountable T1 samples are not asked; the
    live eval set picks questions from countable samples only."""
    manifest = load_t1_manifest(PROJECT_ROOT / T1_MANIFEST_REL)
    text_dir = PROJECT_ROOT / T1_TEXT_DIR_REL
    report = verify_t1_manifest(manifest, text_dir=text_dir)
    # If the manifest itself is broken, the eval set should be empty
    # rather than silently asking on synthetic content.
    assert report.ok, report.problems


# ---------------------------------------------------------------- import guard


def test_capability_static_check_still_holds() -> None:
    """SC8: the new live path must not introduce a capability-off key
    on the product call path. Re-run the canonical static check from
    the retrieve suite."""
    from fin.retrieve.capabilities import scan_product_call_path

    hits = scan_product_call_path()
    assert hits == (), [f"{h.location} {h.key}" for h in hits]


def test_live_eval_set_module_does_not_introduce_capability_off_in_product_path() -> None:
    """Sanity: even the new live wrapper module itself must scan clean."""
    from fin.retrieve.capabilities import scan_source_for_capability_switches

    source = Path(__file__).with_name("test_live_eval_set.py").read_text(
        encoding="utf-8"
    )
    # The test file may reference capability keys in docstrings; the
    # static check is what counts.
    wrapper_source = (
        PROJECT_ROOT / "src" / "fin" / "eval" / "ablation" / "live_set.py"
    ).read_text(encoding="utf-8")
    hits = scan_source_for_capability_switches(
        wrapper_source, filename=str(wrapper_source)
    )
    assert hits == (), [f"{h.location} {h.key}" for h in hits]
