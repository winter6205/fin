"""Operator config for the structured-evidence identity (P2-I2).

The loader is the first production source of an
:class:`~fin.retrieve.evidence_route.EvidenceIdentityBinding`, so what is
asserted here is the seam's contract: a complete environment yields a
binding the binding itself accepts, an incomplete one serves unconfigured
BY NAME, and a value that cannot be honoured fails at boot rather than
becoming an identity that answers for nobody.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest

from fin.host.structured_evidence_identity import (
    FIN_EVIDENCE_IDENTITY_ACTOR_ID_ENV,
    FIN_EVIDENCE_IDENTITY_AUTH_REVISION_ENV,
    FIN_EVIDENCE_IDENTITY_CONSUMER_ID_ENV,
    FIN_EVIDENCE_IDENTITY_DEADLINE_AT_ENV,
    FIN_EVIDENCE_IDENTITY_OWNER_SCOPE_ENV,
    FIN_EVIDENCE_IDENTITY_READER_ENV,
    FIN_EVIDENCE_IDENTITY_SOURCE_REFS_ENV,
    FIN_EVIDENCE_TRANSPORT_FORWARD_ENV,
    StructuredEvidenceIdentity,
    StructuredEvidenceIdentityConfigError,
    load_structured_evidence_identity,
)
from fin.retrieve.evidence_route import EvidenceIdentityBinding


def _deadline(seconds: int = 3600) -> str:
    """A future instant in iknow's whole-second ``Z`` form."""

    moment = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return moment.isoformat().replace("+00:00", "Z")


def _configured(**overrides: str) -> dict[str, str]:
    """A complete environment; ``overrides`` replaces or adds any variable."""

    environ = {
        FIN_EVIDENCE_IDENTITY_ACTOR_ID_ENV: "fin-p3-reader",
        FIN_EVIDENCE_IDENTITY_OWNER_SCOPE_ENV: "fin-p3-scope",
        FIN_EVIDENCE_IDENTITY_READER_ENV: "fin-p3-reader-grant",
        FIN_EVIDENCE_IDENTITY_SOURCE_REFS_ENV: "src-alpha,src-beta",
        FIN_EVIDENCE_IDENTITY_AUTH_REVISION_ENV: "rev-7",
        FIN_EVIDENCE_IDENTITY_DEADLINE_AT_ENV: _deadline(),
        FIN_EVIDENCE_TRANSPORT_FORWARD_ENV: "IKNOW_MEMORY_STORAGE",
        "IKNOW_MEMORY_STORAGE": "pgvector",
    }
    environ.update(overrides)
    return environ


def test_unconfigured_environment_returns_none_and_logs_nothing(caplog) -> None:
    """The default boot: absent means absent, silently.

    A laptop that never configured an identity must not acquire a boot
    warning for the feature it never asked for, so this asserts the absence
    of the log as well as the ``None``.
    """

    with caplog.at_level(logging.WARNING):
        assert load_structured_evidence_identity(environ={}) is None
    assert caplog.records == []


def test_environment_without_any_identity_var_is_still_silent(caplog) -> None:
    """Unrelated variables are not an identity: no warning, no binding."""

    environ = {"FIN_CHECKPOINT_URL": "postgresql://localhost/fin", "PATH": "/usr/bin"}
    with caplog.at_level(logging.WARNING):
        assert load_structured_evidence_identity(environ=environ) is None
    assert caplog.records == []


def test_fully_configured_environment_builds_a_binding_it_accepts() -> None:
    """The promised result: a binding whose own ``__post_init__`` passed."""

    config = load_structured_evidence_identity(environ=_configured())
    assert config is not None

    binding = config.binding()
    # Construction IS the validation, so an instance of the frozen dataclass is
    # the assertion that ``__post_init__`` accepted every field.
    assert isinstance(binding, EvidenceIdentityBinding)
    assert binding.actor_id == "fin-p3-reader"
    assert binding.owner_scope_binding == "fin-p3-scope"
    assert binding.reader_binding == "fin-p3-reader-grant"
    assert binding.source_refs == ("src-alpha", "src-beta")
    assert binding.authorization_revision == "rev-7"
    assert binding.consumer_id == ""
    assert config.child_storage_env() == {"IKNOW_MEMORY_STORAGE": "pgvector"}


