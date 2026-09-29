"""T2f: the ``ensure_attachment_ready`` graph gate (spec thread-attachment-source-read).

Contract covered here (spec Boundaries "graph order", plan T2f, ADR 0089):

* with an ``attachment_service`` injected, the grounded route after the
  compaction gate passes through ``ensure_attachment_ready`` BEFORE the
  grounded ``model`` leaf; the non-grounded leaves are untouched.
* all-ready -> proceed to model; a pending preparation pauses the run on
  ``interrupt({"kind": "attachment_preparation", ...})``; resume
  re-evaluates (still pending -> interrupt again; ready -> proceed), and
  the turn text for rows sent without frozen text is (re)hydrated via the
  service before the model sees it.
* failed / unknown -> typed ``refuse`` carrying the preparation code (or
  ``unknown_locator``), never an empty successful turn.
* the race between a ready verdict and the hydration read (the resolver
  raises its typed not-ready pair after derived data went unreadable)
  re-asks and waits on the same interrupt path — never a bare crash,
  never an empty success; any other resolver error propagates.
* a service violating the consumer-side Protocol (outcome without
  ``state``, no ``resolve_for_turn``) fails loudly, not via a silent
  fallback refusal.
* an ineligible turn (no attachments / non-grounded track) never calls
  the service; ``attachment_service=None`` keeps today's routing and
  topology exactly.
* the interrupted run keeps its canonical ``run_id`` on the checkpoint
  (same durability shape as the choice window).

Hermetic: ScriptedChatModel + InMemorySaver + a scripted fake service —
no live store or model transport. The service seam is duck-typed per the
pinned contract; only the gate's race-catch is type-pinned to
``fin.host.attachment_prepare``'s not-ready pair.
"""

from __future__ import annotations

from typing import Any, Iterator

from langchain_core.messages import AIMessage, BaseMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
import pytest
from pydantic import Field

from fin.corpus.extracted import Locator
from fin.host import EvidenceItem, ScriptedChatModel, compile_graph
from fin.host.attachment_prepare import (
    AttachmentNotFoundError,
    AttachmentNotReadyError,
)
from fin.host.state import reset_input_state
from attachment_fakes import FakeAttachmentService, _PrepOutcome

KNOWN_IDS = {"AAPL-2022Q3", "AAPL-2023Q1", "MSFT-2023Q1", "AMZN-2023Q1"}

# Grounded by the track rules tier (mirrors test_track_decision.py).
GROUNDED = "What was AAPL net sales in 2023 Q3?"
# Dialogue by the track rules tier (mirrors test_track_decision.py).
DIALOGUE = "Hello!"

# Pre-seeded run evidence + a >=32-char extractive answer so a grounded
# turn that clears the attachment gate ends ``done`` at the citation gate
# on one model hop (no tool wiring needed for a routing test).
EVIDENCE_TEXT = "Microsoft Corporation reported total revenue of 52,857 million dollars."
QUOTED_ANSWER = f"Per the filing: {EVIDENCE_TEXT[:40]}"

_ANSWER = AIMessage(content=QUOTED_ANSWER)


class RecordingScriptedChatModel(ScriptedChatModel):
    """ScriptedChatModel that records the full message list of every call.

    ``seen`` lets a test assert exactly what the grounded leaf's model
    call received (e.g. the hydrated attachment block) without touching
    production code.
    """

    seen: list[list[BaseMessage]] = Field(default_factory=list)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> Any:
        self.seen.append(list(messages))
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> Iterator[Any]:
        self.seen.append(list(messages))
        yield from super()._stream(
            messages, stop=stop, run_manager=run_manager, **kwargs
        )


def _cfg(thread_id: str, **configurable: Any) -> dict:
    return {"configurable": {"thread_id": thread_id, **configurable}}


def _input(text: str, attachments: list[dict[str, str]] | None = None) -> dict[str, Any]:
    state = reset_input_state(text, attachments=attachments)
    # Seed one evidence row so the citation gate can pass on a single
    # extractive hop (the gate under test runs BEFORE the leaf; the
    # evidence is not what this suite is about).
    state["evidence"] = [
        EvidenceItem(
            locator=Locator(
                document_id="MSFT-2023Q1", version="e" * 64, unit=1
            ),
            unit_text=EVIDENCE_TEXT,
        )
    ]
    return state


