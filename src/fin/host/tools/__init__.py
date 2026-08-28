"""Host tool wrappers for the v1 agent loop.

Exposes the ADR 0009 tool surface as plain callables. T6 binds these
to the model via ``bind_tools`` when wiring the host graph.
"""

from fin.host.tools.compare_facts import compare_facts
from fin.host.tools.get_xbrl_fact import get_xbrl_fact
from fin.host.tools.list_files import list_files
from fin.host.tools.read_file import read_file

__all__ = ["compare_facts", "get_xbrl_fact", "list_files", "read_file"]