"""T19 / SC26: product path and the all-on eval configuration hit the same locators.

The scripted eval set below stands in for the eval corpus: it is small,
in-process, and reads nothing from disk, so the default suite stays hermetic.
What it has to model faithfully is only the property SC26 asserts over — which
capability produced which hit — and that is exactly what
``CapabilityAwareFakePort`` scripts.

Parity would be vacuous if the harness could not tell the capabilities apart,
so the non-vacuity cases pin that switching each of the 8 keys off changes the
result relative to all-on.
"""

from __future__ import annotations

import pytest

from fin.corpus.extracted import Locator
from fin.retrieve.capabilities import (
    CapabilityConfigurationError,
    all_on_configuration,
    load_capability_keys,
)
from fin.retrieve.fake_capability_port import CapabilityHit, CapabilityAwareFakePort
from fin.retrieve.port import Chunk
from fin.retrieve.tool import retrieve as product_retrieve


CAPABILITY_KEYS = load_capability_keys()


def _chunk(document_id: str, unit: int, text: str, score: float) -> Chunk:
    return Chunk(
        text=text,
        source_ref=Locator(document_id=document_id, version="v-sha", unit=unit),
        score=score,
    )


# One query per producing capability, plus two multi-producer queries carrying
# the ordering the fusion capabilities move: the first has passage lengths that
# disagree with the score order (so ``rerank`` is visible), the second has all
# lengths equal (so ``rrf`` is what decides, and turning it off is visible).
EVAL_SET: dict[str, list[CapabilityHit]] = {
    "营业收入同比": [
        CapabilityHit(
            _chunk("DOC-A", 1, "营业收入同比增长，去年同期基数偏低", 3.0), {"vector"}
        ),
        CapabilityHit(_chunk("DOC-A", 2, "revenue yoy", 9.0), {"keyword_bm25"}),
    ],
    "分部披露": [
        CapabilityHit(_chunk("DOC-B", 4, "分部 披露 口径", 2.0), {"cjk_segmentation"}),
    ],
    '"material weakness"': [
        CapabilityHit(_chunk("DOC-C", 7, "material weakness in controls", 5.0), {"phrase"}),
    ],
    "risk factors heading": [
        CapabilityHit(_chunk("DOC-C", 9, "Item 1A Risk Factors", 4.0), {"field_boost"}),
    ],
    "供应商关联方": [
        CapabilityHit(_chunk("DOC-D", 3, "关联方交易对手", 1.0), {"graph_ppr"}),
    ],
    "现金流与负债": [
        CapabilityHit(_chunk("DOC-E", 1, "经营活动现金", 2.0), {"vector"}),
        CapabilityHit(_chunk("DOC-E", 5, "长期借款余额", 8.0), {"keyword_bm25"}),
        CapabilityHit(
            _chunk("DOC-E", 6, "债务契约条款", 6.0), {"graph_ppr", "vector"}
        ),
    ],
}


@pytest.fixture()
def port() -> CapabilityAwareFakePort:
    return CapabilityAwareFakePort(EVAL_SET)


def _locators(result: dict) -> set[Locator]:
    return {chunk.source_ref for chunk in result["chunks"]}


def _order(result: dict) -> list[Locator]:
    return [chunk.source_ref for chunk in result["chunks"]]


# ----- SC26 -----


def test_product_path_and_all_on_config_hit_the_same_locators(port) -> None:
    all_on = all_on_configuration()
    for query in EVAL_SET:
        product = product_retrieve(query, adapter=port)
        evaluated = port.retrieve_for_eval(query, capabilities=all_on)
        assert _locators(product) == _locators(evaluated), query


def test_the_eval_set_is_not_trivially_empty(port) -> None:
    hit_total = sum(
        len(product_retrieve(query, adapter=port)["chunks"]) for query in EVAL_SET
    )
    assert hit_total == sum(len(hits) for hits in EVAL_SET.values()) > 0


def test_product_path_forwards_no_capability_parameter(port) -> None:
    for query in EVAL_SET:
        product_retrieve(query, adapter=port)
    assert port.product_calls
    for call in port.product_calls:
        assert call.requested_capabilities is None


# ----- non-vacuity: the harness can tell the 8 capabilities apart -----


@pytest.mark.parametrize("key", CAPABILITY_KEYS)
def test_turning_one_capability_off_changes_the_result(port, key: str) -> None:
    all_on = all_on_configuration()
    one_off = {**all_on, key: False}
    differences = [
        query
        for query in EVAL_SET
        if _order(port.retrieve_for_eval(query, capabilities=all_on))
        != _order(port.retrieve_for_eval(query, capabilities=one_off))
    ]
    assert differences, key


