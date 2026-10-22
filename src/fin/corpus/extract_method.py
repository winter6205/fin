"""Extract-method vocabulary — the single authoritative definition.

**standard** (LLM entities/relations) vs **Fast 抽图** (NLP /
co-occurrence) are the two extract methods of the index job (ADR 0067).
ADR 0079 requires exactly one method per corpus version and ADR 0068
makes the operator switch default to standard, so both the catalog's
row contract and the host's propose window need this closed set.

This module is the SSOT: the corpus layer sits below the host (host
modules already import ``fin.corpus``; corpus never imports host), so
:mod:`fin.host.question.extract_method_proposal` imports the vocabulary from
here rather than redefining it. Fast/standard are extract methods,
never 检索档 tokens — this shared vocabulary deliberately stays apart
from the ``search_mode`` token space (spec SC3).

The methods select how the sibling iknow-memory job extracts the graph;
fin only records which one a version uses.
"""

from __future__ import annotations

from typing import Final, Literal

# The closed extract-method set (ADR 0079's two members).
ExtractMethod = Literal["standard", "fast"]

EXTRACT_METHOD_STANDARD: Final[ExtractMethod] = "standard"
EXTRACT_METHOD_FAST: Final[ExtractMethod] = "fast"

EXTRACT_METHODS: Final[frozenset[str]] = frozenset(
    {EXTRACT_METHOD_STANDARD, EXTRACT_METHOD_FAST}
)

# ADR 0068: the operator switch defaults to standard; nothing may
# silently apply Fast.
DEFAULT_EXTRACT_METHOD: Final[ExtractMethod] = EXTRACT_METHOD_STANDARD

__all__ = [
    "DEFAULT_EXTRACT_METHOD",
    "EXTRACT_METHODS",
    "EXTRACT_METHOD_FAST",
    "EXTRACT_METHOD_STANDARD",
    "ExtractMethod",
]
