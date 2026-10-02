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
      - <source> v<version> <p|u><unit>: <quote>

The ``citations:`` header is ALWAYS printed (stable schema for any
operator-grade parser). Entries appear only when ``status == "done"``;
for ``refuse`` / ``step_cap`` the block is empty. ``<source>`` is the
company ``document_id`` or the attachment's filename, and the unit
prefix follows the citation's ``display``. The CLI strips the
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
import os
import sys
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable

from langchain_core.messages import AIMessage, HumanMessage

from fin.corpus.errors import IngestMissingError
from fin.host import ConcurrentRunError, WorkspaceRootMissingError
from fin.host.citation_gate import strip_hidden_bounce_marker
from fin.host.operator_env import apply_dotenv
from fin.host.run_payload import ordinary_person_copy
from fin.host.state import EVIDENCE_RESET, reset_input_state
from fin.host.workspace_registry import default_workspace_root


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


def _citation_source_label(citation) -> str:
    """The label one citation line opens with.

    A company citation names its ``document_id``; an attachment citation
    has none, so it names the safe filename and falls back to the logical
    ``attachment_id`` — the identity that stays unique when two uploads
    share a filename. A ledger row has neither, so it names the permitted
    view plus the record key (ADR 0091), and a kind this CLI does not
    know names itself rather than falling through to the company slot.
    """
    if citation.kind == "attachment":
        return citation.filename or citation.attachment_id
    if citation.kind == "database-row":
        return citation.database_source_id or citation.kind
    if citation.kind == "company":
        return citation.document_id
    return str(citation.kind)


def _citation_unit_label(citation) -> str:
    """``p<unit>`` for a unit presented as a page, else ``u<unit>``.

    A ledger row is neither: a record has no page and no ordinal, so it
    reads as ``record`` rather than ``u1`` — a position in a document
    that does not exist.
    """
    if citation.display == "record":
        return "record"
    prefix = "p" if citation.display == "page" else "u"
    return f"{prefix}{citation.unit}"


def _format_citation_line(citation) -> str:
    """Render one Citation as ``  - <source> v<version> <unit>: <quote>``.

    Newlines inside the quote are collapsed to single spaces so the
    "one citation per line" invariant holds even when an extractive
    span crosses a line break in the source unit text. The span is
    still findable modulo whitespace; we do not rewrite any other
    characters.
    """
    quote = citation.quote
    if "\n" in quote:
        quote = " ".join(quote.splitlines())
    quote = strip_hidden_bounce_marker(quote)
    return (
        f"  - {_citation_source_label(citation)}"
        f" v{citation.version} {_citation_unit_label(citation)}: {quote}"
    )


def _pending_interrupt(state: dict[str, Any]) -> dict[str, Any] | None:
    """The first pending Interrupt payload, or ``None``.

    LangGraph parks an ``interrupt()`` call by sticking a tuple of
    ``Interrupt`` objects under ``__interrupt__`` in the invoke
    result; ``.value`` is the payload the node passed in (here a
    ``choice_window`` dict). Same read shape as the T2 graph tests.
    """
    interrupts = state.get("__interrupt__") or ()
    if not interrupts:
        return None
    value = getattr(interrupts[0], "value", None)
    return value if isinstance(value, dict) else {}


def _render_paused(payload: dict[str, Any]) -> str:
    """The paused-run surface that REPLACES the status/answer/citations
    triple. The triple would be actively misleading here (``status:
    running`` + empty answer), so the operator gets the window title,
    the pickable options, and where to pick instead."""
    kind = payload.get("kind", "unknown")
    title = payload.get("title", "")
    lines = [f"paused: {kind} —— {title}"]
    for option in payload.get("options") or ():
        if not isinstance(option, dict):
            continue
        suffix = "" if option.get("opened") else " (未开放)"
        lines.append(f"- {option.get('id', '')} {option.get('label', '')}{suffix}")
    lines.append(
        "在工作台该 thread 上选择后继续（HTTP resume），CLI 本增量不支持 pick"
    )
    return "\n".join(lines)


def render_result(state: dict[str, Any]) -> str:
    """Render the canonical three-section stdout from a graph result.

    Pure function — no I/O — so callers (and tests) can capture the
    output string directly. The schema is stable across terminal
    statuses: the ``citations:`` header is always present, even when
    the run was ``refuse`` / ``step_cap``. A run that is still parked
    on an interrupt is NOT terminal: the paused block replaces the
    triple (see ``_render_paused``).
    """
    payload = _pending_interrupt(state)
    if payload is not None:
        return _render_paused(payload)
    status = state.get("status", "")
    answer = _derive_answer(state)
    # The ``reset_input_state`` helper at invoke time clears
    # ``citations`` (along with ``answer`` / ``status`` / evidence /
    # caps) so a follow-up refusal cannot surface the prior run's
    # citation lines. The ``citations:`` header stays in the
    # schema for stability; entries appear only when
    # ``status == "done"``.
    raw_citations = state.get("citations") or ()
    citations = raw_citations if status == "done" else ()
    lines = [
        f"status: {status}",
        f"answer: {answer}",
        "citations:",
    ]
    lines.extend(_format_citation_line(c) for c in citations)
    return "\n".join(lines)


# ----- the runner -----


def run(
    question: str,
    *,
    graph: Any,
    thread_id: str | None = None,
    home: str | Path | None = None,
) -> None:
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
    home
        Injected home directory for resolving the default 工作区
        (``$HOME/.fin/default``). ``None`` (the default) reads
        ``$HOME`` at call time. Tests pass an explicit ``tmp_path``
        so the suite never touches the real home. spec Focus 3 / SC8:
        the CLI is the workspace-inject seam for the ask path.

    Stdout sections (in order): ``status:``, ``answer:``, ``citations:``.
    Stderr: ``thread_id: <value>`` always; for ``refuse`` also
    ``refuse reason: <status_reason>``. When the run is still parked
    on an interrupt (``__interrupt__`` pending), the paused block
    replaces the three stdout sections and the exit code stays 0.

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

    resolved_home = home if home is not None else (
        os.environ.get("HOME") or str(Path.home())
    )
    workspace_root = default_workspace_root(resolved_home)
    config: dict[str, Any] = {
        "configurable": {
            "thread_id": thread_id,
            "workspace_root": str(workspace_root),
        }
    }
    # T2: explicit reset via the ``reset_input_state`` helper. The
    # previous ``evidence: []`` literal was a no-op under the old
    # ``operator.add`` reducer (``[1,2,3] + [] == [1,2,3]``), so a
    # CLI follow-up on the same thread inherited the prior run's
    # evidence and could ``done`` on stale pages. The helper clears
    # all six run-scoped fields per ADR 0016 / spec line 34.
    result = graph.invoke(reset_input_state(question), config=config)
    print(render_result(result))

    status = result.get("status")
    # T2: refuse / step_cap carry their ordinary-person copy on the
    # ``assistant_message`` field of the persisted turn row (the
    # SPA renders it). The CLI stdout schema is frozen; stderr
    # surfaces only ``refuse reason: ...`` for operational
    # diagnosis. No additional ``suggestion:`` line is printed
    # (T2 fix: that drift was out of scope).
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
    checkpointer (ADR 0013 + ADR 0015). A local ``.env`` is applied
    here (not at argparse) so ``--help`` stays import-light.
    """
    apply_dotenv()
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
    except WorkspaceRootMissingError as exc:
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
