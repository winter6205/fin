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

from typing import Any, Final, TypedDict

from fin.host.citation_gate import strip_hidden_bounce_marker
from fin.host.state import STEP_CAP
from fin.host.thread_store import RunStatus


class RunResponsePayload(TypedDict):
    """The wire-shape produced by ``_run_to_response_payload``.

    Both ``/ask`` (``AskResponse``) and the SSE ``status`` /
    ``citations`` / ``answer`` frames on ``/messages`` are
    populated from this dict. ``answer`` is empty on ``refuse`` /
    ``step_cap`` per ADR 0021 — no model prose surfaces when the
    gate said no.

    ``assistant_message`` is the ordinary-person copy (T2 / spec
    SC67) the SPA renders when the run ended ``refuse`` or
    ``step_cap``. Empty on ``done`` (the run produced a real
    ``answer``). The workspace SSE ``status`` frame and the
    persisted ``RunRecord`` both carry this string so a reload
    never loses the copy. ``/ask`` ignores the field because the
    AskResponse wire shape is frozen.

    T3 streaming fields: ``thinking_text`` and ``tool_summaries``
    are surfaced here (already bounce-marker-stripped) so the
    workspace SSE + RunRecord persistence reach the SSOT helper.
    ``/ask`` ignores both — the AskResponse wire shape is frozen
    (the streaming shapes live only on the workspace surface).

    Soft-Track T5: ``track`` mirrors the graph decision for the
    SSE status frame; ``track_decision`` is the full auditable
    dict persisted on ``RunRecord`` / GET history.

    T3 (enterprise-kb Focus 1): ``comparisons`` carries the
    ``fin.comparison.v1`` envelopes this run's ``compare_facts``
    calls produced (shape SSOT:
    ``fin.host.tools.compare_facts``). Unlike ``answer`` /
    ``citations`` it survives a ``refuse``: the table is TOOL
    output, and on ``numeric_gate_fail`` it is precisely what
    contradicted the answer. ``/ask`` ignores it — the AskResponse
    wire shape is frozen.

    Trust (the tracked change, spec SC8): ``evidence_locators`` lists every
    locator this run retrieved or read — populated on ``refuse`` /
    ``step_cap`` too, because the point is auditing what the run
    looked at, not repeating what the verifier passed.
    """

    status: RunStatus
    answer: str
    reason: str
    citations: list[dict[str, Any]]
    assistant_message: str
    thinking_text: str
    tool_summaries: list[dict[str, str]]
    track: str
    track_decision: dict[str, Any]
    comparisons: list[dict[str, Any]]
    evidence_locators: list[dict[str, Any]]


# Ordinary-person copy the SPA renders when a run ended
# ``refuse`` or ``step_cap`` (T2 / spec SC67). The mapping lives
# here so the workspace send path, the ``/ask`` route, and the
# CLI's stderr hint all reach for the SAME strings — single
# source of truth for the wording (Standards #14).
ORDINARY_PERSON_COPY: Final[dict[str, str]] = {
    # Gate's unknown-locator path: the model claimed a
    # ``document_id`` shape but no in-corpus filing matches.
    "unknown_locator": (
        "I can't ground this answer in the frozen 10-Qs. Try a "
        "more specific question."
    ),
    # Gate bounced REPAIR_CAP times without grounding the answer.
    "citation_repair_cap": (
        "I couldn't match the answer to the cited pages after a "
        "few tries - try rephrasing or narrower scope."
    ),
    # Numeric gate (ADR 0031): the answer's figure did not match
    # any XBRL fact row the model looked up this run.
    "numeric_gate_fail": (
        "I couldn't match the figures in that answer to the numbers "
        "reported in the filing. Try asking for one figure at a time."
    ),
    # Fin-owned step-cap fuse fired before a terminal decision.
    "step_cap": (
        f"Hit the {STEP_CAP}-step fuse. Try breaking this into a "
        "smaller question."
    ),
}


def ordinary_person_copy(status: str, reason: str) -> str:
    """Return the ordinary-person copy for a terminal status.

    ``done`` -> ``""`` (the run produced a real ``answer``).
    ``refuse`` -> keyed by ``reason`` (``unknown_locator`` /
    ``citation_repair_cap`` / ``numeric_gate_fail``).
    ``step_cap`` -> the step-cap string regardless of ``reason``.
    Unknown terminal -> ``""`` (defensive; the gate never
    produces one).

    The function is intentionally NOT order-sensitive: callers
    pass the (status, reason) the gate emitted, and the helper
    does the dispatch.
    """
    if status == "done":
        return ""
    if status == "step_cap":
        return ORDINARY_PERSON_COPY["step_cap"]
    if status == "refuse":
        return ORDINARY_PERSON_COPY.get(reason, "")
    return ""


