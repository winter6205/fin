"""Reading the `degraded` envelope and recording its reasons (plan T20).

§B3 is the only authority for the two field names and for when the flag
is set. This module covers the *retrieve* half of T20:

* :func:`read_degradation_signal` — reading the envelope verbatim, and
  refusing every off-contract shape by name rather than guessing.
* :class:`DegradationLedger` — recording **every** item of
  ``degraded_reasons`` (SC39's「记录原因」), persistably.
* :class:`~fin.retrieve.mcp_adapter.MCPRetrieveAdapter` — propagating the
  two fields onto the product result, and turning a degraded *failed*
  request into a named failure instead of an empty result.

The host half (the user-visible signal and the citation gate's
independence from the flag) is ``tests/host/test_degradation_signal.py``.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import re
import threading
from pathlib import Path
from typing import Any

import pytest

from fin.corpus.extracted import Locator
from fin.retrieve.degradation import (
    DEGRADATION_LOG_ENV,
    DEGRADATION_STAGE_RETRIEVE,
    DEGRADATION_STAGE_USER_NOTICE,
    DEGRADED_FIELD,
    DEGRADED_REASONS_FIELD,
    FORBIDDEN_DEGRADED_ALIASES,
    DegradationLedger,
    DegradationLedgerCorruptError,
    DegradationLedgerUnwritableError,
    DegradationRecordInvalidError,
    DegradationSignal,
    DegradedEnvelopeInvalidError,
    DegradedFieldAliasError,
    degradation_ledger_from_env,
    degraded_reasons_for_display,
    read_degradation_signal,
    unsignalled_degradation_count,
)
from fin.retrieve.mcp_adapter import (
    MCPRetrieveAdapter,
    RetrieveDegradedFailureError,
    RetrieveUnavailableError,
)


REASONS = ("vector:attempt_failed", "graph_ppr:not_attempted")


def _degraded_envelope(**extra: Any) -> dict[str, Any]:
    return {
        DEGRADED_FIELD: True,
        DEGRADED_REASONS_FIELD: list(REASONS),
        **extra,
    }


# ----- the field names are the contract -----


def test_field_names_are_verbatim() -> None:
    """§B3: 「字段名逐字为 `degraded` 与 `degraded_reasons`」."""
    assert DEGRADED_FIELD == "degraded"
    assert DEGRADED_REASONS_FIELD == "degraded_reasons"


def test_no_shipped_module_spells_the_signal_another_way() -> None:
    """「不得另立名称、另作翻译或改用等价表述」— locked, not just documented.

    The alias vocabulary itself lives in ``degradation.py``; every other
    module that touches the signal must use the two verbatim names.
    """
    root = Path(__file__).resolve().parents[2] / "src" / "fin"
    guarded = [
        root / "retrieve" / "mcp_adapter.py",
        root / "host" / "degradation_notice.py",
        root / "host" / "graph.py",
        root / "host" / "stream_map.py",
    ]
    for module_path in guarded:
        assert module_path.is_file(), module_path
        source = module_path.read_text(encoding="utf-8")
        for alias in FORBIDDEN_DEGRADED_ALIASES:
            if alias.isascii():
                # An ASCII alias can be an attribute or a dict key, so
                # the whole token is what must be absent. Word bounded,
                # so ``degraded`` itself is not read as ``degrade``.
                assert not re.search(rf"\b{re.escape(alias)}\b", source), (
                    module_path,
                    alias,
                )
                continue
            # A translated alias is only a *name* when it is quoted;
            # Chinese prose in comments is the house style and says
            # 降级 constantly.
            for quoted in (f'"{alias}"', f"'{alias}'"):
                assert quoted not in source, (module_path, quoted)


def test_alias_vocabulary_excludes_the_two_real_names() -> None:
    assert DEGRADED_FIELD not in FORBIDDEN_DEGRADED_ALIASES
    assert DEGRADED_REASONS_FIELD not in FORBIDDEN_DEGRADED_ALIASES


@pytest.mark.parametrize("alias", sorted(FORBIDDEN_DEGRADED_ALIASES))
def test_an_aliased_envelope_field_is_a_named_failure(alias: str) -> None:
    """A renamed field is a contract violation on the wire, not a shape
    fin quietly tolerates (and not one it quietly honours either)."""
    with pytest.raises(DegradedFieldAliasError) as excinfo:
        read_degradation_signal({"chunks": [], alias: True})

    assert excinfo.value.code == "degraded_field_alias"
    assert alias in str(excinfo.value)


# ----- reading the envelope -----


def test_absent_flag_is_not_degraded() -> None:
    """§B3: 「`degraded` 缺省即未降级」."""
    signal = read_degradation_signal({"chunks": []})

    assert signal.degraded is False
    assert signal.degraded_reasons == ()


def test_explicit_false_is_not_degraded() -> None:
    signal = read_degradation_signal(
        {"chunks": [], DEGRADED_FIELD: False, DEGRADED_REASONS_FIELD: []}
    )

    assert signal.degraded is False


def test_true_flag_reads_every_reason_in_order() -> None:
    signal = read_degradation_signal(_degraded_envelope(chunks=[]))

    assert signal.degraded is True
    assert signal.degraded_reasons == REASONS


def test_a_non_mapping_envelope_reads_as_not_degraded() -> None:
    """There are no fields to read on a non-mapping; the caller's own
    shape validation owns that failure, not this reader."""
    assert read_degradation_signal(None).degraded is False
    assert read_degradation_signal([1, 2]).degraded is False


@pytest.mark.parametrize(
    "envelope",
    [
        pytest.param({DEGRADED_FIELD: True}, id="no-reasons-key"),
        pytest.param(
            {DEGRADED_FIELD: True, DEGRADED_REASONS_FIELD: []},
            id="empty-reasons",
        ),
        pytest.param(
            {DEGRADED_FIELD: True, DEGRADED_REASONS_FIELD: "vector"},
            id="reasons-not-a-list",
        ),
        pytest.param(
            {DEGRADED_FIELD: True, DEGRADED_REASONS_FIELD: [1]},
            id="reason-not-a-string",
        ),
        pytest.param(
            {DEGRADED_FIELD: True, DEGRADED_REASONS_FIELD: ["  "]},
            id="blank-reason",
        ),
        pytest.param({DEGRADED_FIELD: 1}, id="flag-not-a-bool"),
        pytest.param({DEGRADED_FIELD: "true"}, id="flag-is-a-string"),
        pytest.param(
            {DEGRADED_REASONS_FIELD: ["vector:attempt_failed"]},
            id="reasons-without-flag",
        ),
        pytest.param(
            {DEGRADED_FIELD: False, DEGRADED_REASONS_FIELD: ["vector"]},
            id="reasons-with-false-flag",
        ),
    ],
)
def test_off_contract_envelopes_fail_by_name(envelope: dict[str, Any]) -> None:
    """An unreadable degradation signal must not resolve to "not
    degraded": that is exactly the silent shape §B3 forbids."""
    with pytest.raises(DegradedEnvelopeInvalidError) as excinfo:
        read_degradation_signal(envelope)

    assert excinfo.value.code == "degraded_envelope_invalid"


