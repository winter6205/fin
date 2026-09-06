"""Unconditional user-profile injection into the system prefix.

The user profile is operator-profile machinery, NOT memory-fact
machinery (operator decision on that pull request, cross-session-operator-memory
spec SC13). Its READ path (merged profile text into the system
prefix) must work regardless of ``FIN_MEMORY_ENABLED`` — even with
memory OFF, the operator's profile reaches the model. Only the
memory-fact machinery (existence pointer, prefetch) stays gated
behind memory_on; write tools (edit_user_profile) stay ON-gated
per spec SC12.

This suite pins the two halves:

* direct ``messages_for_model`` calls — byte-identical with no
  profile, profile block present + ordered correctly when set,
  whitespace-only profile = no block, profile + memory_on=False
  still surfaces the profile (the operator's core requirement).
* graph-level: a compiled graph reads the merged profile at model-
  call time, surfaces it on every leaf (research AND non-research)
  regardless of memory_on, leaves the prefix unchanged when no
  profile files exist, and re-reads the file each call so edits
  between calls are visible immediately.

The script reuses the ``RecordingScriptedModel`` pattern from
``test_thread_attach.py`` so the system-message bytes are
introspectable without reaching through compiled-graph internals.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import pytest
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
)
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import Field

from fin.host import compile_graph
from fin.host.agent_instructions import (
    DIALOGUE_INSTRUCTIONS_MARKER,
    DIALOGUE_SYSTEM_PROMPT,
    HOST_INSTRUCTIONS_MARKER,
    HOST_SYSTEM_PROMPT,
    MEMORY_POINTER_MARKER,
    USER_PROFILE_MARKER,
    messages_for_model,
)
from fin.host.memory_buffer import LocalMemoryBuffer
from fin.host.memory_port import MemoryPort, OffMemoryAdapter
from fin.host.scripted_model import ScriptedChatModel
from fin.host.user_profile import (
    GLOBAL_FILENAME,
    WORKSPACE_FILENAME,
    UserProfileStore,
)


# ---------- recording model: capture every _generate call's messages ----------


class _RecordingScriptedModel(ScriptedChatModel):
    """Record every ``_generate`` call's messages list.

    Both the sync and the streamed path funnel through ``_generate``
    when neither ``stream_responses`` nor ``stream_script`` is set,
    so overriding it covers every leaf the host graph reaches.
    """

    seen_calls: list[list[BaseMessage]] = Field(default_factory=list)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ):
        self.seen_calls.append(list(messages))
        return super()._generate(
            messages, stop=stop, run_manager=run_manager, **kwargs
        )


def _model(script: list[AIMessage] | None = None) -> _RecordingScriptedModel:
    return _RecordingScriptedModel(script=script or [AIMessage(content="ok")])


# ---------- fixtures used by both halves ----------


def _profile_store(tmp_path: Path, *, ws: str = "ws") -> UserProfileStore:
    """Profile store rooted under ``tmp_path`` with eager parents."""
    store = UserProfileStore(
        global_path=tmp_path / "home" / ".fin" / GLOBAL_FILENAME,
        workspace_path=tmp_path / ws / ".fin" / WORKSPACE_FILENAME,
    )
    store.global_path.parent.mkdir(parents=True, exist_ok=True)
    store.workspace_path.parent.mkdir(parents=True, exist_ok=True)
    return store


def _system_messages(model: _RecordingScriptedModel) -> list[str]:
    """The SystemMessage content strings from the most recent call.

    Single-call tests get a one-element list; multi-turn runs get
    one entry per ``_generate`` invocation (research + non-research
    leaves both fire ``_generate``).
    """
    out: list[str] = []
    for call in model.seen_calls:
        for message in call:
            if isinstance(message, SystemMessage):
                out.append(str(message.content))
    return out


# ===========================================================================
# Part A — direct messages_for_model contract
# ===========================================================================


def test_no_profile_keeps_prefix_byte_identical() -> None:
    """Default ``user_profile=None`` produces byte-identical output to
    the pre-T-profile ``messages_for_model``. The OFF (or omitted)
    profile path is the regression baseline.
    """
    msgs = [HumanMessage(content="Hello")]
    baseline = messages_for_model(msgs, track="dialogue")
    explicit_none = messages_for_model(
        msgs, track="dialogue", user_profile=None
    )
    assert explicit_none[0].content == baseline[0].content
    assert explicit_none[0].content == DIALOGUE_SYSTEM_PROMPT
    assert USER_PROFILE_MARKER not in str(explicit_none[0].content)


def test_empty_profile_keeps_prefix_byte_identical() -> None:
    """An empty-string profile also yields the OFF (byte-identical)
    output — a caller passing ``""`` is treated the same as ``None``."""
    msgs = [HumanMessage(content="q")]
    out = messages_for_model(msgs, track="dialogue", user_profile="")
    assert out[0].content == DIALOGUE_SYSTEM_PROMPT
    assert USER_PROFILE_MARKER not in str(out[0].content)


def test_whitespace_only_profile_keeps_prefix_byte_identical() -> None:
    """Whitespace-only profile text means no operator profile — byte-
    identical output, no marker. Matches ``UserProfileStore.read_merged``
    semantics (a stray newline should not silently turn into a block).
    """
    msgs = [HumanMessage(content="q")]
    out = messages_for_model(
        msgs, track="dialogue", user_profile="\n\n   \n"
    )
    assert out[0].content == DIALOGUE_SYSTEM_PROMPT
    assert USER_PROFILE_MARKER not in str(out[0].content)


def test_profile_block_present_with_marker_and_text() -> None:
    """Non-empty profile yields a SystemMessage that contains the
    profile marker and the verbatim text (workspace overlay).
    """
    profile = "Operator prefers quarterly revenue comparisons."
    msgs = [HumanMessage(content="q")]
    out = messages_for_model(
        msgs, track="dialogue", user_profile=profile
    )
    content = str(out[0].content)
    assert USER_PROFILE_MARKER in content
    assert profile in content


def test_profile_block_after_identity_before_pointer() -> None:
    """The block ordering is: identity → profile → memory pointer.
    The pointer (when memory_on=True) is the LAST item — adding the
    profile block does not displace it. Identity text is the FIRST
    thing the model sees, then the operator's profile, then the
    advisory memory note.
    """
    profile = "Operator prefers quarterly revenue comparisons."
    msgs = [HumanMessage(content="q")]
    out = messages_for_model(
        msgs, track="research", memory_on=True, user_profile=profile
    )
    content = str(out[0].content)

    identity_pos = content.index(HOST_INSTRUCTIONS_MARKER)
    profile_pos = content.index(USER_PROFILE_MARKER)
    pointer_pos = content.index(MEMORY_POINTER_MARKER)

    assert identity_pos < profile_pos < pointer_pos, (
        f"ordering wrong: identity={identity_pos}, profile={profile_pos}, "
        f"pointer={pointer_pos}"
    )
    # Identity / persona text is still present (the profile block is
    # appended, never a re-write of the track contract).
    assert HOST_SYSTEM_PROMPT in content


def test_profile_present_when_memory_off() -> None:
    """The operator's core requirement: profile text surfaces into the
    system prefix regardless of memory_on. With memory_on=False, the
    memory existence pointer must NOT appear, but the profile block
    must still be present.
    """
    profile = "Operator prefers quarterly revenue comparisons."
    msgs = [HumanMessage(content="q")]
    out = messages_for_model(
        msgs, track="dialogue", memory_on=False, user_profile=profile
    )
    content = str(out[0].content)

    assert USER_PROFILE_MARKER in content
    assert profile in content
    assert MEMORY_POINTER_MARKER not in content


def test_profile_present_when_memory_on() -> None:
    """Profile and memory pointer coexist. Both markers present, ordering
    pinned (identity → profile → pointer).
    """
    profile = "Operator prefers quarterly revenue comparisons."
    msgs = [HumanMessage(content="q")]
    out = messages_for_model(
        msgs, track="research", memory_on=True, user_profile=profile
    )
    content = str(out[0].content)

    assert USER_PROFILE_MARKER in content
    assert profile in content
    assert MEMORY_POINTER_MARKER in content


def test_profile_idempotent_on_pre_injected_block() -> None:
    """If the caller already pre-injected the profile marker (or this
    is a re-entrant call), the second invocation must not add a second
    copy. Same idempotence contract the memory pointer enforces.
    """
    profile = "Operator prefers quarterly revenue comparisons."
    pre_existing = SystemMessage(
        content=(
            f"{HOST_SYSTEM_PROMPT}\n\n"
            f"{USER_PROFILE_MARKER}\n{profile}"
        )
    )
    out = messages_for_model(
        [pre_existing, HumanMessage(content="q")],
        track="research",
        memory_on=True,
        user_profile=profile,
    )
    head = out[0]
    assert isinstance(head, SystemMessage)
    assert str(head.content).count(USER_PROFILE_MARKER) == 1
    # And the memory pointer is still added exactly once (the two
    # blocks are independent in their idempotence guards).
    assert str(head.content).count(MEMORY_POINTER_MARKER) == 1


def test_profile_marker_constant_is_exported() -> None:
    """Marker is exported for other layers (tests, log scrapers)."""
    assert isinstance(USER_PROFILE_MARKER, str)
    assert USER_PROFILE_MARKER.startswith("[")
    assert USER_PROFILE_MARKER.endswith("]")


def test_profile_block_text_is_advisory_not_commanding() -> None:
    """Per the prompt-development guide: a sentence that should be a
    code/schema gate must not be written as a long prompt rule. The
    profile header is advisory framing ("Operator preferences") and
    never commands the model.
    """
    profile = "Operator prefers quarterly revenue comparisons."
    out = messages_for_model(
        [HumanMessage(content="x")], track="dialogue", user_profile=profile
    )
    content = str(out[0].content).lower()
    for forbidden in (
        "you must",
        "always recall",
        "always save",
        "must cite",
    ):
        assert forbidden not in content, (
            f"profile block issued a command: {forbidden!r}"
        )


# ===========================================================================
# Part B — graph-level wiring
# ===========================================================================


@dataclass
class _FakeMemoryPort:
    """In-process ``MemoryPort`` shim with a workspace-aware local buffer.

    Mirrors the shape used in ``test_memory_graph_tools.py`` and
    ``test_memory_gold_set.py``.
    """

    enabled: bool = True
    buffer: LocalMemoryBuffer = field(default_factory=LocalMemoryBuffer)

    def extract(
        self,
        text: str,
        *,
        metadata: Mapping[str, object] | None = None,
        workspace: str = "",
    ) -> dict[str, object]:
        return self.buffer.extract(
            text,
            metadata=metadata,
            title=str((metadata or {}).get("title", "")) if metadata else "",
            workspace=workspace,
        )

    def recall(
        self,
        query: str,
        *,
        limit: int = 8,
        workspace: str = "",
    ) -> dict[str, object]:
        return self.buffer.recall(query, limit=limit, workspace=workspace)

    def gc(self, *, limit: int = 8) -> dict[str, object]:
        return self.buffer.gc(limit=limit)

    def dream(self, *, limit: int = 8) -> dict[str, object]:
        return self.buffer.dream(limit=limit)


def _compiled_graph(
    *,
    tmp_path: Path,
    port: MemoryPort | None,
    profile_store: UserProfileStore | None,
    workspace_root: str | None = None,
    model: _RecordingScriptedModel | None = None,
) -> Any:
    kwargs: dict[str, Any] = {
        "checkpointer": InMemorySaver(),
        "model": model or _model(),
        "workspace_root": workspace_root or str(tmp_path / "ws"),
        "known_document_ids": set(),
    }
    if port is not None:
        kwargs["memory_port"] = port
    else:
        kwargs["memory_port"] = OffMemoryAdapter()
    if profile_store is not None:
        kwargs["user_profile_store"] = profile_store
    return compile_graph(**kwargs)


def _invoke_one_turn(graph: Any, *, thread_id: str) -> None:
    """Send one human utterance and let the graph terminate."""
    graph.invoke(
        {
            "messages": [HumanMessage(content="hello")],
            "evidence": [],
            "repair_count": 0,
            "step_count": 0,
            "answer": "",
            "citations": [],
            "status": "running",
            "thinking_text": "",
            "tool_summaries": [],
        },
        config={"configurable": {"thread_id": thread_id}},
    )


def test_graph_off_with_profile_files_surfaces_profile(
    tmp_path: Path,
) -> None:
    """The operator's core requirement, graph-level: with memory OFF
    (``OffMemoryAdapter``) but profile files present, the model-facing
    SystemMessage for a non-research turn carries the profile block.

    The existence pointer must NOT appear. The track prompt and the
    profile block both appear.
    """
    profile_text = "Operator prefers quarterly revenue comparisons."
    profile_store = _profile_store(tmp_path)
    profile_store.write_layer("global", profile_text)

    graph, _rec = _compiled_graph_with_recording(
        tmp_path=tmp_path,
        port=OffMemoryAdapter(),  # explicit OFF
        profile_store=profile_store,
    )
    _invoke_one_turn(graph, thread_id="t-off-profile")

    seen = _seen_system_messages(graph)
    assert seen, "model was never called with a system message"
    for content in seen:
        assert USER_PROFILE_MARKER in content, (
            f"profile block missing in OFF graph system prefix: {content!r}"
        )
        assert profile_text in content
        assert MEMORY_POINTER_MARKER not in content, (
            "OFF graph leaked memory existence pointer"
        )


def _compiled_graph_with_recording(
    *,
    tmp_path: Path,
    port: MemoryPort | None,
    profile_store: UserProfileStore | None,
    workspace_root: str | None = None,
) -> tuple[Any, _RecordingScriptedModel]:
    """Build a graph whose recording model is returned for inspection."""
    rec = _model()
    graph = _compiled_graph(
        tmp_path=tmp_path,
        port=port,
        profile_store=profile_store,
        workspace_root=workspace_root,
        model=rec,
    )
    # Stash the model on the proxy so tests can pull it back without
    # reaching through ``_graph`` internals.
    graph._seen_calls = rec.seen_calls  # type: ignore[attr-defined]
    return graph, rec


def _seen_system_messages(graph: Any) -> list[str]:
    """Every SystemMessage content string the recording model saw."""
    seen = getattr(graph, "_seen_calls", None)
    if seen is None:
        return []
    out: list[str] = []
    for call in seen:
        for msg in call:
            if isinstance(msg, SystemMessage):
                out.append(str(msg.content))
    return out


def test_graph_on_with_profile_files_surfaces_both_blocks(
    tmp_path: Path,
) -> None:
    """ON graph + profile files: profile block AND existence pointer
    both appear, ordered correctly (identity → profile → pointer).

    Sends a research-style utterance so the track prompt is the
    host/research prompt (HOST_INSTRUCTIONS_MARKER); the same
    identity → profile → pointer ordering holds on the dialogue
    track via DIALOGUE_INSTRUCTIONS_MARKER.
    """
    profile_text = "Operator prefers quarterly revenue comparisons."
    profile_store = _profile_store(tmp_path)
    profile_store.write_layer("workspace", profile_text)

    port = _FakeMemoryPort()
    graph, _rec = _compiled_graph_with_recording(
        tmp_path=tmp_path,
        port=port,
        profile_store=profile_store,
    )
    # Research-style utterance so the Soft-Track routes to the
    # research leaf (HOST_INSTRUCTIONS_MARKER in the prefix).
    graph.invoke(
        {
            "messages": [
                HumanMessage(
                    content="What was AAPL net sales in 2023Q3?"
                )
            ],
            "evidence": [],
            "repair_count": 0,
            "step_count": 0,
            "answer": "",
            "citations": [],
            "status": "running",
            "thinking_text": "",
            "tool_summaries": [],
        },
        config={"configurable": {"thread_id": "t-on-profile"}},
    )

    seen = graph._seen_calls  # type: ignore[attr-defined]
    found = False
    for call in seen:
        for msg in call:
            if not isinstance(msg, SystemMessage):
                continue
            content = str(msg.content)
            if HOST_INSTRUCTIONS_MARKER not in content:
                continue
            found = True
            identity_pos = content.index(HOST_INSTRUCTIONS_MARKER)
            profile_pos = content.index(USER_PROFILE_MARKER)
            pointer_pos = content.index(MEMORY_POINTER_MARKER)
            assert identity_pos < profile_pos < pointer_pos
            assert profile_text in content
    assert found, "model was never called with a host system prefix"


def test_graph_with_no_profile_files_keeps_prefix_byte_stable(
    tmp_path: Path,
) -> None:
    """No profile files on disk → ``read_merged()`` returns ``""`` →
    profile block absent → SystemMessage bytes are exactly the pre-
    profile track prompt. Confirms ``messages_for_model`` is the
    single point that suppresses an empty / whitespace profile and
    the graph does not invent a block.
    """
    profile_store = _profile_store(tmp_path)  # no writes -> empty merge

    graph, _rec = _compiled_graph_with_recording(
        tmp_path=tmp_path,
        port=OffMemoryAdapter(),
        profile_store=profile_store,
    )
    _invoke_one_turn(graph, thread_id="t-no-profile")

    seen = graph._seen_calls  # type: ignore[attr-defined]
    found = False
    for call in seen:
        for msg in call:
            if not isinstance(msg, SystemMessage):
                continue
            found = True
            content = str(msg.content)
            assert USER_PROFILE_MARKER not in content
            # Track prompt is the entire prefix (no profile, no pointer,
            # no attachment) — byte-equal to the bare prompt.
            assert content == DIALOGUE_SYSTEM_PROMPT
    assert found


def test_graph_with_workspace_overlay_prefers_workspace(
    tmp_path: Path,
) -> None:
    """``UserProfileStore.read_merged()`` is workspace-overlay-global;
    the graph reads through that contract, not just one layer. With
    BOTH layers written, the workspace text wins.
    """
    profile_store = _profile_store(tmp_path)
    profile_store.write_layer("global", "Global text should not appear.")
    profile_store.write_layer(
        "workspace", "Workspace overlay text wins."
    )

    graph, _rec = _compiled_graph_with_recording(
        tmp_path=tmp_path,
        port=OffMemoryAdapter(),
        profile_store=profile_store,
    )
    _invoke_one_turn(graph, thread_id="t-overlay")

    seen = graph._seen_calls  # type: ignore[attr-defined]
    found = False
    for call in seen:
        for msg in call:
            if not isinstance(msg, SystemMessage):
                continue
            found = True
            content = str(msg.content)
            assert "Workspace overlay text wins." in content
            assert "Global text should not appear." not in content
    assert found


def test_graph_rereads_profile_on_each_call(tmp_path: Path) -> None:
    """File edits between calls take effect immediately: the profile
    block is rebuilt from ``read_merged()`` at model-call time, not
    cached at compile time.
    """
    profile_store = _profile_store(tmp_path)
    profile_store.write_layer("global", "first call profile text")

    graph, _rec = _compiled_graph_with_recording(
        tmp_path=tmp_path,
        port=OffMemoryAdapter(),
        profile_store=profile_store,
    )

    # First turn: profile is "first call profile text".
    _invoke_one_turn(graph, thread_id="t-reread")
    seen = graph._seen_calls  # type: ignore[attr-defined]
    assert seen, "model was not called on the first turn"
    for call in seen:
        for msg in call:
            if isinstance(msg, SystemMessage):
                assert "first call profile text" in str(msg.content)

    # Edit the file between calls.
    profile_store.write_layer("global", "second call profile text")

    # Second turn (NEW thread_id, fresh start) — re-read.
    _invoke_one_turn(graph, thread_id="t-reread-2")
    seen = graph._seen_calls  # type: ignore[attr-defined]
    second_turn_texts = [
        str(msg.content)
        for call in seen[1:]  # skip the first turn's calls
        for msg in call
        if isinstance(msg, SystemMessage)
    ]
    assert second_turn_texts, "model was not called on the second turn"
    assert any("second call profile text" in t for t in second_turn_texts)


def test_graph_research_leaf_with_profile(tmp_path: Path) -> None:
    """Research leaf also surfaces the profile block. The profile is a
    system-prefix concern, not a track concern — it reaches both the
    research ``model_node`` and the non-research leaves.
    """
    profile_text = "Operator prefers quarterly revenue comparisons."
    profile_store = _profile_store(tmp_path)
    profile_store.write_layer("workspace", profile_text)

    # Scripted model: research turn that returns a single answer
    # message (no tool calls), so the gate runs and the run ends.
    rec = _RecordingScriptedModel(
        script=[
            AIMessage(content="plain research answer"),
        ]
    )
    graph = _compiled_graph(
        tmp_path=tmp_path,
        port=OffMemoryAdapter(),
        profile_store=profile_store,
        model=rec,
    )
    graph._seen_calls = rec.seen_calls  # type: ignore[attr-defined]

    # Force the research track via a question-style utterance.
    graph.invoke(
        {
            "messages": [
                HumanMessage(
                    content="What was AAPL net sales in 2023Q3?"
                )
            ],
            "evidence": [],
            "repair_count": 0,
            "step_count": 0,
            "answer": "",
            "citations": [],
            "status": "running",
            "thinking_text": "",
            "tool_summaries": [],
        },
        config={"configurable": {"thread_id": "t-research-profile"}},
    )

    found = False
    for call in rec.seen_calls:
        for msg in call:
            if not isinstance(msg, SystemMessage):
                continue
            content = str(msg.content)
            if HOST_INSTRUCTIONS_MARKER in content:
                found = True
                assert USER_PROFILE_MARKER in content
                assert profile_text in content
                # Track prompt is research (host); profile block sits
                # inside the same SystemMessage.
                assert HOST_SYSTEM_PROMPT in content
    assert found, "research leaf never received a host system prefix"
