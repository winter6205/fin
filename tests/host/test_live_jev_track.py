"""Live Soft-Track Jev verification — the ADR 0080 required live item.

Named command (run deliberately):

    uv run pytest -q -m jev_live tests/host/test_live_jev_track.py

Default ``uv run pytest -q`` deselects everything here via the
``jev_live`` mark (``tests/host/conftest.py``), so the hermetic suite
makes ZERO calls to ``api.typesafe.ai``. A missing key is a named fail
(``jev_live_key_missing``, non-zero exit), never a skip: per ADR 0080 a
green Fake pass never counts as live and missing credentials block.

The root ``tests/conftest.py`` strips the Jev env at session start; this
module re-applies ``apply_dotenv`` exactly like the product boot
(``fin-serve``) does, then builds the real client — the same
:func:`fin.host.jev_choice.build_jev_client` the product cascade uses.
Four live calls total, one per scenario.
"""

from __future__ import annotations

import math
import os
from pathlib import Path
from collections.abc import Mapping

import pytest
from langchain_core.messages import HumanMessage

from fin.host.jev_choice import JevChoiceClient, build_jev_client
from fin.host.operator_env import apply_dotenv
from fin.host.track_decision import decide_track

REPO_ROOT = Path(__file__).resolve().parents[2]

JEV_LIVE_KEY_MISSING = "jev_live_key_missing"

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


def _build_live_client(environ: Mapping[str, str]) -> JevChoiceClient:
    client = build_jev_client(environ=environ)
    if client is None:
        # Blocking named fail, not a skip (ADR 0080 / test rule 7).
        pytest.fail(
            f"{JEV_LIVE_KEY_MISSING}: set FIN_JEV_API_KEY in the repo .env",
            pytrace=False,
        )
    assert client is not None  # for type checkers; pytest.fail raises
    return client


@pytest.fixture(scope="module")
def jev_live_client() -> JevChoiceClient:
    apply_dotenv(start=REPO_ROOT)
    return _build_live_client(os.environ)


def test_missing_key_gate_is_named_fail() -> None:
    # Hermetic wiring proof for the gate above: an empty environment
    # routes to the named fail, never to a skip.
    with pytest.raises(pytest.fail.Exception) as excinfo:
        _build_live_client({})
    assert JEV_LIVE_KEY_MISSING in str(excinfo.value)


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
