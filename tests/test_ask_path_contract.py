"""Structural guards for the v1 ask-path no-PDF contract (ADR 0020).

The contract: any code on the ask path (the CLI / HTTP / graph / gate /
host-tool modules the operator invokes per question) MUST NOT pull
``pdfplumber`` into ``sys.modules``. The PDF-extraction library is
allowed ONLY inside ``fin.corpus.ingest`` (the offline ingest command).
A previous v1 review found a transitive leak via
``fin.corpus.__init__`` re-exporting ``run_ingest``; the fix removed
that re-export. These tests guard the regression.

A regression here would mean: importing any module that the operator
hits on the ask path transitively loads pdfplumber. That breaks the
spec's success criteria item 3 ("Query/runtime code has no pdfplumber
(or PDF) import on the ask path; missing extracted artifacts fail
fast with a typed ingest-missing error").

Note: this test runs in a subprocess so the state of ``sys.modules``
is isolated from the rest of the test suite. Other tests
(``tests/corpus/test_ingest.py``) import ``fin.corpus.ingest`` which
loads pdfplumber; if we shared the parent interpreter, those modules
would already be in ``sys.modules`` and the guard would always pass
(no useful signal). The subprocess gives a clean ``sys.modules`` per
invocation.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest


_ASK_PATH_MODULES = (
    "fin",
    "fin.host",
    "fin.host.grounded.citation_gate",
    "fin.host.cli",
    "fin.host.compile",
    "fin.host.graph",
    "fin.host.http",
    "fin.host.state",
    "fin.host.tools",
    "fin.retrieve",
)


def _run_in_subprocess(snippet: str) -> str:
    """Run ``snippet`` in a fresh CPython subprocess; return its stdout.

    ``sys.executable`` is the same interpreter pytest is using, so the
    subprocess sees the same installed deps (``.venv``) but a fresh
    ``sys.modules``.
    """
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(snippet)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"subprocess failed (rc={result.returncode}):\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    return result.stdout


def test_no_pdfplumber_after_importing_ask_path_modules() -> None:
    """The ask-path modules MUST NOT transitively load pdfplumber."""
    snippet = f"""
        import sys
        import json
        for name in {list(_ASK_PATH_MODULES)!r}:
            __import__(name)
        leaked = sorted(
            m for m in sys.modules
            if m == 'pdfplumber' or m.startswith('pdfplumber.')
        )
        sys.stdout.write(json.dumps(leaked))
    """
    leaked = _run_in_subprocess(snippet)
    import json
    leaked_list = json.loads(leaked)
    assert leaked_list == [], (
        f"pdfplumber leaked into the ask path via {leaked_list[:5]}"
        f"{'...' if len(leaked_list) > 5 else ''}. "
        "Move any pdfplumber import inside fin.corpus.ingest only."
    )


def test_fin_corpus_does_not_re_export_run_ingest() -> None:
    """``fin.corpus`` re-exports must NOT include ``run_ingest``.

    Importing ``run_ingest`` pulls pdfplumber; the ask path must never
    reach it. Callers that need ingest use
    ``fin.corpus.ingest.run_ingest`` directly (operator-only).
    """
    snippet = """
        import sys
        import fin.corpus
        import json
        sys.stdout.write(json.dumps(list(fin.corpus.__all__)))
    """
    import json
    exports = json.loads(_run_in_subprocess(snippet))
    assert "run_ingest" not in exports, (
        f"fin.corpus re-exports run_ingest ({exports!r}); this "
        "transitively loads pdfplumber on every `import fin.corpus`. "
        "Remove from __all__."
    )


@pytest.mark.parametrize("module_name", list(_ASK_PATH_MODULES))
def test_individual_ask_path_module_does_not_load_pdfplumber(
    module_name: str,
) -> None:
    """Each ask-path module individually must not transitively load pdfplumber."""
    snippet = f"""
        import sys
        import json
        __import__({module_name!r})
        leaked = sorted(
            m for m in sys.modules
            if m == 'pdfplumber' or m.startswith('pdfplumber.')
        )
        sys.stdout.write(json.dumps(leaked))
    """
    import json
    leaked_list = json.loads(_run_in_subprocess(snippet))
    assert leaked_list == [], (
        f"{module_name!r} transitively loads {leaked_list}"
    )


# ---------------------------------------------------------------------------
# SC6 (corpus-serve-ingest): the research graph must not expose 切分.
#
# ADR 0047: the research model must never call 切分 on arbitrary
# strings — chunking happens ONLY on the ingest endpoint. The bound
# tool set of a compiled host graph is the contract surface here: no
# tool name may carry the chunk / split / ingest vocabulary.
# ---------------------------------------------------------------------------


_CHUNK_TOOL_NAME_MARKERS = ("chunk", "split", "ingest", "切分")


def _bound_research_tool_names() -> list[str]:
    """Compile the host graph with the scripted model and return the
    bound tool names (``ScriptedChatModel.bind_tools`` records them)."""
    from langgraph.checkpoint.memory import InMemorySaver

    from fin.host import ScriptedChatModel, compile_graph

    model = ScriptedChatModel(script=[])
    compile_graph(checkpointer=InMemorySaver(), model=model)
    return [getattr(tool, "name", repr(tool)) for tool in model.bound_tools]


def test_research_graph_binds_no_chunking_tool() -> None:
    """No bound research tool may be a 切分 / chunk / ingest tool (SC6)."""
    names = _bound_research_tool_names()
    assert names, "the host graph must bind its research tools"
    offending = {
        name
        for name in names
        for marker in _CHUNK_TOOL_NAME_MARKERS
        if marker in name.lower() or marker in name
    }
    assert offending == set(), (
        f"the research graph binds chunking-shaped tool(s) {sorted(offending)}; "
        "切分 must only run on the corpus ingest endpoint (ADR 0047 / SC6). "
        "Remove the tool from the graph's bind_tools list."
    )


def test_research_graph_tool_names_are_the_known_research_set() -> None:
    """The graph toolset stays the known research tools — the ingest
    endpoint is a corpus/host capability, not a model tool."""
    names = set(_bound_research_tool_names())
    assert "retrieve" in names  # the research evidence tool is present
    assert not names & {"corpus_ingest", "ingest", "chunk", "split_document"}


def test_ask_path_does_not_import_the_eval_dataset_stack() -> None:
    """The serve path must not drag in the eval dataset build stack.

    ``fin.corpus.dataset.__init__`` pulls the exporters and the gated
    live-fetch module; the ingest endpoint classifies document types
    through the neutral ``fin.corpus.doc_type`` module instead, so
    importing the workspace HTTP surface never lands the eval stack in
    ``sys.modules``.
    """
    snippet = """
        import sys
        import json
        for name in {modules!r}:
            __import__(name)
        loaded = sorted(m for m in sys.modules if m == 'fin.corpus.dataset')
        sys.stdout.write(json.dumps(loaded))
    """.format(modules=list(_ASK_PATH_MODULES))
    import json
    loaded = json.loads(_run_in_subprocess(snippet))
    assert loaded == [], (
        f"the ask path imports the eval dataset stack via {loaded}; "
        "classify document types through fin.corpus.doc_type instead."
    )