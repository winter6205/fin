"""Fast 抽图 propose window (search-mode host T4 / ADR 0068 / ADR 0079).

**Fast 抽图** vs **standard** is the *extract method* of the index job:
an **operator switch** (default standard, ADR 0068) with exactly **one
method per corpus version** (ADR 0079). The switch itself lives in
:func:`fin.corpus.catalog.switch_extract_method`; this module opens
the **choice window**, records the operator's answer, and routes ONE
answer onto that switch: a valid ``fast`` pick on a request that names
a recorded ``(document_id, version)``. Every other outcome — no pick,
bad pick, a declined ``standard`` pick, or a fast pick without version
context — applies nothing, which is what "no silent Fast" means on the
host side.

What the host may do (spec search-mode-host "Fast 抽图 propose"
bullet): open a **choice window** whose options distinguish Fast
(cheaper / noisier) vs standard, and record the operator's answer. The
payload keeps ``kind="choice_window"`` — the SAME structured-options →
pick → resume contract the 档 clarify window uses (ADR 0062), so the v1
SPA renders this window with no web change.

Why this module is NOT part of :mod:`fin.host.grounded.search_mode_decision`:
a 检索档 is one of ``fin.retrieve.tiers.LASTING_RETRIEVE_TIERS`` and
``Fast`` is never one of them, so the
extract-method vocabulary deliberately shares no token space with the
档 cascade — a single module boundary per concept keeps a stray
``"Fast"`` from ever typing itself into ``search_mode``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

from fin.corpus.catalog import switch_extract_method
from fin.corpus.errors import (
    CatalogCorruptError,
    CatalogRowError,
    ExtractMethodTargetMissingError,
)
from fin.corpus.extract_method import (
    DEFAULT_EXTRACT_METHOD,
    EXTRACT_METHOD_FAST,
    EXTRACT_METHOD_STANDARD,
    ExtractMethod,
)

# The only method the host may PROPOSE (ADR 0068: AI may suggest Fast).
# Proposing standard is a no-op — it already is the default.
PROPOSED_EXTRACT_METHOD: Final[ExtractMethod] = EXTRACT_METHOD_FAST

# Operator-visible labels. ``opened`` is True for both options; a fast
# pick only becomes a switch when the request also names a recorded
# ``(document_id, version)`` — see :func:`apply_extract_method_pick`.
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

    Total function like :func:`fin.host.grounded.search_mode_decision.normalize_search_mode`:
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

    ``kind`` stays ``"choice_window"`` (the SPA contract) so the SPA
    needs no change; the title/prompt say this selects the 抽图方法, so
    the operator never mistakes it for a 档 picker.
    """
    payload: dict[str, Any] = {
        "kind": "choice_window",
        "title": "选择抽图方法",
        "prompt": (
            "host 提议改用 Fast 抽图（更便宜、更噪）。选择 Fast 会把本提议"
            "指向的资料版本切换为 Fast 并标记待重建；未指向已记录版本、"
            "不选或选 standard 时保持默认 standard（ADR 0068/0079）。"
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


# Typed no-apply reasons surfaced on the audit (never a bare False
# without a recorded cause): the switch target is half-named, the
# named version has no catalog row, or the catalog refused.
APPLY_ERROR_MISSING_CONTEXT: Final[str] = "missing_version_context"
APPLY_ERROR_TARGET_MISSING: Final[str] = "extract_method_target_missing"
APPLY_ERROR_ROW_REJECTED: Final[str] = "catalog_row_rejected"
APPLY_ERROR_CATALOG_UNREADABLE: Final[str] = "catalog_unreadable"


def version_context(request: dict[str, Any]) -> tuple[str, str] | None:
    """The ``(document_id, version)`` a propose request names, if any.

    Both halves must be non-empty strings: half a target is ambiguous
    and the operator switch may never guess the other half (ADR 0079
    keys the switch on the exact version row)."""
    document_id = request.get("document_id")
    version = request.get("version")
    if not isinstance(document_id, str) or not document_id.strip():
        return None
    if not isinstance(version, str) or not version.strip():
        return None
    return document_id.strip(), version.strip()


def apply_extract_method_pick(
    request: dict[str, Any],
    picked: ExtractMethod,
    *,
    catalog_path: Path | str,
) -> tuple[bool, str | None]:
    """Route the operator's resolved pick onto the corpus version switch.

    Only ``fast`` is a real switch: ``standard`` is the ADR 0068
    default, so declining records nothing and writes nothing. Applying
    requires a version context on the request — without it the window
    stays a proposal record and NOTHING reaches the catalog, which is
    the no-silent-Fast wall. The catalog call is idempotent
    (re-switching the same method is a no-op), so LangGraph's
    replay-on-resume of the node body cannot double-apply.

    Returns ``(applied, apply_error)``: the run always continues — a
    refused switch is recorded, never raised into a broken wire.
    """
    if picked != EXTRACT_METHOD_FAST:
        return False, None
    context = version_context(request)
    if context is None:
        return False, APPLY_ERROR_MISSING_CONTEXT
    document_id, version = context
    try:
        switch_extract_method(
            document_id,
            version,
            EXTRACT_METHOD_FAST,
            catalog_path=catalog_path,
        )
    except ExtractMethodTargetMissingError:
        return False, APPLY_ERROR_TARGET_MISSING
    except CatalogRowError:
        return False, APPLY_ERROR_ROW_REJECTED
    except CatalogCorruptError:
        return False, APPLY_ERROR_CATALOG_UNREADABLE
    return True, None


def build_proposal_audit(
    request: dict[str, Any],
    *,
    picked: ExtractMethod,
    rejected: list[str] | None = None,
    applied: bool = False,
    apply_error: str | None = None,
) -> dict[str, Any]:
    """The run-audit record of one resolved propose window.

    ``applied`` mirrors what :func:`apply_extract_method_pick` actually did:
    ``True`` only when a valid fast pick on a version-naming request
    flipped the catalog switch (the catalog then carries the rebuild
    intent); every other outcome — declined, context-free, or typed
    refusal — keeps ``False``, with ``apply_error`` naming the refusal.
    """
    audit: dict[str, Any] = {
        "proposed": normalize_extract_method(request.get("proposed"))
        or PROPOSED_EXTRACT_METHOD,
        "picked": picked,
        "applied": applied,
        "operator_confirmed": picked == EXTRACT_METHOD_FAST,
        "decision_tier": "operator_pick",
    }
    if apply_error:
        audit["apply_error"] = apply_error
    if rejected:
        audit["rejected_picks"] = list(rejected)
    return audit


__all__ = [
    "APPLY_ERROR_CATALOG_UNREADABLE",
    "APPLY_ERROR_MISSING_CONTEXT",
    "APPLY_ERROR_ROW_REJECTED",
    "APPLY_ERROR_TARGET_MISSING",
    "DEFAULT_EXTRACT_METHOD",
    "EXTRACT_METHOD_FAST",
    "EXTRACT_METHOD_LABELS",
    "EXTRACT_METHOD_STANDARD",
    "ExtractMethod",
    "PROPOSED_EXTRACT_METHOD",
    "apply_extract_method_pick",
    "build_proposal_audit",
    "normalize_extract_method",
    "propose_options",
    "propose_window_payload",
    "proposal_is_pending",
    "version_context",
]
