"""Plan and join for the multi-issuer fan-out (T6, spec Focus 4).

Spec Focus 4: "One **research thread** can ask across a capped set of
issuers; the **host graph** fans out then synthesizes; every surviving
claim still passes the **citation verifier**. Over-cap issuer lists are
a typed overflow, not a hang."

ADR 0034 fixes the topology — the host graph Sends one node per issuer,
JOINS, then runs the citation verifier ONCE on the synthesized answer.
This module is the pure half of that topology: it decides *which*
issuers may fan out (:func:`plan_issuer_fanout`) and folds the workers'
findings back into one envelope (:func:`join_issuer_findings`). The
LangGraph wiring, the retrieve call, and the verifier all live in
``fin.host.graph``; nothing here imports LangGraph or a port.

Contract
--------
``plan_issuer_fanout`` returns the accepted plan::

    {"issuers": ["AAPL", "MSFT"], "query": "services revenue",
     "cap": 5, "requested": 2}

or one of two typed EXITs (dict values, never exceptions — spec
§ Boundaries "empty hits not exceptions"):

* ``{"error": "empty_fanout", "limit", "requested", "issuers": [],
  "fanout": <empty skeleton>}`` — no issuers, or no query to run for
  them.
* ``{"error": "issuer_overflow", "limit", "requested", "issuers":
  <the full ask>, "fanout": <empty skeleton>}`` — more issuers than
  the cap. The roster is echoed back in full: the operator has to see
  what was asked for, because truncating to the cap here is precisely
  the silent drop SC7 forbids. The cap is tested on the REQUEST so an
  over-cap ask never fans out work the graph would throw away.

``join_issuer_findings`` returns the ``fin.issuer_fanout.v1``
envelope the join node hands to the model and the workspace tool
trace::

    {
      "fanout": {
        "schema": "fin.issuer_fanout.v1",
        "query": "services revenue",
        "cap": 5,
        "requested": 3,
        "issuers": [
          {"issuer": "AAPL", "status": "hit"|"empty"|"unavailable",
           "chunk_count": 2, "error": "",
           "locators": [{"document_id","version","page"}]}
        ],
        "contributing": ["AAPL"],
        "empty": ["MSFT"],
        "unavailable": ["NVDA"],
        "locator_count": 2
      }
    }

Every planned issuer keeps a row even when it reported nothing: a
dropped row would hide the gap the operator asked about. ``empty``
(the corpus had no passage) and ``unavailable`` (retrieve itself was
down, ADR 0030) stay distinct for the same reason the ``retrieve``
tool refuses to rewrite its typed EXIT as ``{"chunks": []}``.

Like ``compare_facts``, this module owns no evidence: the fan-out
workers contribute ``EvidenceItem``s from the retrieve port, and the
single citation verifier downstream is still what turns a span into a
user-visible citation.
"""

from __future__ import annotations

from typing import Any, Iterable


ISSUER_FANOUT_SCHEMA = "fin.issuer_fanout.v1"

# The model-facing tool name. The graph intercepts a call by this name
# BEFORE the prebuilt ToolNode (the fan-out is graph topology, not a
# tool body), so the name is shared between the bound schema and the
# router.
ISSUER_FANOUT_TOOL = "research_issuers"

# Upper bound on issuers in one fan-out. ADR 0034 recommends 5: it is
# the point where a synthesized answer still reads as an answer rather
# than a report, and it bounds the parallel branch count the ADR
# refuses to leave open.
MAX_ISSUERS = 5

# Per-issuer worker outcomes. ``hit`` is the only one that can carry a
# citation; the other two are why an issuer is absent from the answer.
STATUS_HIT = "hit"
STATUS_EMPTY = "empty"
STATUS_UNAVAILABLE = "unavailable"


def normalize_issuers(issuers: Iterable[Any] | None) -> list[str]:
    """Uppercase, trim, and dedupe the roster, preserving ask order.

    ``document_id`` is ``TICKER-YYYYQn`` (ADR 0006 / manifest), so the
    uppercase ticker is the issuer key the join matches locators
    against. Blanks and repeats are dropped before the cap is applied —
    six spellings of five issuers is a five-issuer ask.
    """
    out: list[str] = []
    for value in issuers or ():
        if value is None:
            continue
        text = str(value).strip().upper()
        if text and text not in out:
            out.append(text)
    return out


def issuer_query(issuer: str, query: str) -> str:
    """The lexical query one fan-out worker sends to the retrieve port.

    Scoping is BOTH lexical (the issuer leads the query) and structural
    (the worker also passes ``ticker=issuer``): the port's own adapter
    decides how much of each it can honour, which is the point of
    keeping ranking behind the port. Shared with the tests so the
    fixture corpus and the worker cannot drift on the key.
    """
    return f"{issuer} {query}".strip()


def _empty_fanout(*, query: str, cap: int, requested: int) -> dict[str, Any]:
    """The skeleton every typed EXIT still carries, so the SPA and the
    turn row never branch on a missing key."""
    return {
        "schema": ISSUER_FANOUT_SCHEMA,
        "query": query,
        "cap": cap,
        "requested": requested,
        "issuers": [],
        "contributing": [],
        "empty": [],
        "unavailable": [],
        "locator_count": 0,
    }


