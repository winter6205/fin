"""The 工作台's bound **资料集合** identity (T4, `first-closed-loop`).

`GET /api/status` answers the left corner's boot-scoped surface; one of
the lines it feeds names the **资料集合** this process is bound to. ADR
0049 settles the v1 shape: **one** process-wide 知识库 — "Future
many-libraries / tenants / collections is not this ADR" — so the bind
is either the one default collection or nothing at all, and spec Open
Question 2 ("is v1 bind the process-wide default collection only?") is
answered yes by that accepted record. No switching rail, no per-thread
root.

The name is a MIRROR, not a read
-------------------------------
iknow-memory owns the string: ``SOURCE_DOC_COLLECTION_NAME =
"默认资料集合"`` in its ``src/core/types.ts`` (its own comment calls it
"stable for the wire, the dashboard, and fin's operator copy"). fin
cannot READ it over the product seam: the MCP server registers exactly
six tools — ``ingest``, ``invalidation``, ``push``, ``extract``,
``retrieve``, ``read_source`` — and **none of them returns a collection
identity or a source list**. `read_source` deliberately answers
``{id, source_ref, title?, content}`` and nothing more. The only iknow
surface that exposes the name is its **separate dashboard HTTP
process**, and calling that would add a second protocol beside the
**retrieve port** — the one retrieve dependency fin is allowed
(``specs/first-closed-loop.md`` Boundaries; ADR 0032/0049).

So :data:`BOUND_COLLECTION_NAME` below is a documented copy. A rename
on the iknow side does not break anything at runtime — it makes this
line name a collection that no longer exists, which is why the constant
carries the sibling file name and why ``tests/host/test_collection_bind.py``
pins the literal. Exposing 资料集合 over MCP is a cross-repo follow-up
(recorded in ``plans/first-closed-loop.md`` T4); until it exists there
is no read to keep the two in sync.

What this line does NOT claim
-----------------------------
Nothing here is a **就绪** signal. ``corpus_filing_count`` is the local
catalog, not iknow chunk count (spec assumption 5 / SC7), and this
module never reads the catalog at all: naming the bound collection is
not a statement that it holds chunks, that retrieve will return hits,
or that a run may cite. Readiness lives on iknow's own 资料页 (spec
assumption 9). The 未绑定 state is likewise a configuration statement —
this process is not talking to an iknow 资料集合 — mirroring how
``retrieve_adapter_kind`` keeps a ``fake`` boot distinguishable from a
product one.
"""

from __future__ import annotations

import os
from typing import Mapping

from fin.host.graph import RETRIEVE_ADAPTER_MCP
from fin.host.knowledge_mcp import (
    KNOWLEDGE_MCP_REACHABLE,
    knowledge_mcp_state,
)
from fin.host.operator_env import resolve_retrieve_adapter_kind


# The v1 资料集合 name. MIRROR of iknow-memory's
# ``SOURCE_DOC_COLLECTION_NAME`` (``src/core/types.ts``); see the module
# docstring for why there is no runtime read and what a rename costs.
BOUND_COLLECTION_NAME = "默认资料集合"

# The value the wire carries when this boot is bound to no iknow
# 资料集合: a test-only (fake / scripted) adapter, or a product boot
# whose 知识 MCP never reached 可达. Deliberately not a collection name —
# v1's name space is iknow's single constant, so the sentinel cannot
# collide with a real one today.
BOUND_COLLECTION_UNBOUND = "unbound"


def resolve_bound_collection_name(
    *, environ: Mapping[str, str] | None = None
) -> str:
    """Return the 资料集合 name this process is bound to, or 未绑定.

    Bound iff BOTH hold:

    * the resolved adapter is the product ``mcp`` port — a ``fake``
      boot must never name a live collection (it answers from no
      corpus at all);
    * the published knowledge-MCP snapshot is 可达 — the same
      in-process snapshot the conversation gate reads, so a boot whose
      handshake never succeeded (or never ran) is 未绑定 rather than
      naming a store it cannot reach.

    Both halves matter: the first is what
    ``resolve_retrieve_adapter_kind`` says about configuration, the
    second is what the real ``initialize`` + ``tools/list`` handshake
    proved. A scripted e2e boot resolves to the product kind by
    configuration but publishes 缺, so it lands on 未绑定 like any other
    test-only lane (``fin.host.knowledge_mcp.publish_knowledge_mcp``).

    Reads no catalog, no run, and no chunk count — see the module
    docstring on 就绪 (SC7).
    """
    source: Mapping[str, str] = os.environ if environ is None else environ
    if resolve_retrieve_adapter_kind(environ=source) != RETRIEVE_ADAPTER_MCP:
        return BOUND_COLLECTION_UNBOUND
    if knowledge_mcp_state() != KNOWLEDGE_MCP_REACHABLE:
        return BOUND_COLLECTION_UNBOUND
    return BOUND_COLLECTION_NAME


__all__ = [
    "BOUND_COLLECTION_NAME",
    "BOUND_COLLECTION_UNBOUND",
    "resolve_bound_collection_name",
]
