"""Translate a raw host-graph result dict into the response payload.

Shared between ``fin.host.http`` (``POST /ask``) and
``fin.host.workspace_http`` (``POST /api/threads/{id}/messages``)
so the two endpoints agree on (status / answer / reason / citations)
extraction. Both routes also depend on the same defensive strip
of the hidden bounce marker (ADR 0021).

Why a module and not a private helper next to one call site? The
two routes duplicate ~30 lines of mapping today; hoisting the
mapping keeps each route under the complexity-anti-drift budget
(function <= 60 lines, cyclomatic <= 10) and makes the wire
contract single-sourced.
"""

from __future__ import annotations

from typing import Any, TypedDict

from fin.host.citation_gate import strip_hidden_bounce_marker
from fin.host.thread_store import RunStatus


class RunResponsePayload(TypedDict):
    """The wire-shape produced by ``_run_to_response_payload``.

    Both ``/ask`` (``AskResponse``) and the SSE ``status`` /
    ``citations`` / ``answer`` frames on ``/messages`` are
    populated from this dict. ``answer`` is empty on ``refuse`` /
    ``step_cap`` per ADR 0021 — no model prose surfaces when the
    gate said no.
    """

    status: RunStatus
    answer: str
    reason: str
    citations: list[dict[str, Any]]


def _run_to_response_payload(raw: dict[str, Any]) -> RunResponsePayload:
    """Map the host graph's result dict into the wire payload.

    WHY each step is here:

    * ``str(... or "")`` defends against a node returning ``None``
      for ``answer`` / ``status_reason`` (the state annotation says
      ``str``, but the intermediate nodes leave those keys unset).
    * ``strip_hidden_bounce_marker`` runs on BOTH ``answer`` and
      ``reason`` so an internal bounce template the gate injected
      cannot leak into a response body (ADR 0021). This is the
      single choke point; neither route re-strips.
    * Citations only populate when ``status == "done"``; on
      ``refuse`` / ``step_cap`` the answer is forced empty and the
      citations list is ``[]`` so the wire shape is consistent no
      matter what the gate stored.
    * An unexpected status value collapses to ``"refuse"`` with an
      empty reason — the same defensive default the ``/ask``
      handler used before this hoist. Callers never branch on a
      status outside the closed enum.
    """
    status_value = raw.get("status")
    answer_value = strip_hidden_bounce_marker(str(raw.get("answer", "") or ""))
    reason_value = strip_hidden_bounce_marker(str(raw.get("status_reason", "") or ""))
    raw_citations = raw.get("citations", ()) or ()

    if status_value == "done":
        citations_payload: list[dict[str, Any]] = [
            {
                "document_id": cit.document_id,
                "version": cit.version,
                "page": cit.page,
                "quote": cit.quote,
            }
            for cit in raw_citations
        ]
        return RunResponsePayload(
            status="done",
            answer=answer_value,
            reason=reason_value,
            citations=citations_payload,
        )

    # ``refuse`` / ``step_cap`` keep their status + reason; anything
    # unexpected collapses to ``refuse`` with no detail.
    if status_value in ("refuse", "step_cap"):
        collapsed_status: RunStatus = status_value  # type: ignore[assignment]
    else:
        collapsed_status = "refuse"
        reason_value = ""

    return RunResponsePayload(
        status=collapsed_status,
        answer="",
        reason=reason_value,
        citations=[],
    )


__all__ = ["RunResponsePayload", "_run_to_response_payload"]
