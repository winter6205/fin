"""Jev Choice client contract: payload shape, typed exits, no-network default.

Every case injects a scripted transport, so pytest never contacts
``api.typesafe.ai`` (ADR 0080: the live round-trip is the T3 acceptance
item). The exits are asserted by their ``exit_code`` names — public
wire tokens — never by matching prose.
"""

from __future__ import annotations

import urllib.error
from typing import Any

import pytest

from fin.host.question.jev_choice import (
    JEV_CHOICE_EXIT_INVALID_LABEL,
    JEV_CHOICE_EXIT_INVALID_SEARCH_MODE,
    JEV_CHOICE_EXIT_LOW_CONFIDENCE,
    JEV_CHOICE_EXIT_NO_JEV_CLIENT,
    JEV_CHOICE_EXIT_SCHEMA,
    JEV_CHOICE_EXIT_TRANSPORT,
    JEV_CHOICE_EXIT_UNEXPECTED_ERROR,
    JevChoice,
    JevChoiceClient,
    JevChoiceError,
    build_jev_client,
)
from fin.host.question.jev_config import FIN_JEV_API_KEY_ENV, JevConfig
from fin.host.grounded.track_decision import (
    _JEV_TRACK_INSTRUCTIONS,
    _JEV_TRACK_LEAF_CRITERIA,
)

OPTIONS = ["dialogue", "clarify", "off_topic", "grounded"]

# Per-option discriminators, derived from OPTIONS so a leaf that joins the
# closed set here cannot arrive without its description (the client rejects
# a key-set mismatch, so a hand-written table would drift silently).
CRITERIA: dict[str, str] = {
    option: f"the {option} leaf of this closed set" for option in OPTIONS
}


def _config() -> JevConfig:
    return JevConfig(
        api_key="ts-test-key-not-a-real-secret",
        model="jev-latest",
        base_url="https://api.typesafe.ai",
    )


def _answer_payload(label: str = "clarify", confidence: float = 0.82) -> dict[str, Any]:
    return {
        "answers": {
            "track": {
                "type": "choice",
                "choice": label,
                "confidence": confidence,
                "probabilities": {option: confidence for option in OPTIONS},
            }
        },
        "model": "jev-latest",
        "usage": {"input_tokens": 10, "output_tokens": 1},
    }


def test_missing_key_builds_no_client() -> None:
    assert build_jev_client(environ={}) is None


def test_blank_key_builds_no_client() -> None:
    assert build_jev_client(environ={FIN_JEV_API_KEY_ENV: "   "}) is None


def test_with_key_builds_client_with_injected_transport() -> None:
    client = build_jev_client(
        environ={FIN_JEV_API_KEY_ENV: "ts-test-key-not-a-real-secret"},
        transport=lambda payload: _answer_payload(),
    )
    assert isinstance(client, JevChoiceClient)
    choice = client.choose(
        utterance="which tier?", options=OPTIONS, criteria=CRITERIA
    )
    assert choice == JevChoice(
        label="clarify",
        confidence=0.82,
        probabilities={option: 0.82 for option in OPTIONS},
    )


def test_transport_receives_documented_payload_shape() -> None:
    seen: list[dict[str, Any]] = []

    def transport(payload: dict[str, Any]) -> dict[str, Any]:
        seen.append(payload)
        return _answer_payload()

    client = JevChoiceClient(_config(), transport=transport)
    client.choose(
        utterance="  which tier?  ", options=OPTIONS, criteria=CRITERIA
    )

    assert len(seen) == 1
    payload = seen[0]
    assert payload["model"] == "jev-latest"
    assert payload["state"] == "  which tier?  "
    question = payload["questions"]["track"]
    assert question["type"] == "choice"
    assert list(question["criteria"].keys()) == OPTIONS
    # The wire criteria is the caller's table verbatim: no identity map, no
    # re-spelling of the option names as their own description.
    assert question["criteria"] == CRITERIA
    assert isinstance(question["instructions"], str) and question["instructions"]


def test_criteria_reaches_the_wire_and_not_the_instructions() -> None:
    """The two slots carry different things and the client keeps them
    apart: the caller's table rides ``criteria`` verbatim under its own
    keys, and ``instructions`` is echoed verbatim as given. This pins the
    client's pass-through only — the no-fold question (does the SHIPPED
    prose paste the bodies back in?) belongs to the real lane constants,
    pinned by ``test_shipped_track_lane_prose_stops_at_the_leaf_names``;
    looping this file's own locally-spelled strings against each other
    could never fail."""
    seen: list[dict[str, Any]] = []

    def transport(payload: dict[str, Any]) -> dict[str, Any]:
        seen.append(payload)
        return _answer_payload()

    instructions = (
        "Classify the operator utterance into exactly one Soft-Track "
        "track: " + ", ".join(OPTIONS) + ". Answer with the leaf name alone."
    )
    client = JevChoiceClient(_config(), transport=transport)
    client.choose(
        utterance="dig into the numbers?",
        options=OPTIONS,
        criteria=CRITERIA,
        instructions=instructions,
    )

    question = seen[0]["questions"]["track"]
    assert question["criteria"] == CRITERIA
    assert question["instructions"] == instructions