def test_signal_renders_the_two_verbatim_fields_back_onto_an_envelope() -> None:
    signal = DegradationSignal(degraded=True, degraded_reasons=REASONS)

    assert signal.as_envelope_fields() == {
        "degraded": True,
        "degraded_reasons": list(REASONS),
    }
    assert DegradationSignal().as_envelope_fields() == {}


def test_display_reader_never_raises_on_a_broken_envelope() -> None:
    """The rendering path reads best-effort; the strict validation is the
    seam's job, and a broken envelope must not take the UI down."""
    assert degraded_reasons_for_display(_degraded_envelope()) == REASONS
    assert degraded_reasons_for_display({DEGRADED_FIELD: True}) == ()
    assert degraded_reasons_for_display("not a mapping") == ()


# ----- the ledger records every reason -----


def test_ledger_records_one_entry_per_reason() -> None:
    """SC39 wants 「`degraded_reasons` 的每一项」 recorded — not a single
    "there was a degradation" tick."""
    ledger = DegradationLedger()
    signal = read_degradation_signal(_degraded_envelope())

    records = ledger.record_signal(signal, stage=DEGRADATION_STAGE_RETRIEVE)

    assert tuple(record.reason for record in records) == REASONS
    assert ledger.count() == len(REASONS)
    assert ledger.reasons_at(DEGRADATION_STAGE_RETRIEVE) == REASONS
    assert ledger.count_by_reason() == {reason: 1 for reason in REASONS}


