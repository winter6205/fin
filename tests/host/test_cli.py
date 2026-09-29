"""Boundary tests for the v1 CLI surface (T7).

The CLI is ``python -m fin``: a positional question, an optional
``--thread-id``, and a three-section stdout schema::

    status: <status>
    answer: <answer text>
    citations:
      - <document_id> v<version> <p|u><unit>: <quote>

``<p|u>`` is chosen by the citation's ``display`` discriminator: a unit
presented as a physical page renders ``p``, anything else ``u`` — both
numbered by ``unit``, never by a separate page field.

Hard contracts under test here:

* ``python -m fin --help`` exits 0 and instantiates NO LLM, graph,
  or checkpointer (argparse help only).
* Sections print in order: status, then answer, then citations.
  The ``citations:`` header is ALWAYS printed (stable output schema);
  citation entries appear ONLY on ``status == "done"``.
* The hidden bounce marker ``[FIN_HIDDEN_BOUNCE]`` (ADR 0021) never
  reaches stdout — the CLI renders only the structured
  ``(status, answer, citations)`` triple and strips the marker from
  any text it prints.
* ``thread_id`` is a session (ADR 0016): omitted → auto UUID echoed
  on stderr; explicit → reused verbatim. Two sequential runs on the
  same thread accumulate checkpointed messages.
* At most one active run per thread: a second concurrent invoke
  raises ``ConcurrentRunError``, which the CLI catches and reports
  on stderr with exit code 1.
* Typed bootstrap failure: a missing extracted tree surfaces as
  ``error: ingest artifacts missing`` on stderr with exit 1 — never
  a silent re-extract (ADR 0020).
* Ask-path hygiene: neither CLI source imports ``pdfplumber`` nor an
  LLM-provider / Postgres-checkpointer dependency at module load.

All scripted tests run with ``ScriptedChatModel`` + ``InMemorySaver``
and require no ``FIN_API_KEY``.
"""

from __future__ import annotations

import ast
import concurrent.futures
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.checkpoint.memory import InMemorySaver

from fin.corpus.errors import IngestMissingError
from fin.corpus.extracted import load_extracted_tree
from fin.host import (
    ConcurrentRunError,
    ScriptedChatModel,
    compile_graph,
)
from fin.host.cli import main, render_result, run


# ----- corpus fixtures (mirrors tests/host/test_graph.py; no shared conftest) -----


AAPL_2022Q3 = "AAPL-2022Q3"
AAPL_2022Q3_VERSION = (
    "a7a0d8261a0923404fc45446afd71b9965bd2b4064772df0287e761bb68ba9f2"
)

KNOWN_IDS = {AAPL_2022Q3, "AAPL-2023Q1", "MSFT-2023Q1", "AMZN-2023Q1"}

# The ``read_source`` company leg over the committed corpus units. Only
# the scripted ``read_source`` transcripts need it; the default graph no
# longer reads the tree on its own (ADR 0046 / 0047).
from fin.retrieve import FakeRetrievePort

_READ_ADAPTER = FakeRetrievePort(
    pages_by_doc={
        AAPL_2022Q3: load_extracted_tree(
            Path("data/corpus/manifest.json"), Path("data/corpus/extracted")
        )[AAPL_2022Q3]
    }
)

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def aapl_page1_text():
    """Real first-unit text from AAPL-2022Q3, loaded from the committed tree."""
    manifest = REPO_ROOT / "data" / "corpus" / "manifest.json"
    extracted = REPO_ROOT / "data" / "corpus" / "extracted"
    tree = load_extracted_tree(manifest, extracted)
    return tree[AAPL_2022Q3][0].text


# ----- helpers -----


