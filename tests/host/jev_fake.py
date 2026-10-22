"""Shared dev/test Fake for the Jev Choice client (ADR 0080).

The Fake is the pytest injection, NOT the product classifier: default
``uv run pytest -q`` asserts closed-set routing with it, while the
product path requires a live Jev round-trip. It duck-types
:meth:`fin.host.question.jev_choice.JevChoiceClient.choose` so callers swap it
in without branching, and records every call's kwargs so zero-call and
argument assertions stay possible.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping, Sequence
from typing import Any

from fin.host.question.jev_choice import JevChoice, JevChoiceError


class FakeJevChoice:
    """Scripted Jev Choice: consume ``results`` in order, then exhaust.

    Only the scripted answers are configurable — the Fake validates
    nothing, because the closed set and its criteria belong to the real
    client's contract (``tests/host/test_jev_choice.py``). It records the
    criteria it was handed so a cascade test can still assert what the
    caller sent.
    """

    def __init__(self, results: Sequence[tuple[str, float] | JevChoiceError]) -> None:
        self._results = deque(results)
        self.calls: list[dict[str, Any]] = []

    def choose(
        self,
        *,
        utterance: str,
        options: Sequence[str],
        criteria: Mapping[str, str],
        question_id: str = "track",
        instructions: str | None = None,
    ) -> JevChoice:
        self.calls.append(
            {
                "utterance": utterance,
                "options": list(options),
                "criteria": dict(criteria),
                "question_id": question_id,
                "instructions": instructions,
            }
        )
        if not self._results:
            raise AssertionError("FakeJevChoice has no scripted results left")
        result = self._results.popleft()
        if isinstance(result, JevChoiceError):
            raise result
        label, confidence = result
        return JevChoice(label=label, confidence=confidence, probabilities={})
