"""CLI surface for the v1 host agent (T7).

Two pieces of surface live here:

* ``run(question, *, graph, thread_id=None) -> None`` — the pure
  runner. It builds the host-graph input dict, invokes the compiled
  graph, and prints the canonical three-section stdout schema plus a
  stderr thread-id echo. Pure: no global state, no I/O outside
  ``stdout`` / ``stderr``; ``ConcurrentRunError`` is the only typed
  error that escapes (the caller — typically ``main`` — decides the
  exit code).
* ``main(argv=None, *, graph_factory=_default_graph_factory) -> int``
  — argparse plumbing + lazy prod-graph resolution + typed-error →
  exit-code mapping. ``python -m fin`` routes here; ``__main__.py``
  is the thin shim that does ``sys.exit(main())``.

The three-section stdout schema is::

    status: <status>
    answer: <answer text>
    citations:
      - <document_id> v<version> p<page>: <quote>

The ``citations:`` header is ALWAYS printed (stable schema for any
operator-grade parser). Entries appear only when ``status == "done"``;
for ``refuse`` / ``step_cap`` the block is empty. The CLI strips the
``[FIN_HIDDEN_BOUNCE]`` marker (ADR 0021) from any text it prints
before sending bytes to the user surface.

``thread_id`` is a session (ADR 0016): omitting it auto-creates a
UUID4, echoed on stderr so the operator can reuse it. Passing
``--thread-id`` reuses the value verbatim. ``ConcurrentRunError``
on a second concurrent ``invoke`` against the same thread is caught
in ``main`` and reported on stderr with exit code 1 (no graph /
LLM is built twice).

Ask-path hygiene: this module imports NO PDF-extraction library,
no LLM provider SDK, and no Postgres-checkpointer package at module
load — the prod graph hook is a lazy import inside
``_default_graph_factory`` so the ``python -m fin --help`` path
instantiates nothing. The hard import-roots blacklist lives in the
CLI test suite.
"""

from __future__ import annotations

import argparse
import sys
import uuid
from typing import Any, Callable, Iterable

from langchain_core.messages import AIMessage, HumanMessage

from fin.corpus.errors import IngestMissingError
from fin.host import ConcurrentRunError
from fin.host.citation_gate import strip_hidden_bounce_marker


# ----- typed errors -----


class CliError(Exception):
    """Base class for CLI-typed errors."""


class RuntimeGraphUnavailableError(CliError):
    """The production graph compiler (T8's
    ``fin.host.compile.compile_for_runtime``) is not installed.

    ``main`` catches this and prints ``error: ...`` to stderr with
    exit code 1, so the operator can distinguish "we don't have T8
    yet" from a generic bootstrap failure.
    """


# ----- pure helpers -----


def _derive_answer(state: dict[str, Any]) -> str:
    """Pick the answer text the CLI prints on the ``answer:`` line.

    On ``done`` the gate's cleaned prose lives on ``state["answer"]``.
    On ``refuse`` / ``step_cap`` no answer is populated, so we fall
    back to the last ``AIMessage`` content in the message history
    (marker-stripped). Both paths yield a ``str`` even when the
    content is structurally empty — the schema is stable.
    """
    answer = state.get("answer")
    if isinstance(answer, str) and answer:
        return strip_hidden_bounce_marker(answer)
    messages = state.get("messages", ())
    for message in reversed(list(messages)):
        if isinstance(message, AIMessage):
            content = message.content
            text = (
                content
                if isinstance(content, str)
                else "".join(
                    block.get("text", "") if isinstance(block, dict) else str(block)
                    for block in content
                )
            )
            return strip_hidden_bounce_marker(text)
    return ""


def _format_citation_line(citation) -> str:
    """Render one Citation as ``  - <document_id> v<version> p<page>: <quote>``.

    Newlines inside the quote are collapsed to single spaces so the
    "one citation per line" invariant holds even when an extractive
    span crosses a line break in the source page text. The span is
    still findable modulo whitespace; we do not rewrite any other
    characters.
    """
    quote = citation.quote
    if "\n" in quote:
        quote = " ".join(quote.splitlines())
    quote = strip_hidden_bounce_marker(quote)
    return f"  - {citation.document_id} v{citation.version} p{citation.page}: {quote}"


def render_result(state: dict[str, Any]) -> str:
    """Render the canonical three-section stdout from a graph result.

    Pure function — no I/O — so callers (and tests) can capture the
    output string directly. The schema is stable across terminal
    statuses: the ``citations:`` header is always present, even when
    the run was ``refuse`` / ``step_cap``.
    """
    status = state.get("status", "")
    answer = _derive_answer(state)
    citations: Iterable = state.get("citations") or ()
    lines = [
        f"status: {status}",
        f"answer: {answer}",
        "citations:",
    ]
    lines.extend(_format_citation_line(c) for c in citations)
    return "\n".join(lines)


# ----- the runner -----