def plan_issuer_fanout(
    *,
    issuers: Iterable[Any] | None,
    query: str,
    cap: int = MAX_ISSUERS,
) -> dict[str, Any]:
    """Decide whether ``issuers`` may fan out, and under what roster.

    Returns the accepted plan, or the typed ``empty_fanout`` /
    ``issuer_overflow`` EXIT documented at the top of this module.
    Never raises.
    """
    roster = normalize_issuers(issuers)
    clean_query = (query or "").strip()
    requested = len(roster)

    if not roster or not clean_query:
        return {
            "error": "empty_fanout",
            "limit": cap,
            "requested": requested,
            "issuers": roster,
            "fanout": _empty_fanout(
                query=clean_query, cap=cap, requested=requested
            ),
        }

    if requested > cap:
        return {
            "error": "issuer_overflow",
            "limit": cap,
            "requested": requested,
            "issuers": roster,
            "fanout": _empty_fanout(
                query=clean_query, cap=cap, requested=requested
            ),
        }

    return {
        "issuers": roster,
        "query": clean_query,
        "cap": cap,
        "requested": requested,
    }


def _finding_row(issuer: str, finding: dict[str, Any] | None) -> dict[str, Any]:
    """One envelope row for ``issuer``.

    A planned issuer whose worker reported nothing at all (``None``)
    reads as ``empty``, not as an absent row: the operator asked about
    it, so the answer owes them the gap.
    """
    if not isinstance(finding, dict):
        return {
            "issuer": issuer,
            "status": STATUS_EMPTY,
            "chunk_count": 0,
            "error": "",
            "locators": [],
        }
    locators = [
        locator
        for locator in (finding.get("locators") or ())
        if isinstance(locator, dict)
    ]
    status = str(finding.get("status") or STATUS_EMPTY)
    if status not in (STATUS_HIT, STATUS_EMPTY, STATUS_UNAVAILABLE):
        status = STATUS_EMPTY
    return {
        "issuer": issuer,
        "status": status,
        "chunk_count": int(finding.get("chunk_count") or 0),
        "error": str(finding.get("error") or ""),
        "locators": locators,
    }


def _index_findings(
    findings: Iterable[dict[str, Any]] | None,
) -> dict[str, dict[str, Any]]:
    """Key the workers' findings by normalized issuer, first one wins.

    Later duplicates are dropped rather than merged: two findings for
    one issuer means the graph fanned out twice, and picking the first
    keeps the join deterministic instead of order-dependent.
    """
    by_issuer: dict[str, dict[str, Any]] = {}
    for finding in findings or ():
        if not isinstance(finding, dict):
            continue
        issuer = str(finding.get("issuer") or "").strip().upper()
        if issuer and issuer not in by_issuer:
            by_issuer[issuer] = finding
    return by_issuer


def _partition_by_status(rows: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Bucket the envelope rows' issuers by status, in roster order.

    The three buckets stay distinct for the reason at the top of this
    module: ``empty`` and ``unavailable`` are different answers to the
    operator. A row whose status is none of the three lands in no
    bucket — ``_finding_row`` already narrows the status, so that is a
    guard, not a case.
    """
    buckets: dict[str, list[str]] = {
        STATUS_HIT: [],
        STATUS_EMPTY: [],
        STATUS_UNAVAILABLE: [],
    }
    for row in rows:
        bucket = buckets.get(row["status"])
        if bucket is not None:
            bucket.append(row["issuer"])
    return buckets


def join_issuer_findings(
    *,
    plan: dict[str, Any],
    findings: Iterable[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Fold the fan-out workers' findings into one envelope.

    ``plan`` is an accepted :func:`plan_issuer_fanout` result.
    ``findings`` are the per-issuer worker records, in whatever order
    the graph merged the parallel branches — the envelope re-orders
    them to the ask, because a human reads it. Findings for an issuer
    that was never planned are dropped: only the roster the cap
    approved may appear in the synthesis.
    """
    roster = [str(issuer) for issuer in (plan.get("issuers") or ())]
    by_issuer = _index_findings(findings)
    rows = [_finding_row(issuer, by_issuer.get(issuer)) for issuer in roster]
    buckets = _partition_by_status(rows)
    return {
        "fanout": {
            "schema": ISSUER_FANOUT_SCHEMA,
            "query": str(plan.get("query") or ""),
            "cap": int(plan.get("cap") or MAX_ISSUERS),
            "requested": int(plan.get("requested") or len(roster)),
            "issuers": rows,
            "contributing": buckets[STATUS_HIT],
            "empty": buckets[STATUS_EMPTY],
            "unavailable": buckets[STATUS_UNAVAILABLE],
            "locator_count": sum(len(row["locators"]) for row in rows),
        }
    }


__all__ = [
    "ISSUER_FANOUT_SCHEMA",
    "ISSUER_FANOUT_TOOL",
    "MAX_ISSUERS",
    "STATUS_EMPTY",
    "STATUS_HIT",
    "STATUS_UNAVAILABLE",
    "issuer_query",
    "join_issuer_findings",
    "normalize_issuers",
    "plan_issuer_fanout",
]
