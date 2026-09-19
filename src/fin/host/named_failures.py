"""具名失败 codes + the decision that picks the retrieve-side one (T3).

CONTEXT **具名失败**: a typed product failure naming empty-bound 资料,
mapping-drop, citation-verifier refuse, 档未开, or 真库未接 as **distinct**
exits — never one ``unknown_locator`` plus empty citations.

Before this module a research send whose retrieve returned nothing
ended the same way whether the bound 资料集合 had no chunks or every
hit was dropped by the mapper: the citation gate refused with its own
reason (``unknown_locator`` / ``citation_repair_cap``) and the operator
saw one card for three different product facts. This module is the
single authority for the two retrieve-side codes and the pure decision
that chooses between them; the citation gate's own vocabulary is
untouched (ADR 0008).

The codes are public wire tokens (spec ``first-closed-loop`` Open
Question 1): snake_case, stable, and asserted by name in the tests —
never by matching prose::

    corpus_not_ready   empty-bound 资料集合: retrieve ran and returned
                       zero chunks with zero mapping failures
    mapping_drop       retrieve returned hits, but every hit failed to
                       map to a fin locator (mapping ledger count > 0)
    tier_not_open      检索档 C 综述 is a plan slot, not implemented
    real_db_not_wired  检索档 D 真数据库 is a plan slot, not implemented
    community_summary_index_failure
                       reserved for once C 综述 ships as an open 档:
                       its 社区 summary index is then not-ready (the
                       Leiden/community-summary job failed or summaries
                       are incomplete, ADR 0064 / 0078) — a runtime
                       fact, distinct from 「档未开」
    invalid_search_mode
                       the documented invalid-mode code on the retrieve
                       side: a filters.search_mode outside A|B|C|D
                       (spec search-mode-retrieve.md Boundaries allows
                       it next to tier_not_open; the iknow retrieve
                       schema answers with this token and
                       fin.retrieve.port.InvalidSearchModeError mirrors
                       it port-side)

``corpus_not_ready`` / ``mapping_drop`` ride ``AgentState.status_reason``
on the run record / SSE ``status`` frame and resolve to ordinary-person
copy through :data:`fin.host.run_payload.ORDINARY_PERSON_COPY`.
``tier_not_open`` / ``real_db_not_wired`` are the routing-time 400
``{"error": <code>}`` (see ``fin.host.workspace_http``): the operator
named a 档 that is not implemented, and the run never starts.

Sibling-repo alignment: the iknow-memory side carries the same
``corpus_not_ready`` / ``mapping_drop`` tokens; see
``plans/first-closed-loop.md`` T3.

**Reach (stated, not accidental):** the decision reads the outcomes of
the single-issuer ``retrieve`` tool — the 阶段一 default path. The
``research_issuers`` fan-out workers do not record outcomes: their
branches run in ONE LangGraph superstep in parallel, and a channel
without a reducer rejects concurrent writes, so a fan-out run keeps
the verifier's own verdict rather than risking a wrong or a crashed
merge. Mapping drops inside a fan-out branch are still counted on the
shared ledger (``_issuer_evidence``), so they remain auditable.
"""

from __future__ import annotations

from typing import Final, Iterable, Mapping, Sequence

# The 档 SSOT — closed set, the Literal derived from it, and the two
# routing-time codes — is DEFINED in the neutral leaf
# :mod:`fin.retrieve.tiers` because the retrieve port validates against
# it and may not import ``fin.host`` (``fin.host.graph`` imports the
# port; review F3). Re-exported here unchanged: this stays the import
# surface every host-side consumer already uses.
from fin.retrieve.tiers import (
    REAL_DB_NOT_WIRED,
    RETRIEVE_TIERS,
    RetrieveTier,
    TIER_NOT_OPEN,
    TIER_UNAVAILABLE_CODE,
)


# ----- the codes (ONE import surface; every surface imports from here) -----

CORPUS_NOT_READY: Final[str] = "corpus_not_ready"

MAPPING_DROP: Final[str] = "mapping_drop"

# Reserved for once C 综述 ships as an open 档: its 社区 summary index
# can then be not-ready (the Leiden / community-summary job failed or
# produced incomplete summaries — ADR 0064 / 0078). That would be a
# runtime index fact, deliberately NOT ``tier_not_open``: today C is
# still a plan slot (``TIER_UNAVAILABLE_CODE`` routes it there), and
# once C is open, telling the operator to name a different 档 for an
# index-job failure would be wrong advice. Public wire token pinned by
# ``specs/search-mode-retrieve.md`` (Boundaries), shared with the
# iknow-memory side.
COMMUNITY_SUMMARY_INDEX_FAILURE: Final[str] = "community_summary_index_failure"

