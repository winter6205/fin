"""``fin.host.collection_bind`` — the v1 资料集合 bind (T4).

The route-level cases live in ``tests/host/test_workspace_http.py``;
this file owns the pure resolver's own contract, which is the part
that says what "bound" even means:

* the 未绑定 sentinel for a boot that is not on the product ``mcp``
  port, and for a product boot whose 知识 MCP never reached 可达;
* the mirrored v1 name otherwise, pinned by literal so an iknow-side
  rename makes a fin test fail rather than leaving a name nothing
  answers to;
* no catalog read of any kind — the resolver must not import
  ``list_filings`` (SC7).
"""

from __future__ import annotations

import pytest

from fin.host.collection_bind import (
    BOUND_COLLECTION_NAME,
    BOUND_COLLECTION_UNBOUND,
    resolve_bound_collection_name,
)
from fin.host.knowledge_mcp import (
    KNOWLEDGE_MCP_FAULT,
    KNOWLEDGE_MCP_MISSING,
    KNOWLEDGE_MCP_REACHABLE,
    publish_knowledge_mcp_state,
)
from fin.host.scripted_boot import SCRIPTED_BOOT_ENV_VAR


@pytest.mark.parametrize(
    "state", [KNOWLEDGE_MCP_MISSING, KNOWLEDGE_MCP_FAULT, KNOWLEDGE_MCP_REACHABLE]
)
def test_fake_adapter_is_unbound_in_every_snapshot_state(state: str) -> None:
    """A test-only port never names a live 资料集合 — even if some test
    published 可达. The adapter kind is the first gate."""
    publish_knowledge_mcp_state(state)

    assert (
        resolve_bound_collection_name(environ={"FIN_RETRIEVE_ADAPTER": "fake"})
        == BOUND_COLLECTION_UNBOUND
    )


@pytest.mark.parametrize("state", [KNOWLEDGE_MCP_MISSING, KNOWLEDGE_MCP_FAULT])
def test_product_adapter_is_unbound_when_the_handshake_failed(state: str) -> None:
    publish_knowledge_mcp_state(state)

    assert (
        resolve_bound_collection_name(environ={}) == BOUND_COLLECTION_UNBOUND
    ), state


def test_product_adapter_is_unbound_before_any_probe() -> None:
    """The never-probed view (plain ``TestClient`` boot) is 未绑定: this
    process has no confirmed reachable MCP, so it cannot claim a bind.

    The autouse snapshot reset in ``tests/host/conftest.py`` is what
    leaves this state in place.
    """
    assert (
        resolve_bound_collection_name(environ={}) == BOUND_COLLECTION_UNBOUND
    )


def test_product_adapter_names_the_v1_collection_when_reachable() -> None:
    publish_knowledge_mcp_state(KNOWLEDGE_MCP_REACHABLE)

    assert resolve_bound_collection_name(environ={}) == BOUND_COLLECTION_NAME


def test_the_name_is_the_mirrored_iknow_literal() -> None:
    """The mirror is pinned by literal.

    iknow-memory owns this string (``SOURCE_DOC_COLLECTION_NAME`` in
    its ``src/core/types.ts``). fin cannot read it over MCP — no tool
    returns a collection identity — so the constant is a documented
    copy, and this assertion is the tripwire for a rename.
    """
    assert BOUND_COLLECTION_NAME == "默认资料集合"
    # The sentinel must not be mistakable for a collection name.
    assert BOUND_COLLECTION_UNBOUND != BOUND_COLLECTION_NAME


def test_scripted_lane_is_unbound_not_bound() -> None:
    """The scripted e2e lane resolves to the product adapter kind by
    configuration but publishes 缺 (``publish_knowledge_mcp``), so it
    must report 未绑定 — its retrieve is the fake port.

    The resolver does not special-case the scripted env var itself; it
    reads the published snapshot, which is the same thing
    ``publish_knowledge_mcp`` fills. Asserted here because "scripted
    lane names the real collection" is the plausible-looking mistake.
    """
    publish_knowledge_mcp_state(KNOWLEDGE_MCP_MISSING)

    assert (
        resolve_bound_collection_name(environ={SCRIPTED_BOOT_ENV_VAR: "1"})
        == BOUND_COLLECTION_UNBOUND
    )


def test_the_resolver_reads_no_catalog() -> None:
    """SC7, structural: the bind decision never touches the local filing
    catalog, so it cannot drift into a 就绪 claim derived from
    ``corpus_filing_count``.

    Checked against the module's IMPORT graph, not its text: the
    docstring names both concepts on purpose (it is where the SC7
    non-claim is written down), so a substring scan would fail on the
    documentation that explains why the code is correct.
    """
    import ast
    from importlib import import_module

    module = import_module("fin.host.collection_bind")
    with open(module.__file__ or "", encoding="utf-8") as handle:
        tree = ast.parse(handle.read())

    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
        elif isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)

    assert not any("catalog" in name for name in imported), imported
    assert not hasattr(module, "list_filings")
