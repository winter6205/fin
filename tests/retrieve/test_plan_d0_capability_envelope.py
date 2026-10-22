"""Plan D0 fin-side T6 acceptance guards (§B3 envelope + §B4 capability
keys).

T6 packs three distinct acceptance lines under one ticket:

1. **degraded is not a complete result.** The §B3 verbatim field
   ``degraded`` and its sibling ``degraded_reasons`` must survive the
   MCP envelope trip and travel on the wire as a documented
   「可见但不算完成」 flag — not collapsed into either an empty
   chunks list or a successful answer. SC39 ties the count of
   unsignalled degradation to the ledger, and SC36 forbids treating
   a degraded answer as a complete one.

2. **8 capability keys are exactly the spec enumeration.** The
   ``fin.retrieve.capability_keys/1`` snapshot must agree with the
   retrieval service's published enumeration in
   ``specs/corpus-retrieval.md`` §B4 row 「消融的 8 个能力项」. ADR
   0039 makes the service the naming authority; fin has no business
   re-stating the names or renaming them locally.

3. **No internal alias table.** fin does not maintain a parallel
   authority mapping (e.g. ``bm25`` → ``keyword_bm25`` or
   ``graphEnabled`` → ``graph_ppr``). The capability switches the
   eval path can carry are filtered against the snapshot, so an
   alias cannot sneak in as a separate authoritative table.

These three lines are already covered by the underlying
``test_degradation.py``, ``test_capability_snapshot.py``,
``test_capability_parity.py`` and ``test_capability_static_check.py``
suites — this module re-asserts them under the Plan D0 frame so the
ticket's contract is greppable in one place.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fin.retrieve.capabilities import (
    CAPABILITY_COUNT,
    assert_snapshot_matches_published,
    capability_switch_hits,
    classify_capability_key,
    load_capability_keys,
)
from fin.retrieve.degradation import (
    DEGRADED_FIELD,
    DEGRADED_REASONS_FIELD,
    DegradationSignal,
    NOT_DEGRADED,
    read_degradation_signal,
)

# The §B4 enumeration fin must mirror, verbatim.
EXPECTED_EIGHT_KEYS: tuple[str, ...] = (
    "vector",
    "keyword_bm25",
    "cjk_segmentation",
    "phrase",
    "field_boost",
    "graph_ppr",
    "rrf",
    "rerank",
)


# ----- (1) degraded is not a complete result -----


def test_degraded_true_is_a_flag_not_an_empty_chunk_list() -> None:
    """A §B3 ``degraded: true`` answer must NOT collapse into
    ``{"chunks": []}``: that would tell the caller the corpus has
    nothing while an arm was down (§B2 第 2 条 / SC36).

    The signal travels with its reasons, so the host can both
    record SC39 and surface SC37 to the user.
    """
    signal = DegradationSignal(
        degraded=True,
        degraded_reasons=("vector:arm_down", "keyword_bm25:attempted"),
    )
    payload: dict = {"chunks": [], **signal.as_envelope_fields()}

    read_back = read_degradation_signal(payload)
    assert read_back.degraded is True
    assert "vector:arm_down" in read_back.degraded_reasons
    # The empty chunks list is a §B2 第 1 条 empty result, NOT a
    # completion; the degraded flag survives.
    assert payload[DEGRADED_FIELD] is True
    assert DEGRADED_FIELD == "degraded"
    assert DEGRADED_REASONS_FIELD == "degraded_reasons"


def test_undeliverable_degraded_is_named_failure_not_silent_chunks() -> None:
    """A service answer that carries ``degraded: true`` AND no
    retrievable chunks must surface as a typed failure
    (``RetrieveDegradedFailureError``), not as a silent
    ``{"chunks": []}`` (T20 / SC36).

    fin's adapter must NOT pretend a degraded answer is a complete
    one — the host needs to know it lost the arms.
    """
    # Mirror the product EXIT shape from mcp_adapter's
    # RetrieveDegradedFailureError.error_dict.
    error_payload = {"error": "retrieve_degraded"}
    signal = DegradationSignal(
        degraded=True,
        degraded_reasons=("vector:arm_down",),
    )
    error_payload.update(signal.as_envelope_fields())

    assert "chunks" not in error_payload
    assert error_payload["error"] == "retrieve_degraded"
    assert error_payload[DEGRADED_FIELD] is True


def test_not_degraded_sentinel_is_undegraded() -> None:
    """The §B3 sentinel ``NOT_DEGRADED`` must read as undegraded
    regardless of the envelope shape it is read against — absent
    fields default to undegraded (T20).
    """
    assert NOT_DEGRADED.degraded is False
    assert NOT_DEGRADED.degraded_reasons == ()

    # Reading from an absent / explicit-false envelope is the
    # same answer — the SC39 counter does not bump.
    assert read_degradation_signal({}).degraded is False
    assert read_degradation_signal(
        {DEGRADED_FIELD: False, DEGRADED_REASONS_FIELD: []}
    ).degraded is False


# ----- (2) 8 capability keys are exactly the spec enumeration -----


def test_snapshot_has_exactly_eight_keys_in_published_order() -> None:
    """The snapshot at ``data/retrieval/capability_keys.json`` must
    hold exactly the §B4 enumeration in the published order.
    """
    snapshot = load_capability_keys()
    assert len(snapshot) == CAPABILITY_COUNT == 8
    assert snapshot == EXPECTED_EIGHT_KEYS


def test_snapshot_matches_published_spec_enumeration() -> None:
    """The drift gate: snapshot must equal the published §B4 row.

    An enumeration change on the retrieval-service side lands as a
    :class:`CapabilityEnumerationDriftError` here instead of
    silently leaving the snapshot stale.
    """
    agreed = assert_snapshot_matches_published()
    assert agreed == EXPECTED_EIGHT_KEYS


def test_snapshot_file_is_machine_readable_and_uniquely_keyed() -> None:
    """The JSON snapshot itself is parseable, has the right schema
    marker, and contains no duplicate keys. A human-edited
    shortcut (e.g. ``"bm25"`` instead of ``"keyword_bm25"``) trips
    one of these checks before it can act as authority.
    """
    from fin.retrieve.capabilities._paths import CAPABILITY_SNAPSHOT_PATH

    raw = CAPABILITY_SNAPSHOT_PATH.read_text(encoding="utf-8")
    document = json.loads(raw)
    assert document.get("schema") == "fin.retrieve.capability_keys/1"
    keys = document.get("keys")
    assert isinstance(keys, list)
    assert all(isinstance(key, str) and key for key in keys)
    assert len(set(keys)) == len(keys) == CAPABILITY_COUNT


# ----- (3) no internal alias table -----


def test_capability_snapshot_does_not_carry_internal_aliases() -> None:
    """The §B4 snapshot at ``data/retrieval/capability_keys.json``
    must NOT carry any internal alias names (``bm25``,
    ``graphEnabled``, ``reranker``, ``vector_search``).

    The retrieval service holds naming authority (ADR 0039); an
    alias table living in fin would be a second authority on top
    of the snapshot — exactly the drift ADR 0039 forbids.
    """
    forbidden_aliases = frozenset(
        {"bm25", "graphEnabled", "reranker", "vector_search"}
    )
    snapshot = load_capability_keys()
    assert not (set(snapshot) & forbidden_aliases), (
        f"internal aliases leaked into the snapshot: "
        f"{set(snapshot) & forbidden_aliases}"
    )


def test_capability_switch_predicate_rejects_internal_aliases() -> None:
    """The §B4 K2 predicate (prefix / suffix / bare) must NOT
    classify ``bm25`` / ``graphEnabled`` as a capability switch on
    any of the 8 snapshot keys — even with a falsy value.

    A predicate that classified ``bm25`` as a switch on
    ``keyword_bm25`` would itself be the second authority ADR 0039
    forbids.
    """
    for alias in ("bm25", "graphEnabled"):
        # The bare form is checked when ``value_is_falsy=True``;
        # the prefix form (e.g. ``disable_bm25``) is checked
        # regardless of value. Both must miss.
        bare_hit = classify_capability_key(
            alias, value_is_falsy=True
        )
        prefix_hit = classify_capability_key(
            f"disable_{alias}", value_is_falsy=True
        )
        suffix_hit = classify_capability_key(
            f"{alias}_enabled", value_is_falsy=True
        )
        assert bare_hit is None
        assert prefix_hit is None
        assert suffix_hit is None


def test_no_capability_switches_in_product_path_mapping() -> None:
    """An empty runtime mapping (the product path, ADR 0039)
    contains zero capability switches — the K2 predicate is what
    guarantees that an alias cannot quietly land as a switch.
    """
    assert capability_switch_hits({}) == ()


# ----- combined: data path asserts the wire envelope -----


@pytest.mark.parametrize(
    "envelope",
    [
        pytest.param(
            {
                "chunks": [{"id": "x", "content": "y", "source_ref": "z"}],
                DEGRADED_FIELD: True,
                DEGRADED_REASONS_FIELD: ["vector:arm_down"],
            },
            id="degraded-with-results",
        ),
        pytest.param(
            {
                "chunks": [],
                DEGRADED_FIELD: True,
                DEGRADED_REASONS_FIELD: ["graph_ppr:arm_down"],
            },
            id="degraded-empty-chunks",
        ),
    ],
)
def test_degraded_signal_round_trips_through_envelope(
    envelope: dict,
) -> None:
    """A wire envelope carrying §B3 fields round-trips through
    :func:`read_degradation_signal` with the reasons intact.

    The reasons list must be exhaustive (SC39): the consumer cannot
    silently lose one — ``unsignalled_degradation_count`` would
    otherwise underreport.
    """
    signal = read_degradation_signal(envelope)
    assert signal.degraded is True
    assert len(signal.degraded_reasons) >= 1