def test_ledger_records_nothing_for_an_undegraded_signal() -> None:
    ledger = DegradationLedger()

    assert ledger.record_signal(
        DegradationSignal(), stage=DEGRADATION_STAGE_RETRIEVE
    ) == ()
    assert ledger.count() == 0


def test_shared_ledger_serializes_concurrent_records_and_appends(
    tmp_path: Path,
) -> None:
    path = tmp_path / "concurrent-degradation.jsonl"
    ledger = DegradationLedger(path=path)
    signal = DegradationSignal(degraded=True, degraded_reasons=REASONS)
    workers = 8
    start = threading.Barrier(workers)

    def record_from_worker(index: int) -> tuple[object, ...]:
        start.wait()
        return ledger.record_signal(
            signal,
            stage=DEGRADATION_STAGE_RETRIEVE,
            detail=f"worker-{index}",
        )

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=workers
    ) as pool:
        records = tuple(pool.map(record_from_worker, range(workers)))

    expected_count = workers * len(REASONS)
    assert sum(len(worker_records) for worker_records in records) == expected_count
    assert ledger.count() == expected_count
    assert len(path.read_text(encoding="utf-8").splitlines()) == expected_count
    assert DegradationLedger(path=path).count() == expected_count


def test_ledger_refuses_a_stage_outside_the_vocabulary() -> None:
    ledger = DegradationLedger()
    signal = read_degradation_signal(_degraded_envelope())

    with pytest.raises(DegradationRecordInvalidError) as excinfo:
        ledger.record_signal(signal, stage="somewhere_else")

    assert excinfo.value.code == "degradation_record_invalid"
    assert ledger.count() == 0


def test_ledger_count_survives_the_process(tmp_path: Path) -> None:
    path = tmp_path / "degradation.jsonl"
    signal = read_degradation_signal(_degraded_envelope())
    DegradationLedger(path=path).record_signal(
        signal, stage=DEGRADATION_STAGE_RETRIEVE
    )

    reloaded = DegradationLedger(path=path)

    assert reloaded.count() == len(REASONS)
    assert reloaded.reasons_at(DEGRADATION_STAGE_RETRIEVE) == REASONS


def test_ledger_that_cannot_persist_fails_instead_of_counting(
    tmp_path: Path,
) -> None:
    """A count the operator cannot read later is not a recorded reason."""
    blocked = tmp_path / "a-file"
    blocked.write_text("not a directory", encoding="utf-8")
    ledger = DegradationLedger(path=blocked / "degradation.jsonl")
    signal = read_degradation_signal(_degraded_envelope())

    with pytest.raises(DegradationLedgerUnwritableError) as excinfo:
        ledger.record_signal(signal, stage=DEGRADATION_STAGE_RETRIEVE)

    assert excinfo.value.code == "degradation_ledger_unwritable"
    assert ledger.count() == 0


def test_a_corrupt_ledger_line_fails_rather_than_lowering_the_count(
    tmp_path: Path,
) -> None:
    path = tmp_path / "degradation.jsonl"
    path.write_text("{not json}\n", encoding="utf-8")

    with pytest.raises(DegradationLedgerCorruptError) as excinfo:
        DegradationLedger(path=path)

    assert excinfo.value.code == "degradation_ledger_corrupt"


def test_ledger_from_env_persists_only_when_configured(tmp_path: Path) -> None:
    path = tmp_path / "degradation.jsonl"

    assert degradation_ledger_from_env({}).path is None
    assert degradation_ledger_from_env({DEGRADATION_LOG_ENV: "  "}).path is None
    assert degradation_ledger_from_env(
        {DEGRADATION_LOG_ENV: str(path)}
    ).path == path


# ----- SC39's assertion shape -----


def test_recorded_but_unsignalled_reasons_are_counted() -> None:
    """SC39: 「既未记录原因也未暴露可见信号的次数 = 0」 is only assertable if
    a reason recorded at the retrieve stage without a matching user-facing
    notice is visible as a number."""
    ledger = DegradationLedger()
    signal = read_degradation_signal(_degraded_envelope())
    ledger.record_signal(signal, stage=DEGRADATION_STAGE_RETRIEVE)

    assert unsignalled_degradation_count(ledger) == len(REASONS)

    ledger.record_signal(signal, stage=DEGRADATION_STAGE_USER_NOTICE)

    assert unsignalled_degradation_count(ledger) == 0