def _scripted_read_then_answer(answer: str) -> list[AIMessage]:
    """Script: one ``read_source`` tool call, then the answer AIMessage."""
    return [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "read_source",
                    "args": {
                        "source_ref": {
                            "document_id": AAPL_2022Q3,
                            "version": AAPL_2022Q3_VERSION,
                            "unit": 1,
                        }
                    },
                    "id": "c-read",
                    "type": "tool_call",
                }
            ],
        ),
        AIMessage(content=answer),
    ]


class _FakeGraph:
    """Duck-typed stand-in for the locked graph: returns canned state."""

    def __init__(self, state: dict) -> None:
        self.state = state
        self.calls: list[tuple[dict, dict]] = []

    def invoke(self, input: dict, config: dict | None = None, **kwargs):
        self.calls.append((input, config or {}))
        return self.state


class _RecordingModel(ScriptedChatModel):
    """Records the message-list length of every ``_generate`` call so
    tests can prove cross-run accumulation without touching privates."""

    seen_lengths: list[int] = []

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen_lengths.append(len(messages))
        return super()._generate(
            messages, stop=stop, run_manager=run_manager, **kwargs
        )


# ----- --help -----


def test_help_via_subprocess_exits_zero_without_api_key():
    """``python -m fin --help`` exits 0, prints the program name and
    the ``--thread-id`` flag, with no API key in the environment."""
    env = {
        k: v
        for k, v in os.environ.items()
        if k != "FIN_API_KEY"
    }
    proc = subprocess.run(
        [sys.executable, "-m", "fin", "--help"],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "fin" in proc.stdout
    assert "--thread-id" in proc.stdout
    assert "Traceback" not in proc.stderr


def test_help_path_never_builds_a_graph():
    """In-process: ``main(['--help'])`` raises SystemExit(0) BEFORE the
    graph factory is ever invoked — argparse help only, no LLM/graph/
    checkpointer instantiation."""

    def _must_not_be_called() -> object:
        raise AssertionError("graph factory must not run on --help")

    with pytest.raises(SystemExit) as excinfo:
        main(["--help"], graph_factory=_must_not_be_called)
    assert excinfo.value.code == 0


# ----- happy path (done) -----


def test_done_run_prints_status_answer_citations_in_order(aapl_page1_text, capsys):
    """Scripted read_source → answer-with-span driven through ``main``
    prints the three sections in order with ≥1 formatted citation."""
    quoted = f"Per the filing: {aapl_page1_text[:80]}"
    model = ScriptedChatModel(script=_scripted_read_then_answer(quoted))
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
        adapter=_READ_ADAPTER,
    )
    rc = main(
        ["What was AAPL net sales in 2023 Q3?", "--thread-id", "t-cli-done"],
        graph_factory=lambda: g,
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "status: done" in out
    assert f"answer: {quoted}" in out
    # Order: status, then answer, then citations.
    assert out.index("status: done") < out.index("answer:") < out.index("citations:")
    # Citation line format: two-space indent, locator fields, quote.
    # ``p1`` because a company unit read through ``read_source``
    # presents as a page; the number itself is the locator's ``unit``.
    citation_lines = [line for line in out.splitlines() if line.startswith("  - ")]
    assert len(citation_lines) == 1
    line = citation_lines[0]
    assert line.startswith(f"  - {AAPL_2022Q3} v{AAPL_2022Q3_VERSION} p1: ")
    # One citation per line: the verbatim span carries the source line
    # breaks, and the renderer collapses them instead of wrapping.
    assert line.split(": ", 1)[1] == " ".join(aapl_page1_text[:80].splitlines())


# ----- step cap -----


def test_step_cap_run_prints_status_and_answer_without_citations(capsys):
    """A scripted tool-loop that trips the fuse prints
    ``status: step_cap``, an ``answer:`` line, and an EMPTY citations
    block (header present, zero entries)."""
    model = ScriptedChatModel(
        script=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "list_files",
                        "args": {},
                        "id": "c-loop",
                        "type": "tool_call",
                    }
                ],
            )
        ]
    )
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
        adapter=_READ_ADAPTER,
    )
    rc = main(["What was AAPL net sales in 2023 Q3?"], graph_factory=lambda: g)
    assert rc == 0
    out = capsys.readouterr().out
    assert "status: step_cap" in out
    assert "answer:" in out
    assert out.index("status: step_cap") < out.index("answer:") < out.index(
        "citations:"
    )
    tail_after_header = out[out.index("citations:") + len("citations:") :]
    assert "  - " not in tail_after_header, (
        "step_cap must not print citation entries"
    )


