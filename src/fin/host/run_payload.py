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

import json
import logging
import re
from collections.abc import Mapping
from functools import lru_cache
from typing import Any, Final, TypedDict

from fin.corpus.catalog import list_filings
from fin.corpus.errors import CatalogCorruptError
from fin.host.grounded.citation_gate import (
    ATTACHMENT_KIND,
    COMPANY_KIND,
    DATABASE_ROW_KIND,
    DATABASE_ROW_SNAPSHOT_KEY,
    Citation,
    database_row_snapshot,
    strip_hidden_bounce_marker,
)
from fin.host.grounded.citation_gate import EvidenceItem
from fin.host.degradation_notice import run_degraded
from fin.host.grounded.grounded_decision import CANDIDATE_TERMINATOR
from fin.host.grounded.readable_projection import (
    FAILURE_CLASS_BY_REASON,
    HIDDEN_MARK,
    OPAQUE_SOURCE_LABEL,
    UNNAMED_ATTACHMENT_LABEL,
    ordinary_person_copy,
    project_admitted_answer,
    source_label,
)
from fin.host.thread_store import RunStatus
from fin.retrieve.mcp_adapter import retrieval_window_of

_LOG = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def corpus_is_finance() -> bool:
    """Whether this deployment's corpus really is a filings corpus.

    ``readable_projection`` has exactly one conditional, and it decides
    whether ``10-Q`` / ``XBRL`` / ``EDGAR`` are the ask's own subject
    matter or leaked boilerplate. Guessing it per answer would mean the
    same word is a legitimate label on one turn and a leak on the next
    with nothing in the run to tell them apart, so it is answered once
    for the process from the corpus catalog — which IS the filing table,
    and the honest answer to "is this a finance corpus?".

    Cached because it reads a file and this runs per answer. NOT
    invalidated on ingest: an empty catalog reads as non-finance and the
    answer is a filter strictness, not a permission, so a stale reading
    costs a redacted word rather than a wrong result. The process that
    ingests the first filing is a fresh boot anyway.
    """
    try:
        rows = list_filings()["filings"]
    except (CatalogCorruptError, OSError):
        # A corrupt or unreadable catalog is not evidence of a finance
        # corpus, and the conservative reading is the stricter filter.
        return False
    return any(row.get("form") for row in rows)


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

    search-mode host T1: ``search_mode_decision`` mirrors the
    ``track_decision`` pattern for the 检索档 audit (``search_mode``
    or explicit ``null`` on typed fail, ``confidence``,
    ``decision_tier``, structured ``signals``, the ``options`` list
    (empty on every tier the cascade produces — only a legacy clarify
    receipt carries one) and the ``failure_code`` 具名失败). Empty dict
    on runs that never reached grounded.

    T3 (enterprise-kb Focus 1): ``comparisons`` carries the
    ``fin.comparison`` envelopes this run's ``compare_facts``
    calls produced (shape SSOT:
    ``fin.host.tools.compare_facts``). Unlike ``answer`` /
    ``citations`` it survives a ``refuse``: the table is TOOL
    output, and on ``numeric_gate_fail`` it is precisely what
    contradicted the answer. ``/ask`` ignores it — the AskResponse
    wire shape is frozen.

    Trust (spec SC8): ``evidence_locators`` lists every
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
    search_mode_decision: dict[str, Any]
    comparisons: list[dict[str, Any]]
    analysis_results: list[dict[str, Any]]
    evidence_locators: list[dict[str, Any]]
    retrieval_window: dict[str, str]


_SECTION_LINE: Final[re.Pattern[str]] = re.compile(
    r"^\s*Section\s+(\d+(?:\.\d+)*)\b", re.IGNORECASE | re.MULTILINE
)


def citation_sections_from_evidence(
    citations: list[dict[str, Any]], evidence: Any
) -> list[str | None]:
    """Return display-only section labels from matching trusted evidence.

    ``quote`` is only the matched substring, so it cannot establish the
    document section. This reads the full admitted ``EvidenceItem`` unit
    text instead. A section is omitted when no trusted heading exists or
    matching evidence disagrees; stored citation rows remain untouched.
    """
    section_by_identity: dict[tuple[Any, ...], set[str]] = {}
    trusted_items = evidence if isinstance(evidence, (list, tuple)) else ()
    for item in trusted_items:
        if not isinstance(item, EvidenceItem) or item.kind == DATABASE_ROW_KIND:
            continue
        locator = item.locator
        match = _SECTION_LINE.search(item.unit_text)
        if match is None:
            continue
        identity = (
            item.kind,
            locator.document_id,
            item.attachment_id,
            locator.version,
            locator.unit,
            item.display,
            item.filename,
        )
        section_by_identity.setdefault(identity, set()).add(
            f"Section {match.group(1)}"
        )

    sections: list[str | None] = []
    for citation in citations:
        identity = (
            citation.get("kind"),
            citation.get("document_id"),
            citation.get("attachment_id"),
            citation.get("version"),
            citation.get("unit"),
            citation.get("display"),
            citation.get("filename"),
        )
        candidates = section_by_identity.get(identity, set())
        sections.append(next(iter(candidates)) if len(candidates) == 1 else None)
    return sections