def test_shipped_track_lane_prose_stops_at_the_leaf_names() -> None:
    """The no-fold guarantee, read from the REAL track-lane constants the
    cascade ships — the way ``test_search_mode_gray_zone_jev.py`` reads
    ``_JEV_MODE_INSTRUCTIONS``. Every leaf must be taught by name in the
    instruction prose, and none of its criteria bodies may appear there:
    a body pasted into the prose is a second copy of the rule the criteria
    slot owns (the drift the ADR 0081 slot A/B retired)."""
    for leaf, body in _JEV_TRACK_LEAF_CRITERIA.items():
        assert leaf in _JEV_TRACK_INSTRUCTIONS
        assert body not in _JEV_TRACK_INSTRUCTIONS


def test_custom_question_id_and_instructions_round_trip() -> None:
    seen: list[dict[str, Any]] = []

    def transport(payload: dict[str, Any]) -> dict[str, Any]:
        seen.append(payload)
        return {
            "answers": {
                "tier": {
                    "type": "choice",
                    "choice": "grounded",
                    "confidence": 0.9,
                    "probabilities": {},
                }
            }
        }

    client = JevChoiceClient(_config(), transport=transport)
    choice = client.choose(
        utterance="q",
        options=OPTIONS,
        criteria=CRITERIA,
        question_id="tier",
        instructions="pick 档",
    )
    assert choice.label == "grounded"
    assert "tier" in seen[0]["questions"]
    assert seen[0]["questions"]["tier"]["instructions"] == "pick 档"


def test_transport_error_exit() -> None:
    def transport(payload: dict[str, Any]) -> dict[str, Any]:
        raise urllib.error.URLError("connection refused")

    client = JevChoiceClient(_config(), transport=transport)
    with pytest.raises(JevChoiceError) as excinfo:
        client.choose(utterance="q", options=OPTIONS, criteria=CRITERIA)
    assert excinfo.value.exit_code == JEV_CHOICE_EXIT_TRANSPORT


def test_missing_answers_exit_schema() -> None:
    client = JevChoiceClient(_config(), transport=lambda payload: {})
    with pytest.raises(JevChoiceError) as excinfo:
        client.choose(utterance="q", options=OPTIONS, criteria=CRITERIA)
    assert excinfo.value.exit_code == JEV_CHOICE_EXIT_SCHEMA


@pytest.mark.parametrize(
    "confidence",
    ["0.8", True, None, -0.1, 1.5, float("nan"), float("inf")],
)
def test_bad_confidence_exits_schema(confidence: object) -> None:
    payload = _answer_payload()
    payload["answers"]["track"]["confidence"] = confidence
    client = JevChoiceClient(_config(), transport=lambda p: payload)
    with pytest.raises(JevChoiceError) as excinfo:
        client.choose(utterance="q", options=OPTIONS, criteria=CRITERIA)
    assert excinfo.value.exit_code == JEV_CHOICE_EXIT_SCHEMA


def test_label_outside_closed_set_exit() -> None:
    client = JevChoiceClient(
        _config(), transport=lambda payload: _answer_payload(label="miniaturized")
    )
    with pytest.raises(JevChoiceError) as excinfo:
        client.choose(utterance="q", options=OPTIONS, criteria=CRITERIA)
    assert excinfo.value.exit_code == JEV_CHOICE_EXIT_INVALID_LABEL


def test_exit_family_keeps_its_public_spellings() -> None:
    """The seven names are audit tokens. Every producer and consumer now
    goes through the constants, so a changed VALUE would stay green on its
    own: this asserts the whole family against its literal spellings in one
    place, which makes a rename a wire change that has to fail here first."""
    assert (
        JEV_CHOICE_EXIT_TRANSPORT,
        JEV_CHOICE_EXIT_SCHEMA,
        JEV_CHOICE_EXIT_INVALID_LABEL,
        JEV_CHOICE_EXIT_NO_JEV_CLIENT,
        JEV_CHOICE_EXIT_LOW_CONFIDENCE,
        JEV_CHOICE_EXIT_UNEXPECTED_ERROR,
        JEV_CHOICE_EXIT_INVALID_SEARCH_MODE,
    ) == (
        "transport",
        "schema",
        "invalid_label",
        "no_jev_client",
        "low_confidence",
        "unexpected_error",
        "invalid_search_mode",
    )


