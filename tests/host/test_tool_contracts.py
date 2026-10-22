"""P3 T1/T2 — one canonical argument contract per registered host tool.

The inventory in :data:`fin.host.tools.contracts.TOOL_CONTRACTS` must cover
every tool the compiled graph binds, and each contract's argument model must
match the bound tool's ``args_schema`` field for field. The tools are
introspected from a compiled scripted graph — no model is invoked.

Hermetic: scripted model, fake retrieve port, in-memory checkpointer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from fin.host import ScriptedChatModel, compile_graph
from fin.host.graph import (
    _make_read_visual_detail_tool,
)
from fin.host.memory_buffer import LocalMemoryBuffer
from fin.host.tools.contracts import (
    TOOL_CONTRACTS,
    AttachmentSourceRef,
    CompanySourceRef,
    UnknownToolContract,
    contract_for,
)
from fin.host.user_profile import GLOBAL_FILENAME, WORKSPACE_FILENAME, UserProfileStore
from fin.retrieve import FakeRetrievePort


REGISTERED_TOOLS = {
    "list_files",
    "retrieve",
    "get_xbrl_fact",
    "compare_facts",
    "research_issuers",
    "redline",
    "extract_tables",
    "three_year_statement",
    "save_memory",
    "recall_memory",
    "edit_user_profile",
    "read_source",
    "read_visual_detail",
}


@dataclass
class _FakeMemoryPort:
    """Minimal ``MemoryPort`` shim so the ON branch binds the memory tools."""

    enabled: bool = True
    buffer: LocalMemoryBuffer = field(default_factory=LocalMemoryBuffer)

    def extract(
        self,
        text: str,
        *,
        metadata: Mapping[str, object] | None = None,
        title: str = "",
        workspace: str = "",
    ) -> dict[str, object]:
        return self.buffer.extract(
            text,
            metadata=metadata,
            title=title,
            workspace=workspace,
        )

    def recall(
        self,
        query: str,
        *,
        limit: int = 8,
        workspace: str = "",
    ) -> dict[str, object]:
        return self.buffer.recall(query, limit=limit, workspace=workspace)

    def gc(self, *, limit: int = 8) -> dict[str, object]:
        return self.buffer.gc(limit=limit)

    def dream(self, *, limit: int = 8) -> dict[str, object]:
        return self.buffer.dream(limit=limit)


def _profile_store(tmp_path: Path) -> UserProfileStore:
    store = UserProfileStore(
        global_path=tmp_path / "home" / ".fin" / GLOBAL_FILENAME,
        workspace_path=tmp_path / "ws" / ".fin" / WORKSPACE_FILENAME,
    )
    store.global_path.parent.mkdir(parents=True, exist_ok=True)
    store.workspace_path.parent.mkdir(parents=True, exist_ok=True)
    return store


def _scripted_model() -> ScriptedChatModel:
    return ScriptedChatModel(script=[AIMessage(content="ok")])


def _default_graph(tmp_path: Path) -> Any:
    return compile_graph(
        checkpointer=InMemorySaver(),
        model=_scripted_model(),
        adapter=FakeRetrievePort(),
        user_profile_store=_profile_store(tmp_path),
    )


def _memory_on_graph(tmp_path: Path) -> Any:
    return compile_graph(
        checkpointer=InMemorySaver(),
        model=_scripted_model(),
        adapter=FakeRetrievePort(),
        memory_port=_FakeMemoryPort(),
        user_profile_store=_profile_store(tmp_path),
    )


def _resolve_refs(value: Any, root: Mapping[str, Any]) -> Any:
    """Inline ``$ref`` targets so two schemas can be compared as data."""
    if isinstance(value, dict):
        if "$ref" in value:
            target: Any = root
            for part in str(value["$ref"]).lstrip("#/").split("/"):
                target = target[part]
            return _resolve_refs(target, root)
        return {
            key: _resolve_refs(item, root)
            for key, item in value.items()
            if key not in {"title", "description"}
        }
    if isinstance(value, list):
        return [_resolve_refs(item, root) for item in value]
    return value


def _canonical(value: Any, root: Mapping[str, Any]) -> Any:
    """Resolved, title-free, ``const``-normalized schema data."""
    resolved = _resolve_refs(value, root)
    if isinstance(resolved, dict):
        if "const" in resolved:
            normalized = {
                key: item for key, item in resolved.items() if key != "const"
            }
            normalized["enum"] = [resolved["const"]]
            resolved = normalized
        return {key: _canonical(item, root) for key, item in resolved.items()}
    if isinstance(resolved, list):
        return [_canonical(item, root) for item in resolved]
    return resolved


def _assert_same_fields(contract: Any, tool: Any) -> None:
    """The bound tool's schema IS the canonical contract model (identity)."""
    assert tool.args_schema is contract.args_model, tool.name


