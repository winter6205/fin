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

from fin.host.jev_choice import (
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
from fin.host.jev_config import FIN_JEV_API_KEY_ENV, JevConfig

OPTIONS = ["dialogue", "clarify", "off_topic", "grounded"]


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
    choice = client.choose(utterance="which tier?", options=OPTIONS)
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
    client.choose(utterance="  which tier?  ", options=OPTIONS)

    assert len(seen) == 1
    payload = seen[0]
    assert payload["model"] == "jev-latest"
    assert payload["state"] == "  which tier?  "
    question = payload["questions"]["track"]
    assert question["type"] == "choice"
    assert list(question["criteria"].keys()) == OPTIONS
    assert isinstance(question["instructions"], str) and question["instructions"]


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
        utterance="q", options=OPTIONS, question_id="tier", instructions="pick 档"
    )
    assert choice.label == "grounded"
    assert "tier" in seen[0]["questions"]
    assert seen[0]["questions"]["tier"]["instructions"] == "pick 档"


def test_transport_error_exit() -> None:
    def transport(payload: dict[str, Any]) -> dict[str, Any]:
        raise urllib.error.URLError("connection refused")

    client = JevChoiceClient(_config(), transport=transport)
    with pytest.raises(JevChoiceError) as excinfo:
        client.choose(utterance="q", options=OPTIONS)
    assert excinfo.value.exit_code == JEV_CHOICE_EXIT_TRANSPORT


def test_missing_answers_exit_schema() -> None:
    client = JevChoiceClient(_config(), transport=lambda payload: {})
    with pytest.raises(JevChoiceError) as excinfo:
        client.choose(utterance="q", options=OPTIONS)
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
        client.choose(utterance="q", options=OPTIONS)
    assert excinfo.value.exit_code == JEV_CHOICE_EXIT_SCHEMA


def test_label_outside_closed_set_exit() -> None:
    client = JevChoiceClient(
        _config(), transport=lambda payload: _answer_payload(label="miniaturized")
    )
    with pytest.raises(JevChoiceError) as excinfo:
        client.choose(utterance="q", options=OPTIONS)
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


def test_injected_transport_never_touches_urllib(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbid(*args: object, **kwargs: object) -> None:
        raise AssertionError("network transport must not run")

    monkeypatch.setattr("fin.host.jev_choice.urllib.request.urlopen", forbid)
    client = JevChoiceClient(_config(), transport=lambda payload: _answer_payload())
    assert client.choose(utterance="q", options=OPTIONS).label == "clarify"


@pytest.mark.parametrize(
    ("options", "utterance"),
    [
        ([], "q"),
        (["a", "a"], "q"),
        (OPTIONS, ""),
        (OPTIONS, "   "),
    ],
)
def test_invalid_inputs_raise_value_error(
    options: list[str], utterance: str
) -> None:
    client = JevChoiceClient(_config(), transport=lambda payload: _answer_payload())
    with pytest.raises(ValueError):
        client.choose(utterance=utterance, options=options)


def test_fake_matches_client_contract_and_records_calls() -> None:
    from jev_fake import FakeJevChoice

    fake = FakeJevChoice([("clarify", 0.6), JevChoiceError("down", exit_code=JEV_CHOICE_EXIT_TRANSPORT)])
    choice = fake.choose(utterance="q", options=OPTIONS)
    assert isinstance(choice, JevChoice)
    assert choice.label == "clarify"
    with pytest.raises(JevChoiceError) as excinfo:
        fake.choose(utterance="q2", options=OPTIONS)
    assert excinfo.value.exit_code == JEV_CHOICE_EXIT_TRANSPORT
    assert [call["utterance"] for call in fake.calls] == ["q", "q2"]
    assert fake.calls[0]["question_id"] == "track"

    exhausted = FakeJevChoice([])
    assert exhausted.calls == []
    with pytest.raises(AssertionError):
        exhausted.choose(utterance="q", options=OPTIONS)
