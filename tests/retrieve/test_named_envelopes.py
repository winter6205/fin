"""The named failure envelopes and the two exact ``not_ready`` shapes.

Spec ``specs/qa-chain-corpus-retrieval.md`` §"Result, error, readiness,
and degradation wire":

* upstream TUF/TUR tokens project to fin's named vocabulary, never
  recoded: ``lexical_not_ready`` with a proven index fact stays the
  exact passthrough ``{"error": "not_ready", "cause": "index",
  "stage": "index"}``; ``lexical_authorization_denied`` →
  ``authorization_denied``; ``lexical_transport_unavailable`` and
  ``lexical_stale_revision`` → ``retrieve_unavailable``;
  ``lexical_protocol_error`` → ``retrieve_protocol_error``;
  ``lexical_unfilterable_generation`` → ``unsupported_filter_mode``;
  the structured lane's ``unsupported_field`` scope refusal →
  ``unsupported_source_scope``.
* ``cause=busy`` is minted fin-side only — the backend owns no queue
  and no budget. Its stage is exactly one of ``queue`` / ``startup`` /
  ``response``.
* Transport, protocol, authorization, unknown and mixed-degradation
  failures keep their own names and are never recoded as index work or
  a successful empty.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from fin.retrieve.mcp_adapter import MCPRetrieveAdapter


def _error_result(body: dict[str, Any]) -> dict[str, Any]:
    """An MCP ``isError`` result whose first text block carries ``body``."""
    return {
        "isError": True,
        "content": [{"type": "text", "text": json.dumps(body)}],
    }


class _ScriptedAdapter(MCPRetrieveAdapter):
    """Skip ``__init__`` and script one response (documented seam)."""

    def __init__(self, scripted: dict[str, Any]) -> None:
        self._command = None
        self._locator_map = {}
        self._known_doc_ids = None
        self._timeout = 5.0
        self._scripted_response = scripted
        self._scripted_exception = None

    async def _call_mcp(self, payload: dict[str, Any]) -> dict[str, Any]:  # type: ignore[override]
        assert self._scripted_response is not None
        return self._scripted_response


@pytest.mark.parametrize(
    ("upstream_body", "expected"),
    [
        pytest.param(
            {
                "error": "lexical_not_ready",
                "cause": "index",
                "stage": "index",
            },
            {"error": "not_ready", "cause": "index", "stage": "index"},
            id="not-ready-passthrough",
        ),
        pytest.param(
            {"error": "lexical_authorization_denied"},
            {"error": "authorization_denied"},
            id="authorization-denied",
        ),
        pytest.param(
            {"error": "lexical_transport_unavailable"},
            {"error": "retrieve_unavailable"},
            id="transport-unavailable",
        ),
        pytest.param(
            {"error": "lexical_stale_revision"},
            {"error": "retrieve_unavailable"},
            id="stale-revision",
        ),
        pytest.param(
            {"error": "lexical_protocol_error"},
            {"error": "retrieve_protocol_error"},
            id="protocol-error",
        ),
        pytest.param(
            {"error": "lexical_unfilterable_generation"},
            {"error": "unsupported_filter_mode"},
            id="unfilterable-generation",
        ),
    ],
)
def test_upstream_tokens_project_to_fin_named_envelopes(
    upstream_body: dict[str, Any], expected: dict[str, Any]
) -> None:
    adapter = _ScriptedAdapter({"result": _error_result(upstream_body)})
    assert adapter.retrieve("revenue") == expected


def test_not_ready_index_shape_is_exact() -> None:
    """The proven index fact keeps the exact two-field shape: no extra
    keys, no prose, no recoding."""
    adapter = _ScriptedAdapter(
        {
            "result": _error_result(
                {
                    "error": "lexical_not_ready",
                    "cause": "index",
                    "stage": "index",
                }
            )
        }
    )
    result = adapter.retrieve("revenue")
    assert result == {"error": "not_ready", "cause": "index", "stage": "index"}


def test_a_bare_not_ready_label_without_index_evidence_is_not_recoded() -> None:
    """A generic not-ready label without cause/stage is not evidence of
    index absence (spec: "a generic ``lexical_not_ready`` label or
    silent empty is not evidence"). It keeps its own operational
    reading — ``retrieve_unavailable`` — and is never minted into
    ``not_ready(index)`` here."""
    adapter = _ScriptedAdapter({"result": _error_result({"error": "lexical_not_ready"})})
    assert adapter.retrieve("revenue") == {"error": "retrieve_unavailable"}


# ----- the fin-minted busy shapes -----


def _session_adapter(session: Any, timeout: float = 0.2) -> Any:
    """An adapter bound to a live session object (skip ``__init__``).

    ``timeout`` is the adapter's own budget: a small value so a held
    lock deterministically exhausts it instead of racing the holder's
    release."""
    adapter = MCPRetrieveAdapter.__new__(MCPRetrieveAdapter)
    adapter._command = None
    adapter._locator_map = {}
    adapter._known_doc_ids = None
    adapter._timeout = timeout
    adapter._scripted_response = None
    adapter._scripted_exception = None
    adapter._session = session
    adapter._mapping_ledger = None
    adapter._input_validation_ledger = None
    return adapter


def test_waiter_lock_timeout_mints_not_ready_busy_queue() -> None:
    """A waiter that exhausts the budget on the LOCK answers the exact
    second ``not_ready`` shape — ``cause=busy``, ``stage=queue`` — and
    the session it waited on stays alive (the owner is untouched)."""
    from fin.retrieve.mcp_session import MCPStdioSession

    import queue as _queue
    import threading as _threading

    session = MCPStdioSession.__new__(MCPStdioSession)
    session._command = ("unused",)
    session._cwd = None
    session._timeout = 60.0
    session._explicit_env = None
    session._process = None
    session._responses = _queue.Queue()
    session._io_lock = _threading.RLock()
    session._closed = False
    session._failed = False
    session._stderr = bytearray()
    session._stderr_lock = _threading.Lock()
    session._requests = []

    release = _threading.Event()
    acquired = _threading.Event()

    def _holder() -> None:
        with session._io_lock:
            acquired.set()
            release.wait(5.0)

    holder = _threading.Thread(target=_holder)
    holder.start()
    try:
        assert acquired.wait(5.0)
        adapter = _session_adapter(session)
        assert adapter.retrieve("revenue") == {
            "error": "not_ready",
            "cause": "busy",
            "stage": "queue",
        }
        # The waiter did not quarantine the session it waited on: the
        # owner is untouched. (``is_spent`` cannot say so while the
        # owner still holds the lock — it answers False without
        # looking — so the session's own flag is what is read here.)
        assert session._failed is False
    finally:
        release.set()
        holder.join(5.0)
    # With no owner holding the lock, the session's own state answers
    # for itself: the waiter left it alive.
    assert session.is_spent is False


@pytest.mark.parametrize(
    "stage",
    ["queue", "startup", "response"],
)
def test_budget_error_dict_shape_is_exact(stage: str) -> None:
    """The busy envelope is exactly ``{error, cause, stage}`` — no
    message, no code, no extra fields ride the wire."""
    from fin.retrieve.mcp_adapter import RetrieveBudgetExhaustedError

    error = RetrieveBudgetExhaustedError("budget expired", stage=stage)
    assert error.error_dict() == {
        "error": "not_ready",
        "cause": "busy",
        "stage": stage,
    }
