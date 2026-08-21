"""Host capability slice: the host graph, tool wrappers, citation gate, and surfaces.

T3 adds the ADR 0009 tool wrappers (``list_files``, ``read_file``) as
plain callables. T6 wires them into the LangGraph host graph via
``bind_tools``; T5 introduces the citation gate at
``src/fin/host/citation_gate.py``.
"""

from fin.host.tools import list_files, read_file

__all__ = ["list_files", "read_file"]