"""The fin-side bounds on the degradation envelope.

Spec ``specs/qa-chain-corpus-retrieval.md`` §"Result, error, readiness,
and degradation wire": the reason list contains one to sixteen strings,
each at most 256 UTF-8 bytes without control characters. A malformed or
oversized degradation envelope is ``retrieve_protocol_error``, never an
undegraded result.

The reader (:func:`fin.retrieve.degradation.read_degradation_signal`)
is the ingress seam: the MCP adapter reads every response through it,
so the bounds live here and a violation surfaces as the named protocol
failure.
"""

from __future__ import annotations

import pytest

from fin.retrieve.degradation import (
    DegradedEnvelopeInvalidError,
    read_degradation_signal,
)


def _envelope(reasons: object) -> dict[str, object]:
    return {"degraded": True, "degraded_reasons": reasons}


# ----- the valid bounds pass through verbatim -----


def test_sixteen_reasons_pass_through_verbatim() -> None:
    reasons = tuple(f"reason-{i}" for i in range(16))
    signal = read_degradation_signal(_envelope(list(reasons)))
    assert signal.degraded is True
    assert signal.degraded_reasons == reasons


def test_a_256_byte_reason_passes_verbatim() -> None:
    reason = "r" * 256
    signal = read_degradation_signal(_envelope([reason]))
    assert signal.degraded_reasons == (reason,)


# ----- count bounds -----


@pytest.mark.parametrize(
    "reasons",
    [
        pytest.param([], id="empty-list"),
        pytest.param([f"r{i}" for i in range(17)], id="seventeen"),
    ],
)
def test_out_of_bounds_reason_count_is_invalid(
    reasons: list[str],
) -> None:
    with pytest.raises(DegradedEnvelopeInvalidError):
        read_degradation_signal(_envelope(reasons))


# ----- per-reason bounds -----


def test_a_257_byte_reason_is_invalid() -> None:
    with pytest.raises(DegradedEnvelopeInvalidError):
        read_degradation_signal(_envelope(["r" * 257]))


def test_a_control_character_reason_is_invalid() -> None:
    with pytest.raises(DegradedEnvelopeInvalidError):
        read_degradation_signal(_envelope(["ok\u0007bell"]))


def test_the_byte_bound_counts_utf8_bytes_not_characters() -> None:
    # 128 × 'é' = 256 UTF-8 bytes (two bytes each) — legal.
    two_byte = "é" * 128
    signal = read_degradation_signal(_envelope([two_byte]))
    assert signal.degraded_reasons == (two_byte,)
    # 129 × 'é' = 258 bytes — over the bound.
    with pytest.raises(DegradedEnvelopeInvalidError):
        read_degradation_signal(_envelope(["é" * 129]))