def _compile(
    script: list[AIMessage],
    *,
    attachment_service: Any | None = None,
) -> Any:
    model = RecordingScriptedChatModel(script=script)
    kwargs: dict[str, Any] = {
        "checkpointer": InMemorySaver(),
        "model": model,
        "known_document_ids": KNOWN_IDS,
    }
    if attachment_service is not None:
        kwargs["attachment_service"] = attachment_service
    graph = compile_graph(**kwargs)
    # Test-only handle on the recording model (the proxy intentionally
    # hides the compiled graph's collaborators).
    graph.test_model = model
    return graph


def _interrupt_payload(out: dict[str, Any]) -> dict[str, Any] | None:
    interrupts = out.get("__interrupt__") or ()
    if not interrupts:
        return None
    return interrupts[0].value


def _attachment_item(
    attachment_id: str, *, filename: str = "msft-10q.pdf", text: str = "AAPL net sales 89,493"
) -> dict[str, str]:
    return {"attachment_id": attachment_id, "filename": filename, "text": text}


def _all_text(messages: list[BaseMessage]) -> str:
    return "\n".join(str(getattr(m, "content", "")) for m in messages)


# ---------------------------------------------------------------------------
# ready routing
# ---------------------------------------------------------------------------


def test_all_ready_proceeds_to_model_and_calls_service_per_attachment() -> None:
    service = FakeAttachmentService({"att-1": ["ready"], "att-2": ["ready"]})
    g = _compile([_ANSWER], attachment_service=service)
    tid = "gate-ready"
    out = g.invoke(
        _input(GROUNDED, attachments=[_attachment_item("att-1"), _attachment_item("att-2")]),
        config=_cfg(tid),
    )
    assert out.get("__interrupt__") is None or not out.get("__interrupt__")
    assert out["status"] == "done"
    # The gate ran on the grounded route: one check per attachment per
    # model entry (this run makes exactly one model call).
    assert service.prep_calls == [(tid, "att-1"), (tid, "att-2")]
    # The already-carried frozen text survived untouched; no hydration was
    # attempted for rows that already have text.
    assert out["attachments"][0]["text"] == "AAPL net sales 89,493"
    assert service.resolve_calls == []
    assert out["answer"] == QUOTED_ANSWER
    assert "AAPL net sales 89,493" in _model_attachment_block(g)


def _model_attachment_block(g: Any) -> str:
    """Concatenated messages of the grounded leaf's LAST model call."""
    return _all_text(g.test_model.seen[-1])


# ---------------------------------------------------------------------------
# pending -> interrupt -> resume
# ---------------------------------------------------------------------------


def test_pending_interrupts_with_pinned_payload_kind() -> None:
    service = FakeAttachmentService({"att-1": ["pending"]})
    g = _compile([_ANSWER], attachment_service=service)
    tid = "gate-pending"
    out = g.invoke(
        _input(GROUNDED, attachments=[_attachment_item("att-1", text="")]),
        config=_cfg(tid),
    )
    payload = _interrupt_payload(out)
    assert payload is not None
    assert payload["kind"] == "attachment_preparation"
    assert payload["attachment_ids"] == ["att-1"]
    assert service.prep_calls == [(tid, "att-1")]
    # The pause happens BEFORE the grounded leaf: no model call ran.
    assert g.test_model.seen == []


def test_resume_after_ready_hydrates_text_and_model_sees_it() -> None:
    hydrated = "MSFT revenue was 52,857 million."
    service = FakeAttachmentService(
        {"att-1": [("pending", None), ("ready", None)]},
        texts={"att-1": hydrated},
        filenames={"att-1": "msft-10q.pdf"},
    )
    g = _compile([_ANSWER], attachment_service=service)
    tid = "gate-resume-hydrate"
    out = g.invoke(
        _input(GROUNDED, attachments=[_attachment_item("att-1", text="")]),
        config=_cfg(tid),
    )
    assert _interrupt_payload(out) is not None
    # A human POST /resume rides a pick value; the gate must ignore it.
    done = g.invoke(Command(resume="lexical"), config=_cfg(tid))
    assert not done.get("__interrupt__")
    assert done["status"] == "done"
    # Replay shape: initial call (pending) + resume re-evaluation (ready).
    assert [c for c in service.prep_calls] == [(tid, "att-1"), (tid, "att-1")]
    assert service.resolve_calls == [(tid, "att-1")]
    # The hydrated text rode the state channel into the model call.
    assert done["attachments"][0]["text"] == hydrated
    model_call = _model_attachment_block(g)
    assert hydrated in model_call
    assert "attached file: msft-10q.pdf" in model_call