# NOTE (T6 reach): a 档 is invoked ONLY through the explicit selector
# (:data:`RETRIEVE_TIERS` and the send door's ``RetrieveTier`` enum).
# Question prose is never scanned for 档 words —「综述这几份
# 10-Q」 is a perfectly ordinary B ask about a small set of filings,
# and reading it as a C invocation would be the 开放业务意图桶 the
# wayfinder doc forbids. See ``plans/first-closed-loop.md`` T6.


# ----- the retrieve-side decision -----


def retrieve_blocking_failure(
    outcomes: Iterable[Mapping[str, object]] | None,
) -> str | None:
    """The retrieve-side 具名失败 this run must refuse with, or ``None``.

    ``outcomes`` is the run-scoped ``retrieve_outcomes`` channel: one
    ``{"chunks": <int>, "mapping_failures": <int>}`` record per
    completed ``retrieve`` call (``fin.host.graph._make_retrieve_tool``).

    Returning ``None`` is not "healthy": it means the verifier's own
    verdict is the whole truth — either at least one call produced a
    usable chunk, or nothing was retrieved. An unusable record
    (non-int counts, missing keys) contributes nothing rather than
    guessing a cause: an invented failure is worse than the verifier's
    reason.
    """
    saw_call = False
    mapping_failures = 0
    for record in outcomes or ():
        if not isinstance(record, Mapping):
            continue
        chunks = record.get("chunks")
        failures = record.get("mapping_failures")
        if not isinstance(chunks, int) or not isinstance(failures, int):
            continue
        saw_call = True
        if chunks > 0:
            # Retrieval worked at least once this run: a later refusal
            # is the verifier's to make, not retrieve's to pre-empt.
            return None
        mapping_failures = max(mapping_failures, failures)
    if not saw_call:
        return None
    if mapping_failures > 0:
        return MAPPING_DROP
    return CORPUS_NOT_READY


def named_refusal_reason(
    *,
    evidence: Sequence[object],
    outcomes: Iterable[Mapping[str, object]] | None,
    verifier_reason: str,
) -> str:
    """The reason a ``refuse`` must carry for THIS run.

    The retrieve-side 具名失败 is a RE-LABEL of a refusal the verifier
    already made, never a replacement for the verifier: it applies only
    when the run ended with no citable evidence at all, and only when
    retrieve is the reason nothing could be cited. A run that brought
    evidence by any other route (``read_file`` included) keeps the
    verifier's own verdict, because the 具名失败 would then be describing
    a collection the run did not actually depend on.
    """
    if evidence:
        return verifier_reason
    return retrieve_blocking_failure(outcomes) or verifier_reason


def retrieve_outcomes_delta(
    records: Sequence[Mapping[str, object]] | None,
    *,
    baseline: int,
) -> list[dict[str, int]]:
    """The ``retrieve_outcomes`` entries recorded since ``baseline``.

    The counts are clamped to ``>= 0``. The live caller reads its
    records and its ``baseline`` from the same per-thread counter
    (monotonically increasing), so a backwards move is not reachable
    on that path: this helper is pure and a caller may hand it a
    baseline its records do not derive from, and a clamped zero is
    the honest answer there rather than a fabricated negative.

    A record outside the contract (non-int counts, missing keys) is
    SKIPPED rather than coerced: it contributes nothing, exactly as
    :func:`retrieve_blocking_failure` treats it. Guessing at a
    malformed record could invent a 具名失败, and raising would make an
    unreadable measurement take down a tool call that is otherwise
    fine.
    """
    out: list[dict[str, int]] = []
    for record in records or ():
        if not isinstance(record, Mapping):
            continue
        chunks = record.get("chunks")
        failures = record.get("mapping_failures")
        if not isinstance(chunks, int) or not isinstance(failures, int):
            continue
        out.append(
            {
                "chunks": chunks,
                "mapping_failures": max(failures - baseline, 0),
            }
        )
    return out


def unavailable_tier(requested_tiers: Iterable[str] | None) -> str | None:
    """The first requested 档 that is a plan slot, or ``None``.

    ``requested_tiers`` are the operator's explicit selectors; matching
    is case-insensitive because a selector is a small closed token, not
    prose. Only ``C`` / ``D`` are refused here; an unknown token is
    ignored — this helper never guesses that a string "meant" a 档 (the
    same closed-list rule as :data:`TIER_UNAVAILABLE_CODE`).
    """
    for tier in requested_tiers or ():
        if str(tier).strip().upper() in TIER_UNAVAILABLE_CODE:
            return str(tier).strip().upper()
    return None


__all__ = [
    "COMMUNITY_SUMMARY_INDEX_FAILURE",
    "CORPUS_NOT_READY",
    "MAPPING_DROP",
    "REAL_DB_NOT_WIRED",
    "RETRIEVE_TIERS",
    "RetrieveTier",
    "TIER_NOT_OPEN",
    "TIER_UNAVAILABLE_CODE",
    "named_refusal_reason",
    "retrieve_blocking_failure",
    "retrieve_outcomes_delta",
    "unavailable_tier",
]
