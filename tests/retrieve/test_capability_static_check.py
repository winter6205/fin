"""T19 / SC25 K2: the product call path carries zero capability-off keys.

Two layers are pinned here:

* the K2 predicate itself (three forms, verbatim from spec §E item 25), and
* the static check that applies it to the product call path — the acceptance
  assertion is ``scan_product_call_path() == ()``.

The scan target list is asserted non-empty and file-by-file resolvable so the
zero-hit result can never be zero-because-we-scanned-nothing.
"""

from __future__ import annotations

import ast
import textwrap

import pytest

from fin.retrieve.capabilities import (
    PRODUCT_CALL_PATH_FILES,
    ProductCallPathTargetMissingError,
    capability_switch_hits,
    project_root,
    scan_product_call_path,
    scan_source_for_capability_switches,
)


# ----- K2 predicate: the three forms, on a runtime parameter mapping -----


@pytest.mark.parametrize(
    "prefix", ["disable", "no", "off", "without", "exclude"]
)
def test_prefix_form_is_a_hit_whatever_the_value(prefix: str) -> None:
    for value in (True, False, None, 1, "yes"):
        hits = capability_switch_hits({f"{prefix}_rerank": value})
        assert [hit.key for hit in hits] == [f"{prefix}_rerank"]
        assert hits[0].form == "prefix"
        assert hits[0].capability == "rerank"


@pytest.mark.parametrize(
    "suffix", ["enabled", "disabled", "off", "on"]
)
def test_suffix_form_is_a_hit_only_on_a_falsy_value(suffix: str) -> None:
    key = f"graph_ppr_{suffix}"
    assert [hit.key for hit in capability_switch_hits({key: False})] == [key]
    assert capability_switch_hits({key: True}) == ()


def test_bare_form_is_a_hit_only_on_a_falsy_value() -> None:
    assert [hit.key for hit in capability_switch_hits({"vector": 0})] == ["vector"]
    assert capability_switch_hits({"vector": True}) == ()


@pytest.mark.parametrize("falsy", [False, 0, "", None, [], {}, ()])
def test_every_falsy_value_counts(falsy: object) -> None:
    assert capability_switch_hits({"rrf": falsy})


def test_keys_unrelated_to_the_snapshot_are_not_hits() -> None:
    params = {
        "query": "revenue",
        "document_id": None,
        "limit": 8,
        "disable_cache": False,
        "vectorizer": False,
        "no_rerank_at_all": False,
        "rerank_threshold": 0,
    }
    assert capability_switch_hits(params) == ()


def test_all_on_eval_configuration_is_not_a_hit() -> None:
    from fin.retrieve.capabilities import all_on_configuration

    assert capability_switch_hits(all_on_configuration()) == ()


# ----- the same predicate applied statically over source text -----


def _scan(source: str) -> tuple:
    return scan_source_for_capability_switches(
        textwrap.dedent(source), filename="<synthetic>"
    )


def test_static_scan_catches_a_keyword_argument() -> None:
    hits = _scan("adapter.retrieve(query, disable_vector=True)")
    assert [(h.key, h.form) for h in hits] == [("disable_vector", "prefix")]
    assert hits[0].location.startswith("<synthetic>:1")


def test_static_scan_catches_a_dict_literal_key() -> None:
    hits = _scan('arguments = {"query": q, "rerank": False}')
    assert [(h.key, h.form) for h in hits] == [("rerank", "bare")]


def test_static_scan_catches_a_subscript_assignment() -> None:
    hits = _scan('arguments["rrf_enabled"] = False')
    assert [(h.key, h.form) for h in hits] == [("rrf_enabled", "suffix")]


def test_static_scan_catches_a_function_parameter_default() -> None:
    hits = _scan(
        """
        def retrieve(query, *, phrase_on=False):
            return query
        """
    )
    assert [(h.key, h.form) for h in hits] == [("phrase_on", "suffix")]


def test_static_scan_treats_a_non_literal_value_as_undecided_and_reports_it() -> None:
    # The check cannot prove ``flag`` is truthy, so a switch-shaped key with a
    # non-literal value is reported rather than waved through.
    hits = _scan("adapter.retrieve(query, field_boost=flag)")
    assert [(h.key, h.form) for h in hits] == [("field_boost", "bare")]


def test_static_scan_leaves_truthy_literals_alone() -> None:
    assert _scan('arguments = {"cjk_segmentation": True, "keyword_bm25": True}') == ()


def test_static_scan_leaves_the_real_product_signature_alone() -> None:
    assert (
        _scan(
            """
            def retrieve(query, *, document_id=None, limit=8, adapter=None):
                return adapter.retrieve(query=query, document_id=document_id,
                                        limit=limit)
            """
        )
        == ()
    )


# ----- the acceptance assertion -----


def test_product_call_path_targets_all_exist_and_parse() -> None:
    assert PRODUCT_CALL_PATH_FILES
    for relative in PRODUCT_CALL_PATH_FILES:
        path = project_root() / relative
        assert path.is_file(), relative
        ast.parse(path.read_text(encoding="utf-8"))


def test_product_call_path_has_zero_capability_off_keys() -> None:
    hits = scan_product_call_path()
    assert hits == (), [
        f"{hit.location} {hit.key} ({hit.form}/{hit.capability})" for hit in hits
    ]


def test_a_missing_scan_target_is_a_named_failure(tmp_path) -> None:
    with pytest.raises(ProductCallPathTargetMissingError) as excinfo:
        scan_product_call_path(root=tmp_path)
    assert excinfo.value.code == "product_call_path_target_missing"
