"""Tests for the scripted-provider boot path (T7).

The browser e2e lane (`specs/workspace.md` Amendment 2026-08-26,
scripted-provider lane) boots the SAME `fin.host.http` app the operator
serves, with the model swapped for a scripted transcript: no network
LLM, no credentials, deterministic frames.

What these tests pin:

* the swap is an EXPLICIT opt-in — an absent / falsy
  ``FIN_SCRIPTED_MODEL`` never auto-fakes the model, and a value that is
  neither truthy nor falsy is a typed configuration error rather than a
  silent "not requested";
* the transcript is derived from the frozen corpus, so the run reaches
  ``done`` with real citations instead of a bounce;
* the scripted boot builds NO provider client, so the lane cannot reach
  the network even when credentials happen to be in the environment;
* the transcript cycles, so a second send on the lane is as
  deterministic as the first.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fin.host.scripted_boot import (
    SCRIPTED_BOOT_ENV_VAR,
    SCRIPTED_DELTA_MS_ENV_VAR,
    SCRIPTED_DOCUMENT_ID,
    SCRIPTED_SCENARIO_BOUNCE,
    SCRIPTED_SCENARIO_ENV_VAR,
    SCRIPTED_SCENARIO_GROUNDED,
    ScriptedBootConfigError,
    ScriptedBootFixtureError,
    compile_for_scripted_e2e,
    scripted_boot_requested,
    scripted_scenario,
)
from fin.host.state import reset_input_state


# Pacing is a browser-lane affordance (the operator must SEE the deltas
# arrive); the host tests run it at zero so they stay fast.
NO_PACING = {SCRIPTED_BOOT_ENV_VAR: "1", SCRIPTED_DELTA_MS_ENV_VAR: "0"}

QUESTION = "What was Apple's total revenue in Q3 2022?"


# ----- opt-in contract -----


def test_scripted_boot_is_not_requested_by_default() -> None:
    """No env var, no scripted model. The dev boot path already
    refuses to auto-fake the model; this path must not become the
    back door that does."""
    assert scripted_boot_requested({}) is False
    assert (
        scripted_boot_requested(
            {"FIN_BASE_URL": "http://localhost:20128/v1", "FIN_MODEL": "fin/x"}
        )
        is False
    )


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_scripted_boot_requested_on_truthy_optin(value: str) -> None:
    assert scripted_boot_requested({SCRIPTED_BOOT_ENV_VAR: value}) is True


@pytest.mark.parametrize("value", ["", "  ", "0", "false", "no", "off"])
def test_scripted_boot_not_requested_on_falsy_optin(value: str) -> None:
    assert scripted_boot_requested({SCRIPTED_BOOT_ENV_VAR: value}) is False


def test_unreadable_optin_value_raises_typed_error() -> None:
    """A typo must stop the boot, not read as "not requested" — the
    operator asked for something and deserves to know it was ignored."""
    with pytest.raises(ScriptedBootConfigError) as exc:
        scripted_boot_requested({SCRIPTED_BOOT_ENV_VAR: "maybe"})
    assert SCRIPTED_BOOT_ENV_VAR in str(exc.value)


@pytest.mark.parametrize("value", ["abc", "-5", "1e9999"])
def test_unreadable_pacing_value_raises_typed_error(value: str) -> None:
    with pytest.raises(ScriptedBootConfigError) as exc:
        compile_for_scripted_e2e(
            env={SCRIPTED_BOOT_ENV_VAR: "1", SCRIPTED_DELTA_MS_ENV_VAR: value}
        )
    assert SCRIPTED_DELTA_MS_ENV_VAR in str(exc.value)


def test_missing_corpus_fixture_raises_typed_error(tmp_path: Path) -> None:
    """No frozen corpus -> no grounded transcript. The lane must say
    so rather than boot a model whose answer can never pass the gate."""
    with pytest.raises(ScriptedBootFixtureError):
        compile_for_scripted_e2e(
            env=NO_PACING,
            manifest_path=tmp_path / "manifest.json",
            extracted_root=tmp_path / "extracted",
        )


# ----- the compiled scripted graph -----


def _run(graph: Any, thread_id: str, question: str = QUESTION) -> dict[str, Any]:
    return graph.invoke(
        reset_input_state(question),
        config={"configurable": {"thread_id": thread_id}},
    )


def test_scripted_run_reaches_done_with_real_citations() -> None:
    """The transcript quotes the frozen page it just read, so the
    citation gate ``done``s on evidence — the lane's browser checks
    need a completed turn with a non-empty answer to assert against."""
    graph = compile_for_scripted_e2e(env=NO_PACING)
    state = _run(graph, "scripted-done")

    assert state["status"] == "done"
    assert state["answer"].strip()
    assert state["citations"], "scripted answer must ground at least one citation"
    assert state["citations"][0].document_id == SCRIPTED_DOCUMENT_ID


def test_scripted_run_streams_multiple_answer_deltas_and_thinking() -> None:
    """The lane's whole point is the delta wire: the model step must
    emit several answer chunks plus reasoning, not one dump."""
    graph = compile_for_scripted_e2e(env=NO_PACING)

    answer_chunks = 0
    reasoning_chunks = 0
    for mode, payload in graph.stream(
        reset_input_state(QUESTION),
        config={"configurable": {"thread_id": "scripted-deltas"}},
        stream_mode=["messages"],
    ):
        if mode != "messages":
            continue
        chunk, _meta = payload
        if getattr(chunk, "content", ""):
            answer_chunks += 1
        if (getattr(chunk, "additional_kwargs", {}) or {}).get("reasoning_details"):
            reasoning_chunks += 1

    assert answer_chunks >= 2
    assert reasoning_chunks >= 1


def test_scripted_transcript_cycles_across_runs() -> None:
    """A second send must behave exactly like the first. Without the
    cycle the exhausted transcript would replay its answer-only tail
    against an empty evidence list and bounce."""
    graph = compile_for_scripted_e2e(env=NO_PACING)

    first = _run(graph, "scripted-cycle-a")
    second = _run(graph, "scripted-cycle-b")

    assert first["status"] == second["status"] == "done"
    assert first["answer"] == second["answer"]
    assert second["citations"]


# ----- the bounce scenario (browser fixture for T3) -----


BOUNCE_PACING = {**NO_PACING, SCRIPTED_SCENARIO_ENV_VAR: SCRIPTED_SCENARIO_BOUNCE}


def test_scenario_defaults_to_grounded() -> None:
    assert scripted_scenario({}) == SCRIPTED_SCENARIO_GROUNDED
    assert scripted_scenario({SCRIPTED_SCENARIO_ENV_VAR: "  "}) == (
        SCRIPTED_SCENARIO_GROUNDED
    )


def test_unknown_scenario_raises_typed_error() -> None:
    """A misspelled scenario must stop the boot. Falling back to the
    grounded transcript would report a green bounce check on a run
    that never bounced."""
    with pytest.raises(ScriptedBootConfigError) as exc:
        scripted_scenario({SCRIPTED_SCENARIO_ENV_VAR: "bouncy"})
    assert SCRIPTED_SCENARIO_ENV_VAR in str(exc.value)


def test_bounce_scenario_ends_grounded_after_one_bounce() -> None:
    """The ungrounded draft is refused, the retry grounds, and the
    turn still ends ``done`` — the browser needs a bounce boundary
    inside an otherwise successful run."""
    graph = compile_for_scripted_e2e(env=BOUNCE_PACING)
    state = _run(graph, "scripted-bounce")

    assert state["status"] == "done"
    assert state["repair_count"] >= 1, "the gate must have bounced once"
    assert state["citations"]
    assert state["citations"][0].document_id == SCRIPTED_DOCUMENT_ID
    # The refused draft must not survive into the answer the operator
    # keeps: the retry replaces it rather than appending to it.
    assert "eighty-three billion" not in state["answer"]


def test_bounce_scenario_streams_the_draft_before_the_retry() -> None:
    """Both answer steps reach the delta wire. That ordering is what
    the browser check watches: prose, then the reset-carrying retry
    prose, with no empty bubble in between."""
    graph = compile_for_scripted_e2e(env=BOUNCE_PACING)

    streamed: list[str] = []
    for mode, payload in graph.stream(
        reset_input_state(QUESTION),
        config={"configurable": {"thread_id": "scripted-bounce-deltas"}},
        stream_mode=["messages"],
    ):
        if mode != "messages":
            continue
        chunk, _meta = payload
        if getattr(chunk, "content", ""):
            streamed.append(chunk.content)

    joined = "".join(streamed)
    assert "eighty-three billion" in joined, "the refused draft must stream"
    assert joined.index("eighty-three billion") < joined.index(
        f"Per {SCRIPTED_DOCUMENT_ID} page 1:"
    )


def test_bounce_scenario_cycles_like_the_grounded_one() -> None:
    """A server compiles once and answers every send from the same
    model object, so send 2 of the bounce lane must bounce too."""
    graph = compile_for_scripted_e2e(env=BOUNCE_PACING)

    first = _run(graph, "scripted-bounce-cycle-a")
    second = _run(graph, "scripted-bounce-cycle-b")

    assert first["status"] == second["status"] == "done"
    assert first["answer"] == second["answer"]
    assert second["repair_count"] >= 1


def test_scripted_boot_builds_no_provider_client(monkeypatch) -> None:
    """No network LLM and no credential read on this path: the shared
    provider factory must never be called. The hard-switch contract
    for the project-owned key is pinned by ``tests/host/test_compile_env.py``;
    this test only asserts the scripted path itself stays away from
    the factory."""
    import fin.host.compile as compile_module

    def explode(**_kwargs: Any) -> Any:
        raise AssertionError("scripted boot must not build a provider client")

    monkeypatch.setattr(compile_module, "_build_default_model", explode)
    monkeypatch.setenv("FIN_API_KEY", "sk-should-never-be-read")

    state = _run(compile_for_scripted_e2e(env=NO_PACING), "scripted-no-provider")
    assert state["status"] == "done"


# ----- HTTP boot wiring -----


def test_compile_for_http_uses_scripted_graph_only_on_optin(monkeypatch) -> None:
    """``compile_for_http`` picks the scripted graph when the operator
    opted in, and keeps the production boot otherwise."""
    import fin.host.compile as compile_module
    import fin.host.http as http_module
    import fin.host.scripted_boot as scripted_module

    sentinel_runtime = object()
    sentinel_scripted = object()
    monkeypatch.setattr(
        compile_module, "compile_for_runtime", lambda **_k: sentinel_runtime
    )
    monkeypatch.setattr(
        scripted_module, "compile_for_scripted_e2e", lambda **_k: sentinel_scripted
    )

    monkeypatch.setattr(http_module, "_http_graph_singleton", None, raising=False)
    monkeypatch.delenv(SCRIPTED_BOOT_ENV_VAR, raising=False)
    assert http_module.compile_for_http() is sentinel_runtime

    monkeypatch.setattr(http_module, "_http_graph_singleton", None, raising=False)
    monkeypatch.setenv(SCRIPTED_BOOT_ENV_VAR, "1")
    assert http_module.compile_for_http() is sentinel_scripted

    # Leave no scripted singleton behind for the rest of the session.
    monkeypatch.setattr(http_module, "_http_graph_singleton", None, raising=False)


def test_compile_for_http_unset_checkpoint_url_raises(
    monkeypatch,
) -> None:
    """Product HTTP compile must not fall back to InMemorySaver.

    ``compile_for_runtime_dev`` stays a test/eval helper; missing
    ``FIN_CHECKPOINT_URL`` is ``MissingCheckpointURLError``, same as
    ``compile_for_runtime``. Explicit ``FIN_SCRIPTED_MODEL`` is the
    only InMemory product-shaped boot.
    """
    from fin.host.compile import MissingCheckpointURLError
    import fin.host.compile as compile_module
    import fin.host.http as http_module

    def _dev_must_not_run() -> None:
        raise AssertionError("InMemory fallback must not run")

    monkeypatch.setattr(http_module, "_http_graph_singleton", None, raising=False)
    monkeypatch.delenv(SCRIPTED_BOOT_ENV_VAR, raising=False)
    monkeypatch.delenv("FIN_CHECKPOINT_URL", raising=False)
    monkeypatch.setattr(compile_module, "compile_for_runtime_dev", _dev_must_not_run)

    with pytest.raises(MissingCheckpointURLError):
        http_module.compile_for_http()

    monkeypatch.setattr(http_module, "_http_graph_singleton", None, raising=False)


def test_serve_refuses_boot_when_checkpoint_url_is_unset(monkeypatch) -> None:
    """``fin-serve`` must not bind the port, then fail on first ask.

    ``serve()`` reads the attachment bounds before it compiles, so this
    deployment env carries a finite set: the refusal asserted below stays the
    checkpoint URL's, not a mis-declared byte cap's.
    """
    from fin.host.compile import MissingCheckpointURLError
    import fin.host.http as http_module
    import uvicorn

    served: list[str] = []
    monkeypatch.setattr(http_module, "apply_dotenv", lambda: None)
    # The boot probe is not what this test exercises; stub it so the
    # assertion stays about the checkpoint-url refusal, not a handshake.
    monkeypatch.setattr(http_module, "publish_knowledge_mcp", lambda: None)
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: served.append("uvicorn"))
    monkeypatch.setattr(http_module, "_http_graph_singleton", None, raising=False)
    # The bounds are read before the graph is compiled, so this boot's read
    # has to start from unset: otherwise whatever an earlier case cached
    # answers here and this test would not be deploying an env at all.
    monkeypatch.setattr(
        http_module, "_http_attachment_budgets_singleton", None, raising=False
    )
    monkeypatch.delenv(SCRIPTED_BOOT_ENV_VAR, raising=False)
    monkeypatch.delenv("FIN_CHECKPOINT_URL", raising=False)
    # Test fixtures, not the deployed numbers. The run budget is derived from
    # the page and crop counts staged below through the module that prices a
    # run, so this boot gets past its own config door and the refusal asserted
    # underneath stays the checkpoint URL's.
    from fin.host.attachment_budgets import AttachmentBudgets, required_run_calls

    fixture_max_bytes = 1_048_576
    fixture_pages = 8
    fixture_crop_calls = 8
    fixture_run_calls = required_run_calls(
        AttachmentBudgets(
            max_upload_bytes=fixture_max_bytes,
            max_pdf_pages=fixture_pages,
            # Only the page and crop counts price a run, so this total is what
            # the call returns, never an input to it.
            max_vision_calls_per_run=fixture_pages + fixture_crop_calls,
            max_vision_crop_calls_per_run=fixture_crop_calls,
        )
    )
    for key, value in {
        "FIN_ATTACHMENT_MAX_BYTES": str(fixture_max_bytes),
        "FIN_ATTACHMENT_MAX_PDF_PAGES": str(fixture_pages),
        "FIN_ATTACHMENT_VISION_CALLS_PER_RUN": str(fixture_run_calls),
        "FIN_ATTACHMENT_VISION_CROP_CALLS_PER_RUN": str(fixture_crop_calls),
    }.items():
        monkeypatch.setenv(key, value)

    with pytest.raises(MissingCheckpointURLError):
        http_module.serve()
    assert served == []
    monkeypatch.setattr(http_module, "_http_graph_singleton", None, raising=False)
    monkeypatch.setattr(
        http_module, "_http_attachment_budgets_singleton", None, raising=False
    )