def _evidence_locators(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """List this run's retrieved / read locators, deduplicated.

    Reads ``AgentState["evidence"]`` — the run-scoped list the tool
    node appends to and ``reset_input_state`` clears between runs
    (ADR 0016) — so the value is always THIS run's, never the
    thread's history. Retrieve contributes several chunks per page,
    so identical ``{document_id, version, page}`` triples collapse
    to one entry in first-touch order.

    An entry without a well-formed ``locator`` is skipped rather
    than crashing the response: the turn row is a presentation
    surface, and the citation gate already rejects malformed
    evidence on the path that matters (``MalformedEvidenceError``).
    """
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str, int]] = set()
    for item in raw.get("evidence", ()) or ():
        locator = getattr(item, "locator", None)
        if locator is None:
            continue
        key = (
            str(getattr(locator, "document_id", "")),
            str(getattr(locator, "version", "")),
            int(getattr(locator, "page", 0) or 0),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {"document_id": key[0], "version": key[1], "page": key[2]}
        )
    return out


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

    # T3 streaming fields: pull thinking prose and one-liner tool
    # summaries off the raw state. ``strip_hidden_bounce_marker``
    # runs on the thinking text and on every summary string so a
    # gate-emitted marker cannot leak into the workspace turn row
    # (ADR 0021). The shapes come from
    # ``graph.model_node`` / ``graph._build_tool_summaries`` and
    # are defaulted to empty strings / empty lists if the run
    # ended before either was populated (e.g. a refused first
    # turn with no model call).
    thinking_value = strip_hidden_bounce_marker(
        str(raw.get("thinking_text", "") or "")
    )
    raw_summaries = raw.get("tool_summaries", ()) or ()
    summaries_value: list[dict[str, str]] = []
    if isinstance(raw_summaries, list):
        for entry in raw_summaries:
            if not isinstance(entry, dict):
                continue
            summaries_value.append(
                {
                    "name": str(entry.get("name", "")),
                    "summary": strip_hidden_bounce_marker(
                        str(entry.get("summary", ""))
                    ),
                    "tool_call_id": str(entry.get("tool_call_id", "")),
                }
            )

    # T3: the comparison channel is graph-owned structured data, so
    # it passes through unshaped — no marker strip (no model prose
    # in a fact table) and no re-validation of the envelope. Only
    # non-dict entries are dropped, so a malformed channel cannot
    # reach a response model.
    raw_comparisons = raw.get("comparisons", ()) or ()
    comparisons_value: list[dict[str, Any]] = []
    if isinstance(raw_comparisons, list):
        comparisons_value = [c for c in raw_comparisons if isinstance(c, dict)]

    locators_value = _evidence_locators(raw)

    raw_td = raw.get("track_decision") or {}
    track_decision_value: dict[str, Any] = (
        dict(raw_td) if isinstance(raw_td, dict) else {}
    )
    track_value = str(
        raw.get("track")
        or track_decision_value.get("track")
        or ""
    )

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
            # ``done`` never carries ordinary-person copy (the
            # answer IS the answer); empty string keeps the
            # wire shape stable across terminal statuses.
            assistant_message="",
            thinking_text=thinking_value,
            tool_summaries=summaries_value,
            track=track_value,
            track_decision=track_decision_value,
            comparisons=comparisons_value,
            evidence_locators=locators_value,
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
        # Ordinary-person copy is what the SPA renders in the
        # answer-bubble slot when ``answer`` is empty (T2).
        # The mapping is reason-aware for ``refuse`` and
        # status-fixed for ``step_cap``; see ``ordinary_person_copy``.
        assistant_message=ordinary_person_copy(collapsed_status, reason_value),
        # Thinking + tool summaries still travel on ``refuse`` /
        # ``step_cap`` if a model turn produced them before the
        # fuse fired; the SPA's turn row shows them. Empty when
        # the run ended before any model call.
        thinking_text=thinking_value,
        tool_summaries=summaries_value,
        track=track_value,
        track_decision=track_decision_value,
        # The comparison SURVIVES the refusal (unlike answer /
        # citations): a ``numeric_gate_fail`` row that hid the fact
        # table would hide the evidence for its own refusal.
        comparisons=comparisons_value,
        # SC8: a refused run still records the locators it read.
        # ``citations`` is empty here BECAUSE the verifier said no;
        # the two fields answer different questions.
        evidence_locators=locators_value,
    )


__all__ = [
    "ORDINARY_PERSON_COPY",
    "RunResponsePayload",
    "_run_to_response_payload",
    "ordinary_person_copy",
]
