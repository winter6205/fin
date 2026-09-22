"""Live 检索档 Jev verification — the required live item for the 档 judge.

Named command (run deliberately):

    uv run pytest -q -m jev_live tests/host/test_live_jev_search_mode.py

Default ``uv run pytest -q`` deselects everything here via the
``jev_live`` mark (the hook in the root ``tests/conftest.py``), so the
hermetic suite makes ZERO calls to ``api.typesafe.ai``. The credential gate
and the ``jev_live_client`` fixture are shared with
``tests/host/test_live_jev_track.py`` and
``tests/retrieve/test_live_jev_tier_reaches_wire.py`` from that root conftest,
so a missing key is the same named fail (``jev_live_key_missing``, non-zero
exit) in every live suite and never a skip: a green Fake pass never counts as
live and missing credentials block.

Same shape as ``tests/host/test_live_jev_track.py``, one tier further
down the cascade: the closed set is the four LASTING 档 tokens
(``fin.host.named_failures.LASTING_RETRIEVE_TIERS``) and the seam is
:func:`fin.host.search_mode_decision.decide_search_mode`.

A live judge is not deterministic, so the CONTRACT cases here stay
value-free: they assert membership of the closed set, a finite confidence
in [0, 1], the shipped ``question_id`` / ``instructions`` request shape and,
where the judge does not decide, the fail-closed SHAPE. A judge EXIT
can only land on **clarify** (it and ``inherit`` are mutually exclusive —
inherit is peeked before the call), and the residual opens nothing; 具名
失败 is the other legal outcome, when the judge confidently names an
unopened 档.

One case is deliberately NOT value-free: the opened-档 leg. What makes it
assertable despite a non-deterministic judge is the OPENED set plus the
confidence floor, never a preferred 档 — and that leg is what turns the
per-档 glosses in :data:`fin.host.search_mode_decision._JEV_MODE_INSTRUCTIONS`
into an asserted product property rather than prompt tuning.

Four live calls total: one raw Choice, then three through the product seam
— the first of those three additionally REQUIRES that the real judge was
REACHED (a ``transport`` EXIT or an audit with no reply evidence fails,
mirroring ``test_live_jev_track``), because a seam case satisfied by any
fail-closed exit could stay green while the endpoint never replied at all.
Reaching is not agreeing: the live judge is not deterministic, so an
abstention satisfies the interaction proof and falls through to the shape
contract asserted here.
"""

from __future__ import annotations

import math
import warnings
from collections.abc import Mapping
from typing import Any

import pytest
from langchain_core.messages import HumanMessage

import fin.host.search_mode_decision as smd
from fin.host.jev_choice import (
    JEV_CHOICE_EXIT_INVALID_LABEL,
    JEV_CHOICE_EXIT_INVALID_SEARCH_MODE,
    JEV_CHOICE_EXIT_LOW_CONFIDENCE,
    JEV_CHOICE_EXIT_NO_JEV_CLIENT,
    JEV_CHOICE_EXIT_SCHEMA,
    JEV_CHOICE_EXIT_TRANSPORT,
    JEV_CHOICE_EXIT_UNEXPECTED_ERROR,
    JevChoiceClient,
)
from fin.host.named_failures import LASTING_RETRIEVE_TIERS, TIER_UNAVAILABLE_CODE
from fin.host.search_mode_decision import decide_search_mode

# The full EXIT family the 档 cascade records when the judge does not
# decide, assembled from the exported names so a rename cannot drift this
# set silently: the client's own three plus the four the cascade authors.
# ``no_jev_client`` belongs here even though the shared fixture makes it
# unreachable live (a missing key is the named gate, never an EXIT): this
# set states what is LEGAL on the audit, and under-reporting a member
# would let a real one fail the shape assert below.
FAIL_CLOSED_EXITS = frozenset(
    {
        JEV_CHOICE_EXIT_TRANSPORT,
        JEV_CHOICE_EXIT_SCHEMA,
        JEV_CHOICE_EXIT_INVALID_LABEL,
        JEV_CHOICE_EXIT_NO_JEV_CLIENT,
        JEV_CHOICE_EXIT_LOW_CONFIDENCE,
        JEV_CHOICE_EXIT_UNEXPECTED_ERROR,
        JEV_CHOICE_EXIT_INVALID_SEARCH_MODE,
    }
)

# The subset of that family that is only producible FROM a reply: the
# request round-tripped and the judge rejected the answer (its shape, its
# label, or its confidence). Together with a recorded
# ``mode_jev_confidence`` these are what make an EXIT count as contact;
# ``transport``, ``unexpected_error`` and ``no_jev_client`` can all fire
# with nothing ever coming back, so they prove nothing about the endpoint.
_REPLY_PROVEN_EXITS = frozenset(
    {
        JEV_CHOICE_EXIT_SCHEMA,
        JEV_CHOICE_EXIT_INVALID_LABEL,
        JEV_CHOICE_EXIT_INVALID_SEARCH_MODE,
        JEV_CHOICE_EXIT_LOW_CONFIDENCE,
    }
)

