"""Fast 抽图 propose window (spec search-mode-host, Ticket 4 / SC3).

Opt-in only (``uv run pytest -m soft_track``; deselected by default —
``tests/host/conftest.py``). ScriptedChatModel + InMemorySaver only, no
live LLM (mirrors ``test_search_mode_choice_window.py``).

Contract covered here (ADR 0068 / 0079, spec "Fast 抽图 propose"
bullet + SC3): the host may PROPOSE Fast via the SAME choice-window
``interrupt`` machinery — payload stays ``kind="choice_window"`` so the
SPA renders it with zero web changes — but must never silent-apply:

* an explicit propose seed (``extract_method_proposal=
  {"proposed": "fast"}`` on the run input, grounded only) pauses the
  run on an extract-method window whose options name Fast vs standard;
* NO pick (window still pending) / an invalid pick / an empty pick
  keep the effective extract method **standard** and never crash;
* a confirmed ``fast`` pick on a request that carries a recorded
  ``(document_id, version)`` applies the ONE sanctioned path — the corpus
  ``switch_extract_method`` operator switch — and the audit reads
  ``applied is True``; without that version context (or on a declined
  / bad pick) nothing reaches the catalog and ``applied`` stays
  ``False``: there is no silent Fast.
* the string ``Fast`` is NEVER a 检索档: it is not an option id in the
  mode window, an out-of-list pick there reopens (no crash), and
  ``search_mode`` after every flow here stays ``None`` or ``A|B|C|D``.
* default grounded flow (no seed) and the lane-off factory default
  never open the window.

Five boundary classes map to: empty -> ``test_empty_pick_reopens_window_and_keeps_standard``;
negative -> ``test_out_of_list_pick_reopens_then_resolves``;
overflow -> ``test_mode_window_rejects_fast_then_operator_pick_assigns_real_tier``
(repeated cross-window picks never accumulate a Fast 档);
concurrent -> the 409 guard is the run-level lock already locked in
``test_search_mode_choice_window.py`` (this ticket adds no new HTTP
surface); exception -> ``test_lane_off_seed_never_opens_window`` (no
interrupt machinery runs when the HITL lane is off).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from fin.corpus.catalog import record_filings
from fin.host import ScriptedChatModel, compile_graph
from fin.host.extract_method_proposal import DEFAULT_EXTRACT_METHOD
from fin.host.state import reset_input_state

pytestmark = [pytest.mark.soft_track]

KNOWN_IDS = {"AAPL-2022Q3", "AAPL-2023Q1", "MSFT-2023Q1", "AMZN-2023Q1"}

# Grounded by the track rules tier, unnamed by the 档 rules tier, no
# inherit on a fresh thread -> the cascade residual ``clarify``.
AMBIGUOUS = "What was AAPL net sales in 2023 Q3?"

_ANSWER = [AIMessage(content="Net sales grew year over year.")]

_FAST_PROPOSAL_SEED = {"proposed": "fast"}


def _cfg(thread_id: str, **configurable: Any) -> dict:
    return {"configurable": {"thread_id": thread_id, **configurable}}


def _input(text: str) -> dict[str, Any]:
    return {**reset_input_state(text), "evidence": []}


def _propose_input(text: str) -> dict[str, Any]:
    """The minimal trigger: an explicit host/model propose seed on the
    run input (a REQUEST signal — the ordinary grounded flow carries
    none of this and never opens the window)."""
    return {
        **reset_input_state(text),
        "evidence": [],
        "extract_method_proposal": dict(_FAST_PROPOSAL_SEED),
    }


def _interrupt_payload(out: dict[str, Any]) -> dict[str, Any] | None:
    interrupts = out.get("__interrupt__") or ()
    if not interrupts:
        return None
    return interrupts[0].value


def _compile(
    script: list[AIMessage],
    *,
    choice_window: bool = True,
    catalog_path: Path | str | None = None,
) -> Any:
    return compile_graph(
        checkpointer=InMemorySaver(),
        model=ScriptedChatModel(script=script),
        known_document_ids=KNOWN_IDS,
        enable_choice_window=choice_window,
        catalog_path=catalog_path,
    )


def _option_ids(payload: dict[str, Any]) -> list[str]:
    return [str(option.get("id")) for option in payload.get("options") or []]


# ---------------------------------------------------------------------------
# the propose window itself
# ---------------------------------------------------------------------------


def test_propose_seed_opens_extract_method_window() -> None:
    """SC3: the scripted propose path emits a choice window. Pin the 档
    so the ONLY window is the extract-method one (the mode cascade is
    decided by rules here)."""
    g = _compile(_ANSWER)
    out = g.invoke(
        _propose_input(AMBIGUOUS),
        config=_cfg("fp-open", pinned_search_mode="B"),
    )
    payload = _interrupt_payload(out)
    assert payload is not None
    # Wire-compatible with the SPA renderer: same kind, options of
    # {id,label} — no web change needed to paint this window.
    assert payload["kind"] == "choice_window"
    # The title/prompt say what this window selects (抽图方法), so the
    # operator never mistakes it for a 档 picker.
    assert "抽图" in payload["title"] or "抽图" in payload["prompt"]
    assert sorted(_option_ids(payload)) == ["fast", "standard"]
    for option in payload["options"]:
        assert set(option) >= {"id", "label"}
        assert option["id"] and option["label"]
    # Paused BEFORE any model / retrieve call.
    assert out["track"] == "grounded"
    assert out["step_count"] == 0


def test_no_pick_keeps_extract_method_standard() -> None:
    """SC3 "with no pick, extract method remains standard": a run still
    paused on the window has recorded no pick and applied nothing; the
    effective method is the default standard."""
    g = _compile(_ANSWER)
    out = g.invoke(
        _propose_input(AMBIGUOUS),
        config=_cfg("fp-nopick", pinned_search_mode="B"),
    )
    assert _interrupt_payload(out) is not None
    proposal = out["extract_method_proposal"]
    # The seed is still just a request: no pick, nothing applied.
    assert proposal == dict(_FAST_PROPOSAL_SEED)
    assert proposal.get("picked") is None
    assert proposal.get("applied") is not True
    # The method a downstream reader treats as in force is the SSOT
    # default (ADR 0068): pinned directly, no read-helper needed.
    assert DEFAULT_EXTRACT_METHOD == "standard"


def test_empty_pick_reopens_window_and_keeps_standard() -> None:
    """Boundary 'empty': an empty resume is an invalid answer — the
    window reopens (never a crash, never Fast) and stays standard."""
    g = _compile(_ANSWER)
    tid = "fp-empty-pick"
    g.invoke(_propose_input(AMBIGUOUS), config=_cfg(tid, pinned_search_mode="B"))
    again = g.invoke(Command(resume=""), config=_cfg(tid, pinned_search_mode="B"))
    payload = _interrupt_payload(again)
    assert payload is not None
    assert payload["kind"] == "choice_window"
    assert "" in payload["rejected_picks"]
    assert again["extract_method_proposal"] == dict(_FAST_PROPOSAL_SEED)
    # The reopened audit is still an unresolved request: no pick, no
    # apply, so nothing a downstream reader could treat as in force.
    assert again["extract_method_proposal"].get("picked") is None
    assert again["extract_method_proposal"].get("applied") is not True


def test_pick_fast_records_proposal_never_applies() -> None:
    """ADR 0068 "no silent-apply", read maximally: a context-free seed
    is only a proposal record — even CONFIRMING fast on it applies
    nothing (there is no version to switch), the effective method stays
    standard, and no snapshot/corpus channel gains an extract-method
    fact. The 档 audit is untouched."""
    g = _compile(_ANSWER)
    tid = "fp-pick-fast"
    g.invoke(_propose_input(AMBIGUOUS), config=_cfg(tid, pinned_search_mode="B"))
    done = g.invoke(Command(resume="fast"), config=_cfg(tid, pinned_search_mode="B"))
    assert not done.get("__interrupt__")
    proposal = done["extract_method_proposal"]
    assert proposal["proposed"] == "fast"
    assert proposal["picked"] == "fast"
    assert proposal["applied"] is False
    assert proposal["operator_confirmed"] is True
    # ``applied is False`` dominates the pick: the method in force is
    # still the SSOT default (a host-side pick is not a switch).
    assert DEFAULT_EXTRACT_METHOD == "standard"
    # No extract/index mutation rode this run: snapshots only ever
    # carry the 档 key, never an extract-method one.
    for snapshot in done.get("status_snapshots") or []:
        assert "extract_method" not in snapshot
    # The 档 (pinned before the window) is unaffected by the pick.
    assert done["search_mode_decision"]["search_mode"] == "B"
    assert done["search_mode_decision"]["search_mode"] not in ("Fast", "fast")


def test_pick_standard_declines_proposal() -> None:
    g = _compile(_ANSWER)
    tid = "fp-pick-standard"
    g.invoke(_propose_input(AMBIGUOUS), config=_cfg(tid, pinned_search_mode="B"))
    done = g.invoke(
        Command(resume="standard"), config=_cfg(tid, pinned_search_mode="B")
    )
    assert not done.get("__interrupt__")
    proposal = done["extract_method_proposal"]
    assert proposal["picked"] == "standard"
    assert proposal["applied"] is False
    assert proposal["operator_confirmed"] is False
    assert DEFAULT_EXTRACT_METHOD == "standard"


def test_out_of_list_pick_reopens_then_resolves() -> None:
    """Boundary 'negative': an out-of-list pick never applies anything —
    the window reopens with the rejected value, and a later valid pick
    resolves normally."""
    g = _compile(_ANSWER)
    tid = "fp-invalid"
    g.invoke(_propose_input(AMBIGUOUS), config=_cfg(tid, pinned_search_mode="B"))
    again = g.invoke(Command(resume="Z"), config=_cfg(tid, pinned_search_mode="B"))
    payload = _interrupt_payload(again)
    assert payload is not None
    assert "Z" in payload["rejected_picks"]
    assert again["extract_method_proposal"] == dict(_FAST_PROPOSAL_SEED)
    done = g.invoke(Command(resume="fast"), config=_cfg(tid, pinned_search_mode="B"))
    assert not done.get("__interrupt__")
    assert done["extract_method_proposal"]["picked"] == "fast"
    assert done["extract_method_proposal"]["applied"] is False


# ---------------------------------------------------------------------------
# Fast is never a 检索档: the two windows chain, ids never cross-apply
# ---------------------------------------------------------------------------


def test_propose_then_mode_window_chain_never_yields_fast_mode() -> None:
    """SC3 "host state does not set 检索档 to Fast" (strong form): on an
    unpinned clarify ask the propose window opens FIRST, then the 档
    window. A 档 id is invalid in the method window and ``fast`` is
    invalid in the 档 window — both reopen without crashing, and the
    final ``search_mode`` is the operator-picked 档, never Fast."""
    g = _compile(_ANSWER)
    tid = "fp-chain"
    out = g.invoke(_propose_input(AMBIGUOUS), config=_cfg(tid))
    assert sorted(_option_ids(_interrupt_payload(out) or {})) == ["fast", "standard"]

    # A "档 pick" inside the method window is out-of-list: reopen.
    again = g.invoke(Command(resume="A"), config=_cfg(tid))
    payload = _interrupt_payload(again)
    assert payload is not None
    assert "A" in payload["rejected_picks"]

    resolved = g.invoke(Command(resume="fast"), config=_cfg(tid))
    # The method proposal is recorded...
    assert resolved["extract_method_proposal"]["picked"] == "fast"
    assert resolved["extract_method_proposal"]["applied"] is False
    # ...and the run then pauses on the SECOND window: the 档 clarify
    # window, with the A/B/C/D option list (Fast is not among them).
    mode_payload = _interrupt_payload(resolved)
    assert mode_payload is not None
    assert _option_ids(mode_payload) == ["A", "B", "C", "D"]
    assert resolved["search_mode_decision"]["search_mode"] not in ("Fast", "fast")

    # "fast" is NOT a 档 id: the mode window reopens, never crash.
    reopened = g.invoke(Command(resume="fast"), config=_cfg(tid))
    mode_payload = _interrupt_payload(reopened)
    assert mode_payload is not None
    assert "fast" in mode_payload["rejected_picks"]
    assert reopened["search_mode_decision"]["search_mode"] not in ("Fast", "fast")

    final = g.invoke(Command(resume="B"), config=_cfg(tid))
    assert not final.get("__interrupt__")
    assert final["search_mode_decision"]["search_mode"] == "B"
    assert final["search_mode_decision"]["search_mode"] not in ("Fast", "fast")
    assert final["extract_method_proposal"]["picked"] == "fast"
    assert final["extract_method_proposal"]["applied"] is False


# ---------------------------------------------------------------------------
# non-triggers: the real grounded flow stays quiet
# ---------------------------------------------------------------------------


def test_default_grounded_flow_never_opens_the_window() -> None:
    """No seed -> no propose: the pinned grounded run completes with no
    interrupt and an EMPTY proposal audit (reset per run)."""
    g = _compile(_ANSWER)
    out = g.invoke(
        _input(AMBIGUOUS), config=_cfg("fp-noop", pinned_search_mode="B")
    )
    assert not out.get("__interrupt__")
    assert out["search_mode_decision"]["search_mode"] == "B"
    assert out["extract_method_proposal"] == {}


def test_greeting_with_seed_never_opens_the_window() -> None:
    """A non-grounded track reaches neither choice window (spec
    assumption 2): even the propose seed cannot interrupt a greeting."""
    g = _compile([AIMessage(content="Hi there.")])
    out = g.invoke(_propose_input("Hi!"), config=_cfg("fp-greeting"))
    assert out["track"] == "dialogue"
    assert not out.get("__interrupt__")
    # The seed rode no window: still an unresolved request at most,
    # never a pick and never an apply.
    assert out.get("extract_method_proposal", {}).get("picked") is None
    assert out.get("extract_method_proposal", {}).get("applied") is not True


def test_lane_off_seed_never_opens_window() -> None:
    """Boundary 'exception' shape: with the HITL lane off (the factory
    default) no interrupt machinery exists at all — a seeded request
    just runs through, applying nothing."""
    g = _compile(_ANSWER, choice_window=False)
    out = g.invoke(
        _propose_input(AMBIGUOUS), config=_cfg("fp-lane-off", pinned_search_mode="B")
    )
    assert not out.get("__interrupt__")
    assert out["search_mode_decision"]["search_mode"] == "B"
    assert out["extract_method_proposal"].get("applied") is not True
    assert out["extract_method_proposal"].get("picked") is None


# ---------------------------------------------------------------------------
# the operator pick lands on the version switch (ADR 0068/0079)
# ---------------------------------------------------------------------------


def _catalog_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "document_id": "AAPL-2022Q3",
        "company": "Apple",
        "period": "2022 Q3",
        "version": "a" * 64,
        "page_count": 3,
    }
    row.update(overrides)
    return row


def _stored_rows(catalog_path: Path) -> list[dict[str, object]]:
    return json.loads(catalog_path.read_text())["filings"]


def _seed_catalog(tmp_path: Path) -> Path:
    """A tmp catalog whose one version records the default standard."""
    catalog_path = tmp_path / "catalog.json"
    record_filings([_catalog_row()], catalog_path=catalog_path)
    return catalog_path


def _propose_input_for_version(
    text: str, document_id: str, version: str
) -> dict[str, Any]:
    """The seed the HTTP send stamps when the caller names the switch
    target: the propose request plus its ``(document_id, version)``."""
    return {
        **reset_input_state(text),
        "evidence": [],
        "extract_method_proposal": {
            "proposed": "fast",
            "document_id": document_id,
            "version": version,
        },
    }


def test_fast_pick_with_version_context_applies_the_switch(
    tmp_path: Path,
) -> None:
    """Acceptance "operator 有效 pick fast → 该 (document_id, version)
    记录 fast，audit applied=true": the window routes the pick through
    the ONLY sanctioned path (``switch_extract_method``), so the
    stored row flips to fast AND carries the rebuild intent. The 档
    pin survives untouched — fast is not a search mode."""
    catalog_path = _seed_catalog(tmp_path)
    g = _compile(_ANSWER, catalog_path=catalog_path)
    tid = "fp-apply-fast"
    g.invoke(
        _propose_input_for_version(AMBIGUOUS, "AAPL-2022Q3", "a" * 64),
        config=_cfg(tid, pinned_search_mode="B"),
    )
    done = g.invoke(Command(resume="fast"), config=_cfg(tid, pinned_search_mode="B"))
    assert not done.get("__interrupt__")
    proposal = done["extract_method_proposal"]
    assert proposal["picked"] == "fast"
    assert proposal["applied"] is True
    assert proposal["operator_confirmed"] is True
    stored = _stored_rows(catalog_path)[0]
    assert stored["extract_method"] == "fast"
    assert stored["rebuild_pending"] is True
    assert done["search_mode_decision"]["search_mode"] == "B"
    assert done["search_mode_decision"]["search_mode"] not in ("fast", "Fast")


def test_fast_pick_without_version_context_never_touches_catalog(
    tmp_path: Path,
) -> None:
    """The apply gate is the version context itself: a recorded
    standard version plus a confirmed fast pick on a seed that names NO
    ``(document_id, version)`` must leave the row standard — the switch
    may never guess its target, so this is the no-silent-Fast wall."""
    catalog_path = _seed_catalog(tmp_path)
    g = _compile(_ANSWER, catalog_path=catalog_path)
    tid = "fp-apply-nocontext"
    g.invoke(_propose_input(AMBIGUOUS), config=_cfg(tid, pinned_search_mode="B"))
    done = g.invoke(Command(resume="fast"), config=_cfg(tid, pinned_search_mode="B"))
    proposal = done["extract_method_proposal"]
    assert proposal["picked"] == "fast"
    assert proposal["applied"] is False
    assert proposal["apply_error"] == "missing_version_context"
    assert _stored_rows(catalog_path)[0]["extract_method"] == "standard"
    assert "rebuild_pending" not in _stored_rows(catalog_path)[0]


def test_fast_pick_for_unrecorded_version_is_typed_noop(tmp_path: Path) -> None:
    """A named-but-unrecorded target is the catalog's typed
    ``ExtractMethodTargetMissingError`` surfaced as an audit code: the
    switch never creates a version (ADR 0079), the run finishes, and
    nothing is applied."""
    catalog_path = _seed_catalog(tmp_path)
    g = _compile(_ANSWER, catalog_path=catalog_path)
    tid = "fp-apply-missing"
    g.invoke(
        _propose_input_for_version(AMBIGUOUS, "AAPL-2022Q3", "b" * 64),
        config=_cfg(tid, pinned_search_mode="B"),
    )
    done = g.invoke(Command(resume="fast"), config=_cfg(tid, pinned_search_mode="B"))
    assert not done.get("__interrupt__")
    proposal = done["extract_method_proposal"]
    assert proposal["applied"] is False
    assert proposal["apply_error"] == "extract_method_target_missing"
    assert _stored_rows(catalog_path)[0]["extract_method"] == "standard"
    assert len(_stored_rows(catalog_path)) == 1


def test_standard_pick_with_context_declines_without_writing(
    tmp_path: Path,
) -> None:
    """Picking standard is declining the proposal: the version already
    records the default, so NO switch runs — no flip, no rebuild
    intent, ``applied`` stays False."""
    catalog_path = _seed_catalog(tmp_path)
    g = _compile(_ANSWER, catalog_path=catalog_path)
    tid = "fp-apply-decline"
    g.invoke(
        _propose_input_for_version(AMBIGUOUS, "AAPL-2022Q3", "a" * 64),
        config=_cfg(tid, pinned_search_mode="B"),
    )
    done = g.invoke(
        Command(resume="standard"), config=_cfg(tid, pinned_search_mode="B")
    )
    proposal = done["extract_method_proposal"]
    assert proposal["picked"] == "standard"
    assert proposal["applied"] is False
    assert "apply_error" not in proposal
    stored = _stored_rows(catalog_path)[0]
    assert stored["extract_method"] == "standard"
    assert "rebuild_pending" not in stored


def test_bad_picks_with_context_keep_version_standard_until_valid(
    tmp_path: Path,
) -> None:
    """Acceptance "坏 pick → version 保持 standard": every rejected
    answer reopens the window with the catalog untouched; only the
    first valid fast pick writes the switch, exactly once."""
    catalog_path = _seed_catalog(tmp_path)
    g = _compile(_ANSWER, catalog_path=catalog_path)
    tid = "fp-apply-bads"
    g.invoke(
        _propose_input_for_version(AMBIGUOUS, "AAPL-2022Q3", "a" * 64),
        config=_cfg(tid, pinned_search_mode="B"),
    )
    for bad in ("turbo", ""):
        again = g.invoke(Command(resume=bad), config=_cfg(tid, pinned_search_mode="B"))
        assert _interrupt_payload(again) is not None
        assert again["extract_method_proposal"] == {
            "proposed": "fast",
            "document_id": "AAPL-2022Q3",
            "version": "a" * 64,
        }
        assert _stored_rows(catalog_path)[0]["extract_method"] == "standard"
    done = g.invoke(Command(resume="fast"), config=_cfg(tid, pinned_search_mode="B"))
    assert done["extract_method_proposal"]["applied"] is True
    assert done["extract_method_proposal"]["rejected_picks"] == ["turbo", ""]
    assert _stored_rows(catalog_path)[0]["extract_method"] == "fast"


def test_lane_off_with_context_applies_nothing(tmp_path: Path) -> None:
    """Lane off = no window = no operator confirmation possible, so
    even a version-naming seed applies nothing — the pick, not the
    seed, is what switches."""
    catalog_path = _seed_catalog(tmp_path)
    g = _compile(_ANSWER, choice_window=False, catalog_path=catalog_path)
    out = g.invoke(
        _propose_input_for_version(AMBIGUOUS, "AAPL-2022Q3", "a" * 64),
        config=_cfg("fp-apply-laneoff", pinned_search_mode="B"),
    )
    assert not out.get("__interrupt__")
    assert out["extract_method_proposal"].get("applied") is not True
    assert _stored_rows(catalog_path)[0]["extract_method"] == "standard"