def test_still_pending_on_resume_interrupts_again_loop_safe() -> None:
    service = FakeAttachmentService({"att-1": ["pending"]})
    g = _compile([_ANSWER], attachment_service=service)
    tid = "gate-repending"
    out = g.invoke(
        _input(GROUNDED, attachments=[_attachment_item("att-1", text="")]),
        config=_cfg(tid),
    )
    assert _interrupt_payload(out) is not None
    again = g.invoke(Command(resume="job-done"), config=_cfg(tid))
    payload = _interrupt_payload(again)
    assert payload is not None
    assert payload["kind"] == "attachment_preparation"
    assert payload["attachment_ids"] == ["att-1"]
    # Each pause re-checked the job (idempotent join), never proceeded:
    # 1 call in the original run + 2 in the resume replay (the head of
    # the node body re-evaluates, and the post-interrupt loop checks
    # once more before raising the next interrupt).
    assert service.prep_calls == [(tid, "att-1")] * 3


def test_only_pending_ids_are_named_in_the_payload() -> None:
    service = FakeAttachmentService({"att-a": ["ready"], "att-b": ["pending"]})
    g = _compile([_ANSWER], attachment_service=service)
    tid = "gate-mixed-pending"
    out = g.invoke(
        _input(
            GROUNDED,
            attachments=[_attachment_item("att-a"), _attachment_item("att-b", text="")],
        ),
        config=_cfg(tid),
    )
    payload = _interrupt_payload(out)
    assert payload is not None
    assert payload["attachment_ids"] == ["att-b"]


def test_run_id_persists_across_interrupt_and_resume() -> None:
    service = FakeAttachmentService(
        {"att-1": [("pending", None), ("ready", None)]},
        texts={"att-1": "HYDRATED"},
    )
    g = _compile([_ANSWER], attachment_service=service)
    tid = "gate-runid"
    config = _cfg(tid, run_id="run-t2f-1")
    out = g.invoke(
        _input(GROUNDED, attachments=[_attachment_item("att-1", text="")]),
        config=config,
    )
    assert _interrupt_payload(out) is not None
    # The durable wait shape: the checkpoint of the interrupted run still
    # names the SAME canonical run the send started with.
    assert g.checkpoint_run_id(tid) == "run-t2f-1"
    done = g.invoke(Command(resume="job-done"), config=config)
    assert done["status"] == "done"
    assert g.checkpoint_run_id(tid) == "run-t2f-1"


# ---------------------------------------------------------------------------
# typed exits
# ---------------------------------------------------------------------------


def test_failed_after_resume_exits_typed_with_preparation_code() -> None:
    service = FakeAttachmentService(
        {"att-1": [("pending", None), ("failed", "attachment_page_limit_exceeded")]}
    )
    g = _compile([_ANSWER], attachment_service=service)
    tid = "gate-failed"
    out = g.invoke(
        _input(GROUNDED, attachments=[_attachment_item("att-1", text="")]),
        config=_cfg(tid),
    )
    assert _interrupt_payload(out) is not None
    done = g.invoke(Command(resume="job-done"), config=_cfg(tid))
    assert not done.get("__interrupt__")
    # Never an empty successful turn: the failure is the run's typed exit.
    assert done["status"] == "refuse"
    assert done["status_reason"] == "attachment_page_limit_exceeded"
    assert done["answer"] == ""
    assert g.test_model.seen == []


def test_failed_immediately_exits_typed_without_interrupt() -> None:
    service = FakeAttachmentService(
        {"att-1": [("failed", "attachment_vision_budget_exceeded")]}
    )
    g = _compile([_ANSWER], attachment_service=service)
    tid = "gate-failed-now"
    out = g.invoke(
        _input(GROUNDED, attachments=[_attachment_item("att-1", text="")]),
        config=_cfg(tid),
    )
    assert _interrupt_payload(out) is None
    assert out["status"] == "refuse"
    assert out["status_reason"] == "attachment_vision_budget_exceeded"


