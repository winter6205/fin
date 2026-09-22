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
    tier_not_open      a 检索档 that is a routing-time plan slot, not a
                       real arm (token formerly spelled ``C`` for global
                       综述). The global 档 is now open, so this code has
                       no live producer; it stays in the public vocabulary
                       as a distinct, reserved exit, and 「档未开」 is not
                       what a runtime summary-index failure is (that is
                       ``community_summary_index_failure`` below)
    real_db_not_wired  检索档 sql 真数据库 is a plan slot, not
                       implemented (token formerly spelled ``D``)
    community_summary_index_failure
                       the open global 综述 档's runtime not-ready exit:
                       its 社区 summary index is not-ready (the
                       Leiden/community-summary job failed or summaries
                       are incomplete, ADR 0064 / 0078) — a runtime
                       fact, distinct from 「档未开」, and never invented
                       from a bare empty
    empty_fast_graph_failure
                       the Fast 抽图 job built an EMPTY entity-relation
                       graph over the 资料 set (spec
                       graph-index-standard-fast SC5) — neither 资料
                       未就绪 (the documents are there, the method
                       failed them) nor 档未开 (Fast is an extract
                       method, never a 检索档 token). Rides the same
                       ``status_reason`` channel as the two above.
                       Public spelling confirmed 2026-09-21
                       (spec Open Question 1); the TYPE and
                       distinctness are settled and tested.
    invalid_search_mode
                       the documented invalid-mode code on the retrieve
                       side: a filters.search_mode outside the closed 档
                       set named by ``fin.retrieve.tiers``
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
from fin.retrieve.port import (
    RETRIEVE_OUTCOME_COMMUNITY_SUMMARY_UNREADY,
    RETRIEVE_OUTCOME_FAST_GRAPH_EMPTY,
)
from fin.retrieve.tiers import (
    COMMUNITY_SUMMARY_INDEX_FAILURE,
    LASTING_RETRIEVE_TIERS,
    REAL_DB_NOT_WIRED,
    RETRIEVE_TIERS,
    RetrieveTier,
    TIER_LABELS,
    TIER_NOT_OPEN,
    TIER_UNAVAILABLE_CODE,
    normalize_tier_token,
)


# ----- the codes (ONE import surface; every surface imports from here) -----

CORPUS_NOT_READY: Final[str] = "corpus_not_ready"

MAPPING_DROP: Final[str] = "mapping_drop"

# The global 综述 档 is open, and this is its runtime not-ready exit: the
# 社区 summary index the report arm reads is absent or unpopulated (the
# Leiden / community-summary job failed or produced incomplete summaries —
# ADR 0064 / 0078). It is deliberately NOT ``tier_not_open`` (the 档 runs a
# real arm now) and never invented from a bare empty (that stays
# ``corpus_not_ready``). The value is DEFINED in the neutral leaf
# :mod:`fin.retrieve.tiers` so the retrieve adapter can match it without a
# host import; re-exported here so every host-side surface keeps working.
# Public wire token pinned by ``specs/search-mode-retrieve.md`` (Boundaries),
# shared with the iknow-memory side.
# ``COMMUNITY_SUMMARY_INDEX_FAILURE`` is imported from ``fin.retrieve.tiers``
# above and re-exported via ``__all__`` below.

# The Fast 抽图 job's empty-graph exit (spec graph-index-standard-fast
# SC5): a typed 具名失败, deliberately NOT ``corpus_not_ready`` (the
# 资料 are present; the extract method found no entity-relation graph
# in them) and NOT ``tier_not_open`` (Fast is an extract method, never
# a 检索档). Same reserved-token construct as the code above; the
# public spelling confirmed 2026-09-21 (spec Open Question 1) — the
# tests assert the TYPE and the distinctness.
EMPTY_FAST_GRAPH_FAILURE: Final[str] = "empty_fast_graph_failure"

# NOTE (T6 reach): a 档 is invoked ONLY through the explicit selector
# (the send door's ``tiers`` enum, typed to the closed set
# :data:`RETRIEVE_TIERS`, so a name outside it is a schema refusal rather
# than a spelling to resolve). Question prose is never scanned
# for 档 words —「综述这几份 10-Q」 is a perfectly ordinary 知识问答 ask
# about a small set of filings, and reading it as a 综述 invocation would
# be the 开放业务意图桶 the wayfinder doc forbids. See
# ``plans/first-closed-loop.md`` T6.


# ----- the retrieve-side decision -----


# The RECORDED job facts, in the priority order that resolves a blocking
# failure ahead of the mapping / empty-collection readings: a recorded job
# fact EXPLAINS the empty, so it outranks the inference. One row is the
# whole contract — adding a 档's job fact appends here and nowhere else,
# never a new boolean in the decision or a new copy branch at a call site.
# The order between these two is immaterial (a send is never
# simultaneously a 综述 and a Fast-graph send); the tuple spelling keeps
# the resolution data-driven rather than an if-ladder.
_RETRIEVE_JOB_FACTS: Final[tuple[tuple[str, str], ...]] = (
    (RETRIEVE_OUTCOME_FAST_GRAPH_EMPTY, EMPTY_FAST_GRAPH_FAILURE),
    (
        RETRIEVE_OUTCOME_COMMUNITY_SUMMARY_UNREADY,
        COMMUNITY_SUMMARY_INDEX_FAILURE,
    ),
)

_JOB_FACT_KEYS: Final[frozenset[str]] = frozenset(
    key for key, _ in _RETRIEVE_JOB_FACTS
)


