"""The one owner of the ``invalidations`` shapes every drain test builds.

Five suites drive :func:`fin.host.evidence.structured_evidence_invalidation.drain_invalidation_batch`:
the drain's own cases, the real-Postgres lane, the MCP interaction lane, the cadence
worker and the recovery sweep. Each of them used to spell the batch out as a raw
dict literal, which is why a change to the production key set could leave four
suites asserting a payload the parser no longer accepts — and why the drift was
invisible until a whole lane went red.

So the shape is stated once, HERE, from the spec and not from the parser:

* ``specs/structured-corpus-evidence.md:218-219`` — the response token
  ``iknow.evidence_result`` and its version sibling ``schema_version``. Both are
  required: they are what makes the payload the envelope fin asked for.
* ``:222`` — the ``invalidations`` success fields ``events``,
  ``next_checkpoint`` and ``gap``.
* ``action`` is NOT built here. It is an optional echo no shipped IK renderer
  sends, and register §1.3 is where it starts being sent, so a batch that omits
  it is the conforming one.
* ADR 0097 §2.1 — the event is five closed fields. No ``schema_version`` and no
  ``consumer_id``: the first rides the envelope, the second is stored and never
  sent.

WHY these builders do not read the parser's own constants: a builder derived
from ``fin.host.evidence.structured_evidence_invalidation`` cannot disagree with it, so a
case asserting that the two agree would be asserting ``x == x``. Written from
the spec, they can — and that disagreement is the defect these suites exist to
catch.
"""

from __future__ import annotations

import uuid
from typing import Any, Final

#: The envelope's required head: the response token and its version sibling.
EVIDENCE_RESULT_ENVELOPE: Final[dict[str, Any]] = {
    "schema": "iknow.evidence_result",
    "schema_version": 1,
}

#: The three success fields ``specs/structured-corpus-evidence.md:222`` names for
#: the ``invalidations`` action.
INVALIDATION_SUCCESS_FIELDS: Final[tuple[str, ...]] = (
    "events",
    "next_checkpoint",
    "gap",
)

#: A whole-second ``Z`` instant, which is the only ``occurred_at`` form ADR 0097
#: §2.1 accepts.
OCCURRED_AT: Final[str] = "2026-10-07T00:00:00Z"


def build_envelope(**over: Any) -> dict[str, Any]:
    """One conforming envelope head. ``over`` names the case's own variation."""

    return {**EVIDENCE_RESULT_ENVELOPE, **over}


def build_event(
    evidence_ref: str,
    *,
    seq: int,
    reason: str = "access_revoked",
    occurred_at: str = OCCURRED_AT,
    event_id: str | None = None,
    **over: Any,
) -> dict[str, Any]:
    """One ADR 0097 §2.1 event: exactly the five fields the parser accepts.

    ``over`` is the escape hatch for the cases that are ABOUT a sixth key — a
    case naming ``schema_version`` is testing that the withdrawn field refuses,
    not that a batch may carry it.
    """

    payload: dict[str, Any] = {
        "event_id": event_id if event_id is not None else str(uuid.uuid4()),
        "seq": seq,
        "evidence_ref": evidence_ref,
        "reason": reason,
        "occurred_at": occurred_at,
    }
    payload.update(over)
    return payload


def build_batch(
    *events: dict[str, Any],
    next_checkpoint: str = "ck-2",
    gap: bool = False,
    **extra: Any,
) -> dict[str, Any]:
    """One conforming batch: the envelope head plus the three success fields.

    ``extra`` carries the optional echoes a case turns on — ``checkpoint`` for
    the contiguity re-check, ``action`` for the case that names another action.
    """

    payload: dict[str, Any] = {
        **EVIDENCE_RESULT_ENVELOPE,
        "events": list(events),
        "next_checkpoint": next_checkpoint,
        "gap": gap,
    }
    payload.update(extra)
    return payload