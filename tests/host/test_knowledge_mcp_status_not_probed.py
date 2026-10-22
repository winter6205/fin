"""``GET /api/status`` must tell 未探测 (never probed) from 缺 (probed absent).

The three probe outcomes are 缺 / 坏 / 可达 (``missing`` / ``fault`` /
``reachable``) and each one asserts something a probe ESTABLISHED.
``knowledge_mcp_state()`` returning ``None`` asserts nothing at all: this
process never probed (a plain ``TestClient`` boot that skipped
``fin.host.http.serve()``), so absence was never established. Rendering
that ``None`` as ``missing`` claims "we looked and the knowledge MCP is
not there" about a process that never looked — a false negative that
makes an unbooted host read like a broken one.

So ``not_probed`` is a DISPLAY-ONLY fourth wire value, derived from
``None``, never a publishable probe result: ``publish_knowledge_mcp_state``
still rejects it, which keeps the invariant "you can only publish what a
probe observed".

The unknown-value net is pinned here too: a snapshot that is neither
``None`` nor one of the three states is a corrupt reading, and the route
must degrade it to a fail-closed 坏 and SAY SO in the log rather than
echo an unvouched value or pass it through silently.
"""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from fin.host import knowledge_mcp as knowledge_mcp_module
from fin.host.knowledge_mcp import (
    KNOWLEDGE_MCP_FAULT,
    KNOWLEDGE_MCP_MISSING,
    KNOWLEDGE_MCP_NOT_PROBED,
    KNOWLEDGE_MCP_REACHABLE,
    KNOWLEDGE_MCP_STATES,
    knowledge_mcp_gate_closed,
    knowledge_mcp_status_value,
    knowledge_mcp_state,
    publish_knowledge_mcp_state,
)


def _status() -> dict:
    """``GET /api/status`` through a bare app, i.e. a never-probed process.

    No published state and no ``serve()`` boot: this is exactly the case
    the ``not_probed`` value exists for. The autouse snapshot reset in
    ``tests/host/conftest.py`` guarantees the ``None``.
    """
    from fin.host.http import app

    response = TestClient(app).get("/api/status")
    assert response.status_code == 200
    return response.json()


# ----- None is not 缺 -----


def test_status_reports_not_probed_when_this_process_never_probed() -> None:
    """A ``None`` snapshot must not be coerced into ``missing``."""
    assert knowledge_mcp_state() is None

    body = _status()

    assert body["knowledge_mcp"] == KNOWLEDGE_MCP_NOT_PROBED
    # The false negative this value exists to kill, stated directly.
    assert body["knowledge_mcp"] != KNOWLEDGE_MCP_MISSING


def test_not_probed_is_data_not_a_500_and_blanks_no_other_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR 0033: every state is a 200 with the whole payload populated.

    A never-probed host says so about ONE field; the left corner's other
    four facts (adapter kind, model, filing count, bind name) are still
    real and must not render as a hole next to it.
    """
    monkeypatch.setenv("FIN_MODEL", "synthetic-status-model")
    body = _status()

    # The three lifecycle counts are aggregates with no identity, so they
    # are admitted here; every other key still has to reopen this set.
    assert set(body.keys()) == {
        "knowledge_mcp",
        "retrieve_adapter_kind",
        "model_identity",
        "corpus_filing_count",
        "bound_collection_name",
        "run_counts",
    }
    assert body["model_identity"]
    assert isinstance(body["corpus_filing_count"], int)


def test_not_probed_leaves_the_conversation_gate_open() -> None:
    """The change is display-only: the gate still treats ``None`` as open.

    Asserted so a future "make ``not_probed`` a state" refactor cannot
    quietly close the gate for every hermetic ``TestClient`` boot.
    """
    assert knowledge_mcp_state() is None
    assert knowledge_mcp_gate_closed(environ={"FIN_RETRIEVE_ADAPTER": "mcp"}) is False


# ----- the three probed states are echoed verbatim -----


@pytest.mark.parametrize(
    "state",
    [KNOWLEDGE_MCP_MISSING, KNOWLEDGE_MCP_FAULT, KNOWLEDGE_MCP_REACHABLE],
)
def test_status_echoes_each_probed_state_verbatim(state: str) -> None:
    """缺 / 坏 / 可达 keep their existing wire values and meaning."""
    publish_knowledge_mcp_state(state)

    body = _status()

    assert body["knowledge_mcp"] == state


# ----- the unknown-value net still catches -----


def test_status_never_echoes_a_value_outside_the_published_set(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A typo'd / corrupt snapshot is NOT echoed and NOT a silent pass.

    ``publish_knowledge_mcp_state`` rejects unknown values, so a stray
    state can only reach the snapshot by a programming error. The route
    degrades it to 坏 (the one value that asserts no probe finding) and
    logs the bad reading, so the corruption is loud instead of rendered.
    """
    monkeypatch.setattr(knowledge_mcp_module, "_state", "reachble")
    caplog.set_level(logging.ERROR, logger=knowledge_mcp_module.__name__)

    body = _status()

    assert body["knowledge_mcp"] != "reachble"
    # Fail-closed, and NOT mislabelled as 缺 (which would claim a probe
    # established absence) nor as 未探测 (which would claim none ran).
    assert body["knowledge_mcp"] == KNOWLEDGE_MCP_FAULT
    assert body["knowledge_mcp"] != KNOWLEDGE_MCP_NOT_PROBED
    assert any("reachble" in record.getMessage() for record in caplog.records)


def test_status_value_is_the_one_resolution_of_the_snapshot() -> None:
    """The route's coercion is one named function, not inline shape.

    ``None`` → 未探测; a probed state → verbatim; anything else → 坏.
    """
    assert knowledge_mcp_status_value(None) == KNOWLEDGE_MCP_NOT_PROBED
    for state in KNOWLEDGE_MCP_STATES:
        assert knowledge_mcp_status_value(state) == state
    assert knowledge_mcp_status_value("reachble") == KNOWLEDGE_MCP_FAULT
    assert knowledge_mcp_status_value("") == KNOWLEDGE_MCP_FAULT


# ----- not_probed is display-only -----


def test_not_probed_is_not_a_publishable_probe_result() -> None:
    """No probe can observe "not probed", so nothing may publish it.

    Keeping it out of ``KNOWLEDGE_MCP_STATES`` is what preserves
    "you can only publish what a probe observed"; publishing it would let
    a caller fake the one state no probe can produce.
    """
    assert KNOWLEDGE_MCP_NOT_PROBED not in KNOWLEDGE_MCP_STATES

    with pytest.raises(ValueError):
        publish_knowledge_mcp_state(KNOWLEDGE_MCP_NOT_PROBED)

    assert knowledge_mcp_state() is None
