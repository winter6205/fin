"""``fin.host.source_scope`` — the versioned thread source-scope snapshot.

Cases derive expected values from ``specs/question-task-runtime-contract.md``
("Versioned source intent in the existing snapshot") and the scope-transition
table in ``specs/qa-chain-grounded-experience.md``, not from the
implementation:

* normal: unrestricted and restricted snapshots round-trip exactly;
* failure: mode/source-count/identity violations are typed codes, not
  silent reinterpretations; a recheck that cannot confirm identity, version
  or permission never widens to the whole corpus;
* boundary: 256-byte ids, the 200-source bound, negative and non-int
  ``accepted_input_version`` (bool included);
* the decision-4 regression for the snapshot era: a fresh unscoped input and
  pending candidates never broaden a restriction, a clear is an explicit key,
  and ``legacy_document_ids`` is a projection, never a second authority.
"""

from __future__ import annotations

from dataclasses import replace as dataclass_replace
from typing import Any

import pytest

from fin.host.source_scope import (
    MAX_SCOPE_FIELD_BYTES,
    MAX_SCOPE_SOURCES,
    SCOPE_SNAPSHOT_INPUT_INVALID,
    SCOPE_SNAPSHOT_KIND_UNKNOWN,
    SCOPE_SNAPSHOT_MODE_INVALID,
    SCOPE_SNAPSHOT_NOT_AN_OBJECT,
    SCOPE_SNAPSHOT_SOURCES_INVALID,
    SCOPE_SNAPSHOT_SCHEMA_UNSUPPORTED,
    SCOPE_SNAPSHOT_VERSION_INVALID,
    SOURCE_SCOPE_SCHEMA_VERSION,
    UNSET_SCOPED_INPUT,
    SourceIdentity,
    SourceScopeAuthority,
    SourceScopeSnapshot,
    ScopeInput,
    clear_scope,
    legacy_document_ids,
    replace_scope,
    recheck_scope,
    restricted_scope,
    scope_from_candidates,
    unrestricted_scope,
    validate_source_scope,
)
from fin.host.task.task_runtime_types import (
    TASK_APPLICABILITY_UNRESOLVED,
    TASK_DEPENDENCY_FAILURE,
    TASK_INPUT_INVALID,
    TASK_PERMISSION_DENIED,
    TaskRuntimeError,
)


def _identity(
    kind: str = "corpus", source_id: str = "doc-1", version: str = "3"
) -> SourceIdentity:
    return SourceIdentity(
        source_kind=kind,  # type: ignore[arg-type]
        source_id=source_id,
        version=version,
    )


def _restricted(
    *identities: SourceIdentity, accepted_input_version: int = 7
) -> SourceScopeSnapshot:
    return restricted_scope(
        identities or (_identity(),), accepted_input_version=accepted_input_version
    )


def _envelope(mode: str, sources: Any, accepted_input_version: Any = 1) -> dict[str, Any]:
    return {
        "schema_version": SOURCE_SCOPE_SCHEMA_VERSION,
        "mode": mode,
        "sources": sources,
        "accepted_input_version": accepted_input_version,
    }


def _invalid(raw: object) -> tuple[str, str]:
    """The (kind, scope code) raised for a malformed envelope."""
    with pytest.raises(TaskRuntimeError) as excinfo:
        SourceScopeSnapshot.from_state_dict(raw)
    return excinfo.value.kind, excinfo.value.detail["code"]


# ----- round-trip -----


def test_unrestricted_snapshot_round_trips() -> None:
    """The frozen wire shape survives to_state_dict/from_state_dict exactly."""
    snapshot = unrestricted_scope(accepted_input_version=5)
    raw = snapshot.to_state_dict()
    assert raw == {
        "schema_version": SOURCE_SCOPE_SCHEMA_VERSION,
        "mode": "unrestricted",
        "sources": [],
        "accepted_input_version": 5,
    }
    assert SourceScopeSnapshot.from_state_dict(raw) == snapshot


def test_restricted_snapshot_round_trips() -> None:
    """Corpus and attachment identities round-trip through the state dict."""
    snapshot = _restricted(
        _identity("corpus", "doc-1", "3"),
        _identity("attachment", "att-9", "11"),
        accepted_input_version=12,
    )
    raw = snapshot.to_state_dict()
    assert raw["schema_version"] == SOURCE_SCOPE_SCHEMA_VERSION
    assert raw["mode"] == "restricted"
    assert raw["sources"] == [
        {"source_kind": "corpus", "source_id": "doc-1", "version": "3"},
        {"source_kind": "attachment", "source_id": "att-9", "version": "11"},
    ]
    assert raw["accepted_input_version"] == 12
    assert SourceScopeSnapshot.from_state_dict(raw) == snapshot


