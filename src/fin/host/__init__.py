"""Host capability slice: the host graph, tool wrappers, citation gate, and surfaces.

T3 adds the ADR 0009 tool wrappers (``list_files``, ``read_file``) as
plain callables. T6 wires them into the LangGraph host graph via
``bind_tools``. T5 introduces the citation gate at
``src/fin/host/citation_gate.py``: the code gate on the path to END
that fills the user-visible citations from extractive spans that
matched this run's tool evidence (ADR 0008 + ADR 0021).
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
from fin.host.tools import list_files, read_file

__all__ = [
    "BounceResult",
    "Citation",
    "CitationGateError",
    "DoneResult",
    "EvidenceItem",
    "GateResult",
    "MalformedEvidenceError",
    "RefuseResult",
    "list_files",
    "read_file",
    "verify_citations",
]