@pytest.mark.parametrize(
    "key",
    ["vector", "keyword_bm25", "cjk_segmentation", "phrase", "field_boost", "graph_ppr"],
)
def test_turning_a_producing_capability_off_shrinks_a_locator_set(port, key: str) -> None:
    all_on = all_on_configuration()
    one_off = {**all_on, key: False}
    shrunk = [
        query
        for query in EVAL_SET
        if _locators(port.retrieve_for_eval(query, capabilities=one_off))
        < _locators(port.retrieve_for_eval(query, capabilities=all_on))
    ]
    assert shrunk, key


@pytest.mark.parametrize("key", ["rrf", "rerank"])
def test_a_fusion_capability_moves_order_without_moving_the_locator_set(
    port, key: str
) -> None:
    all_on = all_on_configuration()
    one_off = {**all_on, key: False}
    moved = []
    for query in EVAL_SET:
        on = port.retrieve_for_eval(query, capabilities=all_on)
        off = port.retrieve_for_eval(query, capabilities=one_off)
        assert _locators(on) == _locators(off), query
        if _order(on) != _order(off):
            moved.append(query)
    assert moved, key


# ----- eval configuration is validated, not silently completed -----


def test_an_incomplete_eval_configuration_is_a_named_failure(port) -> None:
    partial = dict(all_on_configuration())
    partial.pop("rerank")
    with pytest.raises(CapabilityConfigurationError) as excinfo:
        port.retrieve_for_eval("营业收入同比", capabilities=partial)
    assert excinfo.value.code == "capability_configuration_incomplete"
    assert "rerank" in str(excinfo.value)


def test_an_unknown_capability_key_is_a_named_failure(port) -> None:
    extra = {**all_on_configuration(), "long_document_recall": True}
    with pytest.raises(CapabilityConfigurationError) as excinfo:
        port.retrieve_for_eval("营业收入同比", capabilities=extra)
    assert excinfo.value.code == "unknown_capability"
    assert "long_document_recall" in str(excinfo.value)


def test_a_hit_scripted_under_an_unknown_capability_is_rejected() -> None:
    bogus = {"q": [CapabilityHit(_chunk("DOC-Z", 1, "t", 1.0), {"long_document_recall"})]}
    with pytest.raises(CapabilityConfigurationError) as excinfo:
        CapabilityAwareFakePort(bogus)
    assert excinfo.value.code == "unknown_capability"


def test_a_hit_scripted_under_no_capability_is_rejected() -> None:
    bogus = {"q": [CapabilityHit(_chunk("DOC-Z", 1, "t", 1.0), frozenset())]}
    with pytest.raises(CapabilityConfigurationError) as excinfo:
        CapabilityAwareFakePort(bogus)
    assert excinfo.value.code == "capability_configuration_incomplete"


# ----- search_mode mirroring (search-mode-retrieve T2 / T3) -----
#
# This fake is not a 档 (SC8): an accepted token answers with the SAME
# all-on script, while the port's named exit for an illegal token is
# mirrored rather than swallowed — the eval lane must reject the same
# calls the product adapter rejects. Since A2 the routing-time refusal
# class is empty (every closed-set token is accepted); the closed-set
# teeth below are unchanged.


def test_an_open_tier_answers_with_the_same_script(port) -> None:
    plain = port.retrieve("营业收入同比")
    tiered = port.retrieve("营业收入同比", search_mode="lexical")
    assert tiered == plain
    # 综述 opened: the capability fake answers it with the SAME all-on
    # script exactly like the other open 档 — it is not a named refusal.
    global_tiered = port.retrieve("营业收入同比", search_mode="global")
    assert global_tiered == plain


def test_the_capability_fake_routes_the_open_sql_tier_like_the_port(
    port,
) -> None:
    """A2 (issue #322 T7) retired the routing-time refusal this test used
    to mirror: the closed set is now ALL open, so eval-lane parity means
    ``sql`` is accepted and answers the SAME all-on script — Fake is not
    a 档 (SC8), opening a 档 changes routing, never ranking. The
    closed-set teeth are unchanged: an out-of-set token still raises
    ``invalid_search_mode`` (next test), so the shrinking refusal never
    became a silent map onto some other 档."""
    plain = port.retrieve("营业收入同比")
    sql_tiered = port.retrieve("营业收入同比", search_mode="sql")
    assert sql_tiered == plain


def test_the_capability_fake_refuses_an_illegal_mode_like_the_port(port) -> None:
    from fin.retrieve.port import InvalidSearchModeError

    with pytest.raises(InvalidSearchModeError) as excinfo:
        port.retrieve("营业收入同比", search_mode="b")
    assert excinfo.value.code == "invalid_search_mode"