def _citation_field(citation: Citation | Mapping[str, Any], name: str) -> Any:
    """One citation field, read off the dataclass or its checkpoint mapping.

    A checkpoint restores ``Citation`` as a plain mapping, not the
    dataclass, so the replay path hands the shaper that form. Both forms
    must publish the same wire shape, so the fields are read here once
    rather than at every use. A mapping may predate a wire field — the
    dataclass never can — and a missing key reads as the empty string the
    dataclass would carry, not as ``None``: one wire shape, never a second
    one with nulls in it.
    """
    if isinstance(citation, Mapping):
        return citation.get(name, "")
    return getattr(citation, name)


def citation_to_wire(
    citation: Citation | Mapping[str, Any],
) -> dict[str, Any]:
    """One ``Citation`` as the wire dict the shared shaper publishes.

    A ledger row's retained snapshot rides on
    :data:`~fin.host.grounded.citation_gate.DATABASE_ROW_SNAPSHOT_KEY`, assembled
    by the gate family's single owner
    (:func:`~fin.host.grounded.citation_gate.database_row_snapshot`), so every
    surface that reads this dict — the ``/ask`` response, the workspace
    SSE frame, the turn table — offers the same evidence and opening it
    never means a query (spec 8.1). The key is OMITTED for the two
    document kinds rather than set to ``None``: their rows are already
    stored in ``fin_run.citations``, and adding a key to every one of
    them is a migration nothing here can do.
    """
    kind = _citation_field(citation, "kind")
    out: dict[str, Any] = {
        "kind": kind,
        "document_id": _citation_field(citation, "document_id"),
        "attachment_id": _citation_field(citation, "attachment_id"),
        "version": _citation_field(citation, "version"),
        "unit": _citation_field(citation, "unit"),
        "display": _citation_field(citation, "display"),
        "filename": _citation_field(citation, "filename"),
        "quote": _citation_field(citation, "quote"),
    }
    if kind == DATABASE_ROW_KIND:
        snapshot = database_row_snapshot(citation)
        if snapshot is not None:
            out[DATABASE_ROW_SNAPSHOT_KEY] = snapshot
    return out


# Ordinary-person copy the SPA renders when a run ended ``refuse`` or
# ``step_cap`` (T2 / spec SC67). The SENTENCES live in
# :mod:`fin.host.grounded.readable_projection` — the one module that decides what
# ordinary-person Chinese copy a class of failure gets — and this table
# is DERIVED from them rather than restated. It existed as a second,
# hand-maintained English table beside the frozen Chinese one, and the
# two shipped at once: the projection's four distinct sentences reached
# nothing but its own tests while these strings were what a refusing run
# actually rendered. One owner, one table, one sentence per class.
ORDINARY_PERSON_COPY: Final[dict[str, str]] = {
    reason: ordinary_person_copy("refuse", reason)
    for reason in FAILURE_CLASS_BY_REASON
}