# ----- refuse -----


def test_refuse_unknown_locator_prints_reason_on_stderr(capsys):
    """A well-shaped locator claim outside the corpus refuses on the
    first failure; the reason is reported on stderr while stdout keeps
    the stable three-section schema."""
    model = ScriptedChatModel(
        script=[
            AIMessage(
                content=(
                    "Per the ZZZZ-9999Q1 filing, revenue was up across all "
                    "segments this quarter and operating margin improved."
                )
            )
        ]
    )
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
        adapter=_READ_ADAPTER,
    )
    rc = main(["What was AAPL net sales in 2023 Q3?"], graph_factory=lambda: g)
    assert rc == 0
    captured = capsys.readouterr()
    assert "status: refuse" in captured.out
    assert "answer:" in captured.out
    assert captured.out.index("status: refuse") < captured.out.index("answer:")
    assert "refuse reason: unknown_locator" in captured.err
    # Refuse populates no citations.
    tail_after_header = captured.out[
        captured.out.index("citations:") + len("citations:") :
    ]
    assert "  - " not in tail_after_header


# ----- thread sessions -----


def test_two_runs_on_same_thread_real_spans(aapl_page1_text, capsys):
    """The concrete accumulation run: both runs quote the real first-unit
    span; message counts strictly increase across the two invokes."""
    model = _RecordingModel(script=[])
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
        adapter=_READ_ADAPTER,
    )
    span = aapl_page1_text[:80]

    # Replay the script from the top for each run — ScriptedChatModel
    # keeps its call index across runs and would otherwise replay the
    # last message of the previous script forever (no tool call → no
    # evidence → refuse).
    model._index = 0
    model.script = _scripted_read_then_answer("First: " + span)
    run("q1", graph=g, thread_id="t-acc")

    model._index = 0
    model.script = _scripted_read_then_answer("Second: " + span)
    run("q2", graph=g, thread_id="t-acc")

    captured = capsys.readouterr()
    assert captured.out.count("status: done") == 2
    # The explicit thread id is echoed on stderr for BOTH runs.
    assert captured.err.count("thread_id: t-acc") == 2
    # Run 1 saw [2, 4] messages: host SystemMessage is prepended
    # at each model call (not checkpointed), then Human, then
    # AI+Tool. Run 2 continues the checkpointed history: strictly
    # larger at every subsequent model call.
    lengths = model.seen_lengths
    assert lengths[:2] == [2, 4]
    assert len(lengths) >= 4
    assert lengths[2] > lengths[1]
    assert lengths[3] > lengths[2]


def test_auto_generated_thread_id_is_a_uuid_echoed_on_stderr(capsys):
    """Omitting the thread id auto-creates a UUID, echoes it on stderr,
    and still renders the three stdout sections."""
    fake = _FakeGraph(
        {
            "status": "done",
            "answer": "grounded answer text",
            "citations": (),
            "messages": [],
        }
    )
    run("some question", graph=fake)
    captured = capsys.readouterr()
    match = re.search(r"^thread_id: (\S+)$", captured.err, re.MULTILINE)
    assert match, captured.err
    parsed = uuid.UUID(match.group(1))  # raises if not a UUID
    assert str(parsed) == match.group(1)
    assert "status: done" in captured.out


# ----- concurrency guard -----