def test_injected_transport_never_touches_urllib(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbid(*args: object, **kwargs: object) -> None:
        raise AssertionError("network transport must not run")

    monkeypatch.setattr("fin.host.question.jev_choice.urllib.request.urlopen", forbid)
    client = JevChoiceClient(_config(), transport=lambda payload: _answer_payload())
    choice = client.choose(utterance="q", options=OPTIONS, criteria=CRITERIA)
    assert choice.label == "clarify"


@pytest.mark.parametrize(
    ("options", "utterance", "owning_check"),
    [
        ([], "q", "options must be a non-empty closed set"),
        (["a", "a"], "q", "options must be unique"),
        (OPTIONS, "", "utterance must not be blank"),
        (OPTIONS, "   ", "utterance must not be blank"),
    ],
)
def test_invalid_inputs_raise_value_error(
    options: list[str], utterance: str, owning_check: str
) -> None:
    """Boundary 'invalid input' for the pre-send checks, each pinned to its
    OWNING check by the message it raises (the validation order in
    ``jev_choice._validate_question`` makes that attribution unique: empty
    options, then duplicate options, then blank utterance). The criteria
    table is the fixed non-identity one on purpose: an identity map built
    from ``options`` made the empty case raisable by TWO checks (empty
    options and empty criteria) and proved nothing about which one fired.
    The criteria checks own the next test."""
    client = JevChoiceClient(_config(), transport=lambda payload: _answer_payload())
    with pytest.raises(ValueError, match=owning_check):
        client.choose(utterance=utterance, options=options, criteria=CRITERIA)


@pytest.mark.parametrize(
    ("criteria", "named_keys"),
    [
        ({}, []),
        ({**CRITERIA, "extra": "a leaf nobody offered"}, ["extra"]),
        ({k: v for k, v in CRITERIA.items() if k != "grounded"}, ["grounded"]),
        ({option: "   " for option in OPTIONS}, sorted(OPTIONS)),
    ],
)
def test_criteria_outside_the_closed_set_raises_value_error(
    criteria: dict[str, str], named_keys: list[str]
) -> None:
    """Boundary 'invalid input' for the criteria slot, next to the options
    and utterance cases above: an empty table, a key set wider or narrower
    than the options, and a table of blank descriptions are all wiring
    errors. Each must raise before the transport runs, and the message must
    name the offending option KEYS — never echo a criteria body back into
    an audit-visible exception."""
    seen: list[dict[str, Any]] = []

    def transport(payload: dict[str, Any]) -> dict[str, Any]:
        seen.append(payload)
        return _answer_payload()

    client = JevChoiceClient(_config(), transport=transport)
    with pytest.raises(ValueError) as excinfo:
        client.choose(utterance="q", options=OPTIONS, criteria=criteria)
    assert seen == []
    message = str(excinfo.value)
    assert "criteria" in message
    for key in named_keys:
        assert key in message
    for body in criteria.values():
        assert body not in message


def test_criteria_key_order_is_not_part_of_the_contract() -> None:
    """A caller's mapping may be built in any order; only the key SET has
    to match the options, and the wire keeps the caller's own pairs. The
    options here stay in their OWN (forward) order while the criteria is
    built reversed: if the client ever sorted the table or rebuilt it to
    trail the options, the recorded wire order would change — the reversed
    criteria against the forward options is what makes that fail here."""
    seen: list[dict[str, Any]] = []

    def transport(payload: dict[str, Any]) -> dict[str, Any]:
        seen.append(payload)
        return _answer_payload()

    client = JevChoiceClient(_config(), transport=transport)
    client.choose(
        utterance="q",
        options=list(OPTIONS),
        criteria={option: CRITERIA[option] for option in reversed(OPTIONS)},
    )
    question = seen[0]["questions"]["track"]
    assert list(question["criteria"].keys()) == list(reversed(OPTIONS))


def test_fake_matches_client_contract_and_records_calls() -> None:
    from jev_fake import FakeJevChoice

    fake = FakeJevChoice(
        [
            ("clarify", 0.6),
            JevChoiceError("down", exit_code=JEV_CHOICE_EXIT_TRANSPORT),
        ]
    )
    choice = fake.choose(utterance="q", options=OPTIONS, criteria=CRITERIA)
    assert isinstance(choice, JevChoice)
    assert choice.label == "clarify"
    with pytest.raises(JevChoiceError) as excinfo:
        fake.choose(utterance="q2", options=OPTIONS, criteria=CRITERIA)
    assert excinfo.value.exit_code == JEV_CHOICE_EXIT_TRANSPORT
    assert [call["utterance"] for call in fake.calls] == ["q", "q2"]
    assert fake.calls[0]["question_id"] == "track"
    # The Fake records the criteria it was handed, so a cascade test can
    # assert what the caller actually sent without a real client.
    assert fake.calls[0]["criteria"] == CRITERIA

    exhausted = FakeJevChoice([])
    assert exhausted.calls == []
    with pytest.raises(AssertionError):
        exhausted.choose(utterance="q", options=OPTIONS, criteria=CRITERIA)