# Grounded knowledge asks with no 档 naming (no letter or CONTEXT label
# adjacent to 档) and no inheritable history: exactly the gray zone the
# 档 judge exists to decide.
GROUNDING_UTTERANCE = "what was AAPL net sales in 2023 Q3?"
SECOND_GROUNDING_UTTERANCE = (
    "How did Microsoft's server products revenue trend across fiscal 2023?"
)

pytestmark = pytest.mark.jev_live


class _RecordingClient:
    """Delegates to the real client and records the request kwargs.

    The live answer is not assertable, but the REQUEST shape is: this
    proves the product seam really sends the closed 档 set under
    ``question_id="search_mode"`` with the shipped instructions text.
    """

    def __init__(self, inner: JevChoiceClient) -> None:
        self._inner = inner
        self.calls: list[dict[str, Any]] = []

    def choose(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self._inner.choose(**kwargs)


def _record(label: str, confidence: float, *, tier: str, via: str) -> None:
    """Surface the observed judge answer in the run's warnings summary.

    A pass with no trace of what the real judge said is indistinguishable
    from a stubbed run, so the evidence travels with the report. No
    credential is involved: label and confidence only.
    """
    warnings.warn(
        f"jev_live evidence via={via} tier={tier} label={label} "
        f"confidence={confidence:.3f}",
        stacklevel=2,
    )


def test_live_lasting_tier_choice_round_trip(jev_live_client: JevChoiceClient) -> None:
    choice = jev_live_client.choose(
        utterance=GROUNDING_UTTERANCE,
        options=LASTING_RETRIEVE_TIERS,
        question_id="search_mode",
        instructions=smd._JEV_MODE_INSTRUCTIONS,
    )
    assert choice.label in LASTING_RETRIEVE_TIERS
    assert math.isfinite(choice.confidence)
    assert 0.0 <= choice.confidence <= 1.0
    assert isinstance(choice.probabilities, Mapping)
    _record(choice.label, choice.confidence, tier="raw", via="choose")


def _assert_named_by_judge(decision: Any, *, via: str) -> None:
    """The judge answered and its answer decided the turn.

    The ``decision_tier`` dispatch runs BEFORE any label assert, so an
    unexpected tier reports the named ``unexpected_search_mode_tier``
    reason instead of tripping a bare membership assert on the way.
    """
    signals = decision.signals
    tier = decision.decision_tier
    label = signals.get("mode_jev_label")
    confidence = signals.get("mode_jev_confidence")

    if tier == "jev":
        assert label in LASTING_RETRIEVE_TIERS
        assert decision.search_mode == label
        assert decision.confidence == confidence
        assert confidence >= smd._MIN_JEV_CONFIDENCE
    elif tier == "typed_fail":
        # A confident name of an unopened 档 is 具名失败, not an invented
        # substitute: the requested token stays on the audit and the
        # failure code comes from the availability SSOT.
        assert label in LASTING_RETRIEVE_TIERS
        assert confidence >= smd._MIN_JEV_CONFIDENCE
        assert decision.search_mode is None
        assert signals["requested_mode"] == label
        assert decision.failure_code == TIER_UNAVAILABLE_CODE[label]
        assert signals["named_failure"] == decision.failure_code
    else:
        pytest.fail(
            f"unexpected_search_mode_tier: {tier!r} with no recorded exit",
            pytrace=False,
        )
    assert math.isfinite(confidence)
    assert 0.0 <= confidence <= 1.0
    _record(str(label), confidence, tier=str(tier), via=via)


def _assert_judge_answered(decision: Any, *, via: str) -> None:
    """The live round-trip proof: the endpoint was REACHED, not agreed with.

    Two conditions, each with its own named failure, because the raw
    signals dict alone misattributes an abstention to a connectivity
    problem:

    1. ``jev_live_unreachable`` — a ``transport`` EXIT is the request never
       coming back, so no shape can stand in for the round trip.
    2. ``jev_live_no_reply_evidence`` — nothing on the audit proves a reply
       arrived: no ``mode_jev_confidence`` (the cascade writes that key only
       after the client returned) and no EXIT from the reply-only family.

    Agreement is deliberately NOT required. The utterance is gray-zone by
    construction, so a working judge may still abstain; an abstention
    satisfies both conditions and falls through to the shape contract,
    which is the assertable half of a non-deterministic answer.
    """
    signals = decision.signals
    exit_code = signals.get("mode_jev_exit")
    if exit_code == JEV_CHOICE_EXIT_TRANSPORT:
        pytest.fail(
            "jev_live_unreachable: condition 1 (the judge must not answer "
            f"with a transport EXIT) violated; mode_jev_exit="
            f"{exit_code!r}, signals={signals}",
            pytrace=False,
        )
    if "mode_jev_confidence" not in signals and exit_code not in _REPLY_PROVEN_EXITS:
        pytest.fail(
            "jev_live_no_reply_evidence: condition 2 (a recorded "
            "mode_jev_confidence or a reply-only EXIT must prove the "
            f"endpoint replied) violated; mode_jev_exit={exit_code!r}, "
            f"signals={signals}",
            pytrace=False,
        )
    _assert_decision_shape(decision, via=via)


def _assert_decision_shape(decision: Any, *, via: str) -> None:
    """Contract, not value. Two shapes are legal: the judge decided (an
    opened 档 named by a high-confidence closed-set answer, or 具名失败 for
    a 档 named but not open), or it failed closed and opened nothing. The
    cascade never assigns a 档 the judge did not name.
    """
    signals = decision.signals
    exit_code = signals.get("mode_jev_exit")
    if exit_code is None:
        _assert_named_by_judge(decision, via=via)
        return

    # The judge did not decide (unreachable, bad schema, out-of-set or
    # sub-floor). Then no 档 may be carried: the ban is on a SILENT
    # global, so the residual opens nothing. A judge EXIT and ``inherit``
    # cannot co-occur — inherit is peeked before the call and the peek
    # gets its own copy of the signals — so when the EXIT fired the
    # cascade had already ruled inherit out: the residual is clarify.
    assert exit_code in FAIL_CLOSED_EXITS
    assert decision.decision_tier == "clarify"
    assert decision.search_mode is None
    label = signals.get("mode_jev_label")
    if label is not None:
        assert label in LASTING_RETRIEVE_TIERS
    _record(str(label), math.nan, tier=str(decision.decision_tier), via=via)


def test_live_tier_decision_via_product_seam(jev_live_client: JevChoiceClient) -> None:
    recording = _RecordingClient(jev_live_client)
    decision = decide_search_mode(
        [HumanMessage(content=GROUNDING_UTTERANCE)],
        snapshots=None,
        jev_client=recording,
    )
    (call,) = recording.calls
    assert call["options"] == LASTING_RETRIEVE_TIERS
    assert call["question_id"] == "search_mode"
    assert call["instructions"] == smd._JEV_MODE_INSTRUCTIONS
    assert call["utterance"] == GROUNDING_UTTERANCE
    _assert_judge_answered(decision, via="decide_search_mode")


def test_live_second_grounding_utterance_via_product_seam(
    jev_live_client: JevChoiceClient,
) -> None:
    # A second grounded ask through the same seam: whatever the judge
    # prefers, the contract holds on a different question too.
    decision = decide_search_mode(
        [HumanMessage(content=SECOND_GROUNDING_UTTERANCE)],
        snapshots=None,
        jev_client=jev_live_client,
    )
    _assert_decision_shape(decision, via="decide_search_mode#2")


# The 档 a filing-figure ask may legally land on: the OPENED ones, derived
# from the availability SSOT (``TIER_UNAVAILABLE_CODE``) rather than spelled
# out, so this set and the one in
# ``tests/retrieve/test_live_jev_tier_reaches_wire.py`` cannot drift apart.
# The derivation is also the warning: once ``global`` or ``D`` opens this set
# widens HERE, and it then has to be re-examined rather than waved through —
# the glosses must still keep a figure that lives only in a filing off the
# 真数据库 and off a corpus-wide synthesis.
_FILING_FIGURE_TIERS = frozenset(
    tier for tier in LASTING_RETRIEVE_TIERS if tier not in TIER_UNAVAILABLE_CODE
)


def test_live_judge_binds_an_opened_tier(jev_live_client: JevChoiceClient) -> None:
    """The required opened-档 leg: the real judge decides this ask.

    Not a shape contract. A ``clarify`` (low confidence / EXIT) or a
    ``typed_fail`` on an unopened 档 means the discriminator in the shipped
    instructions stopped separating a figure that lives only in a filing
    from one that needs a live database, which is exactly the regression the
    bare-label prompt had. Either opened 档 satisfies it — pinning WHICH one
    would be the flaky landmine the rest of this file avoids.
    """
    decision = decide_search_mode(
        [HumanMessage(content=GROUNDING_UTTERANCE)],
        snapshots=None,
        jev_client=jev_live_client,
    )
    signals = decision.signals
    label = signals.get("mode_jev_label")
    confidence = signals.get("mode_jev_confidence")
    decided_an_opened_tier = (
        decision.decision_tier == "jev"
        and decision.search_mode in _FILING_FIGURE_TIERS
        and isinstance(confidence, (int, float))
        and confidence >= smd._MIN_JEV_CONFIDENCE
    )
    if not decided_an_opened_tier:
        pytest.fail(
            "jev_live_tier_discriminator_regressed: the real judge must "
            f"bind an OPENED 档 (one of {sorted(_FILING_FIGURE_TIERS)}) at "
            f"or above the floor {smd._MIN_JEV_CONFIDENCE} for "
            f"{GROUNDING_UTTERANCE!r}; observed decision_tier="
            f"{decision.decision_tier!r} label={label!r} "
            f"confidence={confidence!r} exit={signals.get('mode_jev_exit')!r} "
            f"failure_code={decision.failure_code!r}",
            pytrace=False,
        )
    assert decision.search_mode == label
    assert decision.confidence == confidence
    assert decision.failure_code is None
    _record(
        str(label),
        float(confidence),
        tier=str(decision.decision_tier),
        via="opened_tier",
    )