def test_unknown_exits_unknown_locator() -> None:
    service = FakeAttachmentService({"att-1": [("unknown", None)]})
    g = _compile([_ANSWER], attachment_service=service)
    tid = "gate-unknown"
    out = g.invoke(
        _input(GROUNDED, attachments=[_attachment_item("att-1", text="")]),
        config=_cfg(tid),
    )
    assert _interrupt_payload(out) is None
    assert out["status"] == "refuse"
    assert out["status_reason"] == "unknown_locator"


def test_failed_without_code_still_names_a_typed_reason() -> None:
    service = FakeAttachmentService({"att-1": [("failed", None)]})
    g = _compile([_ANSWER], attachment_service=service)
    tid = "gate-failed-nocode"
    out = g.invoke(
        _input(GROUNDED, attachments=[_attachment_item("att-1", text="")]),
        config=_cfg(tid),
    )
    assert out["status"] == "refuse"
    assert out["status_reason"]  # never empty
    assert out["status_reason"] != "done"


def test_failure_outranks_pending_in_the_same_set() -> None:
    service = FakeAttachmentService(
        {
            "att-a": [("pending", None)],
            "att-b": [("failed", "attachment_index_unavailable")],
        }
    )
    g = _compile([_ANSWER], attachment_service=service)
    tid = "gate-fail-over-pending"
    out = g.invoke(
        _input(
            GROUNDED,
            attachments=[_attachment_item("att-a", text=""), _attachment_item("att-b", text="")],
        ),
        config=_cfg(tid),
    )
    # Waiting on a job whose sibling already terminally failed would run
    # the model on a partially-failed eligible set: the typed exit wins.
    assert _interrupt_payload(out) is None
    assert out["status"] == "refuse"
    assert out["status_reason"] == "attachment_index_unavailable"


# ---------------------------------------------------------------------------
# eligibility: the service is only asked on grounded attachment turns
# ---------------------------------------------------------------------------


def test_grounded_without_attachments_never_calls_service() -> None:
    service = FakeAttachmentService({})
    g = _compile([_ANSWER], attachment_service=service)
    out = g.invoke(_input(GROUNDED), config=_cfg("gate-no-attach"))
    assert service.prep_calls == []
    assert out["status"] == "done"


def test_dialogue_turn_with_attachments_never_calls_service() -> None:
    service = FakeAttachmentService({"att-1": ["ready"]})
    g = _compile([AIMessage(content="Hi there.")], attachment_service=service)
    out = g.invoke(
        _input(DIALOGUE, attachments=[_attachment_item("att-1")]),
        config=_cfg("gate-dialogue"),
    )
    assert service.prep_calls == []
    assert out["status"] == "done"
    # The non-grounded leaf keeps today's attachment rendering untouched.
    assert _model_attachment_block(g).count("attached file: msft-10q.pdf") == 1


# ---------------------------------------------------------------------------
# service=None keeps today's graph exactly
# ---------------------------------------------------------------------------


def test_service_none_keeps_old_routing_and_topology() -> None:
    g = _compile([_ANSWER])
    nodes = set(g._graph.get_graph().nodes)
    assert "ensure_attachment_ready" not in nodes
    assert "model" in nodes
    tid = "gate-off"
    out = g.invoke(
        _input(GROUNDED, attachments=[_attachment_item("att-1")]),
        config=_cfg(tid),
    )
    assert out["status"] == "done"


def test_service_wires_the_gate_between_gate_and_model() -> None:
    service = FakeAttachmentService({"att-1": ["ready"]})
    g = _compile([_ANSWER], attachment_service=service)
    graph = g._graph.get_graph()
    edges = {(e.source, e.target) for e in graph.edges}
    assert "ensure_attachment_ready" in set(graph.nodes)
    assert ("compaction_gate", "ensure_attachment_ready") in edges
    assert ("ensure_attachment_ready", "model") in edges
    assert ("compaction_gate", "model") not in edges


# ---------------------------------------------------------------------------
# hydration race: ready verdict, then resolve_for_turn reports not-ready
# ---------------------------------------------------------------------------