@pytest.fixture
def default_graph(tmp_path: Path) -> Any:
    return _default_graph(tmp_path)


def test_every_registered_tool_has_exactly_one_contract() -> None:
    assert set(TOOL_CONTRACTS) == REGISTERED_TOOLS
    for name, contract in TOOL_CONTRACTS.items():
        assert contract.name == name
        assert contract_for(name) is contract
    with pytest.raises(UnknownToolContract):
        contract_for("no_such_tool")


def test_every_bound_tool_of_a_compiled_graph_has_a_contract(
    default_graph: Any,
) -> None:
    bound = list(default_graph._bound_tools)
    node_tools = dict(default_graph._tool_node.tools_by_name)
    assert bound, "the scripted graph binds no tool"
    for tool in bound:
        assert tool.name in TOOL_CONTRACTS, tool.name
    for name in node_tools:
        assert name in TOOL_CONTRACTS, name
    # The model-only fan-out tool is bound, not executed by the ToolNode.
    assert "research_issuers" in {tool.name for tool in bound}
    assert "research_issuers" not in node_tools


def test_contract_models_match_the_bound_tool_schemas(default_graph: Any) -> None:
    for tool in list(default_graph._bound_tools) + list(
        default_graph._tool_node.tools_by_name.values()
    ):
        _assert_same_fields(TOOL_CONTRACTS[tool.name], tool)


def test_contract_models_match_the_memory_tool_schemas(tmp_path: Path) -> None:
    graph = _memory_on_graph(tmp_path)
    for tool in list(graph._bound_tools) + list(
        graph._tool_node.tools_by_name.values()
    ):
        _assert_same_fields(TOOL_CONTRACTS[tool.name], tool)


def test_contract_model_matches_the_visual_tool_schema() -> None:
    # The visual lane binds only in a deployment with a page lane; the
    # factory is the same object ``compile_graph`` would build.
    tool = _make_read_visual_detail_tool(None)
    _assert_same_fields(TOOL_CONTRACTS["read_visual_detail"], tool)


def test_host_injected_fields_are_recorded(default_graph: Any) -> None:
    tools = list(default_graph._bound_tools) + list(
        default_graph._tool_node.tools_by_name.values()
    )
    tools.append(_make_read_visual_detail_tool(None))
    for tool in tools:
        injected = {
            name
            for name in tool.args_schema.model_fields
            if name in {"tool_call_id", "state"}
        }
        contract = TOOL_CONTRACTS[tool.name]
        assert contract.host_injected_fields == injected, tool.name
        # Host-injected fields stay out of what the model sees.
        assert not (injected & set(tool.tool_call_schema.model_fields)), tool.name


def test_read_source_locator_shape_is_preserved() -> None:
    """The locator is the two canonical models, in the declared union."""
    contract = TOOL_CONTRACTS["read_source"]
    root = contract.args_model.model_json_schema()
    source_ref = root["properties"]["source_ref"]
    branches = {
        branch["$ref"].rsplit("/", 1)[-1]: root["$defs"][branch["$ref"].rsplit("/", 1)[-1]]
        for branch in source_ref["anyOf"]
    }
    assert set(branches) == {"CompanySourceRef", "AttachmentSourceRef"}

    company = branches["CompanySourceRef"]
    assert set(company["properties"]) == {"document_id", "version", "unit"}
    assert set(company["required"]) == {"document_id", "version", "unit"}
    assert company["additionalProperties"] is False

    attachment = branches["AttachmentSourceRef"]
    assert set(attachment["properties"]) == {
        "kind",
        "attachment_id",
        "version",
        "unit",
    }
    assert set(attachment["required"]) == {
        "kind",
        "attachment_id",
        "version",
        "unit",
    }
    assert attachment["additionalProperties"] is False
    # The two branches ARE the canonical models, not a second schema.
    assert _canonical(company, root) == _canonical(
        CompanySourceRef.model_json_schema(), root
    )
    assert _canonical(attachment, root) == _canonical(
        AttachmentSourceRef.model_json_schema(), root
    )