def test_state_dict_is_exact_inverse_for_both_modes() -> None:
    """from_state_dict(to_state_dict(s)) == s for every valid snapshot."""
    snapshots = [
        unrestricted_scope(accepted_input_version=0),
        _restricted(_identity("corpus", "d", "1")),
        _restricted(
            _identity("attachment", "a", "2"), _identity("corpus", "c", "9")
        ),
    ]
    for snapshot in snapshots:
        assert (
            SourceScopeSnapshot.from_state_dict(snapshot.to_state_dict()) == snapshot
        )


# ----- mode / source-count validity -----


def test_unrestricted_with_sources_is_invalid() -> None:
    """``unrestricted`` requires ``sources == ()`` — never a silent narrowing."""
    kind, code = _invalid(
        _envelope(
            "unrestricted",
            [{"source_kind": "corpus", "source_id": "d", "version": "1"}],
        )
    )
    assert kind == TASK_INPUT_INVALID
    assert code == SCOPE_SNAPSHOT_MODE_INVALID
    # The same combination through an accepted replacement is refused too.
    with pytest.raises(TaskRuntimeError) as excinfo:
        replace_scope(
            unrestricted_scope(accepted_input_version=1),
            requested=ScopeInput(
                mode="unrestricted",
                sources=(_identity("corpus", "d", "1"),),
            ),
            accepted_input_version=2,
        )
    assert excinfo.value.kind == TASK_INPUT_INVALID


def test_restricted_with_zero_sources_is_invalid() -> None:
    """An empty restricted selection is task_input_invalid, never unrestricted."""
    kind, code = _invalid(_envelope("restricted", []))
    assert kind == TASK_INPUT_INVALID
    assert code == SCOPE_SNAPSHOT_SOURCES_INVALID
    with pytest.raises(TaskRuntimeError):
        restricted_scope((), accepted_input_version=1)


def test_restricted_with_201_sources_is_invalid() -> None:
    """The 200-distinct-identity bound holds; 201 is invalid."""
    sources = [
        {"source_kind": "corpus", "source_id": f"doc-{n}", "version": "1"}
        for n in range(MAX_SCOPE_SOURCES + 1)
    ]
    assert len(sources) == MAX_SCOPE_SOURCES + 1
    kind, code = _invalid(_envelope("restricted", sources))
    assert kind == TASK_INPUT_INVALID
    assert code == SCOPE_SNAPSHOT_SOURCES_INVALID


def test_duplicate_identity_in_restricted_scope_is_invalid() -> None:
    """The same (kind, id, version) twice is not 200 distinct identities."""
    entry = {"source_kind": "corpus", "source_id": "doc-1", "version": "3"}
    kind, code = _invalid(_envelope("restricted", [entry, dict(entry)]))
    assert kind == TASK_INPUT_INVALID
    assert code == SCOPE_SNAPSHOT_SOURCES_INVALID


# ----- identity field validity -----


@pytest.mark.parametrize("field", ["source_id", "version"])
def test_empty_identity_field_is_invalid(field: str) -> None:
    """A nonempty id/version is required; an empty string names no source."""
    raw = {"source_kind": "corpus", "source_id": "doc-1", "version": "3"}
    raw[field] = ""
    kind, code = _invalid(_envelope("restricted", [raw]))
    assert kind == TASK_INPUT_INVALID
    assert code == SCOPE_SNAPSHOT_INPUT_INVALID


@pytest.mark.parametrize("field", ["source_id", "version"])
def test_oversized_identity_field_is_invalid(field: str) -> None:
    """At most 256 UTF-8 bytes per id/version — 257 bytes is invalid."""
    raw = {"source_kind": "corpus", "source_id": "doc-1", "version": "3"}
    raw[field] = "字" * 100  # 300 bytes
    assert len(raw[field].encode("utf-8")) > MAX_SCOPE_FIELD_BYTES
    kind, code = _invalid(_envelope("restricted", [raw]))
    assert kind == TASK_INPUT_INVALID
    assert code == SCOPE_SNAPSHOT_INPUT_INVALID


def test_exactly_256_byte_identity_field_is_valid() -> None:
    """The bound is inclusive: 256 UTF-8 bytes is accepted."""
    value = "a" * 253 + "字"  # 253 + 3 = 256 bytes
    assert len(value.encode("utf-8")) == MAX_SCOPE_FIELD_BYTES
    snapshot = _restricted(_identity("corpus", value, "3"))
    assert snapshot.sources[0].source_id == value