def test_consumer_id_travels_only_when_the_operator_names_it() -> None:
    """An unset consumer stays ABSENT on the wire rather than blank.

    ``child_env`` omits the variable entirely when the field is empty, so a
    host that named no consumer and a host that named an empty one stay
    distinguishable in the child.
    """

    absent = load_structured_evidence_identity(environ=_configured())
    assert absent is not None
    assert "IKNOW_EVIDENCE_CONSUMER_ID" not in absent.binding().child_env()

    named = load_structured_evidence_identity(
        environ=_configured(**{FIN_EVIDENCE_IDENTITY_CONSUMER_ID_ENV: "fin-consumer-a"})
    )
    assert named is not None
    assert named.binding().child_env()["IKNOW_EVIDENCE_CONSUMER_ID"] == "fin-consumer-a"


def test_partial_configuration_names_the_missing_variable(caplog) -> None:
    """Half an identity is refused BY NAME, and the host serves unconfigured."""

    environ = _configured()
    del environ[FIN_EVIDENCE_IDENTITY_READER_ENV]
    environ[FIN_EVIDENCE_IDENTITY_OWNER_SCOPE_ENV] = "   "

    with caplog.at_level(logging.WARNING):
        assert load_structured_evidence_identity(environ=environ) is None

    named = "\n".join(record.getMessage() for record in caplog.records)
    assert FIN_EVIDENCE_IDENTITY_READER_ENV in named, named
    # A blank value is the same answer as an absent one, so both are reported.
    assert FIN_EVIDENCE_IDENTITY_OWNER_SCOPE_ENV in named, named


def test_only_the_transport_forward_set_reports_every_required_variable(
    caplog,
) -> None:
    """Naming the transport alone is still an attempt to configure an identity."""

    with caplog.at_level(logging.WARNING):
        assert (
            load_structured_evidence_identity(
                environ={
                    FIN_EVIDENCE_TRANSPORT_FORWARD_ENV: "IKNOW_MEMORY_STORAGE",
                    "IKNOW_MEMORY_STORAGE": "pgvector",
                }
            )
            is None
        )

    named = "\n".join(record.getMessage() for record in caplog.records)
    for variable in (
        FIN_EVIDENCE_IDENTITY_ACTOR_ID_ENV,
        FIN_EVIDENCE_IDENTITY_SOURCE_REFS_ENV,
        FIN_EVIDENCE_IDENTITY_DEADLINE_AT_ENV,
    ):
        assert variable in named, named


def test_blank_source_refs_value_is_refused_and_never_becomes_a_binding() -> None:
    """An empty authorized set is refused, not widened to every source."""

    environ = _configured(**{FIN_EVIDENCE_IDENTITY_SOURCE_REFS_ENV: "   "})
    assert load_structured_evidence_identity(environ=environ) is None

    only_separators = _configured(**{FIN_EVIDENCE_IDENTITY_SOURCE_REFS_ENV: ", ,"})
    with pytest.raises(StructuredEvidenceIdentityConfigError) as refusal:
        load_structured_evidence_identity(environ=only_separators)
    assert FIN_EVIDENCE_IDENTITY_SOURCE_REFS_ENV in str(refusal.value)


def test_malformed_deadline_at_is_refused() -> None:
    """An unparseable instant names its variable instead of booting."""

    environ = _configured(**{FIN_EVIDENCE_IDENTITY_DEADLINE_AT_ENV: "next tuesday"})

    with pytest.raises(StructuredEvidenceIdentityConfigError) as refusal:
        load_structured_evidence_identity(environ=environ)
    assert FIN_EVIDENCE_IDENTITY_DEADLINE_AT_ENV in str(refusal.value)
    assert "ISO instant" in str(refusal.value)


def test_deadline_at_without_a_timezone_is_refused() -> None:
    """A naive instant is refused: a deadline must never read as local time."""

    naive = datetime.now(timezone.utc) + timedelta(hours=1)
    environ = _configured(
        **{
            FIN_EVIDENCE_IDENTITY_DEADLINE_AT_ENV: naive.replace(tzinfo=None).isoformat()
        }
    )

    with pytest.raises(StructuredEvidenceIdentityConfigError) as refusal:
        load_structured_evidence_identity(environ=environ)
    assert "timezone" in str(refusal.value)


