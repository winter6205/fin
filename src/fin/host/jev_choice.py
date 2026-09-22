"""TypeSafe Jev (System One) closed-set Choice client for host judges.

Soft-Track's gray zone and 档 assignment consume a Jev Choice over a
closed option set plus a confidence (ADR 0080). This module is the only
place that speaks the TypeSafe wire shape; callers never hand-roll the
HTTP call. There is no MiniMax fallback here: a missing key means no
client, and a failed call means a typed exit, never ``None``.

The exits are public wire tokens (same convention as
:mod:`fin.host.named_failures`): asserted by name, never by prose::

    transport       HTTP / URLError / timeout — the judge could not be reached
    schema          a well-delivered response whose JSON shape is not the
                    documented Choice contract, or a confidence outside a
                    finite [0, 1]
    invalid_label   the returned label is not in the caller's closed set

A gray-zone tier also records exits the client never raises, and they
belong to the same family because they land in the same ``*_jev_exit``
audit slot and mean the same thing (nothing was decided)::

    no_jev_client           the tier was reached with no client to ask
    low_confidence          the judge answered under the confidence floor
    unexpected_error        a non-typed throw, contained at the boundary
    invalid_search_mode     the 档 lane's judge named a label its 档 SSOT
                            cannot map (the retrieve side spells the same
                            token for its own invalid-selector failure —
                            shared spelling, separate facts)

Those four are authored by the cascades, both of which already import
this module, so the names are exported here rather than re-spelled per
lane.

Invalid *inputs* (empty options, duplicate options, blank utterance, a
criteria mapping that does not describe exactly the offered options —
empty, off-key, or blank-valued) are programming errors at this boundary
and raise ``ValueError``, not a typed exit: a typed exit describes a judge
fact, and the judge was never asked anything when the call was malformed
before send.
"""

from __future__ import annotations

import json
import math
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Callable, Final, Sequence

from fin.host.jev_config import JevConfig, load_jev_config

JEV_CHOICE_EXIT_TRANSPORT: Final[str] = "transport"
JEV_CHOICE_EXIT_SCHEMA: Final[str] = "schema"
JEV_CHOICE_EXIT_INVALID_LABEL: Final[str] = "invalid_label"
JEV_CHOICE_EXIT_NO_JEV_CLIENT: Final[str] = "no_jev_client"
JEV_CHOICE_EXIT_LOW_CONFIDENCE: Final[str] = "low_confidence"
JEV_CHOICE_EXIT_UNEXPECTED_ERROR: Final[str] = "unexpected_error"
JEV_CHOICE_EXIT_INVALID_SEARCH_MODE: Final[str] = "invalid_search_mode"

DEFAULT_JEV_CHOICE_INSTRUCTIONS: Final[str] = (
    "Choose exactly one option that best fits the state."
)


@dataclass(frozen=True, slots=True)
class JevChoice:
    label: str
    confidence: float
    probabilities: Mapping[str, float] = field(default_factory=dict)


class JevChoiceError(Exception):
    """Typed Jev Choice failure; ``exit_code`` names the closed exit set."""

    def __init__(self, message: str, *, exit_code: str) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def _validate_question(
    utterance: str,
    options: Sequence[str],
    criteria: Mapping[str, str],
) -> None:
    """Refuse a malformed question BEFORE anything is sent.

    Every check here runs before the send: a malformed question is a
    programming error, so the judge is never asked a broken thing. The
    criteria messages name option keys only and never echo a value, so
    a mis-wired table cannot put its own text on an audit surface. A
    ``ValueError`` raised here reaches the two cascades through their
    wide ``except``, where it lands as ``*_jev_exit=unexpected_error``
    plus ``*_jev_error_type`` — which keeps a wiring bug separable from
    a judge fact on the audit while both fail closed.

    The check ORDER (empty options, then duplicate options, then blank
    utterance, then the criteria contract) is what lets a test pin each
    malformed request to exactly one owning check.
    """
    if not options:
        raise ValueError("options must be a non-empty closed set")
    if len(set(options)) != len(options):
        raise ValueError("options must be unique")
    if not utterance.strip():
        raise ValueError("utterance must not be blank")
    if not criteria:
        raise ValueError("criteria must describe every option")
    missing = sorted(set(options) - set(criteria))
    extra = sorted(set(criteria) - set(options))
    if missing or extra:
        raise ValueError(
            "criteria keys must match options exactly: "
            f"missing={missing} extra={extra}"
        )
    blank = sorted(
        option for option, text in criteria.items() if not text.strip()
    )
    if blank:
        raise ValueError(f"criteria description must not be blank: blank={blank}")


