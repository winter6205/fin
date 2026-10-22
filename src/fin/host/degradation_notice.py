"""Consuming a retrieval response's `degraded` signal (plan T20, SC39).

§B3's consumption rule has two halves and SC39 counts the runs that miss
either one: 「记录 `degraded_reasons` 的每一项」 and 「向用户侧暴露可见信号」.
This module is the host's single consumption point for both, so the two
places a retrieval response reaches the host — the ``retrieve`` tool and
a fan-out worker — cannot drift on what a degraded answer looks like.

Exposure is a parameter, not a convention: :func:`consume_degradation`
takes the ``expose`` sink and calls it *before* it records the
notice-stage reason. A caller that never surfaces the line cannot get
the ledger to say it did, which is what keeps
``unsignalled_degradation_count`` an honest number.

The visible form is the run's **tool trace** — the same one-liner surface
the workspace already renders per tool call (spec line 32), so a degraded
answer announces itself where the operator is already looking, without a
new wire channel. What the notice does NOT do is change the answer's
standing: a degraded result is 可用但必须可见 (§B2 第 4 条), so the citation
gate rules on it exactly as it rules on any other evidence.

The reasons stay off the visible line. They name a backend capability
that failed (``vector:attempt_failed``) and are meaningful only beside the
access-controlled ledger, so the rendered notice carries the safe sentence
and the exact reason strings reach the ledger and nothing else. The safe
sentence is owned by :mod:`fin.host.grounded.readable_projection` — the one module
that decides what ordinary-person Chinese copy a class of failure gets —
and is IMPORTED here rather than restated, so the notice and the failure
class can never drift apart. The dependency therefore points one way:
the projection owns the copy, this module renders it.
"""

from __future__ import annotations

from typing import Any, Callable, Final

from fin.host.grounded.readable_projection import DEGRADATION_SAFE_COPY
from fin.retrieve.degradation import (
    DEGRADATION_STAGE_RETRIEVE,
    DEGRADATION_STAGE_USER_NOTICE,
    DegradationLedger,
    DegradationSignal,
    read_degradation_signal,
)


# The tool-trace row a degradation notice is attributed to. ``retrieve``
# is the capability that degraded, so the operator reads the notice on
# the same row as the call it belongs to.
DEGRADATION_NOTICE_TOOL: str = "retrieve"

# Where the rendered notice rides on a tool envelope. This is the
# *rendered copy* of the signal, not a second name for it: the two
# verbatim §B3 fields travel on the same envelope untouched, and the
# renderer falls back to expanding them when this key is absent.
DEGRADATION_NOTICE_FIELD: str = "degradation_notice"

# The single wire name for "preparation failed with no stored reason",
# shared by the gate exit and the search-surface fallback.
ATTACHMENT_PREPARATION_FAILED: Final[str] = "attachment_preparation_failed"


def degradation_notice_line(signal: DegradationSignal) -> str:
    """The user-visible one-liner for a degraded response.

    Carries the safe sentence and NOTHING ELSE. An undegraded signal has
    no notice, so this returns ``""`` rather than announcing a signal that
    was never raised.

    WHY the reasons are absent here and present on the ledger: a reason
    names the backend capability that failed, which is an operator-
    UNACTIONABLE detail on a default surface and belongs beside the
    access-controlled ledger that already records every one of them.
    SC39 counts recorded-and-signalled reasons, and the ledger half is
    unchanged by this; only the rendered half loses the raw text.
    """
    if not signal.degraded:
        return ""
    return DEGRADATION_SAFE_COPY


def degradation_notice_entry(
    signal: DegradationSignal, *, tool_call_id: str = ""
) -> dict[str, str]:
    """The ``tool_summaries`` row carrying the notice."""
    return {
        "name": DEGRADATION_NOTICE_TOOL,
        "summary": degradation_notice_line(signal),
        "tool_call_id": tool_call_id,
    }


def run_degraded(tool_summaries: Any) -> bool:
    """Whether THIS run consumed a degraded retrieval response.

    Read from the run's own ``tool_summaries`` rather than passed down as a
    flag: the notice row is the host's record that it already consumed the
    signal, so a caller that re-derived the fact from the raw envelope would
    be reading a second source of truth for a decision the run has made. It
    is also the only one available at payload time — the §B3 fields ride the
    retrieve envelope, not a run-scoped channel.

    Only the BOOLEAN is read. The reasons stay on the access-controlled
    ledger; a caller that forwards them puts a backend capability name in the
    default view (T10).
    """
    if not isinstance(tool_summaries, (list, tuple)):
        return False
    return any(
        isinstance(row, dict)
        and row.get("name") == DEGRADATION_NOTICE_TOOL
        and row.get("summary") == DEGRADATION_SAFE_COPY
        for row in tool_summaries
    )


def consume_degradation(
    result: Any,
    *,
    ledger: DegradationLedger,
    expose: Callable[[DegradationSignal, str], None],
    detail: str = "",
) -> DegradationSignal:
    """Record and surface the degradation ``result`` carries.

    Reads the two verbatim fields strictly (an unreadable signal raises
    a named :class:`~fin.retrieve.degradation.DegradationError` rather
    than passing as healthy), records every reason, hands the signal and
    its notice line to ``expose``, and only then records that the reason
    was shown.

    Returns the signal so the caller can keep the two fields on whatever
    envelope it builds next. An undegraded response records nothing and
    calls nothing — §B3 rule 3 / 4 mean there is nothing to report.
    """
    signal = read_degradation_signal(result)
    if not signal.degraded:
        return signal
    ledger.record_signal(
        signal, stage=DEGRADATION_STAGE_RETRIEVE, detail=detail
    )
    line = degradation_notice_line(signal)
    expose(signal, line)
    # The notice-stage record keeps the reason STRING, not the rendered
    # line: the ledger is the access-controlled surface, so this is the
    # one place a reason may be stored verbatim.
    ledger.record_signal(
        signal, stage=DEGRADATION_STAGE_USER_NOTICE, detail=detail
    )
    return signal


__all__ = [
    "ATTACHMENT_PREPARATION_FAILED",
    "DEGRADATION_NOTICE_FIELD",
    "DEGRADATION_NOTICE_TOOL",
    "consume_degradation",
    "degradation_notice_entry",
    "degradation_notice_line",
    "run_degraded",
]
