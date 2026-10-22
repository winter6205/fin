"""The versioned parse-result envelope every parser adapter emits (SC4).

The envelope is the one contract between an adapter and the parser
service. It carries the source SHA-256, the declared and detected
format, the parser/provider build, the model and config fingerprints,
the parse-rule version, the ordered units, the extracted-asset
references, the coverage, and the typed diagnostics.

Two rules shape the whole module:

* The envelope REFERENCES derived output and never replaces the
  original bytes. No field holds raw source bytes or a full extracted
  text blob: the derived text of a unit is named by ``text_ref`` and
  the derived result by ``result_ref``. The source bytes stay with
  their owner; the envelope carries only their digest.
* The envelope is VERSIONED. ``envelope_version`` names the contract
  revision, unknown fields are rejected, and an unsupported version is
  a validation failure. A producer that adds a required field must
  bump the version, and drift is detected rather than absorbed.

Coverage and diagnostics are TYPED. Coverage states complete, partial,
or failed, and every non-complete state carries an explicit reason; a
missing required coverage can never be reported as a complete success.
A diagnostic carries a stable machine-readable code and a severity.

The controlled result used in the tests exercises this contract end to
end. It is not a parser-fidelity claim: no provider is selected here,
no adapter is integrated, and no real file is parsed.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from fin.parser.errors import ParserEnvelopeError

# The envelope contract revision this build reads and writes. Bump it
# when a required field is added; consumers reject unknown versions.
ENVELOPE_VERSION: Final[int] = 1

SUPPORTED_ENVELOPE_VERSIONS: Final[frozenset[int]] = frozenset({ENVELOPE_VERSION})

COVERAGE_COMPLETE: Final[str] = "complete"
COVERAGE_PARTIAL: Final[str] = "partial"
COVERAGE_FAILED: Final[str] = "failed"

SEVERITY_INFO: Final[str] = "info"
SEVERITY_WARNING: Final[str] = "warning"
SEVERITY_ERROR: Final[str] = "error"

# Stable diagnostic codes the envelope itself interprets. An adapter
# may emit other codes; these two make the declared/detected format
# pair observable instead of silently resolved.
DIAGNOSTIC_DECLARED_FORMAT_MISMATCH: Final[str] = "declared_format_mismatch"
DIAGNOSTIC_FORMAT_DETECTION_FAILED: Final[str] = "format_detection_failed"

CoverageStatus = Literal["complete", "partial", "failed"]
Severity = Literal["info", "warning", "error"]

_CODE_PATTERN = r"^[a-z][a-z0-9_]*$"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"


class _EnvelopeModel(BaseModel):
    """Strict base for every envelope model.

    Unknown fields are rejected so a producer's new required field is
    detected instead of silently dropped, and instances are immutable
    so a committed result cannot drift after the fact.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


class ParserBuild(_EnvelopeModel):
    """The parser/provider build that produced this envelope."""

    parser: str = Field(min_length=1, max_length=256)
    provider: str = Field(min_length=1, max_length=256)
    version: str = Field(min_length=1, max_length=256)


class ParsedUnit(_EnvelopeModel):
    """One ordered unit of the parsed document.

    ``text_ref`` REFERENCES the unit's derived text; the text itself
    is never carried here, so the envelope cannot replace the original
    bytes nor embed a full extracted text blob.
    """

    index: int = Field(ge=0)
    kind: str = Field(min_length=1, max_length=128)
    text_ref: str = Field(min_length=1, max_length=1024)
    char_count: int = Field(default=0, ge=0)
    locator: str | None = Field(default=None, max_length=512)


class ExtractedAsset(_EnvelopeModel):
    """A reference to an asset extracted from the source.

    ``ref`` points at the stored asset (an object-store key or a
    staging path); the asset bytes stay outside the envelope.
    """

    asset_id: str = Field(min_length=1, max_length=256)
    media_type: str = Field(min_length=1, max_length=128)
    ref: str = Field(min_length=1, max_length=1024)
    unit_index: int | None = Field(default=None, ge=0)
    byte_size: int | None = Field(default=None, ge=0)


class ParseDiagnostic(_EnvelopeModel):
    """One typed diagnostic.

    ``code`` is stable and machine-readable (callers branch on it, not
    on ``message``); ``severity`` says how much the diagnostic matters.
    """

    code: str = Field(min_length=1, max_length=128, pattern=_CODE_PATTERN)
    severity: Severity
    message: str = Field(min_length=1, max_length=2000)
    unit_index: int | None = Field(default=None, ge=0)


class CoverageScope(_EnvelopeModel):
    """The coverage of one source aspect (text, tables, images, ...).

    A scope that is not complete states why; a required scope that
    did not complete can never be reported as a complete document.
    """

    scope: str = Field(min_length=1, max_length=128)
    status: CoverageStatus
    reason: str | None = Field(default=None, max_length=1000)
    required: bool = True

    @model_validator(mode="after")
    def _state_the_reason(self) -> CoverageScope:
        if self.status == COVERAGE_COMPLETE:
            if self.reason is not None:
                raise ValueError("a complete coverage scope carries no reason")
        elif not (self.reason or "").strip():
            raise ValueError(
                f"a {self.status} coverage scope requires an explicit reason"
            )
        return self


