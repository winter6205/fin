"""Live Soft-Track Jev verification — the ADR 0080 required live item.

Named command (run deliberately):

    uv run pytest -q -m jev_live tests/host/test_live_jev_track.py

Default ``uv run pytest -q`` deselects everything here via the
``jev_live`` mark (the hook in the root ``tests/conftest.py``), so the
hermetic suite makes ZERO calls to ``api.typesafe.ai``. A missing key is a
named fail (``jev_live_key_missing``, non-zero exit), never a skip: per ADR
0080 a green Fake pass never counts as live and missing credentials block.

The credential gate, the client builder and the ``jev_live_client``
fixture all live in that root ``tests/conftest.py`` and are shared with
``tests/host/test_live_jev_search_mode.py`` and
``tests/retrieve/test_live_jev_tier_reaches_wire.py``, so the skip-vs-fail
semantics of a missing key cannot drift between the live suites. Four live
calls total, one per scenario.
"""

from __future__ import annotations

import math

import pytest
from langchain_core.messages import HumanMessage

from fin.host.jev_choice import JevChoiceClient
from fin.host.track_decision import decide_track

# The closed set the product cascade hands the judge (ADR 0080).
CLOSED_SET = ("dialogue", "clarify", "off_topic", "grounded")

GROUNDED_UTTERANCE = "What was Apple's total revenue in 2023 Q3? Check the 10-Q."
OFF_DOMAIN_UTTERANCE = "What is the weather like in Paris this weekend?"
# Gray-zone by construction: no greeting / off-domain / filing / company /
# period token, so rules miss, and with no snapshots inherit and clarify
# cannot decide — the judge owns this utterance.
GRAY_UTTERANCE = "I'm feeling a bit down today, can we just chat for a while?"
# Off-domain but outside the rule token list, so the rules tier misses
# and the gray-zone judge decides with the shipped product instructions.
OFF_DOMAIN_GRAY_UTTERANCE = (
    "Can you recommend a cozy restaurant for a birthday dinner?"
)

# An early probe showed the client's generic default instructions are
# ambiguous (weather came back ``grounded`` @ 0.45), so the product
# cascade sends ``_JEV_TRACK_INSTRUCTIONS``. The raw-client sanity check
# below spells the option meanings via its own ``instructions`` seam; a
# final case drives the same weather utterance through the PRODUCT
# cascade to prove that shipped instruction text round-trips live.
OFF_DOMAIN_INSTRUCTIONS = (
    "The state is a user utterance sent to a 10-Q financial research "
    "assistant. grounded: the user wants company financial data from "
    "filings. off_topic: unrelated to financial research. dialogue: "
    "conversational chat or a general assistant task. clarify: a "
    "financial ask missing company or period. Choose exactly one option."
)

pytestmark = pytest.mark.jev_live


def test_live_four_leaf_choice_round_trip(jev_live_client: JevChoiceClient) -> None:
    grounded = jev_live_client.choose(
        utterance=GROUNDED_UTTERANCE,
        options=CLOSED_SET,
    )
    assert grounded.label in CLOSED_SET
    assert math.isfinite(grounded.confidence)
    assert 0.0 <= grounded.confidence <= 1.0

    off_domain = jev_live_client.choose(
        utterance=OFF_DOMAIN_UTTERANCE,
        options=CLOSED_SET,
        instructions=OFF_DOMAIN_INSTRUCTIONS,
    )
    assert off_domain.label in CLOSED_SET
    assert off_domain.label != "grounded"


def test_live_gray_zone_via_decide_track(jev_live_client: JevChoiceClient) -> None:
    # The product cascade slot itself, real client injected: proves the
    # gray-zone tier reached the judge instead of failing closed.
    decision = decide_track(
        [HumanMessage(content=GRAY_UTTERANCE)],
        snapshots=None,
        jev_client=jev_live_client,
    )
    assert decision.track in CLOSED_SET
    assert decision.decision_tier == "jev"


def test_live_product_instructions_reject_off_domain(
    jev_live_client: JevChoiceClient,
) -> None:
    # The exact shipped path (decide_track + _JEV_TRACK_INSTRUCTIONS):
    # the judge must answer with its leaf label; whether high-conf
    # (``jev``) or low-conf fail-closed (``clarify``), an off-domain
    # ask never resolves grounded (ADR 0080).
    decision = decide_track(
        [HumanMessage(content=OFF_DOMAIN_GRAY_UTTERANCE)],
        snapshots=None,
        jev_client=jev_live_client,
    )
    assert decision.signals.get("jev_label") in CLOSED_SET
    assert decision.decision_tier in {"jev", "clarify"}
    assert decision.track != "grounded"