def run(question: str, *, graph: Any, thread_id: str | None = None) -> None:
    """Drive one host-graph invoke and print the result to stdout.

    Parameters
    ----------
    question
        The user-supplied question. Must be non-empty after
        ``strip()`` — ``main`` enforces this before constructing the
        graph; calling ``run`` directly with an empty string passes
        through to the graph (the gate will bounce).
    graph
        The compiled graph object — typically the
        ``_LockedGraph`` proxy returned by ``compile_graph``. May be
        any object exposing ``invoke(input, config)`` (duck typed;
        the contract is documented at ``fin.host.graph``).
    thread_id
        Session id; auto-created as a UUID4 when ``None``. The chosen
        value is echoed on stderr.

    Stdout sections (in order): ``status:``, ``answer:``, ``citations:``.
    Stderr: ``thread_id: <value>`` always; for ``refuse`` also
    ``refuse reason: <status_reason>``.

    Concurrent-run guard
    --------------------
    ``ConcurrentRunError`` from ``graph.invoke`` propagates; ``main``
    catches it and reports on stderr with exit 1. Tests that drive
    ``run`` directly and want exit-code mapping must wrap their own
    try/except.
    """
    if not thread_id:
        thread_id = str(uuid.uuid4())
    print(f"thread_id: {thread_id}", file=sys.stderr)

    config: dict[str, Any] = {"configurable": {"thread_id": thread_id}}
    input_state: dict[str, Any] = {
        "messages": [HumanMessage(content=question)],
        "evidence": [],
        "repair_count": 0,
        "step_count": 0,
    }
    result = graph.invoke(input_state, config=config)
    print(render_result(result))

    status = result.get("status")
    if status == "refuse":
        reason = result.get("status_reason", "")
        if reason:
            print(f"refuse reason: {reason}", file=sys.stderr)


# ----- argparse -----


def _build_parser() -> argparse.ArgumentParser:
    """Build the ``python -m fin`` argument parser.

    No LLM, graph, or checkpointer is imported here — only ``argparse``
    and stdlib. The ``--help`` path is a pure argparse round-trip.
    """
    parser = argparse.ArgumentParser(
        prog="fin",
        description=(
            "Ask the v1 research agent one question. Stdout prints the "
            "three-section schema (status, answer, citations). Stderr "
            "carries the thread id and refuse reason."
        ),
    )
    parser.add_argument(
        "question",
        nargs="?",
        default=None,
        help=(
            "The question to ask. Omit (or pass an empty string) to "
            "show this help and exit 1."
        ),
    )
    parser.add_argument(
        "--thread-id",
        default=None,
        help=(
            "Conversation thread id. Omit to auto-create a UUID "
            "(echoed on stderr). Reuse the value to continue a prior "
            "thread."
        ),
    )
    return parser


# ----- prod graph resolution (lazy) -----


def _default_graph_factory() -> Any:
    """Resolve the production compiled graph (T8 provides this).

    The import is LAZY — neither LLM-provider nor Postgres-checkpointer
    modules are touched unless this function is actually called. The
    ``--help`` path therefore never instantiates an LLM, a graph, or a
    checkpointer (ADR 0013 + ADR 0015).
    """
    try:
        from fin.host.compile import compile_for_runtime  # type: ignore[import-not-found]
    except ModuleNotFoundError as exc:
        raise RuntimeGraphUnavailableError(
            "production graph compiler not available: "
            "fin.host.compile.compile_for_runtime (ticket T8) is "
            "required to build the durable graph"
        ) from exc
    return compile_for_runtime()


# ----- main -----


def main(
    argv: list[str] | None = None,
    *,
    graph_factory: Callable[[], Any] | None = None,
) -> int:
    """Parse args, resolve the prod graph, drive one ``run``.

    Returns the intended process exit code:

    * ``0`` — terminal status reached (``done`` / ``refuse`` /
      ``step_cap``). The ask succeeded in producing a bounded outcome;
      the answer and citations are on stdout, refuse reasons on
      stderr.
    * ``1`` — typed failure (empty question, missing prod compile
      hook, missing extracted tree, or a concurrent-run guard on the
      requested thread). The error is on stderr.

    ``--help`` / argparse usage errors raise ``SystemExit`` directly
    (argparse convention); this function does not catch them.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    question = (args.question or "").strip()
    if not question:
        print(
            "error: a question is required (pass it positionally, or "
            "see --help for usage)",
            file=sys.stderr,
        )
        return 1

    factory = graph_factory or _default_graph_factory
    try:
        graph = factory()
    except RuntimeGraphUnavailableError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except IngestMissingError:
        print("error: ingest artifacts missing", file=sys.stderr)
        return 1

    try:
        run(question, graph=graph, thread_id=args.thread_id)
    except ConcurrentRunError as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    return 0


__all__ = [
    "CliError",
    "RuntimeGraphUnavailableError",
    "main",
    "render_result",
    "run",
    "strip_hidden_bounce_marker",
]