def test_deadline_at_already_past_is_refused_at_boot() -> None:
    """An expired reader binding is never served, because a read does not renew it."""

    environ = _configured(
        **{FIN_EVIDENCE_IDENTITY_DEADLINE_AT_ENV: _deadline(seconds=-60)}
    )

    with pytest.raises(StructuredEvidenceIdentityConfigError) as refusal:
        load_structured_evidence_identity(environ=environ)
    assert FIN_EVIDENCE_IDENTITY_DEADLINE_AT_ENV in str(refusal.value)
    assert "passed" in str(refusal.value)


def test_transport_forward_must_name_a_set_variable() -> None:
    """The registry refuses an empty transport_env; this refuses it by name."""

    without = _configured()
    del without[FIN_EVIDENCE_TRANSPORT_FORWARD_ENV]
    with pytest.raises(StructuredEvidenceIdentityConfigError) as absent_list:
        load_structured_evidence_identity(environ=without)
    assert FIN_EVIDENCE_TRANSPORT_FORWARD_ENV in str(absent_list.value)

    unset_child = _configured(
        **{FIN_EVIDENCE_TRANSPORT_FORWARD_ENV: "IKNOW_MEMORY_DB_URL"}
    )
    with pytest.raises(StructuredEvidenceIdentityConfigError) as unset:
        load_structured_evidence_identity(environ=unset_child)
    assert "IKNOW_MEMORY_DB_URL" in str(unset.value)


def test_transport_forward_refuses_an_identity_variable() -> None:
    """The binding is written last, so a forwarded identity variable is a mistake."""

    environ = _configured(**{FIN_EVIDENCE_TRANSPORT_FORWARD_ENV: "IKNOW_EVIDENCE_ACTOR_ID"})

    with pytest.raises(StructuredEvidenceIdentityConfigError) as refusal:
        load_structured_evidence_identity(environ=environ)
    assert "IKNOW_EVIDENCE_ACTOR_ID" in str(refusal.value)


def test_resolver_hands_out_the_identity_only_inside_its_deadline() -> None:
    """A host that outlives its own deadline refuses the read, not the caller.

    ``None`` is what the registry refuses by name
    (:class:`~fin.host.evidence_session_registry.EvidenceBindingRefusal`), so
    this is a refusal rather than a read under some other identity.
    """

    live = StructuredEvidenceIdentity(
        actor_id="fin-p3-reader",
        owner_scope_binding="fin-p3-scope",
        reader_binding="fin-p3-reader-grant",
        source_refs=("src-alpha",),
        authorization_revision="rev-7",
        deadline_at=_deadline(seconds=60),
    )
    assert live.resolver()(None).actor_id == "fin-p3-reader"

    expired = StructuredEvidenceIdentity(
        actor_id="fin-p3-reader",
        owner_scope_binding="fin-p3-scope",
        reader_binding="fin-p3-reader-grant",
        source_refs=("src-alpha",),
        authorization_revision="rev-7",
        deadline_at=_deadline(seconds=-60),
    )
    assert expired.resolver()({"thread_id": "t-1"}) is None


def test_the_loader_returns_the_whole_identity_not_a_partial_default() -> None:
    """Six fields, all supplied: none of them was defaulted by the loader.

    A defaulted field would be an invented authority fact, which is what ADR
    B1 forbids; this asserts every configured field is the operator's own
    value, byte for byte.
    """

    config = load_structured_evidence_identity(
        environ=_configured(
            **{
                FIN_EVIDENCE_IDENTITY_AUTH_REVISION_ENV: "rev-9",
                FIN_EVIDENCE_IDENTITY_SOURCE_REFS_ENV: " src-gamma , src-delta ",
            }
        )
    )
    assert config is not None
    assert config.authorization_revision == "rev-9"
    assert config.source_refs == ("src-gamma", "src-delta")
    assert config.binding().authorization_revision == "rev-9"