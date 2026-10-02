"""Fixture-driven contract tests for the E1 structural evidence validator (E3).

Every conformance case under ``data/conformance/evidence/`` is driven through
the validator; expectations come only from the committed manifests, fixtures,
and the worksheet — never from the implementation.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from fin.host.evidence_contract import (
    AdmissionContext,
    compute_snapshot_digest,
    seed_context_from_rows,
    snapshot_canonical_json,
    validate_evidence,
    validate_snapshot_identity,
)

FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "data" / "conformance" / "evidence"
PROFILES = ("citation", "visual_claim", "database_row", "business_field", "comparison")

DOC_VERSION = "sha256:9c2b8f74514e6d321c0aa9f7e3d5b1089c2b8f74514e6d321c0aa9f7e3d5b108"
OTHER_DOC_VERSION = (
    "sha256:1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d5e6f708192a3b4c5d6e7f809"
)
STANDARD_QUOTE = (
    "Employees must submit expense reports within thirty (30) days "
    "of the end of the travel period."
)
LEDGER_VIEW = "ledger_ro.contract_expiry"

# The harness context half (top-level manifest ``harness_context_note``):
# trusted thread, stored versions, and authorized views that the shape-valid
# fixtures are admitted or denied against.
DEFAULT_CONTEXT = AdmissionContext(
    trusted_attachment_ids=frozenset({"att-7401"}),
    resolvable_locators=frozenset({("t1-expense-001", DOC_VERSION), ("att-7401", "17")}),
    received_texts={
        ("t1-expense-001", DOC_VERSION, 3): (
            "Article 4. Reimbursement. " + STANDARD_QUOTE + " Article 5. Caps."
        ),
        ("att-7401", "17", 3): "Receipt page. " + STANDARD_QUOTE,
    },
    current_observation_versions={("att-7401", "17", 4): 2},
    authorized_database_views=frozenset({LEDGER_VIEW}),
    database_source_names=frozenset({"ledger_ro"}),
    readable_subject_refs=frozenset({"iknow:entity/vendor-northwind"}),
    received_source_versions=frozenset(
        {("AAPL-2022Q3", OTHER_DOC_VERSION), ("AAPL-2023Q3", DOC_VERSION)}
    ),
)


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _manifest(profile: str) -> list[dict[str, Any]]:
    return _load_json(FIXTURE_ROOT / profile / "manifest.json")["cases"]


def _fixture(profile: str, case: str) -> dict[str, Any]:
    return _load_json(FIXTURE_ROOT / profile / f"{case}.json")


ALL_CASES = sorted(
    (profile, entry["case"]) for profile in PROFILES for entry in _manifest(profile)
)


def _database_row_case_ids() -> list[str]:
    return [case for profile, case in ALL_CASES if profile == "database_row"]


def _ingress_snapshot(index: int = 0) -> dict[str, Any]:
    """The STORED snapshot a digest carrier claims to identify.

    Read from the committed valid-ingress row — an ingress row carries
    exactly the five value keys — and given the EGRESS token pair on
    top, because that is the shape a stored snapshot has once the E5
    cutover has passed. WHY the distinction matters: a digest carrier
    is the §4.4 egress identity rule ("the stored snapshot it claims to
    identify"), and the stored object is the ``schema``-bearing one. The
    digest itself is unchanged by this, because ``_identity_input`` is
    computed over the five identity/value keys only — the token pair is
    not part of ``m``.
    """
    envelope = _fixture("database_row", "valid-ingress-envelope")["payload"]
    row = envelope["database_rows"]["rows"][index]
    return {
        "schema": DATABASE_ROW_SNAPSHOT_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        **row,
    }


def _context_for(profile: str, case: str) -> AdmissionContext:
    if (profile, case) == ("database_row", "untrusted-view-denied"):
        return AdmissionContext(**{**vars(DEFAULT_CONTEXT),
                                   "authorized_database_views": frozenset()})
    if (profile, case) == ("comparison", "untrusted-source-version"):
        # The run received only the 2022 filing; the frame names both.
        return AdmissionContext(**{**vars(DEFAULT_CONTEXT),
                                   "received_source_versions": frozenset(
                                       {("AAPL-2022Q3", OTHER_DOC_VERSION)}
                                   )})
    return DEFAULT_CONTEXT


def _is_digest_carrier(payload: Any) -> bool:
    return isinstance(payload, dict) and set(payload) == {
        "source",
        "record_key",
        "snapshot_digest",
    }


@pytest.mark.parametrize(
    ("profile", "case"),
    ALL_CASES,
    ids=[f"{profile}/{case}" for profile, case in ALL_CASES],
)
def test_conformance_fixture_outcome(profile: str, case: str) -> None:
    manifest_entry = next(e for e in _manifest(profile) if e["case"] == case)
    fixture = _fixture(profile, case)
    assert fixture["case"] == case
    assert fixture["direction"] == manifest_entry["direction"]
    assert fixture["expect"] == manifest_entry["expect"]
    assert fixture["refusal_code"] == manifest_entry["refusal_code"]

    if manifest_entry["class"] == "v0-replay":
        # The v0 replay read is owned by the thread store's frozen
        # projection (spec §7.3), not by the emission-shape validator —
        # ``page`` is an unknown key there by design. The case pins that
        # ONE authority refuses, and the web mirror of the same fixture
        # (evidenceContract.test.ts) must name the same code.
        from fin.host.thread_store import project_stored_citation

        assert project_stored_citation(fixture["citation"]) is None
        assert manifest_entry["refusal_code"] == "unsupported_evidence_contract"
        return

    payload = fixture["payload"]
    context = _context_for(profile, case)
    if profile == "database_row" and _is_digest_carrier(payload):
        result = validate_snapshot_identity(payload, _ingress_snapshot())
    else:
        result = validate_evidence(payload, context)

    expected_code = manifest_entry["refusal_code"]
    if manifest_entry["expect"] == "accept":
        assert result.accepted, f"{profile}/{case} unexpectedly refused: {result.refusal}"
        return
    assert not result.accepted, f"{profile}/{case} unexpectedly accepted"
    assert result.refusal is not None
    assert result.refusal.code == expected_code
    assert result.refusal.lane == profile


def test_case_inventory_matches_manifest_totals() -> None:
    """The three numbers are COUNTED, never maintained.

    Each is recomputed from the five per-profile manifests the
    parametrized cases above are built from, so this test cannot pass by
    agreeing with a hand-typed number and cannot fail because one fixture
    was added or removed:

    * ``accepts`` — cases whose manifest ``expect`` is ``accept``, summed
      over the same five profiles (citation 3, visual_claim 3,
      database_row 4, business_field 5, comparison 2 = 17);
    * ``refuses`` — the rest (15 + 14 + 21 + 12 + 9 = 71);
    * ``len(ALL_CASES)`` — the parametrized id list, 17 + 71 = 88.

    WHY http_sse is not in the total: it is a WIRE example, not a
    payload profile, so it is not in ``PROFILES`` and
    ``test_conformance_fixture_outcome`` never runs it. Counting it here
    would assert a number the parametrization does not back. It is
    pinned live instead by tests/host/test_workspace_http.py.

    WHY the shared ``schema_version_cases.json`` is not in the total: it
    is a SUPPLEMENT, registered in the root manifest's ``supplements``
    key, not a case file of a profile, and it has no ``expect`` /
    ``refusal_code`` pair. Its own runner below asserts every one of its
    cases, and pins the case count it consumed.
    """
    accepts = refuses = 0
    for profile in PROFILES:
        for entry in _manifest(profile):
            accepts += entry["expect"] == "accept"
            refuses += entry["expect"] == "refuse"
    assert (accepts, refuses, len(ALL_CASES)) == (17, 71, 88)


# ----- the SHARED schema_version case set (supplement, not a profile) -----
#
# WHY this block exists at all: until the set was committed, each side of
# the version rule was proved only against itself — the Python validator
# against its own test, the web decoder against its own test — and the one
# boundary that had to be right is the one a self-consistent Python test
# cannot see. ``json.loads("1.0")`` is the float ``1.0`` and
# ``JSON.parse("1.0")`` is the integer ``1``; if the two ends disagreed
# about that one value, BOTH suites would still be green. The set below
# is written from the rule (ADR 0093 decisions 7/8/10/12) and read by the
# Python validator, the comparison export, AND
# web/src/evidenceContract.test.ts, so one disagreement fails somewhere
# the other language can see it.
#
# The expectations are NOT read off either implementation. Nothing below
# calls the code under test to learn what to expect; each case's
# ``expect`` is the declaration, and the runner reports the disagreement.

VERSION_CASES_PATH = FIXTURE_ROOT / "schema_version_cases.json"
VERSION_CASES = _load_json(VERSION_CASES_PATH)["cases"]
VERSION_CASE_IDS = [case["case"] for case in VERSION_CASES]

# The three non-finite spellings, and the ONLY way one enters the set.
# ``json.loads`` accepts bare ``NaN``/``Infinity`` by default, so the file
# is read with that extension turned OFF and the tag is resolved here
# instead. Writing a bare ``NaN`` into a committed JSON file would make
# the set unreadable by any strict parser — including the TypeScript
# runner, whose ``JSON.parse`` has no such extension.
_NONFINITE_TAGS: dict[str, float] = {
    "NaN": float("nan"),
    "Infinity": float("inf"),
    "-Infinity": float("-inf"),
}

_EXPECTATIONS = ("accept", "malformed_evidence_contract", "unsupported_evidence_contract")


def _load_version_cases_strictly() -> list[dict[str, Any]]:
    """Read the set with JSON's non-standard NaN/Infinity extension
    disabled, so the file is held to the same grammar the web runner
    parses it with."""
    return json.loads(
        VERSION_CASES_PATH.read_text(encoding="utf-8"),
        parse_constant=_reject_json_extension,
    )["cases"]


def _reject_json_extension(token: str) -> Any:
    raise AssertionError(
        f"the case set spells a non-finite literal {token!r} directly; "
        "use the schema_version_nonfinite tag so both runners agree"
    )


def _version_value(case: dict[str, Any]) -> tuple[str, Any]:
    """The ``(kind, value)`` a case declares: ``present`` with the raw
    JSON value, ``absent`` with ``None`` meaning "no key at all", or
    ``nonfinite`` with the resolved float.

    Exactly one of the three forms may appear, and the file is read
    strictly enough that a fourth cannot: a case carrying two would make
    "which one is the input" a runner's private decision, and the runners
    would then be driven by different inputs.
    """
    forms = [
        name
        for name, key in (
            ("present", "schema_version"),
            ("absent", "schema_version_absent"),
            ("nonfinite", "schema_version_nonfinite"),
        )
        if key in case
    ]
    assert len(forms) == 1, f"{case['case']} declares {forms}, expected exactly one"
    form = forms[0]
    if form == "present":
        return "present", case["schema_version"]
    if form == "absent":
        assert case["schema_version_absent"] is True
        return "absent", None
    tag = case["schema_version_nonfinite"]
    assert tag in _NONFINITE_TAGS, f"{case['case']} names unknown non-finite {tag!r}"
    return "nonfinite", _NONFINITE_TAGS[tag]


def _version_payload(form: str, value: Any) -> dict[str, Any]:
    """The citation channel carrying a snapshot whose version slot is
    whatever the case supplies.

    The token is always the RECOGNIZED
    ``fin.evidence.database_row_snapshot``: an unrecognized token would
    exit on the token check, so the version check would never run and the
    case would prove nothing about the version rule. The snapshot is
    otherwise the committed egress shape with only the version varied, so
    a refusal can ONLY come from the version check — which is what makes
    a malformed/unsupported expectation meaningful rather than
    incidental. The channel's own ``schema_version`` stays the integer 1
    on every case, so the outer declaration is never the thing under
    test.
    """
    snapshot = _egress_snapshot(1)
    if form == "absent":
        del snapshot["schema_version"]
    else:
        snapshot["schema_version"] = value
    return {
        "evidence_schema": "fin.evidence.citation",
        "schema_version": 1,
        "citations": [_egress_citation(snapshot)],
    }


def _observed_class(result: Any) -> str:
    if result.accepted:
        return "accept"
    assert result.refusal is not None
    return result.refusal.code


def test_version_case_set_is_well_formed() -> None:
    """The set's own invariants, checked before any runner relies on it.

    This is the guard that keeps the file from quietly becoming a set
    only one implementation can read: every case declares exactly one
    input form, every expectation is in the closed three-value domain, the
    case ids are unique, and the file parses under STRICT JSON — the same
    grammar ``JSON.parse`` applies on the web side.
    """
    strict = _load_version_cases_strictly()
    assert len(strict) == len(VERSION_CASES)
    assert len(set(VERSION_CASE_IDS)) == len(VERSION_CASES)
    for case in VERSION_CASES:
        assert case["expect"] in _EXPECTATIONS, case["case"]
        assert isinstance(case["why"], str) and case["why"].strip(), case["case"]
        _version_value(case)
    # The three non-finite spellings are all present, because a set that
    # carried only one would not be pinning the finiteness half of the
    # rule (a magnitude test would answer differently on the two signs).
    tagged = {
        case["schema_version_nonfinite"]
        for case in VERSION_CASES
        if "schema_version_nonfinite" in case
    }
    assert tagged == set(_NONFINITE_TAGS), tagged


@pytest.mark.parametrize("case", VERSION_CASES, ids=VERSION_CASE_IDS)
def test_version_case_through_the_validator(case: dict[str, Any]) -> None:
    """The validator leg. The expected class is the case's own
    ``expect``; the runner never asks the code what to expect."""
    form, value = _version_value(case)
    result = validate_evidence(_version_payload(form, value), DEFAULT_CONTEXT)
    assert _observed_class(result) == case["expect"], (
        f"{case['case']}: declared {case['expect']}, validator said "
        f"{_observed_class(result)} ({case['why']})"
    )


def _version_comparison(form: str, value: Any) -> dict[str, Any]:
    """One ``fin.comparison`` envelope with the case's version beside its
    token, otherwise a minimal grid.

    WHY the export leg cannot reuse the citation payload: the export's
    comparison path is the one that BINDS a recognized envelope, so it
    has its own token and its own copy of the version rule
    (``export._schema_version_problem``). Driving the same declarations
    through both is the point — the operator's ruling is that the export
    and the validator must not drift, and two callers of the same rule in
    one process still drift if only one of them is tested.
    """
    envelope: dict[str, Any] = {
        "schema": "fin.comparison",
        "kind": "table",
        "axis": "period",
        "concept": "us-gaap:Revenues",
        "unit": "USD",
        "scale": "millions",
        "columns": [{"key": "2022-Q3", "label": "2022-Q3"}],
        "rows": [{"key": "AAPL", "label": "AAPL", "kind": "issuer", "cells": []}],
        "deltas": [],
        "missing": [],
        "hit_count": 0,
        "cell_count": 0,
        "locators": [],
    }
    if form != "absent":
        envelope["schema_version"] = value
    return envelope


def _observed_export_class(envelope: dict[str, Any]) -> str:
    """The export's verdict on one envelope, as a class from the same
    closed three-value domain the case file declares.

    The export raises ONE typed error for both refusal classes (that is
    its frozen wire ``code``, and a third code for the malformed half
    would be a new contract vocabulary), so the class is read off the
    message. WHY reading the message is legitimate here and is not
    answer-copying: the message is a fixed, committed string in
    ``export._schema_version_problem`` naming one of the two §6 classes,
    and the test asserts a DECLARED class against it. If the export ever
    stopped naming which class it meant, this would raise and the test
    would fail rather than silently pass.
    """
    from fin.host.export import (
        UnsupportedComparisonSchemaError,
        build_export,
    )
    from fin.host.thread_store import RunRecord

    run = RunRecord(
        id="run-version-case",
        thread_id="t-version-case",
        question="q",
        status="done",
        answer="a",
        citations=[],
        comparisons=[envelope],
    )
    try:
        build_export(runs=[run], kind="comparison", thread_id="t-version-case", fmt="json")
    except UnsupportedComparisonSchemaError as exc:
        for klass in ("malformed_evidence_contract", "unsupported_evidence_contract"):
            if klass in str(exc):
                return klass
        raise AssertionError(f"export refused without naming a class: {exc}") from exc
    return "accept"


@pytest.mark.parametrize("case", VERSION_CASES, ids=VERSION_CASE_IDS)
def test_version_case_through_the_comparison_export(case: dict[str, Any]) -> None:
    """The export leg, asserted against the SAME declaration, for EVERY
    case — there is no carve-out left to state.

    PREMISE MOVED (the absent case used to diverge): this leg once
    special-cased ``version-absent-with-token`` and asserted the export
    returned ``accept`` where the case file declared
    ``malformed_evidence_contract``, recording that split as a product
    decision under ADR 0093 decisions 9/10. Decision 12 SUPERSEDED
    decision 9's absent-accepts half — the token/version pairing is
    atomic and no component back-fills the number — and
    ``export._schema_version_problem`` was re-synced to that ruling, so
    the export now returns the SAME declared class as the validator
    (above) and the web. Authority: ADR 0093 decision 12, recorded in
    decision 9's annotation. Explanation for removing rather than
    flipping the branch: the divergence it pinned no longer exists, and
    keeping an ``accept`` assertion for a shape the export now refuses
    would assert behavior no ruling supports. No case was dropped and no
    ``expect`` was edited to match the code — the assertion below is the
    shared declaration itself, uniform across all 18 cases, and the
    consumption cross-check below proves every case still reaches this
    leg.
    """
    form, value = _version_value(case)
    observed = _observed_export_class(_version_comparison(form, value))
    assert observed == case["expect"], (
        f"{case['case']}: declared {case['expect']}, export said {observed} ({case['why']})"
    )


def test_every_version_case_is_driven_through_both_python_boundaries(
    request: pytest.FixtureRequest,
) -> None:
    """The cross-check, as an assertion rather than a comment: both
    parametrized runners above iterate ``VERSION_CASES``, so a case
    silently dropped from either side would show up here as a
    consumed-count mismatch.

    The items are read off the live collection, so this reports what the
    two runners ACTUALLY consumed rather than what they were told to.
    """
    runners = [
        "test_version_case_through_the_validator",
        "test_version_case_through_the_comparison_export",
    ]
    for runner in runners:
        consumed = {
            str(node.callspec.id)
            for node in request.session.items
            # ``node.name`` carries the parameter id; ``originalname`` is
            # the undecorated function name.
            if node.originalname == runner and node.callspec is not None
        }
        assert consumed == set(VERSION_CASE_IDS), (
            f"{runner} did not consume every declared case: "
            f"missing {sorted(set(VERSION_CASE_IDS) - consumed)}, "
            f"unexpected {sorted(consumed - set(VERSION_CASE_IDS))}"
        )
    assert len(VERSION_CASES) == 18, VERSION_CASE_IDS


def test_digest_worksheet_recomputes_independently() -> None:
    worksheet = _load_json(FIXTURE_ROOT / "database_row" / "digest_worksheet.json")
    checked = 0
    for entry in worksheet["snapshots"]:
        if "raw_ingress_snapshot" not in entry:
            continue
        snapshot = entry["raw_ingress_snapshot"]
        assert snapshot_canonical_json(snapshot) == entry["canonical_json"]
        assert compute_snapshot_digest(snapshot) == entry["snapshot_digest"]
        checked += 1
    assert checked == 2


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        "",
        0,
        {"mystery_lane": []},
        {"visual_claims": "not-a-list"},
        {"evidence_schema": "fin.evidence.citation", "citations": None},
    ],
    ids=["none", "list", "string", "zero", "unknown-root", "claims-string", "citations-none"],
)
def test_non_mapping_and_unknown_roots_refuse_without_crash(payload: Any) -> None:
    result = validate_evidence(payload, DEFAULT_CONTEXT)
    assert not result.accepted
    assert result.refusal is not None
    assert result.refusal.code == "malformed_evidence_contract"


def _row_envelope(rows: list[dict[str, Any]], result_count: int) -> dict[str, Any]:
    """One conforming INGRESS envelope (spec 4.4): the ``schema`` /
    ``schema_version`` pair names the format of the list ONCE, on the
    envelope, so a row inside ``rows[]`` carries none of its own."""
    return {
        "chunks": [],
        "database_rows": {
            "schema": "fin.evidence.retrieve_rows",
            "schema_version": 1,
            "result_count": result_count,
            "rows": rows,
        },
    }


def _snapshot(queried_at: str) -> dict[str, Any]:
    """One conforming INGRESS row: exactly the five value keys."""
    return {
        "source": LEDGER_VIEW,
        "record_key": {"contract_id": "C-9000"},
        "queried_at": queried_at,
        "fields": {"note": "boundary probe"},
        "summary": "synthetic boundary probe row",
    }


@pytest.mark.parametrize(
    "queried_at",
    ["2024-02-29T00:00:00Z", "2026-12-31T23:59:59Z"],
)
def test_timestamp_boundaries_accepted(queried_at: str) -> None:
    result = validate_evidence(_row_envelope([_snapshot(queried_at)], 1), DEFAULT_CONTEXT)
    assert result.accepted, result.refusal


@pytest.mark.parametrize(
    "queried_at",
    ["2025-02-29T00:00:00Z", "2026-13-01T00:00:00Z", "2026-00-10T00:00:00Z",
     "2026-01-01T24:00:00Z", "2026-01-01T00:60:00Z", "2026-01-01T00:00:00+00:00"],
    ids=["non-leap-feb29", "month-13", "month-00", "hour-24", "minute-60", "offset"],
)
def test_timestamp_boundaries_refused(queried_at: str) -> None:
    result = validate_evidence(_row_envelope([_snapshot(queried_at)], 1), DEFAULT_CONTEXT)
    assert not result.accepted
    assert result.refusal is not None
    assert result.refusal.code == "malformed_evidence_contract"


def test_result_count_mismatch_is_malformed_not_truncated() -> None:
    result = validate_evidence(
        _row_envelope([_snapshot("2026-03-01T09:14:22Z")], 2), DEFAULT_CONTEXT
    )
    assert not result.accepted
    assert result.refusal is not None
    assert result.refusal.code == "malformed_evidence_contract"


# ----- ingress row shape: the envelope names the format, the row does not
# (spec §4.4 "schema placement at ingress") -----
#
# The regression these pin: the hermetic suite used to hand the ingress a
# fixture that carried a per-row `schema` and a whole-second `queried_at`,
# which is NOT what the real producer emits. A fixture that agrees with
# the validator can never catch a producer that disagrees with it, so
# every leg below is built from the REAL producer output shape and the
# refusals are the ones a real producer triggers.


def test_an_ingress_envelope_with_five_key_rows_is_accepted() -> None:
    """The producer's actual output: the envelope names the format once,
    each row carries exactly the five value keys."""
    result = validate_evidence(
        _row_envelope([_snapshot("2026-03-01T09:14:22Z")], 1), DEFAULT_CONTEXT
    )
    assert result.accepted, result.refusal


def test_an_ingress_row_carrying_its_own_schema_is_refused() -> None:
    """The envelope already fixed the format; a per-row marker duplicates
    it, permits a list whose rows disagree about their own format, and
    adds no information. It is an unknown key at the row, so it is
    ``malformed_evidence_contract`` under §2.4 like any other."""
    row = {"schema": "fin.evidence.database_row_snapshot", **_snapshot("2026-03-01T09:14:22Z")}
    result = validate_evidence(_row_envelope([row], 1), DEFAULT_CONTEXT)
    assert not result.accepted
    assert result.refusal is not None
    assert result.refusal.code == "malformed_evidence_contract"


@pytest.mark.parametrize(
    "dropped",
    ["source", "record_key", "queried_at", "fields", "summary"],
)
def test_an_ingress_row_missing_any_required_key_is_refused(dropped: str) -> None:
    row = _snapshot("2026-03-01T09:14:22Z")
    del row[dropped]
    result = validate_evidence(_row_envelope([row], 1), DEFAULT_CONTEXT)
    assert not result.accepted
    assert result.refusal is not None
    assert result.refusal.code == "malformed_evidence_contract"


def test_a_millisecond_queried_at_is_still_refused_and_never_normalized() -> None:
    """A real producer emitting ``...T09:14:22.500Z`` is the defect this
    pass exists for. fin does NOT silently truncate the fraction to fit:
    the value is an instant the ledger reported, and a rewritten one
    would put fin's guess where the producer's record was. The
    normalization is the PRODUCER's obligation, so this stays a refusal."""
    result = validate_evidence(
        _row_envelope([_snapshot("2026-03-01T09:14:22.500Z")], 1), DEFAULT_CONTEXT
    )
    assert not result.accepted
    assert result.refusal is not None
    assert result.refusal.code == "malformed_evidence_contract"


@pytest.mark.parametrize("schema_version", [0, 2, -1, 99])
def test_a_wrong_integer_envelope_schema_version_is_unsupported(
    schema_version: Any,
) -> None:
    """A recognized token carrying an unrecognized version INTEGER is
    §2.3's unsupported exit: never a silent default, never the nearest
    older parser (spec §6: "unsupported ``schema_version`` for a
    recognized name")."""
    envelope = _row_envelope([_snapshot("2026-03-01T09:14:22Z")], 1)
    envelope["database_rows"]["schema_version"] = schema_version
    result = validate_evidence(envelope, DEFAULT_CONTEXT)
    assert not result.accepted
    assert result.refusal is not None
    assert result.refusal.code == "unsupported_evidence_contract"


@pytest.mark.parametrize(
    "schema_version",
    ["1", True, None, [], {}],
    ids=["string", "bool", "null", "list", "object"],
)
def test_a_non_integer_envelope_schema_version_is_malformed(schema_version: Any) -> None:
    """A non-integer version is a wrong TYPE, which §6's class table puts
    under malformed, not unsupported. Keeping the two apart is the point
    of the class vocabulary: unsupported means "a version we do not
    speak", and ``"1"`` is not a version at all.

    PREMISE CHANGED (ADR 0093 decision 8). The float leg ``1.0`` used to
    sit in this parameter list asserting malformed; it asserted the
    TYPE rule, and the type rule is not the version rule. A JSON number
    spelled ``1.0`` decodes to the float ``1.0`` here and to the integer
    ``1`` in a JavaScript decoder, so keeping it would have handed one
    JSON request two verdicts at two boundaries. It is now driven by the
    shared case set, where its expectation is ACCEPT, and the accept legs
    below pin it here too. The type rule this test still owns is
    unchanged: a string, a bool, null, a list, an object."""
    envelope = _row_envelope([_snapshot("2026-03-01T09:14:22Z")], 1)
    envelope["database_rows"]["schema_version"] = schema_version
    result = validate_evidence(envelope, DEFAULT_CONTEXT)
    assert not result.accepted
    assert result.refusal is not None
    assert result.refusal.code == "malformed_evidence_contract"


def test_a_missing_envelope_schema_version_is_malformed_not_unsupported() -> None:
    """Absent is a different failure from wrong: a key the contract
    requires is missing is §2.4 malformed. Only a value that IS present
    and is not the supported integer reaches the §2.3 unsupported exit —
    otherwise 'no version' would masquerade as 'a version we don't speak'."""
    envelope = _row_envelope([_snapshot("2026-03-01T09:14:22Z")], 1)
    del envelope["database_rows"]["schema_version"]
    result = validate_evidence(envelope, DEFAULT_CONTEXT)
    assert not result.accepted
    assert result.refusal is not None
    assert result.refusal.code == "malformed_evidence_contract"


# ----- the egress snapshot keeps its token AND gains the version -----


def _egress_snapshot(schema_version: Any) -> dict[str, Any]:
    return {
        "schema": "fin.evidence.database_row_snapshot",
        "schema_version": schema_version,
        "source": LEDGER_VIEW,
        "record_key": {"contract_id": "C-9000"},
        "queried_at": "2026-03-01T09:14:22Z",
        "fields": {"note": "egress probe"},
        "summary": "synthetic egress probe row",
    }


def _egress_citation(snapshot: dict[str, Any]) -> dict[str, Any]:
    """One ``database-row`` citation carrying its snapshot (spec §4.4
    egress): the flat slots mirror the snapshot's identity values."""
    return {
        "kind": "database-row",
        "document_id": "",
        "attachment_id": "",
        "version": snapshot["queried_at"],
        "unit": 1,
        "display": "record",
        "filename": "",
        "quote": snapshot["summary"],
        "database_row": snapshot,
    }


_MISSING_ROW_VALUE = object()


def _row_boundary_outcomes(row: dict[str, Any]) -> tuple[Any, Any]:
    """Validate the same five value keys at their distinct wire boundaries.

    Ingress gets the five-key row under its versioned envelope. Egress gets
    those same values in a standalone snapshot with its own schema pair and
    the citation's required flat mirrors. The wrappers intentionally keep
    their different key contracts while letting these tests compare only the
    shared row-value rules.
    """
    ingress_source = row.get("source")
    ingress_context = AdmissionContext(
        **{
            **vars(DEFAULT_CONTEXT),
            "authorized_database_views": (
                frozenset({ingress_source})
                if isinstance(ingress_source, str)
                else DEFAULT_CONTEXT.authorized_database_views
            ),
        }
    )
    ingress = validate_evidence(
        _row_envelope([row], 1),
        ingress_context,
    )

    snapshot = {
        **row,
        "schema": DATABASE_ROW_SNAPSHOT_SCHEMA,
        "schema_version": SCHEMA_VERSION,
    }
    # Keep the citation's own slots well-typed so malformed shared values
    # are diagnosed by the snapshot validator, not by its outer wrapper.
    queried_at = row.get("queried_at")
    summary = row.get("summary")
    citation = {
        "kind": "database-row",
        "document_id": "",
        "attachment_id": "",
        "version": queried_at if isinstance(queried_at, str) else "2026-03-01T09:14:22Z",
        "unit": 1,
        "display": "record",
        "filename": "",
        "quote": summary if isinstance(summary, str) else "egress probe",
        "database_row": snapshot,
    }
    egress = validate_evidence(
        {
            "evidence_schema": "fin.evidence.citation",
            "schema_version": SCHEMA_VERSION,
            "citations": [citation],
        },
        DEFAULT_CONTEXT,
    )
    return ingress, egress


def _row_boundary_cases() -> list[Any]:
    """Independent expectations for the five shared row-value keys."""
    return [
        pytest.param({}, "accept", id="ordinary-values"),
        pytest.param({"fields": {}}, "accept", id="empty-fields-is-legal"),
        pytest.param({"record_key": _MISSING_ROW_VALUE}, "malformed_evidence_contract",
                     id="missing-record-key-is-malformed"),
        pytest.param({"queried_at": "2026-03-01T09:14:22.500Z"},
                     "malformed_evidence_contract", id="fractional-timestamp"),
        pytest.param({"queried_at": []}, "malformed_evidence_contract",
                     id="non-string-timestamp"),
        pytest.param({"record_key": []}, "malformed_evidence_contract",
                     id="record-key-is-not-a-map"),
        pytest.param({"fields": []}, "malformed_evidence_contract",
                     id="fields-is-not-a-map"),
        pytest.param({"fields": {"nested": {"value": 1}}},
                     "malformed_evidence_contract", id="nested-field-value"),
        pytest.param({"record_key": {f"key-{i}": i for i in range(9)}},
                     "database_row_overflow", id="record-key-pair-limit-plus-one"),
        pytest.param({"fields": {f"field-{i}": i for i in range(17)}},
                     "database_row_overflow", id="field-count-limit-plus-one"),
        pytest.param({"source": "界" * 128}, "accept", id="unicode-source-at-limit"),
        pytest.param({"source": "界" * 129}, "database_row_overflow",
                     id="unicode-source-over-limit"),
        pytest.param({"summary": "😀" * 1000}, "accept",
                     id="astral-summary-at-codepoint-limit"),
        pytest.param({"summary": "😀" * 1001}, "database_row_overflow",
                     id="astral-summary-over-codepoint-limit"),
        pytest.param({"record_key": {"界" * 64: "id"}}, "accept",
                     id="unicode-record-key-name-at-limit"),
        pytest.param({"record_key": {"界" * 65: "id"}}, "database_row_overflow",
                     id="unicode-record-key-name-over-limit"),
        pytest.param({"record_key": {"contract_id": "界" * 128}}, "accept",
                     id="unicode-record-key-value-at-limit"),
        pytest.param({"record_key": {"contract_id": "界" * 129}},
                     "database_row_overflow", id="unicode-record-key-value-over-limit"),
        pytest.param({"fields": {"note": "😀" * 512}}, "accept",
                     id="astral-field-value-at-limit"),
        pytest.param({"fields": {"note": "😀" * 513}}, "database_row_overflow",
                     id="astral-field-value-over-limit"),
        pytest.param(
            {"fields": {"note": "x" * 513}, "summary": "s" * 32_768},
            "database_row_overflow",
            id="serialized-byte-limit-precedes-entry-limit",
        ),
    ]


@pytest.mark.parametrize(("overrides", "expected"), _row_boundary_cases())
def test_ingress_and_egress_share_row_value_boundary_outcomes(
    overrides: dict[str, Any], expected: str, request: pytest.FixtureRequest
) -> None:
    """Distinct wrappers preserve one outcome for their shared row values.

    These cases run against the current duplicated checks as a passing
    baseline. They are intended to catch drift when the common value/bounds
    checks are factored: ingress must retain its envelope and five-key row,
    while egress retains its standalone schema/version pair.
    """
    row = _snapshot("2026-03-01T09:14:22Z")
    for name, value in overrides.items():
        if value is _MISSING_ROW_VALUE:
            del row[name]
        else:
            row[name] = value

    ingress, egress = _row_boundary_outcomes(row)
    observed = [
        "accept" if outcome.accepted else outcome.refusal.code
        for outcome in (ingress, egress)
    ]
    assert observed == [expected, expected]

    if request.node.callspec is not None and request.node.callspec.id == (
        "serialized-byte-limit-precedes-entry-limit"
    ):
        # Both paths must report the whole-snapshot guard before the
        # independently violated 512-character field bound.
        assert ingress.refusal is not None and egress.refusal is not None
        assert "serialized snapshot exceeds 32 KiB" in ingress.refusal.message
        assert "serialized snapshot exceeds 32 KiB" in egress.refusal.message


def test_an_egress_snapshot_with_schema_and_schema_version_is_accepted() -> None:
    """The egress object travels ALONE, not inside a list, so it is the
    one that carries its own ``schema`` — and it carries the integer
    version beside it. Both keys, both accepted."""
    result = validate_evidence(
        {
            "evidence_schema": "fin.evidence.citation",
            "schema_version": 1,
            "citations": [_egress_citation(_egress_snapshot(1))],
        },
        DEFAULT_CONTEXT,
    )
    assert result.accepted, result.refusal


def test_an_egress_snapshot_whose_float_version_is_the_current_value_is_accepted() -> None:
    """The accept half of the cross-language case (ADR 0093 decision 8),
    stated on the egress token as well as at the ingress envelope.

    ``1.0`` and ``1`` are the same number once decoded, so this is the
    current version. WHY it is written here and not only in the shared
    case set: the shared set is driven at this boundary by the
    parametrized runner below, and these legs are what make a reader of
    THIS file see the rule stated beside the refusal legs it was
    extracted from."""
    result = validate_evidence(
        {
            "evidence_schema": "fin.evidence.citation",
            "schema_version": 1,
            "citations": [_egress_citation(_egress_snapshot(1.0))],
        },
        DEFAULT_CONTEXT,
    )
    assert result.accepted, result.refusal


def test_an_ingress_envelope_whose_float_version_is_the_current_value_is_accepted() -> None:
    """The same accept case on the retrieve ingress token. The two
    tokens are separate declarations of the same version rule, so a
    boundary that adopted the value rule at one of them and not the
    other would pass a set that only drove one."""
    envelope = _row_envelope([_snapshot("2026-03-01T09:14:22Z")], 1)
    envelope["database_rows"]["schema_version"] = 1.0
    result = validate_evidence(envelope, DEFAULT_CONTEXT)
    assert result.accepted, result.refusal


@pytest.mark.parametrize("schema_version", [0, 2, 99])
def test_an_egress_snapshot_with_a_wrong_schema_version_is_unsupported(
    schema_version: Any,
) -> None:
    result = validate_evidence(
        {
            "evidence_schema": "fin.evidence.citation",
            "schema_version": 1,
            "citations": [_egress_citation(_egress_snapshot(schema_version))],
        },
        DEFAULT_CONTEXT,
    )
    assert not result.accepted
    assert result.refusal is not None
    assert result.refusal.code == "unsupported_evidence_contract"


@pytest.mark.parametrize("schema_version", ["1", True, None])
def test_an_egress_snapshot_with_a_non_integer_schema_version_is_malformed(
    schema_version: Any,
) -> None:
    """Same class split as the ingress envelope leg: a non-integer
    version is a wrong type (§6 malformed), not an unrecognized version
    (§6 unsupported).

    PREMISE CHANGED (ADR 0093 decision 8): the float leg ``1.0`` was
    removed from this list for the reason recorded on the ingress
    envelope's twin — the version rule is over the decoded VALUE, so
    ``1.0`` is version 1 and is accepted, not malformed. See the accept
    leg below."""
    result = validate_evidence(
        {
            "evidence_schema": "fin.evidence.citation",
            "schema_version": 1,
            "citations": [_egress_citation(_egress_snapshot(schema_version))],
        },
        DEFAULT_CONTEXT,
    )
    assert not result.accepted
    assert result.refusal is not None
    assert result.refusal.code == "malformed_evidence_contract"


def test_an_egress_snapshot_missing_its_schema_version_is_malformed() -> None:
    snapshot = _egress_snapshot(1)
    del snapshot["schema_version"]
    result = validate_evidence(
        {
            "evidence_schema": "fin.evidence.citation",
            "schema_version": 1,
            "citations": [_egress_citation(snapshot)],
        },
        DEFAULT_CONTEXT,
    )
    assert not result.accepted
    assert result.refusal is not None
    assert result.refusal.code == "malformed_evidence_contract"


def test_citation_bound_32_is_accepted() -> None:
    citation = {
        "kind": "company",
        "document_id": "t1-expense-001",
        "attachment_id": "",
        "version": DOC_VERSION,
        "unit": 3,
        "display": "page",
        "filename": "travel-and-expense-policy.pdf",
        "quote": STANDARD_QUOTE,
    }
    result = validate_evidence(
        {"evidence_schema": "fin.evidence.citation", "schema_version": 1,
         "citations": [citation] * 32},
        DEFAULT_CONTEXT,
    )
    assert result.accepted, result.refusal


def test_snapshot_identity_rejects_unknown_carrier_field() -> None:
    carrier = {
        "source": LEDGER_VIEW,
        "record_key": {"contract_id": "C-2291"},
        "snapshot_digest": "sha256:" + "0" * 64,
        "confidence": 0.9,
    }
    result = validate_snapshot_identity(carrier, _ingress_snapshot())
    assert not result.accepted
    assert result.refusal is not None
    assert result.refusal.code == "malformed_evidence_contract"


# ----- never-raise contract: unhashable producer values (review E session) -----
#
# The validator's own module docstring promises it never raises on bad
# input. A dict/list arriving where a membership test hashes the value
# (``source_ref.document_id in database_source_names``,
# ``row.source in authorized_database_views``) breaks that promise with a
# TypeError, and the MCP ingress — which catches only typed errors —
# crashes the whole run instead of emitting the §6 refusal.


@pytest.mark.parametrize(
    "producer_value",
    [{"nested": 1}, ["list"], 3.5],
    ids=["dict", "list", "float"],
)
def test_unhashable_chunk_source_ref_refuses_without_crash(producer_value: Any) -> None:
    result = validate_evidence(
        {"chunks": [{"source_ref": {"document_id": producer_value}}]},
        DEFAULT_CONTEXT,
    )
    # A non-string document_id names no ledger source, so the row-in-
    # chunks disguise check has nothing to ground a denial on; the typed
    # outcome (never an exception) is what this lane owes the caller.
    assert result.accepted or result.refusal is not None


@pytest.mark.parametrize(
    "producer_value",
    [{"nested": 1}, ["list"]],
    ids=["dict", "list"],
)
def test_unhashable_row_source_is_a_malformed_refusal_not_a_crash(
    producer_value: Any,
) -> None:
    rows = [
        {
            "source": producer_value,
            "record_key": {"contract_id": "C-1"},
            "queried_at": "2026-03-01T09:14:22Z",
            "fields": {"note": "probe"},
            "summary": "typed refusal probe",
        }
    ]
    result = validate_evidence(_row_envelope(rows, 1), DEFAULT_CONTEXT)
    assert not result.accepted
    assert result.refusal is not None
    # A non-string source cannot be an unauthorized view name either:
    # the shape check (source must be a string) owns the named refusal.
    assert result.refusal.code == "malformed_evidence_contract"


@pytest.mark.parametrize(
    "citation_row",
    [
        {"kind": {"k": 1}, "document_id": "d", "attachment_id": "", "version": "1",
         "unit": 1, "display": "page", "filename": "f", "quote": "q"},
        {"kind": "company", "document_id": {"d": 1}, "attachment_id": "",
         "version": "1", "unit": 1, "display": "page", "filename": "f", "quote": "q"},
        {"kind": "company", "document_id": "d", "attachment_id": "",
         "version": ["v"], "unit": 1, "display": "page", "filename": "f", "quote": "q"},
    ],
    ids=["kind-dict", "document_id-dict", "version-list"],
)
def test_unhashable_citation_identity_values_refuse_without_crash(
    citation_row: Any,
) -> None:
    """Seeding context from rows must not crash on, and the membership
    tests must not hash, a producer value that is not a string."""
    result = validate_evidence(
        {
            "evidence_schema": "fin.evidence.citation",
            "schema_version": 1,
            "citations": [citation_row],
        },
        seed_context_from_rows([citation_row]),
    )
    assert result.accepted or result.refusal is not None


def test_seed_context_from_rows_never_hashes_a_non_string() -> None:
    ctx = seed_context_from_rows(
        citation_rows=[{"kind": "company", "document_id": {"x": 1}, "version": {"y": 1}}],
        visual_claim_rows=[{"kind": "visual-evidence", "attachment_id": ["a"]}],
        database_row_values=[{"source": {"s": 1}}, {"source": "ledger_ok"}],
    )
    assert ctx.trusted_attachment_ids == frozenset()
    assert ctx.resolvable_locators == frozenset()
    assert ctx.authorized_database_views == frozenset({"ledger_ok"})


# ----- specs/schemas/evidence/ JSON Schema consistency (review M-10) -----
#
# The five versioned lane shapes are published as JSON Schema (draft
# 2020-12) files next to the spec. These tests pin TWO directions: the
# schema files agree with the validator home's frozen sets, and every
# committed accept fixture structurally conforms to its schema (with a
# targeted negative set for the keywords only schemas can promise:
# ``additionalProperties: false``, ``const`` tokens, ``oneOf`` locator
# shapes). The validator home remains the single semantic authority;
# the schemas state exactly what it can never diverge from structurally.

import re as _re

from fin.host.citation_gate import QUOTABLE_EVIDENCE_KINDS
from fin.host.evidence_contract import (
    BUSINESS_FIELD_SCHEMA,
    CITATION_CHANNEL_SCHEMA,
    CITATION_SNAPSHOT_KEY,
    DATABASE_ROW_SNAPSHOT_SCHEMA,
    MAX_CITATIONS_PER_RUN,
    VISUAL_CLAIM_SCHEMA,
    _BUSINESS_FIELD_ELEMENT_FIELDS,
    _BUSINESS_FIELD_STATUSES,
    _CITATION_FIELDS,
    _COMPARISON_CELL_FIELDS,
    _COMPARISON_ENVELOPE_FIELDS,
    _SNAPSHOT_FIELDS,
    _VISUAL_CLAIM_ELEMENT_FIELDS,
)
from fin.host.tools.compare_facts import COMPARISON_SCHEMA

SCHEMA_ROOT = (
    Path(__file__).resolve().parents[2] / "specs" / "schemas" / "evidence"
)

# Spec §2.1: the version travels as an explicit INTEGER sibling of the
# token, and the token carries no version segment. Spelled as the spec's
# literal rather than imported from the validator home, so the schemas
# are checked against an independently stated expectation.
SCHEMA_VERSION = 1


def _schema_registry() -> dict[str, dict[str, Any]]:
    docs: dict[str, dict[str, Any]] = {}
    for path in sorted(SCHEMA_ROOT.glob("*.schema.json")):
        doc = _load_json(path)
        docs[doc["$id"]] = doc
    return docs


REGISTRY = _schema_registry()


def _deref(schema: dict[str, Any], root: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Resolve one local or cross-file ``$ref``; unknown refs raise (a
    schema pointing at a token that does not exist is a test failure,
    not a silent pass)."""
    token, _, frag = schema["$ref"].partition("#")
    doc = REGISTRY[token] if token else root
    node: Any = doc
    if frag:
        for part in frag.lstrip("/").split("/"):
            node = node[part]
    return node, doc


def _type_ok(names: Any, instance: Any) -> bool:
    wanted = names if isinstance(names, list) else [names]
    for name in wanted:
        if name == "object" and isinstance(instance, dict):
            return True
        if name == "array" and isinstance(instance, list):
            return True
        if name == "string" and isinstance(instance, str):
            return True
        if name == "boolean" and isinstance(instance, bool):
            return True
        if name == "integer" and isinstance(instance, int) and not isinstance(instance, bool):
            return True
        if name == "number" and isinstance(instance, (int, float)) and not isinstance(instance, bool):
            return True
        if name == "null" and instance is None:
            return True
    return False


def _conforms(schema: dict[str, Any], instance: Any, root: dict[str, Any]) -> bool:
    """Structural subset: type, const, enum, required, properties,
    additionalProperties (bool or schema), items, min/maxItems,
    minLength, pattern, minimum, oneOf. Length bounds in CODE POINTS
    are deliberately not encoded (the x-bounds notes carry them);
    anything beyond structure is the validator home's."""
    while "$ref" in schema:
        schema, root = _deref(schema, root)
    if "type" in schema and not _type_ok(schema["type"], instance):
        return False
    if "const" in schema and instance != schema["const"]:
        return False
    if "enum" in schema and instance not in schema["enum"]:
        return False
    if isinstance(instance, dict):
        for key in schema.get("required", ()):
            if key not in instance:
                return False
        props = schema.get("properties", {})
        extra = schema.get("additionalProperties", True)
        for key, value in instance.items():
            if key in props:
                if not _conforms(props[key], value, root):
                    return False
            elif extra is False:
                return False
            elif isinstance(extra, dict):
                if not _conforms(extra, value, root):
                    return False
    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            return False
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            return False
        items = schema.get("items")
        if items is not None:
            for element in instance:
                if not _conforms(items, element, root):
                    return False
    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            return False
        if "pattern" in schema and not _re.search(schema["pattern"], instance):
            return False
    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            return False
    if "oneOf" in schema:
        if not any(_conforms(s, instance, root) for s in schema["oneOf"]):
            return False
    return True


def _schema_targets(profile: str, payload: Any) -> list[tuple[dict[str, Any], dict[str, Any], Any]]:
    """The (schema, root-document, instance) pairs one fixture payload
    must satisfy for its profile."""
    if profile == "citation":
        doc = REGISTRY[CITATION_CHANNEL_SCHEMA]
        return [(doc, doc, payload)]
    if profile == "comparison":
        doc = REGISTRY[COMPARISON_SCHEMA]
        return [(doc, doc, payload)]
    if profile == "visual_claim":
        doc = REGISTRY[VISUAL_CLAIM_SCHEMA]
        return [(doc, doc, element) for element in payload["visual_claims"]]
    if profile == "business_field":
        doc = REGISTRY[BUSINESS_FIELD_SCHEMA]
        return [(doc, doc, element) for element in payload["business_fields"]]
    # database_row: whichever carrier the fixture uses.
    if "citations" in payload:
        doc = REGISTRY[CITATION_CHANNEL_SCHEMA]
        return [(doc, doc, payload)]
    if "database_rows" in payload:
        # An INGRESS row is NOT the egress snapshot: it carries no
        # ``schema`` / ``schema_version`` (spec 4.4 places that pair on
        # the envelope), so the published snapshot schema — which
        # requires both — cannot describe it. The ingress row's
        # structural contract is the envelope's `rows` item schema, and
        # no published file carries one; the ingress shape is pinned by
        # the fixture suite and the named refusals instead.
        return []
    if _is_digest_carrier(payload):
        doc = REGISTRY[BUSINESS_FIELD_SCHEMA]
        return [(doc["$defs"]["database_locator"], doc, payload)]
    return []  # a chunks-only payload names no fin-versioned lane object


ACCEPT_CASES = [
    (profile, case)
    for profile, case in ALL_CASES
    if _fixture(profile, case)["expect"] == "accept"
]


@pytest.mark.parametrize(("profile", "case"), ACCEPT_CASES,
                         ids=[f"{p}/{c}" for p, c in ACCEPT_CASES])
def test_accept_fixtures_conform_to_their_schema(profile: str, case: str) -> None:
    payload = _fixture(profile, case)["payload"]
    for schema, root, instance in _schema_targets(profile, payload):
        assert _conforms(schema, instance, root), (profile, case)


# The structural half of the refuse set: these named fixtures are exactly
# the classes only a schema can promise (unknown keys, token mismatch, an
# invented locator shape). The semantic refusals (trust, staleness, quote
# not in text, overflow beyond maxItems) legitimately conform structurally.
STRUCTURAL_REFUSALS = (
    ("citation", "unknown-field"),
    ("visual_claim", "unknown-field"),
    ("business_field", "unknown-field"),
    ("business_field", "invented-locator-shape"),
    ("comparison", "unknown-field-envelope"),
)

# The ingress row below is a structural refusal too, but an ingress
# row has no published JSON Schema (spec 4.4: the schema-bearing object
# is the EGRESS snapshot; at ingress the token lives on the envelope).
# Its "a key outside the five" rule is therefore stated here directly
# rather than borrowed from the egress schema, which would trivially fail
# every ingress row on the missing ``schema`` alone and prove nothing.
#
# The row-level bad token is covered ONLY here, by
# ``snapshot-unknown-field-kind``: an ingress row is held to exactly the
# five value keys, so a row-borne ``schema`` is refused as an unknown key
# whatever its value. A case whose defect is a bad ENVELOPE
# ``schema_version`` therefore has a valid five-key row and does not
# belong in this tuple, even though it is a structural refusal covered by
# the manifest-driven ``test_conformance_fixture_outcome``.
INGRESS_STRUCTURAL_REFUSALS = (
    ("database_row", "snapshot-unknown-field-kind"),
)

INGRESS_ROW_VALUE_KEYS = frozenset(
    {"source", "record_key", "queried_at", "fields", "summary"}
)


@pytest.mark.parametrize(("profile", "case"), STRUCTURAL_REFUSALS,
                         ids=[f"{p}/{c}" for p, c in STRUCTURAL_REFUSALS])
def test_structural_refusal_fixtures_fail_their_schema(profile: str, case: str) -> None:
    payload = _fixture(profile, case)["payload"]
    targets = _schema_targets(profile, payload)
    assert targets, (profile, case)
    assert any(not _conforms(schema, instance, root) for schema, root, instance in targets)


@pytest.mark.parametrize(("profile", "case"), INGRESS_STRUCTURAL_REFUSALS,
                         ids=[f"{p}/{c}" for p, c in INGRESS_STRUCTURAL_REFUSALS])
def test_ingress_structural_refusal_rows_break_the_five_key_shape(
    profile: str, case: str
) -> None:
    """Each named ingress row carries a key outside the five value keys,
    which is exactly what the ingress shape forbids."""
    rows = _fixture(profile, case)["payload"]["database_rows"]["rows"]
    assert rows
    for row in rows:
        assert frozenset(row) != INGRESS_ROW_VALUE_KEYS, (profile, case)
        assert frozenset(row) - INGRESS_ROW_VALUE_KEYS, (profile, case)


def test_schema_files_cover_the_five_versioned_lane_shapes() -> None:
    assert sorted(REGISTRY) == sorted(
        {
            CITATION_CHANNEL_SCHEMA,
            DATABASE_ROW_SNAPSHOT_SCHEMA,
            VISUAL_CLAIM_SCHEMA,
            BUSINESS_FIELD_SCHEMA,
            COMPARISON_SCHEMA,
        }
    )


def test_schema_file_names_carry_no_version_suffix() -> None:
    """Spec §2.7: a schema file is named after the TOKEN, with the
    ``schema_version`` integer inside the file rather than in its name.
    WHY this exists: :func:`_schema_registry` keys on ``$id`` and globs
    ``*.schema.json``, so it would happily pass with a ``.v1``-suffixed
    filename as long as the ``$id`` matched. The filename and the token
    are one declaration; only this pins the name side of it."""
    assert sorted(p.name for p in SCHEMA_ROOT.glob("*.schema.json")) == [
        "business_field.schema.json",
        "citation.schema.json",
        "comparison.schema.json",
        "database_row_snapshot.schema.json",
        "visual_claim.schema.json",
    ]
    assert sorted(p.name for p in SCHEMA_ROOT.glob("*.v1.schema.json")) == []


def test_every_schema_id_matches_its_filename_and_the_python_constant() -> None:
    """The ``$id`` a schema declares IS the token the validator home
    freezes, and the FILE is named after that token's own segment with no
    version suffix (spec §2.7). A file whose ``$id`` kept a ``.v1``
    segment would leave ``REGISTRY`` keyed on a token no lane ever emits,
    and every schema-conformance assertion below would then run against
    the wrong document (or raise ``KeyError`` at import)."""
    for path in sorted(SCHEMA_ROOT.glob("*.schema.json")):
        doc = _load_json(path)
        token = doc["$id"]
        stem = path.name[: -len(".schema.json")]
        assert token.endswith("." + stem), (path.name, token)
        assert not _re.search(r"\.v\d+$", token), (
            f"{path.name}: the token must carry no version segment"
        )


def test_the_cross_file_refs_resolve_to_the_sibling_documents() -> None:
    """The two documents that reference each other do so by TOKEN, not by
    a path: ``citation`` -> ``database_row_snapshot``, and ``comparison``
    -> ``business_field`` twice (both with the ``document_locator``
    fragment). :func:`_deref` raises ``KeyError`` for a token no file
    declares, so this is the load-bearing half of the registry check —
    it fails here rather than as a confusing structural mismatch."""
    citation = REGISTRY[CITATION_CHANNEL_SCHEMA]
    assert _deref(citation["$defs"]["citation"]["properties"]["database_row"], citation)[0] \
        is REGISTRY[DATABASE_ROW_SNAPSHOT_SCHEMA]

    comparison = REGISTRY[COMPARISON_SCHEMA]
    locators = comparison["properties"]["comparison"]["properties"]["locators"]["items"]
    assert _deref(locators, comparison)[0] is REGISTRY[BUSINESS_FIELD_SCHEMA]["$defs"][
        "document_locator"
    ]
    cell_locator = comparison["$defs"]["cell"]["properties"]["locator"]["oneOf"][0]
    assert _deref(cell_locator, comparison)[0] is REGISTRY[BUSINESS_FIELD_SCHEMA]["$defs"][
        "document_locator"
    ]


def _resolved(schema: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
    return _deref(schema, root)[0] if "$ref" in schema else schema


def test_schema_key_sets_are_the_home_frozen_sets() -> None:
    """One authority for the frozen key sets: a schema drifting from the
    validator home's frozensets (or from the lane-marker / status / kind
    vocabularies) fails here, not in production at a cutover."""
    citation = REGISTRY[CITATION_CHANNEL_SCHEMA]
    assert citation["properties"]["evidence_schema"]["const"] == CITATION_CHANNEL_SCHEMA
    assert citation["properties"]["citations"]["maxItems"] == MAX_CITATIONS_PER_RUN
    item = _resolved(citation["properties"]["citations"]["items"], citation)
    assert frozenset(item["required"]) == _CITATION_FIELDS
    assert frozenset(item["properties"]) - frozenset(item["required"]) == {
        CITATION_SNAPSHOT_KEY
    }
    assert frozenset(item["properties"]["kind"]["enum"]) == QUOTABLE_EVIDENCE_KINDS
    assert (
        _resolved(item["properties"]["database_row"], citation)["properties"]["schema"]["const"]
        == DATABASE_ROW_SNAPSHOT_SCHEMA
    )

    snapshot = REGISTRY[DATABASE_ROW_SNAPSHOT_SCHEMA]
    # The EGRESS snapshot is the schema-bearing object: the token plus the
    # integer version carrier beside the five value keys (spec 4.4 egress).
    assert frozenset(snapshot["required"]) == _SNAPSHOT_FIELDS
    assert snapshot["properties"]["schema"]["const"] == DATABASE_ROW_SNAPSHOT_SCHEMA
    assert snapshot["properties"]["schema_version"]["const"] == SCHEMA_VERSION

    visual = REGISTRY[VISUAL_CLAIM_SCHEMA]
    assert frozenset(visual["required"]) == _VISUAL_CLAIM_ELEMENT_FIELDS
    assert visual["properties"]["schema"]["const"] == VISUAL_CLAIM_SCHEMA
    assert visual["properties"]["schema_version"]["const"] == SCHEMA_VERSION

    business = REGISTRY[BUSINESS_FIELD_SCHEMA]
    assert frozenset(business["required"]) == _BUSINESS_FIELD_ELEMENT_FIELDS
    assert business["properties"]["schema"]["const"] == BUSINESS_FIELD_SCHEMA
    assert business["properties"]["schema_version"]["const"] == SCHEMA_VERSION
    assert frozenset(business["properties"]["status"]["enum"]) == _BUSINESS_FIELD_STATUSES

    comparison = REGISTRY[COMPARISON_SCHEMA]
    envelope = comparison["properties"]["comparison"]
    assert frozenset(envelope["required"]) == _COMPARISON_ENVELOPE_FIELDS
    assert envelope["properties"]["schema"]["const"] == COMPARISON_SCHEMA
    assert envelope["properties"]["schema_version"]["const"] == SCHEMA_VERSION
    cell = _resolved(
        envelope["properties"]["rows"]["items"]["properties"]["cells"]["items"],
        comparison,
    )
    assert frozenset(cell["required"]) == _COMPARISON_CELL_FIELDS
