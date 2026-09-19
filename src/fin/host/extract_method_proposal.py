"""Fast 抽图 propose window (search-mode host T4 / ADR 0068 / ADR 0079).

**Fast 抽图** vs **standard** is the *extract method* of the index job:
an **operator switch** (default standard, ADR 0068) with exactly **one
method per corpus version** (ADR 0079). Owning and applying that switch
lives in spec 2 (``graph-index-standard-fast``) — NOTHING here writes to
the extract/index pipeline.

What the host may do (spec search-mode-host "Fast 抽图 propose"
bullet): open a **choice window** whose options distinguish Fast
(cheaper / noisier) vs standard, and record the operator's answer. The
payload keeps ``kind="choice_window"`` — the SAME structured-options →
pick → resume contract the 档 clarify window uses (ADR 0062), so the v1
SPA renders this window with no web change.

Why this module is NOT part of :mod:`fin.host.search_mode_decision`:
a 检索档 is A/B/C/D and ``Fast`` must never be one (spec SC3), so the
extract-method vocabulary deliberately shares no token space with the
档 cascade — a single module boundary per concept keeps a stray
``"Fast"`` from ever typing itself into ``search_mode``.
"""

from __future__ import annotations

from typing import Any, Final, Literal

# The closed extract-method set (ADR 0079's two members). NOT the 档
# set: ``fast``/``standard`` never appear as ``search_mode`` values and
# vice versa.
ExtractMethod = Literal["standard", "fast"]

EXTRACT_METHOD_STANDARD: Final[ExtractMethod] = "standard"
EXTRACT_METHOD_FAST: Final[ExtractMethod] = "fast"

# ADR 0068: the operator switch defaults to standard, and the host has
# no authority to flip it — so the EFFECTIVE method stays standard for
# every audit this increment can produce (``applied`` is hardcoded
# ``False`` in :func:`build_proposal_audit`).
DEFAULT_EXTRACT_METHOD: Final[ExtractMethod] = EXTRACT_METHOD_STANDARD

# The only method the host may PROPOSE (ADR 0068: AI may suggest Fast).
# Proposing standard is a no-op — it already is the default.
PROPOSED_EXTRACT_METHOD: Final[ExtractMethod] = EXTRACT_METHOD_FAST

# Operator-visible labels. ``opened`` is True for both: this window
# records a pick, it cannot apply one, so no option is 具名失败-bound.
EXTRACT_METHOD_LABELS: Final[dict[str, str]] = {
    EXTRACT_METHOD_STANDARD: "标准抽图",
    EXTRACT_METHOD_FAST: "Fast 抽图（更便宜、更噪）",
}

# Option order is deliberate: the safe default first.
_OPTION_ORDER: Final[tuple[str, ...]] = (
    EXTRACT_METHOD_STANDARD,
    EXTRACT_METHOD_FAST,
)


def normalize_extract_method(raw: object) -> ExtractMethod | None:
    """Map a wire/pick token onto the closed method set (case-insensitive).

    Total function like :func:`fin.host.search_mode_decision.normalize_search_mode`:
    an out-of-set token returns ``None`` so the graph node reopens the
    window — a bad pick never applies anything and never crashes.
    """
    if not isinstance(raw, str):
        return None
    mapped = raw.strip().lower()
    if mapped in (EXTRACT_METHOD_STANDARD, EXTRACT_METHOD_FAST):
        return mapped  # type: ignore[return-value]
    return None


def propose_options() -> list[dict[str, Any]]:
    """The structured option list of the propose window (same
    ``{id,label,opened}`` shape the 档 window emits)."""
    return [
        {
            "id": method,
            "label": EXTRACT_METHOD_LABELS[method],
            "opened": True,
        }
        for method in _OPTION_ORDER
    ]


def propose_window_payload(
    rejected: list[str] | None = None,
) -> dict[str, Any]:
    """The ``interrupt`` payload for one propose window.

    ``kind`` stays ``"choice_window"`` (T2 wire contract) so the SPA
    needs no change; the title/prompt say this selects the 抽图方法, so
    the operator never mistakes it for a 档 picker.
    """
    payload: dict[str, Any] = {
        "kind": "choice_window",
        "title": "选择抽图方法",
        "prompt": (
            "host 提议改用 Fast 抽图（更便宜、更噪）。此处选择只记录"
            "提议与你的选择，不切换抽图方法（默认 standard，ADR 0068）。"
        ),
        "options": propose_options(),
    }
    if rejected:
        payload["rejected_picks"] = list(rejected)
    return payload


def proposal_is_pending(request: object) -> bool:
    """True only for an explicit, unresolved propose request.

    The trigger is minimal on purpose: the audit must carry
    ``proposed == "fast"`` AND no ``picked`` yet. ``{}`` (the per-run
    reset), a resolved audit, or any stray shape never reopens the
    window — the ordinary grounded flow cannot stumble into it.
    """
    if not isinstance(request, dict) or not request:
        return False
    if request.get("picked") is not None:
        return False
    return normalize_extract_method(request.get("proposed")) == EXTRACT_METHOD_FAST


def build_proposal_audit(
    request: dict[str, Any],
    *,
    picked: ExtractMethod,
    rejected: list[str] | None = None,
) -> dict[str, Any]:
    """The run-audit record of one resolved propose window.

    ``applied`` is hardcoded ``False``: applying the method means
    rebuilding the corpus version (ADR 0079) and belongs to the index
    job (spec 2), so no host-side pick — not even a confirmed
    ``fast`` — may flip it this increment. ``operator_confirmed`` is
    what the future switch consumes as evidence, nothing more.
    """
    audit: dict[str, Any] = {
        "proposed": normalize_extract_method(request.get("proposed"))
        or PROPOSED_EXTRACT_METHOD,
        "picked": picked,
        "applied": False,
        "operator_confirmed": picked == EXTRACT_METHOD_FAST,
        "decision_tier": "operator_pick",
    }
    if rejected:
        audit["rejected_picks"] = list(rejected)
    return audit


__all__ = [
    "DEFAULT_EXTRACT_METHOD",
    "EXTRACT_METHOD_LABELS",
    "ExtractMethod",
    "PROPOSED_EXTRACT_METHOD",
    "build_proposal_audit",
    "normalize_extract_method",
    "propose_options",
    "propose_window_payload",
    "proposal_is_pending",
]