def _evidence_locators(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """List this run's retrieved / read locators, deduplicated.

    Reads ``AgentState["evidence"]`` — the run-scoped list the tool
    node appends to and ``reset_input_state`` clears between runs
    (ADR 0016) — so the value is always THIS run's, never the
    thread's history. Retrieve contributes several chunks per unit,
    so identical ``{document_id, version, unit}`` triples collapse
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
            int(getattr(locator, "unit", 0) or 0),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {"document_id": key[0], "version": key[1], "unit": key[2]}
        )
    return out


def _retrieval_window(raw: dict[str, Any]) -> dict[str, str]:
    """Last retrieve tool message that carried any window string."""
    window: dict[str, str] = {}
    for message in raw.get("messages") or ():
        if getattr(message, "name", None) != "retrieve":
            continue
        content = getattr(message, "content", None)
        if not isinstance(content, str):
            continue
        try:
            payload = json.loads(content)
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        found = retrieval_window_of(payload)
        if found:
            window = found
    return window


def _citation_identity(citation: Any) -> tuple[str, str, str, str] | None:
    """An exact admitted document identity and its trusted attachment name."""
    if isinstance(citation, dict):
        kind = citation.get("kind")
        document_id = citation.get("document_id")
        attachment_id = citation.get("attachment_id")
        version = citation.get("version")
        filename = citation.get("filename")
    else:
        kind = getattr(citation, "kind", None)
        document_id = getattr(citation, "document_id", None)
        attachment_id = getattr(citation, "attachment_id", None)
        version = getattr(citation, "version", None)
        filename = getattr(citation, "filename", None)
    source_id = document_id if kind == COMPANY_KIND else attachment_id
    if (
        kind not in (COMPANY_KIND, ATTACHMENT_KIND)
        or not isinstance(source_id, str)
        or not source_id
        or not isinstance(version, str)
        or not version
    ):
        return None
    trusted_filename = (
        filename if kind == ATTACHMENT_KIND and isinstance(filename, str) else ""
    )
    return (kind, source_id, version, trusted_filename)


def _admitted_source_labels(
    citations: Any,
) -> dict[tuple[str, str, str, str], str]:
    """Readable labels keyed only by this run's admitted source identities."""
    identities = {
        identity
        for citation in citations
        if (identity := _citation_identity(citation)) is not None
    }
    if not identities:
        return {}
    if any(identity[0] == COMPANY_KIND for identity in identities):
        try:
            rows = list_filings()["filings"]
        except (CatalogCorruptError, OSError) as exc:
            # EXIT: an unreadable catalog cannot verify a human title, so each
            # admitted company below receives the shared opaque label.
            _LOG.warning(
                "answer source-label metadata unavailable (%s)",
                type(exc).__name__,
            )
            rows = []
    else:
        rows = []
    rows_by_identity = {
        (row.get("document_id"), row.get("version")): row for row in rows
    }
    labels: dict[tuple[str, str, str, str], str] = {}
    for identity in identities:
        kind, source_id, version, filename = identity
        if kind == ATTACHMENT_KIND:
            label = source_label(
                {"kind": ATTACHMENT_KIND, "filename": filename}
            )
            labels[identity] = (
                UNNAMED_ATTACHMENT_LABEL if HIDDEN_MARK in label else label
            )
            continue
        row = rows_by_identity.get((source_id, version))
        title = ""
        if row is not None:
            company = row.get("company")
            period = row.get("period")
            if (
                isinstance(company, str)
                and company.strip()
                and isinstance(period, str)
                and period.strip()
            ):
                title = f"{company.strip()} · {period.strip()}"
        label = source_label(
            {"kind": COMPANY_KIND, "document_id": source_id},
            title=title,
            corpus_is_finance=True,
        )
        labels[identity] = (
            OPAQUE_SOURCE_LABEL if HIDDEN_MARK in label else label
        )
    return labels


def _replace_admitted_document_ids(
    text: str, *, labels: dict[Any, str]
) -> str:
    """Replace exact admitted document IDs, leaving all citation data intact."""
    for (_kind, source_id, _version, _filename), label in sorted(
        labels.items(), key=lambda entry: len(entry[0][1]), reverse=True
    ):
        pattern = re.compile(
            rf"(?<![\w-]){re.escape(source_id)}(?![\w-])"
        )
        text = pattern.sub(lambda _match: label, text)
    return text


def _project_admitted_text(text: str, *, labels: dict[Any, str]) -> str:
    """The one admitted-ID replacement + projection both reader surfaces use."""
    readable = _replace_admitted_document_ids(text, labels=labels)
    return project_admitted_answer(
        [readable], corpus_is_finance=corpus_is_finance()
    )


def _admitted_citation_quote(
    quote: str,
    *,
    labels: dict[Any, str],
) -> str:
    """Project a citation quote exactly as the answer prose is projected.

    The answer replaces admitted document IDs with readable labels; a
    quote that kept the raw ID would no longer be found inside the prose,
    so the inline marker and its excerpt would vanish for exactly the
    citations that name their source. Both surfaces share one projection.
    A quote never carries the candidate terminator, so it skips the
    answer-only cut.
    """
    return _project_admitted_text(quote, labels=labels)


def _wire_citations(
    citations: Any, labels: dict[Any, str]
) -> list[dict[str, Any]]:
    """The wire citations, each quote projected with the answer's labels.

    The full citation contract goes on the wire, not just the company
    triple: an attachment citation carries no ``document_id`` and is only
    openable from its identity plus ``display`` / ``filename``, and a
    ledger row names neither id and is only openable from its own
    retained snapshot.
    """
    payloads: list[dict[str, Any]] = []
    for cit in citations:
        wire = citation_to_wire(cit)
        quote = wire.get("quote")
        if isinstance(quote, str) and quote:
            wire["quote"] = _admitted_citation_quote(quote, labels=labels)
        payloads.append(wire)
    return payloads


def _admitted_answer_text(
    answer: str,
    *,
    citations: Any = (),
    labels: dict[Any, str] | None = None,
) -> str:
    """``answer`` with the unadmitted payload removed, then projected.

    This is the single entry every reader surface goes through (GE-5:
    browser, SSE, history and export use the same admitted projection), so
    the rule lives here once rather than being restated per surface. The SSE
    terminal frame, the persisted ``RunRecord.answer`` the history route
    reads and the export's copy all originate from this function's return
    value.

    Two steps, in this order and both necessary:

    * cut at :data:`CANDIDATE_TERMINATOR` — the terminator and everything
      it introduces are the model's candidate payload, which admission
      either turned into a ``document_choice`` frame or discarded as
      malformed. It is never part of the answer, and on the malformed path
      the verifier is handed the ORIGINAL output terminator and all, so its
      verdict's text is the one place those bytes can still surface.
    * :func:`project_admitted_answer` over the cut text, which joins before
      scanning so a leak split across the stream's deltas reassembles and is
      then filtered. ``corpus_is_finance`` is read from the catalog
      rather than defaulted, because a filing corpus that had its own
      vocabulary redacted would be a projection that destroys the ask.
    """
    cut = answer.find(CANDIDATE_TERMINATOR)
    admitted = answer[:cut] if cut >= 0 else answer
    if labels is None:
        labels = _admitted_source_labels(citations)
    return _project_admitted_text(admitted, labels=labels)


def _run_to_response_payload(
    raw: dict[str, Any], *, degraded: bool = False
) -> RunResponsePayload:
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
    * ``degraded`` is the BOOLEAN half of §B3's signal, never its
      reason strings: a degraded terminal must read as a partial
      result (T10's 「部分资料暂不可用」), and forwarding the raw
      reasons here would put a backend capability name in the
      default view. The reasons stay on the access-controlled
      ledger.
    """
    # The run's own record that it consumed a degraded response. Read
    # here rather than at the two call sites so a payload built for the
    # SSE terminal frame, the persisted turn row and the ``/ask``
    # response cannot disagree about whether the run was degraded —
    # the same one-projection rule as the answer itself.
    degraded = degraded or run_degraded(raw.get("tool_summaries"))
    status_value = raw.get("status")
    raw_citations = raw.get("citations", ()) or ()
    reason_value = strip_hidden_bounce_marker(str(raw.get("status_reason", "") or ""))

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

    raw_analysis = raw.get("analysis_results", ()) or ()
    analysis_value: list[dict[str, Any]] = []
    if isinstance(raw_analysis, list):
        analysis_value = [entry for entry in raw_analysis if isinstance(entry, dict)]

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

    # search-mode host T1: structured graph-owned data (same passthrough
    # policy as ``track_decision`` / ``comparisons`` — no marker strip,
    # no re-validation of the envelope).
    raw_smd = raw.get("search_mode_decision") or {}
    search_mode_decision_value: dict[str, Any] = (
        dict(raw_smd) if isinstance(raw_smd, dict) else {}
    )

    if status_value == "done":
        # One catalog read for every projection below: the answer and
        # each quote must share the SAME labels, or a quote can name a
        # source the reader's prose no longer contains.
        source_labels = _admitted_source_labels(raw_citations)
        answer_value = _admitted_answer_text(
            strip_hidden_bounce_marker(str(raw.get("answer", "") or "")),
            citations=raw_citations,
            labels=source_labels,
        )
        # The full citation contract goes on the wire, not just the
        # company triple; each quote is projected with the same labels as
        # the answer so the reader's exact-substring match holds.
        citations_payload = _wire_citations(raw_citations, source_labels)
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
            search_mode_decision=search_mode_decision_value,
            comparisons=comparisons_value,
            analysis_results=analysis_value,
            evidence_locators=locators_value,
            retrieval_window=_retrieval_window(raw),
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
        assistant_message=ordinary_person_copy(
            collapsed_status, reason_value, degraded=degraded
        ),
        # Thinking + tool summaries still travel on ``refuse`` /
        # ``step_cap`` if a model turn produced them before the
        # fuse fired; the SPA's turn row shows them. Empty when
        # the run ended before any model call.
        thinking_text=thinking_value,
        tool_summaries=summaries_value,
        track=track_value,
        track_decision=track_decision_value,
        search_mode_decision=search_mode_decision_value,
        # The comparison SURVIVES the refusal (unlike answer /
        # citations): a ``numeric_gate_fail`` row that hid the fact
        # table would hide the evidence for its own refusal.
        comparisons=comparisons_value,
        analysis_results=analysis_value,
        # SC8: a refused run still records the locators it read.
        # ``citations`` is empty here BECAUSE the verifier said no;
        # the two fields answer different questions.
        evidence_locators=locators_value,
        retrieval_window=_retrieval_window(raw),
    )


__all__ = [
    "ORDINARY_PERSON_COPY",
    "RunResponsePayload",
    "_run_to_response_payload",
    "corpus_is_finance",
    "ordinary_person_copy",
]