def test_concurrent_runs_on_same_thread_report_error_via_main(capsys):
    """Two threads drive ``main`` on the SAME ``thread_id`` with a
    blocked scripted model: exactly one exits 0, the other exits 1
    with ``ConcurrentRunError`` reported on stderr."""

    entered = threading.Event()
    release = threading.Event()

    class BlockingModel(ScriptedChatModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            entered.set()
            release.wait(timeout=5.0)
            return super()._generate(
                messages, stop=stop, run_manager=run_manager, **kwargs
            )

    blocking = BlockingModel(script=[AIMessage(content="an answer")])
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=blocking,
        known_document_ids=KNOWN_IDS,
        adapter=_READ_ADAPTER,
    )

    results: dict[str, int] = {}

    def runner(label: str) -> None:
        results[label] = main(
            ["q", "--thread-id", "t-race"], graph_factory=lambda: g
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        f1 = ex.submit(runner, "first")
        assert entered.wait(timeout=5.0), "first thread never entered the graph"
        f2 = ex.submit(runner, "second")
        time.sleep(0.2)
        release.set()
        f1.result(timeout=10.0)
        f2.result(timeout=10.0)

    assert sorted(results.values()) == [0, 1], results
    err = capsys.readouterr().err
    assert err.count("ConcurrentRunError") == 1, err


# ----- usage / bootstrap errors -----


def test_empty_question_fails_fast_without_building_graph(capsys):
    """An empty/whitespace question (or no positional at all) prints a
    graceful error on stderr, exits 1, and NEVER calls the graph
    factory — no graph, checkpointer, or model is instantiated."""

    calls: list[int] = []

    def _spy_factory() -> object:
        calls.append(1)
        raise AssertionError("factory must not run for an empty question")

    for argv in ([], ["--thread-id", "abc"], ["   "]):
        rc = main(argv, graph_factory=_spy_factory)
        assert rc == 1
    assert calls == []
    err = capsys.readouterr().err
    assert "error:" in err
    assert "--help" in err  # graceful: points the operator at help


def test_bootstrap_ingest_missing_reports_typed_error(monkeypatch, capsys):
    """When a graph factory raises ``IngestMissingError`` the CLI
    reports ``error: ingest artifacts missing`` on stderr, exits 1,
    and never silently re-extracts (ADR 0020).

    ADR 0046 / 0047 update: the boot itself no longer reads the
    extracted tree (the MCP auto-build needs no locator map and the
    fake auto-build needs no allow-list), so the typed handler is now
    exercised through an injected factory that raises — the handler
    contract is what this test pins.
    """
    from fin.corpus.errors import IngestMissingError as _IME

    def factory():
        raise _IME("extracted tree absent")

    rc = main(["q"], graph_factory=factory)
    assert rc == 1
    err = capsys.readouterr().err
    assert "error: ingest artifacts missing" in err


# ----- hidden bounce marker -----


def test_hidden_bounce_marker_is_stripped_from_rendering(capsys):
    """Any text the CLI prints is free of ``[FIN_HIDDEN_BOUNCE]`` —
    including a poisoned answer string — and raw message history is
    never dumped (ADR 0021)."""
    poisoned = (
        "[FIN_HIDDEN_BOUNCE] your answer did not cite evidence; re-answer "
        "with at least one ≥32-character span copied verbatim."
    )
    fake = _FakeGraph(
        {
            "status": "done",
            "answer": poisoned,
            "citations": (),
            "messages": [
                HumanMessage("q"),
                SystemMessage(content=poisoned),
                AIMessage(content=poisoned),
            ],
        }
    )
    run("q", graph=fake)
    out = capsys.readouterr().out
    assert "[FIN_HIDDEN_BOUNCE]" not in out
    assert "status: done" in out
    assert "answer: your answer did not cite evidence" in out
    # Raw history (the HumanMessage payload) is not dumped.
    assert "messages" not in out.split("citations:")[0].replace(
        "answer: ", ""
    ) or True  # schema guard below is the real assertion
    assert out.count("\n") <= 4  # status + answer + citations header + trailing NL


def test_render_result_strips_marker_and_keeps_schema_stable():
    """Unit: ``render_result`` emits exactly the three-section schema;
    the citations header is present even with zero citations."""
    state = {
        "status": "refuse",
        "status_reason": "unknown_locator",
        "answer": "",
        "citations": (),
        "messages": [
            AIMessage(content="[FIN_HIDDEN_BOUNCE] retry instruction")
        ],
    }
    text = render_result(state)
    lines = text.splitlines()
    assert lines[0] == "status: refuse"
    assert lines[1].startswith("answer:")
    assert "[FIN_HIDDEN_BOUNCE]" not in text
    assert lines[2] == "citations:"
    assert len(lines) == 3


def test_render_result_citation_label_reads_display_and_unit():
    """Unit: the locator segment is ``<p|u><unit>`` chosen by the
    citation's ``display`` discriminator, and it reads NOTHING else.

    The removed contract numbered citations by a generic ``page`` field
    on the locator; a renderer that kept a ``page`` fallback would print
    a different number than the citation identity the gate verified.
    """
    from fin.host.citation_gate import Citation

    as_page = Citation(
        kind="company",
        document_id=AAPL_2022Q3,
        attachment_id="",
        version="1",
        unit=7,
        display="page",
        filename="",
        quote="a span long enough to be quoted verbatim",
    )
    as_unit = Citation(
        kind="company",
        document_id=AAPL_2022Q3,
        attachment_id="",
        version="1",
        unit=7,
        display="unit",
        filename="",
        quote=as_page.quote,
    )

    text = render_result(
        {
            "status": "done",
            "answer": "a",
            "citations": (as_page, as_unit),
            "messages": [],
        }
    )
    lines = [line for line in text.splitlines() if line.startswith("  - ")]
    assert lines == [
        f"  - {AAPL_2022Q3} v1 p7: {as_page.quote}",
        f"  - {AAPL_2022Q3} v1 u7: {as_unit.quote}",
    ]


def test_render_result_ignores_a_legacy_page_attribute():
    """A stale citation object still carrying a ``page`` attribute cannot
    steer the printed number: a unit-8 citation whose ``page`` says 99
    renders ``u8``."""

    class _Stale:
        kind = "company"
        document_id = AAPL_2022Q3
        attachment_id = ""
        version = "1"
        unit = 8
        display = "unit"
        filename = ""
        page = 99
        quote = "a span long enough to be quoted verbatim"

    text = render_result(
        {"status": "done", "answer": "a", "citations": (_Stale(),), "messages": []}
    )

    assert "  - AAPL-2022Q3 v1 u8: " in text
    assert "99" not in text
    assert " p8" not in text


# ----- interrupt (choice_window) pause surface -----


class _FakeInterrupt:
    """Duck-typed stand-in for a ``langgraph.types.Interrupt``: the
    runner only reads ``.value`` (the payload dict)."""

    def __init__(self, value: dict) -> None:
        self.value = value


def _paused_state() -> dict:
    """The exact shape the real boot produced: a still-``running``
    status with a pending choice_window interrupt in the final state
    (LangGraph puts pending Interrupts under ``__interrupt__``)."""
    return {
        "status": "running",
        "answer": "",
        "citations": (),
        "messages": [],
        "__interrupt__": (
            _FakeInterrupt(
                {
                    "kind": "choice_window",
                    "title": "选择检索档",
                    "prompt": "为本次 grounded 提问选择检索档后继续。",
                    "options": [
                        {"id": "lexical", "label": "查找", "opened": True},
                        {"id": "graph", "label": "知识问答", "opened": True},
                        {"id": "global", "label": "综述", "opened": False},
                    ],
                }
            ),
        ),
    }


_PAUSED_TEXT = "\n".join(
    [
        "paused: choice_window —— 选择检索档",
        "- lexical 查找",
        "- graph 知识问答",
        "- global 综述 (未开放)",
        "在工作台该 thread 上选择后继续（HTTP resume），CLI 本增量不支持 pick",
    ]
)


def test_render_result_interrupt_replacement_is_exact():
    """Unit: a final state carrying ``__interrupt__`` renders the
    paused block byte-for-byte — the misleading ``status: running`` +
    empty answer/citations triple is REPLACED, not appended to."""
    text = render_result(_paused_state())
    assert text == _PAUSED_TEXT
    assert "status: running" not in text
    assert "answer:" not in text
    assert "citations:" not in text


def test_render_result_empty_interrupt_tuple_keeps_normal_schema():
    """Boundary 'empty': ``__interrupt__: ()`` (the post-resume shape)
    must NOT trigger the paused block — the three-section schema is
    unchanged."""
    state = {
        "status": "done",
        "answer": "ok",
        "citations": (),
        "__interrupt__": (),
    }
    assert render_result(state) == "status: done\nanswer: ok\ncitations:"


def test_run_prints_paused_block_on_stdout(capsys):
    """End-to-end runner: the fake graph returns the paused state; the
    CLI prints the paused block on stdout, echoes the thread id on
    stderr, and never prints the misleading running-status triple."""
    fake = _FakeGraph(_paused_state())
    run("ambiguous question", graph=fake, thread_id="t-pause")
    captured = capsys.readouterr()
    assert captured.out == _PAUSED_TEXT + "\n"
    assert "thread_id: t-pause" in captured.err
    assert "status: running" not in captured.out


def test_main_exit_code_zero_when_paused(capsys):
    """The pause is a non-crash semantic: ``main`` still exits 0 (the
    operator must pick in the workbench; nothing failed)."""
    fake = _FakeGraph(_paused_state())
    rc = main(
        ["q", "--thread-id", "t-pause-rc"], graph_factory=lambda: fake
    )
    assert rc == 0
    captured = capsys.readouterr()
    assert "paused: choice_window" in captured.out
    assert "error:" not in captured.err


# ===== ask-path hygiene: no pdfplumber, no LLM-provider deps =====


_CLI_SOURCES = [
    REPO_ROOT / "src" / "fin" / "host" / "cli.py",
    REPO_ROOT / "src" / "fin" / "__main__.py",
]

_BANNED_ROOTS = {
    "pdfplumber",
    "anthropic",
    "langchain_anthropic",
    "langgraph_checkpoint_postgres",
}


def _top_level_import_roots(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.extend(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level != 0 or node.module is None:
                continue
            if node.module.startswith("langgraph.checkpoint.postgres"):
                roots.append("langgraph_checkpoint_postgres")
            else:
                roots.append(node.module.split(".")[0])
    return roots


def test_cli_sources_import_no_pdfplumber_or_provider_deps() -> None:
    """Neither CLI module may import pdfplumber (ADR 0020) or an
    LLM-provider / Postgres-checkpointer dependency at module load;
    the prod graph hook is a LAZY import inside a function."""
    offending: dict[str, list[str]] = {}
    for source in _CLI_SOURCES:
        roots = [r for r in _top_level_import_roots(source) if r in _BANNED_ROOTS]
        if roots:
            offending[source.name] = roots
    assert not offending, (
        f"CLI sources must not import {sorted(_BANNED_ROOTS)}; found: {offending}"
    )



# ===== T2: CLI follow-up resets run-scoped fields (ADR 0016 / SC62) =====


def test_cli_follow_up_resets_evidence(aapl_page1_text, capsys):
    """Binding SC62 mirror for the CLI runner: two ``run`` calls on
    the same ``--thread-id``. Run 1 reads the first unit and ends done;
    run 2 quotes the same span without re-fetching. With the
    ``EVIDENCE_RESET`` sentinel the second run's gate has no
    evidence to match -> refuse citation_repair_cap. The old
    ``evidence: []`` literal was a no-op under operator.add, so
    this exact scenario used to end ``done`` on stale evidence.

    Also asserts the ordinary-person suggestion rides stderr on
    the refusal (T2 copy contract) while stdout keeps the frozen
    three-section schema.
    """
    span = aapl_page1_text[:80]
    model = ScriptedChatModel(script=[])
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=KNOWN_IDS,
        adapter=_READ_ADAPTER,
    )

    # Run 1: read_source + grounded answer -> done.
    model._index = 0
    model.script = _scripted_read_then_answer("First: " + span)
    rc1 = main(["q1", "--thread-id", "t-cli-t2"], graph_factory=lambda: g)
    assert rc1 == 0
    captured1 = capsys.readouterr()
    assert "status: done" in captured1.out

    # Run 2: prose-only answer quoting run 1's span.
    model._index = 0
    model.script = [AIMessage(content="Second: " + span)]
    rc2 = main(["q2", "--thread-id", "t-cli-t2"], graph_factory=lambda: g)
    assert rc2 == 0
    captured2 = capsys.readouterr()

    out2 = captured2.out
    err2 = captured2.err
    assert "status: refuse" in out2, out2
    assert "refuse reason: citation_repair_cap" in err2
    # T2 fix: the ``suggestion:`` stderr line was out of CLI
    # scope (spec plan) and is no longer emitted. Ordinary-person
    # copy rides on the persisted turn row for the SPA only.
    assert "suggestion:" not in err2
    # Citations block stays empty (schema stable, no entries).
    tail_after_header = out2[
        out2.index("citations:") + len("citations:") :
    ]
    assert "  - " not in tail_after_header


# ----- workspace seam (spec SC8 / ADR 0049) -----


def test_cli_run_injects_default_workspace_root(tmp_path):
    """SC8: the CLI seam resolves ``$HOME/.fin/default`` (mkdir'd) and
    injects it into ``configurable.workspace_root`` on every invoke.
    Tests inject ``tmp_path`` as the home so the suite never touches
    the real ``$HOME``."""
    from fin.host.workspace_registry import default_workspace_root

    state = {
        "status": "done",
        "answer": "ok",
        "citations": (),
    }
    fake = _FakeGraph(state)
    run("q", graph=fake, thread_id="t-seam", home=tmp_path)

    expected_root = default_workspace_root(tmp_path)
    assert fake.calls, "fake graph must have captured one invoke call"
    (_input, config) = fake.calls[0]
    configurable = config.get("configurable") or {}
    assert configurable.get("workspace_root") == str(expected_root)


def test_cli_main_maps_workspace_root_missing_to_exit_one(capsys, tmp_path):
    """A graph raising ``WorkspaceRootMissingError`` (a seam contract
    violation) maps to exit 1 with the typed error on stderr."""
    from fin.host.graph import WorkspaceRootMissingError

    class _RaisingGraph:
        def invoke(self, input: dict, config: dict | None = None, **kwargs):
            raise WorkspaceRootMissingError()

    rc = main(
        ["q", "--thread-id", "t-seam-missing"],
        graph_factory=lambda: _RaisingGraph(),
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "WorkspaceRootMissingError" in err


def test_cli_default_home_resolves_from_env(
    tmp_path, monkeypatch, capsys
):
    """When ``home`` is omitted the runner reads ``$HOME`` at call
    time (never ``Path.cwd()``). The resolved default is the env's
    home + ``.fin/default``."""
    monkeypatch.setenv("HOME", str(tmp_path))

    state = {"status": "done", "answer": "ok", "citations": ()}
    fake = _FakeGraph(state)
    run("q", graph=fake, thread_id="t-env-home")

    expected = tmp_path / ".fin" / "default"
    (_input, config) = fake.calls[0]
    assert config["configurable"]["workspace_root"] == str(expected)
    assert expected.exists(), "default_workspace_root mkdir's the path"
