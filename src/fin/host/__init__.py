"""Host capability slice: the host graph, tool wrappers, citation gate, and surfaces.

T3 adds the ADR 0009 tool wrappers (``list_files``, ``read_file``) as
plain callables. T4 adds ``retrieve`` against an injected
``BM25Adapter``. T5 introduces the citation gate at
``src/fin/host/citation_gate.py``: the code gate on the path to END
that fills the user-visible citations from extractive spans that
matched this run's tool evidence (ADR 0008 + ADR 0021). T6 wires
all three into the LangGraph host graph via ``compile_graph``.
"""

from fin.host.citation_gate import (
    BounceResult,
    Citation,
    CitationGateError,
    DoneResult,
    EvidenceItem,
    GateResult,
    MalformedEvidenceError,
    RefuseResult,
    verify_citations,
)
from fin.host.graph import ConcurrentRunError, compile_graph
from fin.host.scripted_model import ScriptedChatModel
from fin.host.state import (
    AgentState,
    REPAIR_CAP,
    RECURSION_LIMIT,
    STEP_CAP,
    Status,
    TERMINAL_STATUSES,
)
from fin.host.tools import list_files, read_file

__all__ = [
    "AgentState",
    "BounceResult",
    "Citation",
    "CitationGateError",
    "ConcurrentRunError",
    "DoneResult",
    "EvidenceItem",
    "GateResult",
    "MalformedEvidenceError",
    "REPAIR_CAP",
    "RECURSION_LIMIT",
    "RefuseResult",
    "STEP_CAP",
    "ScriptedChatModel",
    "Status",
    "TERMINAL_STATUSES",
    "compile_graph",
    "list_files",
    "read_file",
    "verify_citations",
]