@pytest.mark.parametrize("field", ["source_id", "version"])
@pytest.mark.parametrize("control", ["\n", "\t", "\x00", "\x1b"])
def test_control_character_in_identity_field_is_invalid(
    field: str, control: str
) -> None:
    """Control characters are rejected in identifiers, per the shared contract."""
    raw = {"source_kind": "corpus", "source_id": "doc-1", "version": "3"}
    raw[field] = f"bad{control}id"
    kind, code = _invalid(_envelope("restricted", [raw]))
    assert kind == TASK_INPUT_INVALID
    assert code == SCOPE_SNAPSHOT_INPUT_INVALID


@pytest.mark.parametrize("field", ["source_id", "version"])
def test_non_string_identity_field_is_invalid(field: str) -> None:
    """An id/version that is not a string names no source — never coerced."""
    raw = {"source_kind": "corpus", "source_id": "doc-1", "version": "3"}
    raw[field] = 3
    kind, code = _invalid(_envelope("restricted", [raw]))
    assert code == SCOPE_SNAPSHOT_INPUT_INVALID


# ----- kind / envelope validity -----


def test_unknown_source_kind_is_invalid() -> None:
    """``source_kind`` is a closed set; anything else is invalid."""
    kind, code = _invalid(
        _envelope(
            "restricted", [{"source_kind": "url", "source_id": "x", "version": "1"}]
        )
    )
    assert kind == TASK_INPUT_INVALID
    assert code == SCOPE_SNAPSHOT_KIND_UNKNOWN


def test_non_object_input_is_invalid() -> None:
    """A non-object envelope is invalid, never guessed into a snapshot."""
    for raw in (None, 7, "restricted", [], ("restricted",)):
        kind, code = _invalid(raw)
        assert kind == TASK_INPUT_INVALID
        assert code == SCOPE_SNAPSHOT_NOT_AN_OBJECT


def test_identity_from_state_dict_rejects_non_object() -> None:
    """``SourceIdentity.from_state_dict`` raises a typed error, never guesses."""
    with pytest.raises(TaskRuntimeError) as excinfo:
        SourceIdentity.from_state_dict("doc-1")
    assert excinfo.value.kind == TASK_INPUT_INVALID


def test_wrong_schema_version_is_invalid() -> None:
    """An envelope from another schema version is not silently reinterpreted."""
    kind, code = _invalid(
        {**_envelope("unrestricted", []), "schema_version": SOURCE_SCOPE_SCHEMA_VERSION + 1}
    )
    assert kind == TASK_INPUT_INVALID
    assert code == SCOPE_SNAPSHOT_SCHEMA_UNSUPPORTED


@pytest.mark.parametrize("bad", [-1, -(2**63) - 1, 2**63, 1.5, True, False, "3", None])
def test_invalid_accepted_input_version_is_invalid(bad: Any) -> None:
    """A nonnegative signed-64-bit counter; a bool is not an int here."""
    kind, code = _invalid(_envelope("unrestricted", [], bad))
    assert kind == TASK_INPUT_INVALID
    assert code == SCOPE_SNAPSHOT_VERSION_INVALID


def test_accepted_input_version_at_int64_boundaries() -> None:
    """0 and 2**63-1 are valid; the bounds are inclusive/exclusive exactly."""
    ok = SourceScopeSnapshot.from_state_dict(
        _envelope("unrestricted", [], 2**63 - 1)
    )
    assert ok.accepted_input_version == 2**63 - 1
    kind, code = _invalid(_envelope("unrestricted", [], 2**63))
    assert code == SCOPE_SNAPSHOT_VERSION_INVALID


# ----- validate_source_scope: codes, not exceptions -----


def test_validate_source_scope_returns_codes_without_raising() -> None:
    """The semantic validator answers a typed code and never raises."""
    valid = validate_source_scope(_envelope("unrestricted", [], 3))
    assert valid.snapshot == unrestricted_scope(accepted_input_version=3)
    assert valid.code is None
    invalid = validate_source_scope(_envelope("restricted", []))
    assert invalid.snapshot is None
    assert invalid.code == SCOPE_SNAPSHOT_SOURCES_INVALID
    assert invalid.kind == TASK_INPUT_INVALID


# ----- transitions -----


def test_explicit_clear_writes_unrestricted_and_is_distinguishable() -> None:
    """A clear is an explicit key: mode=unrestricted with sources=(), recorded
    as its own accepted intent, never an omitted key."""
    previous = _restricted(_identity("corpus", "doc-1", "3"))
    cleared = clear_scope(previous, accepted_input_version=21)
    assert cleared.mode == "unrestricted"
    assert cleared.sources == ()
    assert cleared.accepted_input_version == 21
    assert cleared.in_force is False
    assert cleared != previous