def _valid_counts(record: object) -> tuple[int, int] | None:
    """The ``(chunks, mapping_failures)`` of a contract-conforming record.

    ``None`` when the record contributes nothing — a non-Mapping, or a
    non-int count. An invented failure is worse than skipping an
    unreadable record, so both the decision and the delta rebuild treat a
    malformed entry this one way.
    """
    if not isinstance(record, Mapping):
        return None
    chunks = record.get("chunks")
    failures = record.get("mapping_failures")
    if not isinstance(chunks, int) or not isinstance(failures, int):
        return None
    return chunks, failures


def _recorded_job_fact_keys(record: Mapping[str, object]) -> set[str]:
    """The job-fact keys this record explicitly marks ``True``.

    Only an explicit ``True`` is the fact: fin never upgrades a plain
    empty collection into a job failure, so a lookalike or absent flag is
    left out here exactly as it is by the copy helper below.
    """
    return {key for key in _JOB_FACT_KEYS if record.get(key) is True}


def copy_recorded_job_facts(
    source: Mapping[str, object],
    target: dict[str, object],
) -> dict[str, object]:
    """Copy every RECORDED job fact from ``source`` onto ``target``.

    Shared by the tool seam (``fin.host.graph._make_retrieve_tool``) and
    the :func:`retrieve_outcomes_delta` rebuild so one more job fact is one
    row in :data:`_RETRIEVE_JOB_FACTS`, not a second copy site. Returns
    ``target`` for chaining.
    """
    for key in _recorded_job_fact_keys(source):
        target[key] = True
    return target


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

    A record may additionally carry a RECORDED job fact — the Fast 抽图
    empty-graph fact under :data:`RETRIEVE_OUTCOME_FAST_GRAPH_EMPTY` or the
    global 综述 index-not-ready fact under
    :data:`RETRIEVE_OUTCOME_COMMUNITY_SUMMARY_UNREADY` — and only an
    explicit ``True`` is that fact: fin never upgrades a plain empty
    collection into a job failure (that would blame a job which never
    reported one). When the run ends with no chunk anywhere, a recorded
    job fact wins over the mapping / empty-collection readings, because it
    EXPLAINS why nothing mapped or matched; :data:`_RETRIEVE_JOB_FACTS`
    carries that priority.
    """
    saw_call = False
    recorded_job_facts: set[str] = set()
    mapping_failures = 0
    for record in outcomes or ():
        counts = _valid_counts(record)
        if counts is None:
            continue
        chunks, failures = counts
        saw_call = True
        if chunks > 0:
            # Retrieval worked at least once this run: a later refusal
            # is the verifier's to make, not retrieve's to pre-empt.
            return None
        recorded_job_facts |= _recorded_job_fact_keys(record)
        mapping_failures = max(mapping_failures, failures)
    if not saw_call:
        return None
    for key, code in _RETRIEVE_JOB_FACTS:
        if key in recorded_job_facts:
            return code
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
) -> list[dict[str, object]]:
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

    A RECORDED job fact (an explicit ``True`` under a
    :data:`_RETRIEVE_JOB_FACTS` key) rides the rebuild — the tool seam
    reconstructs records through this helper, and dropping the fact here
    would erase the only path the decision has to see it. Any other flag
    value (absent, False, a lookalike) is left off the rebuilt record: the
    output shape stays the plain two-count contract for every ordinary
    call.
    """
    out: list[dict[str, object]] = []
    for record in records or ():
        counts = _valid_counts(record)
        if counts is None:
            continue
        chunks, failures = counts
        entry: dict[str, object] = {
            "chunks": chunks,
            "mapping_failures": max(failures - baseline, 0),
        }
        out.append(copy_recorded_job_facts(record, entry))
    return out


def unavailable_tier(requested_tiers: Iterable[str] | None) -> str | None:
    """The first requested 档 that is a plan slot, or ``None``.

    ``requested_tiers`` are the operator's explicit selectors and they
    arrive ALREADY TYPED over the closed set: the send door's
    ``tiers: list[RetrieveTier]`` field (:mod:`fin.host.workspace_http`)
    refuses every other spelling — case variant, padding, retired letter
    — as a 422 input error before this helper is reached. So the lookup in
    :data:`TIER_UNAVAILABLE_CODE` is exact and nothing is normalized here:
    a normalize step would quietly accept a shape the door already refuses,
    which is the second vocabulary the rename forbids. Only ``sql`` is
    refused here (``global`` 综述 is now an open 档); an unknown token is
    ignored — this helper never guesses that a string "meant" a 档 (the same
    closed-list rule as :data:`TIER_UNAVAILABLE_CODE`).
    """
    for tier in requested_tiers or ():
        if tier in TIER_UNAVAILABLE_CODE:
            return tier
    return None


__all__ = [
    "COMMUNITY_SUMMARY_INDEX_FAILURE",
    "CORPUS_NOT_READY",
    "EMPTY_FAST_GRAPH_FAILURE",
    "LASTING_RETRIEVE_TIERS",
    "MAPPING_DROP",
    "REAL_DB_NOT_WIRED",
    "RETRIEVE_OUTCOME_COMMUNITY_SUMMARY_UNREADY",
    "RETRIEVE_OUTCOME_FAST_GRAPH_EMPTY",
    "RETRIEVE_TIERS",
    "RetrieveTier",
    "TIER_LABELS",
    "TIER_NOT_OPEN",
    "TIER_UNAVAILABLE_CODE",
    "copy_recorded_job_facts",
    "named_refusal_reason",
    "normalize_tier_token",
    "retrieve_blocking_failure",
    "retrieve_outcomes_delta",
    "unavailable_tier",
]