def test_ready_then_resolve_not_ready_rerequests_and_resumes() -> None:
    # The send door (workspace_http) guards the race between the ready
    # verdict and the resolve read by treating the not-ready pair as
    # pending; the gate node must do the same: re-ask (interrupt, pending
    # shape), never crash bare and never proceed on empty text.
    hydrated = "MSFT revenue was 52,857 million."
    service = FakeAttachmentService(
        {"att-1": ["ready"]},
        texts={"att-1": hydrated},
        resolve_errors={"att-1": [AttachmentNotReadyError]},
    )
    g = _compile([_ANSWER], attachment_service=service)
    tid = "gate-resolve-race"
    out = g.invoke(
        _input(GROUNDED, attachments=[_attachment_item("att-1", text="")]),
        config=_cfg(tid),
    )
    payload = _interrupt_payload(out)
    assert payload == {
        "kind": "attachment_preparation",
        "attachment_ids": ["att-1"],
    }
    # The race pauses the run BEFORE the grounded leaf.
    assert g.test_model.seen == []
    # Resume: the loop's head re-asks preparation (still ready) and the
    # now-drained resolver hydrates — the run completes normally.
    done = g.invoke(Command(resume="job-done"), config=_cfg(tid))
    assert not done.get("__interrupt__")
    assert done["status"] == "done"
    assert service.prep_calls == [(tid, "att-1"), (tid, "att-1")]
    assert service.resolve_calls == [(tid, "att-1"), (tid, "att-1")]
    assert done["attachments"][0]["text"] == hydrated


def test_ready_then_resolve_race_then_failed_exits_named() -> None:
    # "Never an unhandled crash and never an empty success": if the
    # re-ask after the race answers failed, the run takes the named
    # refusal exit, not the pending wait.
    service = FakeAttachmentService(
        {"att-1": [("ready", None), ("failed", "attachment_text_rejected")]},
        resolve_errors={"att-1": [AttachmentNotFoundError]},
    )
    g = _compile([_ANSWER], attachment_service=service)
    tid = "gate-race-then-failed"
    out = g.invoke(
        _input(GROUNDED, attachments=[_attachment_item("att-1", text="")]),
        config=_cfg(tid),
    )
    payload = _interrupt_payload(out)
    assert payload is not None
    assert payload["attachment_ids"] == ["att-1"]
    done = g.invoke(Command(resume="job-done"), config=_cfg(tid))
    assert not done.get("__interrupt__")
    assert done["status"] == "refuse"
    assert done["status_reason"] == "attachment_text_rejected"
    assert g.test_model.seen == []


def test_resolve_unrelated_error_propagates_never_swallowed() -> None:
    # Only the typed not-ready pair is treated as pending; any other
    # resolver error is a real bug and must surface (repo rule: typed,
    # never swallowed).
    service = FakeAttachmentService(
        {"att-1": ["ready"]},
        resolve_errors={"att-1": [ValueError]},
    )
    g = _compile([_ANSWER], attachment_service=service)
    with pytest.raises(ValueError):
        g.invoke(
            _input(GROUNDED, attachments=[_attachment_item("att-1", text="")]),
            config=_cfg("gate-resolve-bug"),
        )


def test_outcome_without_state_attribute_raises_loudly() -> None:
    # The consumer-side Protocol contract: the gate reads ``state``
    # directly, so a service violating the pinned outcome shape fails
    # loudly instead of silently falling back to a generic refusal.
    class _NoStateOutcome:
        code = None
        version = 1

    class _BadOutcomeService(FakeAttachmentService):
        def request_preparation(self, thread_id: str, attachment_id: str) -> Any:
            self.prep_calls.append((thread_id, attachment_id))
            return _NoStateOutcome()

    service = _BadOutcomeService({"att-1": ["ready"]})
    g = _compile([_ANSWER], attachment_service=service)
    with pytest.raises(AttributeError):
        g.invoke(
            _input(GROUNDED, attachments=[_attachment_item("att-1", text="")]),
            config=_cfg("gate-loud-contract"),
        )


def test_missing_resolve_for_turn_raises_loudly() -> None:
    # A service without the hydration seam used to be silently skipped
    # (``getattr(..., None)``); the Protocol makes it a loud contract
    # violation on the first row that needs hydration.
    class _NoResolver:
        def request_preparation(self, thread_id: str, attachment_id: str) -> Any:
            return _PrepOutcome(state="ready")

    g = _compile([_ANSWER], attachment_service=_NoResolver())
    with pytest.raises(AttributeError):
        g.invoke(
            _input(GROUNDED, attachments=[_attachment_item("att-1", text="")]),
            config=_cfg("gate-loud-resolver"),
        )