def test_fresh_unscoped_input_keeps_previous_restriction() -> None:
    """UNSET requested keeps the restriction — no silent widening — while the
    resulting accepted_input_version is the authoritative new one supplied by
    the caller, not assumed contiguous with the previous snapshot's."""
    previous = _restricted(
        _identity("corpus", "doc-1", "3"), accepted_input_version=4
    )
    carried = replace_scope(
        previous,
        requested=UNSET_SCOPED_INPUT,
        accepted_input_version=99,
    )
    assert carried == dataclass_replace(previous, accepted_input_version=99)
    assert carried.mode == "restricted"
    assert carried.sources == previous.sources
    assert carried.accepted_input_version == 99


def test_explicit_accepted_replacement_replaces_scope() -> None:
    """An explicitly accepted replacement is the only non-clear way to change
    the selection."""
    previous = _restricted(_identity("corpus", "doc-1", "3"))
    replacement = ScopeInput(
        mode="restricted",
        sources=(_identity("corpus", "doc-2", "5"),),
    )
    replaced = replace_scope(
        previous, requested=replacement, accepted_input_version=8
    )
    assert replaced.sources == (_identity("corpus", "doc-2", "5"),)
    assert replaced.accepted_input_version == 8


def test_pending_candidates_do_not_mutate_snapshot() -> None:
    """A pending-candidate list passed through unchanged leaves the previous
    snapshot identical — pending candidates are not durable user preferences."""
    previous = _restricted(_identity("corpus", "doc-1", "3"))
    candidates = (
        _identity("corpus", "doc-2", "5"),
        _identity("attachment", "att-1", "9"),
    )
    proposal = scope_from_candidates(candidates, accepted_input_version=10)
    # A proposal may be derived for Runtime to accept, but the stored snapshot
    # is not the proposal and nothing above mutated it.
    assert proposal == restricted_scope(candidates, accepted_input_version=10)
    assert previous == _restricted(
        _identity("corpus", "doc-1", "3"), accepted_input_version=7
    )
    # Carrying the pending state as UNSET keeps the previous restriction value.
    assert replace_scope(
        previous, requested=UNSET_SCOPED_INPUT, accepted_input_version=10
    ) == dataclass_replace(previous, accepted_input_version=10)


def test_scope_from_candidates_collapses_duplicates_and_empty_is_none() -> None:
    """Duplicate candidates collapse; no candidate means no proposal — never a
    silent unrestricted snapshot."""
    proposal = scope_from_candidates(
        (_identity("corpus", "doc-2", "5"), _identity("corpus", "doc-2", "5")),
        accepted_input_version=3,
    )
    assert proposal == restricted_scope(
        (_identity("corpus", "doc-2", "5"),), accepted_input_version=3
    )
    assert scope_from_candidates((), accepted_input_version=3) is None


# ----- legacy projection -----


def test_legacy_document_ids_returns_corpus_ids() -> None:
    """Corpus identities project to their source_id tuple, in order."""
    snapshot = _restricted(
        _identity("corpus", "doc-7", "1"),
        _identity("corpus", "doc-2", "4"),
    )
    assert legacy_document_ids(snapshot) == ("doc-7", "doc-2")


def test_legacy_document_ids_attachment_only_is_empty() -> None:
    """An attachment-only restriction cannot be expressed as document_ids."""
    snapshot = _restricted(
        _identity("attachment", "att-1", "9"),
        _identity("attachment", "att-2", "2"),
    )
    assert legacy_document_ids(snapshot) == ()


def test_empty_legacy_projection_is_not_unrestricted() -> None:
    """document_ids == () is never read as unrestricted while a snapshot is
    present — the projection is not a second authority."""
    restricted = _restricted(_identity("attachment", "att-1", "9"))
    unrestricted = unrestricted_scope(accepted_input_version=1)
    assert legacy_document_ids(restricted) == ()
    assert legacy_document_ids(unrestricted) == ()
    # The snapshot itself, not the projection, carries the decision.
    assert restricted.in_force is True
    assert unrestricted.in_force is False


# ----- recheck -----