class ParseCoverage(_EnvelopeModel):
    """How much of the source the parse actually covered.

    ``partial`` and ``failed`` always carry an explicit reason, and a
    ``complete`` coverage requires every required scope to be complete
    — so a missing required coverage is never representable as a
    false complete success.
    """

    status: CoverageStatus
    reason: str | None = Field(default=None, max_length=1000)
    scopes: tuple[CoverageScope, ...] = ()

    @model_validator(mode="after")
    def _state_the_reason(self) -> ParseCoverage:
        if self.status == COVERAGE_COMPLETE:
            if self.reason is not None:
                raise ValueError("a complete coverage carries no reason")
            incomplete = [
                scope.scope
                for scope in self.scopes
                if scope.required and scope.status != COVERAGE_COMPLETE
            ]
            if incomplete:
                raise ValueError(
                    "required coverage is missing for: " + ", ".join(incomplete)
                )
        elif not (self.reason or "").strip():
            raise ValueError(
                f"a {self.status} coverage requires an explicit reason"
            )
        return self


class ParsedDocument(_EnvelopeModel):
    """The versioned parse result every adapter emits.

    The envelope references the derived output (``result_ref``,
    ``text_ref``, asset ``ref``) and never carries the source bytes.
    """

    envelope_version: int
    source_sha256: str = Field(pattern=_SHA256_PATTERN)
    declared_format: str | None = Field(default=None, max_length=128)
    detected_format: str | None = Field(default=None, max_length=128)
    parser_build: ParserBuild
    model_fingerprint: str = Field(min_length=1, max_length=256)
    config_fingerprint: str = Field(min_length=1, max_length=256)
    parse_rule_version: str = Field(min_length=1, max_length=256)
    result_ref: str = Field(min_length=1, max_length=1024)
    units: tuple[ParsedUnit, ...] = ()
    assets: tuple[ExtractedAsset, ...] = ()
    coverage: ParseCoverage
    diagnostics: tuple[ParseDiagnostic, ...] = ()

    @property
    def format_mismatch(self) -> bool:
        """Whether the declared format contradicts the detected one."""
        return (
            self.declared_format is not None
            and self.detected_format is not None
            and self.declared_format != self.detected_format
        )

    @model_validator(mode="after")
    def _validate_contract(self) -> ParsedDocument:
        if self.envelope_version not in SUPPORTED_ENVELOPE_VERSIONS:
            raise ValueError(
                f"unsupported envelope version {self.envelope_version}; "
                f"supported versions: {sorted(SUPPORTED_ENVELOPE_VERSIONS)}"
            )
        self._validate_unit_order()
        self._validate_unit_references()
        self._validate_format_observability()
        self._validate_coverage_consistency()
        return self

    def _validate_unit_order(self) -> None:
        """The unit order is meaningful, so it must be dense and in order."""
        indexes = [unit.index for unit in self.units]
        if indexes != list(range(len(self.units))):
            raise ValueError(
                "unit indexes must be 0..n-1 in list order; got "
                f"{indexes} — the unit order is part of the contract"
            )

    def _validate_unit_references(self) -> None:
        known = {unit.index for unit in self.units}
        for asset in self.assets:
            if asset.unit_index is not None and asset.unit_index not in known:
                raise ValueError(
                    f"asset {asset.asset_id!r} points at unknown unit index "
                    f"{asset.unit_index}"
                )
        for diagnostic in self.diagnostics:
            if (
                diagnostic.unit_index is not None
                and diagnostic.unit_index not in known
            ):
                raise ValueError(
                    f"diagnostic {diagnostic.code!r} points at unknown unit "
                    f"index {diagnostic.unit_index}"
                )

    def _validate_format_observability(self) -> None:
        """A format contradiction must be disclosed, never resolved away."""
        codes = {diagnostic.code for diagnostic in self.diagnostics}
        if self.format_mismatch:
            if DIAGNOSTIC_DECLARED_FORMAT_MISMATCH not in codes:
                raise ValueError(
                    "declared and detected formats differ; a diagnostic "
                    f"with code {DIAGNOSTIC_DECLARED_FORMAT_MISMATCH!r} is "
                    "required"
                )
        elif self.declared_format is not None and self.detected_format is None:
            if DIAGNOSTIC_FORMAT_DETECTION_FAILED not in codes:
                raise ValueError(
                    "a declared format without a detected format; a "
                    "diagnostic with code "
                    f"{DIAGNOSTIC_FORMAT_DETECTION_FAILED!r} is required"
                )

    def _validate_coverage_consistency(self) -> None:
        if self.coverage.status == COVERAGE_COMPLETE and not self.units:
            raise ValueError(
                "a complete coverage requires at least one parsed unit"
            )


def parse_envelope(payload: Mapping[str, Any]) -> ParsedDocument:
    """Validate a mapping into a ``ParsedDocument``.

    Raises :class:`ParserEnvelopeError` instead of returning ``None``
    or a sentinel. The message names the failing fields and the failure
    kind only: it never echoes the rejected value, which may be source
    material.
    """
    try:
        return ParsedDocument.model_validate(payload)
    except ValidationError as exc:
        raise ParserEnvelopeError(_describe_failure(exc)) from exc


def _describe_failure(exc: ValidationError) -> str:
    problems = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "<envelope>"
        problems.append(f"{location}: {error['type']}")
    return "parse envelope validation failed: " + "; ".join(problems)


__all__ = [
    "COVERAGE_COMPLETE",
    "COVERAGE_FAILED",
    "COVERAGE_PARTIAL",
    "DIAGNOSTIC_DECLARED_FORMAT_MISMATCH",
    "DIAGNOSTIC_FORMAT_DETECTION_FAILED",
    "ENVELOPE_VERSION",
    "SEVERITY_ERROR",
    "SEVERITY_INFO",
    "SEVERITY_WARNING",
    "SUPPORTED_ENVELOPE_VERSIONS",
    "CoverageScope",
    "ExtractedAsset",
    "ParseCoverage",
    "ParseDiagnostic",
    "ParsedDocument",
    "ParsedUnit",
    "ParserBuild",
    "parse_envelope",
]