def test_a_partly_signalled_degradation_still_counts_the_gap() -> None:
    ledger = DegradationLedger()
    ledger.record_signal(
        DegradationSignal(degraded=True, degraded_reasons=REASONS),
        stage=DEGRADATION_STAGE_RETRIEVE,
    )
    ledger.record_signal(
        DegradationSignal(degraded=True, degraded_reasons=REASONS[:1]),
        stage=DEGRADATION_STAGE_USER_NOTICE,
    )

    assert unsignalled_degradation_count(ledger) == 1


# ----- the MCP adapter propagates the envelope -----


class _ScriptedMCP(MCPRetrieveAdapter):
    """Adapter with the documented ``_call_mcp`` transport seam scripted."""

    def __init__(self, response: Any, **kwargs: Any) -> None:
        super().__init__(
            locator_map={
                "row-1": Locator(
                    document_id="DOC-2023Q1",
                    version="v-" + "a" * 60,
                    page=3,
                )
            },
            **kwargs,
        )
        self._response = response

    async def _call_mcp(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._response


def _mcp_result(**fields: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": 2, "result": fields}


HIT = {"source_ref": "row-1", "text": "net sales rose", "score": 2.0}


def test_undegraded_response_carries_no_extra_fields() -> None:
    adapter = _ScriptedMCP(_mcp_result(hits=[HIT]))

    result = adapter.retrieve("net sales")

    assert set(result) == {"chunks"}
    assert len(result["chunks"]) == 1


def test_degraded_response_propagates_both_fields_verbatim() -> None:
    adapter = _ScriptedMCP(
        _mcp_result(hits=[HIT], **_degraded_envelope())
    )

    result = adapter.retrieve("net sales")

    assert result["degraded"] is True
    assert result["degraded_reasons"] == list(REASONS)
    assert len(result["chunks"]) == 1


def test_degraded_response_with_no_hits_key_is_a_named_failure() -> None:
    """§B2 第 2 条 / SC39: 「请求失败则转为具名失败」 — the one shape that must
    never come back as ``{"chunks": []}``."""
    adapter = _ScriptedMCP(_mcp_result(**_degraded_envelope()))

    result = adapter.retrieve("net sales")

    assert result["error"] == "retrieve_degraded_failure"
    assert result["degraded"] is True
    assert result["degraded_reasons"] == list(REASONS)
    assert "chunks" not in result


def test_degraded_jsonrpc_error_is_a_named_degraded_failure() -> None:
    adapter = _ScriptedMCP(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "error": {"code": -32000, "message": "arms down"},
            "result": _degraded_envelope(),
        }
    )

    result = adapter.retrieve("net sales")

    assert result["error"] == "retrieve_degraded_failure"
    assert result["degraded_reasons"] == list(REASONS)


def test_the_degraded_failure_is_raised_typed_on_the_async_seam() -> None:
    adapter = _ScriptedMCP(_mcp_result(**_degraded_envelope()))

    with pytest.raises(RetrieveDegradedFailureError) as excinfo:
        asyncio.run(adapter.aretrieve("net sales"))

    assert excinfo.value.code == "retrieve_degraded_failure"
    assert excinfo.value.signal.degraded_reasons == REASONS
    # Same family as the existing typed EXIT, so callers that already
    # branch on "retrieve did not answer" keep working.
    assert isinstance(excinfo.value, RetrieveUnavailableError)


def test_an_undegraded_broken_response_keeps_the_existing_exit() -> None:
    """The generic unavailable EXIT is unchanged for everything that is
    not degraded — this ticket adds a code, it does not rename one."""
    adapter = _ScriptedMCP(_mcp_result(hits="not a list"))

    assert adapter.retrieve("net sales") == {"error": "retrieve_unavailable"}


def test_an_aliased_envelope_from_the_service_is_a_named_failure() -> None:
    adapter = _ScriptedMCP(_mcp_result(hits=[HIT], is_degraded=True))

    with pytest.raises(DegradedFieldAliasError):
        asyncio.run(adapter.aretrieve("net sales"))


def test_degraded_failure_dict_is_json_serializable() -> None:
    """The host serializes the typed EXIT into a ``ToolMessage``."""
    adapter = _ScriptedMCP(_mcp_result(**_degraded_envelope()))

    payload = json.loads(json.dumps(adapter.retrieve("net sales")))

    assert payload["degraded_reasons"] == list(REASONS)