class _TableAuthority:
    """A ``SourceScopeAuthority`` over an in-memory table (test seam).

    ``versions`` maps (kind, id) → current version string; an absent entry
    means no authority stands behind the identity. ``denied`` lists
    identities the caller is not permitted to use even though they exist.
    ``raises`` lists ids whose check faults, modeling an authority that
    cannot answer at all.
    """

    def __init__(
        self,
        versions: dict[tuple[str, str], str],
        denied: frozenset[tuple[str, str]] = frozenset(),
        raises: frozenset[str] = frozenset(),
    ) -> None:
        self._versions = versions
        self._denied = denied
        self._raises = raises

    def identity_exists(self, source: SourceIdentity) -> bool:
        if source.source_id in self._raises:
            raise RuntimeError("authority unavailable")
        return (source.source_kind, source.source_id) in self._versions

    def version_current(self, source: SourceIdentity) -> bool:
        return (
            self._versions.get((source.source_kind, source.source_id))
            == source.version
        )

    def caller_permitted(self, source: SourceIdentity) -> bool:
        return (source.source_kind, source.source_id) not in self._denied


def test_recheck_identity_gone_is_applicability_unresolved() -> None:
    """A source no authority stands behind is unresolved, never widened."""
    snapshot = _restricted(
        _identity("corpus", "doc-1", "3"), _identity("corpus", "gone", "1")
    )
    result = recheck_scope(
        snapshot, authority=_TableAuthority({("corpus", "doc-1"): "3"})
    )
    assert result.kind == TASK_APPLICABILITY_UNRESOLVED
    assert result.snapshot is None
    assert result.in_force is False


def test_recheck_version_stale_is_applicability_unresolved() -> None:
    """A stale saved version is unresolved, not silently current."""
    snapshot = _restricted(_identity("corpus", "doc-1", "3"))
    result = recheck_scope(
        snapshot, authority=_TableAuthority({("corpus", "doc-1"): "4"})
    )
    assert result.kind == TASK_APPLICABILITY_UNRESOLVED
    assert result.snapshot is None


def test_recheck_permission_denied_is_permission_denied() -> None:
    """Denied access is distinct from a gone identity."""
    snapshot = _restricted(_identity("corpus", "doc-1", "3"))
    authority = _TableAuthority(
        {("corpus", "doc-1"): "3"}, denied=frozenset({("corpus", "doc-1")})
    )
    result = recheck_scope(snapshot, authority=authority)
    assert result.kind == TASK_PERMISSION_DENIED
    assert result.snapshot is None


def test_recheck_authority_raising_is_dependency_failure() -> None:
    """An authority that cannot answer is distinguishable from a denial."""
    snapshot = _restricted(_identity("corpus", "doc-1", "3"))
    authority = _TableAuthority(
        {("corpus", "doc-1"): "3"}, raises=frozenset({"doc-1"})
    )
    result = recheck_scope(snapshot, authority=authority)
    assert result.kind == TASK_DEPENDENCY_FAILURE
    assert result.snapshot is None


def test_recheck_all_authorized_is_in_force() -> None:
    """Only a fully confirmed recheck turns the saved intent into authority."""
    snapshot = _restricted(
        _identity("corpus", "doc-1", "3"),
        _identity("attachment", "att-1", "9"),
    )
    authority = _TableAuthority(
        {("corpus", "doc-1"): "3", ("attachment", "att-1"): "9"}
    )
    result = recheck_scope(snapshot, authority=authority)
    assert result.snapshot == snapshot
    assert result.kind is None
    assert result.in_force is True


def test_recheck_unrestricted_is_in_force_without_checks() -> None:
    """Unrestricted names no identities, so nothing can fail recheck."""
    snapshot = unrestricted_scope(accepted_input_version=2)
    result = recheck_scope(snapshot, authority=_TableAuthority({}))
    assert result.snapshot == snapshot
    assert result.kind is None


# ----- no failure path widens -----


@pytest.mark.parametrize(
    ("case", "versions", "denied", "raises"),
    [
        ("identity_gone", {}, frozenset(), frozenset()),
        ("version_stale", {("corpus", "doc-1"): "9"}, frozenset(), frozenset()),
        (
            "permission_denied",
            {("corpus", "doc-1"): "3"},
            frozenset({("corpus", "doc-1")}),
            frozenset(),
        ),
        ("authority_raised", {("corpus", "doc-1"): "3"}, frozenset(), frozenset({"doc-1"})),
    ],
)
def test_no_failure_path_widens_to_unrestricted(
    case: str,
    versions: dict[tuple[str, str], str],
    denied: frozenset[tuple[str, str]],
    raises: frozenset[str],
) -> None:
    """Every recheck failure leaves the restriction unsatisfied — none falls
    back to the whole corpus."""
    del case  # the parametrization exists so each cause is named in the report
    snapshot = _restricted(_identity("corpus", "doc-1", "3"))
    result = recheck_scope(
        snapshot,
        authority=_TableAuthority(versions, denied=denied, raises=raises),
    )
    assert result.snapshot is None
    assert result.kind is not None
    assert result.in_force is False