class JevChoiceClient:
    """One closed-set Choice question per call against System One.

    ``transport`` is the injectable ``payload -> response_dict`` seam:
    tests and the dev Fake pass their own, so pytest never contacts
    ``api.typesafe.ai``. Left unset, the default urllib POST is used —
    the only path that touches the network, and only after a key exists.
    """

    def __init__(
        self,
        config: JevConfig,
        *,
        transport: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        timeout: float = 20.0,
    ) -> None:
        self._config = config
        self._timeout = timeout
        self._transport = self._post_systemone if transport is None else transport

    def choose(
        self,
        *,
        utterance: str,
        options: Sequence[str],
        criteria: Mapping[str, str],
        question_id: str = "track",
        instructions: str | None = None,
    ) -> JevChoice:
        # The whole input contract lives in ``_validate_question``: a
        # malformed question raises there, before this method builds a
        # payload or touches the transport.
        _validate_question(utterance, options, criteria)

        payload: dict[str, Any] = {
            "model": self._config.model,
            "state": utterance,
            "questions": {
                question_id: {
                    "type": "choice",
                    "instructions": (
                        DEFAULT_JEV_CHOICE_INSTRUCTIONS
                        if instructions is None
                        else instructions
                    ),
                    # Each option carries the description its caller wrote
                    # for it; a label is never its own discriminator.
                    "criteria": dict(criteria),
                }
            },
        }

        try:
            response = self._transport(payload)
        except JevChoiceError:
            raise
        except Exception as exc:
            raise JevChoiceError(
                "jev choice transport failed",
                exit_code=JEV_CHOICE_EXIT_TRANSPORT,
            ) from exc

        if not isinstance(response, Mapping):
            raise JevChoiceError(
                "jev choice response is not an object", exit_code=JEV_CHOICE_EXIT_SCHEMA
            )
        answers = response.get("answers")
        if not isinstance(answers, Mapping):
            raise JevChoiceError(
                "jev choice response has no answers object",
                exit_code=JEV_CHOICE_EXIT_SCHEMA,
            )
        answer = answers.get(question_id)
        if not isinstance(answer, Mapping):
            raise JevChoiceError(
                "jev choice answer is missing or not an object",
                exit_code=JEV_CHOICE_EXIT_SCHEMA,
            )
        label = answer.get("choice")
        if not isinstance(label, str):
            raise JevChoiceError(
                "jev choice label is not a string", exit_code=JEV_CHOICE_EXIT_SCHEMA
            )
        confidence = self._require_confidence(answer.get("confidence"))
        # probabilities is display metadata; the label + confidence pair
        # is the decision. An absent map is empty, a present map must be
        # well-formed — a half-broken distribution is a schema fact.
        probabilities: dict[str, float] = {}
        if "probabilities" in answer:
            raw = answer["probabilities"]
            if not isinstance(raw, Mapping):
                raise JevChoiceError(
                    "jev choice probabilities is not an object",
                    exit_code=JEV_CHOICE_EXIT_SCHEMA,
                )
            for key, value in raw.items():
                if not isinstance(key, str) or isinstance(value, bool) or not isinstance(
                    value, (int, float)
                ):
                    raise JevChoiceError(
                        "jev choice probabilities entry is not numeric",
                        exit_code=JEV_CHOICE_EXIT_SCHEMA,
                    )
                probabilities[key] = float(value)

        if label not in options:
            raise JevChoiceError(
                "jev choice returned a label outside the closed set",
                exit_code=JEV_CHOICE_EXIT_INVALID_LABEL,
            )
        return JevChoice(label=label, confidence=confidence, probabilities=probabilities)

    @staticmethod
    def _require_confidence(value: object) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise JevChoiceError(
                "jev choice confidence is not numeric",
                exit_code=JEV_CHOICE_EXIT_SCHEMA,
            )
        confidence = float(value)
        if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
            raise JevChoiceError(
                "jev choice confidence is outside [0, 1]",
                exit_code=JEV_CHOICE_EXIT_SCHEMA,
            )
        return confidence

    def _post_systemone(self, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._config.base_url}/v1/systemone"
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {self._config.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                decoded = json.loads(response.read())
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            raise JevChoiceError(
                "jev choice request failed", exit_code=JEV_CHOICE_EXIT_TRANSPORT
            ) from exc
        if not isinstance(decoded, dict):
            raise JevChoiceError(
                "jev choice returned a non-object payload",
                exit_code=JEV_CHOICE_EXIT_SCHEMA,
            )
        return decoded


def build_jev_client(
    *,
    environ: Mapping[str, str] | None = None,
    transport: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> JevChoiceClient | None:
    """Return a configured client, or ``None`` when no TypeSafe key is set."""
    config = load_jev_config(environ=environ)
    if config is None:
        return None
    return JevChoiceClient(config, transport=transport)


__all__ = [
    "DEFAULT_JEV_CHOICE_INSTRUCTIONS",
    "JEV_CHOICE_EXIT_INVALID_LABEL",
    "JEV_CHOICE_EXIT_INVALID_SEARCH_MODE",
    "JEV_CHOICE_EXIT_LOW_CONFIDENCE",
    "JEV_CHOICE_EXIT_NO_JEV_CLIENT",
    "JEV_CHOICE_EXIT_SCHEMA",
    "JEV_CHOICE_EXIT_TRANSPORT",
    "JEV_CHOICE_EXIT_UNEXPECTED_ERROR",
    "JevChoice",
    "JevChoiceClient",
    "JevChoiceError",
    "build_jev_client",
]
