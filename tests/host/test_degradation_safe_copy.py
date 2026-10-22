"""The user-facing degradation presentation is the safe copy.

Spec ``specs/qa-chain-corpus-retrieval.md`` §"Result, error, readiness,
and degradation wire": default status, SSE, tool folds and history
expose the degradation flag and the safe copy
"部分资料暂不可用，当前结果可能不完整" — without raw reason strings or
exception details. Operational degradation stays visible without
leaking backend names, IDs, or technical tokens.

The reasons themselves are preserved verbatim through the retrieve port
INTO ACCESS-CONTROLLED DIAGNOSTICS (the degradation ledger), which is
where an operator reads them; the user-facing surfaces name no reason.
"""

from __future__ import annotations

from fin.host.degradation_notice import degradation_notice_line
from fin.retrieve.degradation import DegradationSignal


def test_notice_line_is_the_safe_copy() -> None:
    line = degradation_notice_line(
        DegradationSignal(
            degraded=True,
            degraded_reasons=("vector:attempt_failed", "graph_ppr:x"),
        )
    )
    assert "部分资料暂不可用，当前结果可能不完整" in line


def test_notice_line_carries_no_raw_reason() -> None:
    """Backend names, capability items, and technical tokens stay out of
    the user-facing line."""
    line = degradation_notice_line(
        DegradationSignal(
            degraded=True,
            degraded_reasons=("vector:attempt_failed", "graph_ppr:x"),
        )
    )
    assert "vector" not in line
    assert "graph_ppr" not in line
    assert "attempt_failed" not in line


def test_an_undegraded_signal_renders_nothing() -> None:
    assert degradation_notice_line(DegradationSignal()) == ""