def test_every_contract_model_is_strict_about_extra_fields() -> None:
    for name, contract in TOOL_CONTRACTS.items():
        assert contract.args_model.model_config.get("extra") == "forbid", name
    for nested in (CompanySourceRef, AttachmentSourceRef):
        assert nested.model_config.get("extra") == "forbid", nested.__name__


def test_every_contract_records_effect_retry_and_reconciliation() -> None:
    for name, contract in TOOL_CONTRACTS.items():
        assert contract.effect in {
            "none",
            "local_read",
            "network_read",
            "persistent_write",
            "external_effect",
        }, name
        assert contract.retry.status in {"declared", "open", "not_applicable"}, name
        assert contract.reconciliation.status in {
            "declared",
            "open",
            "not_applicable",
        }, name
        assert contract.retry.summary, name
        assert contract.reconciliation.summary, name
        assert contract.schema_source, name
        for bound in contract.bounds:
            assert bound.field and bound.constraint and bound.source, name
        for check in contract.domain_checks:
            assert check.name and check.source, name
        if contract.effect in {"persistent_write", "external_effect"}:
            # An effectful slice with an open policy stays blocked.
            if (
                contract.retry.status == "open"
                or contract.reconciliation.status == "open"
            ):
                assert contract.blocked_slices, name
        if contract.effect not in {"persistent_write", "external_effect"}:
            assert contract.reconciliation.status == "not_applicable", name


def test_blocked_and_open_entries_are_explicit() -> None:
    save = TOOL_CONTRACTS["save_memory"]
    assert save.effect == "persistent_write"
    assert save.retry.status == "open"
    assert save.reconciliation.status == "open"
    assert set(save.blocked_slices) == {"execution", "retry", "reconciliation"}
    assert save.operation_identity is None

    recall = TOOL_CONTRACTS["recall_memory"]
    assert recall.retry.status == "open"
    assert "retry" in recall.blocked_slices

    profile = TOOL_CONTRACTS["edit_user_profile"]
    assert profile.effect == "persistent_write"
    assert profile.retry.status == "open"
    assert profile.reconciliation.status == "open"
    assert "reconciliation" in profile.blocked_slices

    visual = TOOL_CONTRACTS["read_visual_detail"]
    assert visual.effect == "external_effect"
    assert visual.reconciliation.status == "open"
    assert visual.blocked_slices == ("reconciliation",)
    assert visual.operation_identity


def test_open_non_effectful_retry_policies_are_recorded() -> None:
    assert TOOL_CONTRACTS["retrieve"].retry.status == "open"
    assert TOOL_CONTRACTS["read_source"].retry.status == "open"


def test_surfaces_match_the_binding_surfaces() -> None:
    assert "clarify" in TOOL_CONTRACTS["list_files"].surfaces
    assert {"model_bound", "tool_node"} <= TOOL_CONTRACTS["list_files"].surfaces
    assert "issuer" in TOOL_CONTRACTS["research_issuers"].surfaces
    assert "tool_node" not in TOOL_CONTRACTS["research_issuers"].surfaces
    for name in (
        "retrieve",
        "get_xbrl_fact",
        "compare_facts",
        "redline",
        "extract_tables",
        "three_year_statement",
        "save_memory",
        "recall_memory",
        "edit_user_profile",
        "read_source",
        "read_visual_detail",
    ):
        assert {"model_bound", "tool_node"} <= TOOL_CONTRACTS[name].surfaces, name
