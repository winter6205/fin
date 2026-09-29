"""The model-facing result of one visual read (ADR 0089, spec T5).

:func:`fin.host.attachment_visual_read.read_visual_detail` decides what a run
earned and hands back provenance tokens only — an observation's prose never
rides a claim row. This module is the other half of the tool boundary: it
turns one ``(faces, ask)`` pair into the ONE object the model sees plus the
rows the run persists, and it owns the page-and-body serialization so the
host graph stays a wiring file.

Two rules shape both exits:

* a granted read shows each bounded observation body alongside its own
  provenance, so the model can say which page said what;
* a refusal shows exactly one typed code — no body, no rows, nothing else.

Never raises to the model: every documented exit is plain data.
"""

from __future__ import annotations

import importlib
from typing import Any

from fin.host.visual_evidence import VISUAL_UNAVAILABLE


def _lane() -> Any:
    """The visual-read leg, imported lazily.

    WHY lazy: the leg reaches the vision port and the composition root,
    which imports the host graph back — the same cycle every other
    attachment seam here dodges.
    """
    return importlib.import_module("fin.host.attachment_visual_read")


def visual_read_result(
    faces: Any,
    *,
    attachment_id: str,
    page: int,
    chart: str,
    series: str,
    category: str,
    scope: list[float] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Read one plotted value, as ``(model_body, claim_rows)``.

    The second element is what the run's ``visual_claims`` channel and the
    turn row persist; the first is what the model is allowed to see of it.
    A malformed region or target is the lane's typed refusal, not a
    validation restated here.
    """
    lane = _lane()
    request = lane.VisualReadRequest(
        attachment_id=attachment_id,
        page=page,
        detail=lane.VisualDetail(chart=chart, series=series, category=category),
        scope=tuple(scope) if scope is not None else None,
    )
    outcome = lane.read_visual_detail(faces, request)
    if not outcome.granted:
        # EXIT: the lane's contract is one side populated; a neither-outcome
        # is a producer fault, and the honest code for "nothing was read" is
        # the unavailable one rather than a null on the wire.
        return {"error": outcome.refusal or VISUAL_UNAVAILABLE}, []
    return (
        {
            "observations": [
                {
                    "page": read.token.page,
                    "observation_version": read.token.observation_version,
                    "text": read.text,
                    "truncated": read.truncated,
                }
                for read in outcome.reads
            ]
        },
        [read.token.as_claim_row() for read in outcome.reads],
    )


__all__ = ["visual_read_result"]
