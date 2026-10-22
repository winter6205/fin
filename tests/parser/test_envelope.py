"""Contract tests for the versioned parse-result envelope (SC4).

The envelope is the one shape every parser adapter emits, so the
service pins it here: required identity fields, version drift, the
observable declared/detected format pair, ordered units, typed
coverage, typed diagnostics, and the rule that derived output is
REFERENCED rather than carried.

The payload below is a CONTROLLED result: it exercises validation end
to end. It makes no parser-fidelity claim and selects no provider.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from fin.parser import envelope as envelope_module
from fin.parser.envelope import (
    DIAGNOSTIC_DECLARED_FORMAT_MISMATCH,
    DIAGNOSTIC_FORMAT_DETECTION_FAILED,
    ENVELOPE_VERSION,
    SUPPORTED_ENVELOPE_VERSIONS,
    CoverageScope,
    ExtractedAsset,
    ParseCoverage,
    ParseDiagnostic,
    ParsedDocument,
    ParsedUnit,
    ParserBuild,
    parse_envelope,
)
from fin.parser.errors import ParserEnvelopeError

SOURCE_SHA256 = "a" * 64
RESULT_REF = "fin-parser/results/job-1/derived.json"


def _unit(index: int, *, text_ref: str | None = None) -> dict[str, Any]:
    return {
        "index": index,
        "kind": "page",
        "text_ref": text_ref or f"fin-parser/results/job-1/units/{index}.txt",
        "char_count": 12,
        "locator": f"page {index + 1}",
    }


def _payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "envelope_version": ENVELOPE_VERSION,
        "source_sha256": SOURCE_SHA256,
        "declared_format": "pdf",
        "detected_format": "pdf",
        "parser_build": {
            "parser": "controlled",
            "provider": "controlled",
            "version": "controlled-1",
        },
        "model_fingerprint": "controlled-model-fingerprint",
        "config_fingerprint": "controlled-config-fingerprint",
        "parse_rule_version": "parse-2",
        "result_ref": RESULT_REF,
        "units": [_unit(0), _unit(1)],
        "assets": [
            {
                "asset_id": "asset-1",
                "media_type": "image/png",
                "ref": "fin-parser/results/job-1/assets/asset-1.png",
                "unit_index": 0,
            }
        ],
        "coverage": {"status": "complete"},
        "diagnostics": [],
    }
    payload.update(overrides)
    return payload


def _locs(exc: ValidationError) -> set[tuple[str, ...]]:
    return {tuple(str(part) for part in error["loc"]) for error in exc.errors()}


# --- happy path -----------------------------------------------------------


def test_a_controlled_result_validates_end_to_end() -> None:
    document = ParsedDocument.model_validate(_payload())
    assert document.envelope_version == ENVELOPE_VERSION
    assert document.source_sha256 == SOURCE_SHA256
    assert document.declared_format == "pdf"
    assert document.detected_format == "pdf"
    assert document.parser_build.provider == "controlled"
    assert document.model_fingerprint == "controlled-model-fingerprint"
    assert document.config_fingerprint == "controlled-config-fingerprint"
    assert document.parse_rule_version == "parse-2"
    assert document.result_ref == RESULT_REF
    assert len(document.units) == 2
    assert len(document.assets) == 1
    assert document.coverage.status == "complete"
    assert document.diagnostics == ()


def test_a_controlled_result_survives_a_json_round_trip() -> None:
    document = ParsedDocument.model_validate(_payload())
    again = ParsedDocument.model_validate(document.model_dump(mode="json"))
    assert again == document


def test_parse_envelope_returns_the_validated_document() -> None:
    document = parse_envelope(_payload())
    assert isinstance(document, ParsedDocument)
    assert document.result_ref == RESULT_REF


# --- empty / missing input classes ----------------------------------------


@pytest.mark.parametrize(
    "missing",
    [
        "envelope_version",
        "source_sha256",
        "parser_build",
        "model_fingerprint",
        "config_fingerprint",
        "parse_rule_version",
        "result_ref",
        "coverage",
    ],
)
def test_a_missing_required_field_is_rejected(missing: str) -> None:
    payload = _payload()
    del payload[missing]
    with pytest.raises(ValidationError) as excinfo:
        ParsedDocument.model_validate(payload)
    assert (missing,) in _locs(excinfo.value)


@pytest.mark.parametrize(
    "field",
    ["model_fingerprint", "config_fingerprint", "parse_rule_version", "result_ref"],
)
def test_a_blank_required_string_is_rejected(field: str) -> None:
    with pytest.raises(ValidationError) as excinfo:
        ParsedDocument.model_validate(_payload(**{field: ""}))
    assert (field,) in _locs(excinfo.value)


def test_an_empty_payload_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ParsedDocument.model_validate({})


# --- invalid input classes ------------------------------------------------


@pytest.mark.parametrize(
    "digest",
    ["", "abc", "a" * 63, "a" * 65, "A" * 64, "g" * 64],
)
def test_a_malformed_source_sha256_is_rejected(digest: str) -> None:
    with pytest.raises(ValidationError) as excinfo:
        ParsedDocument.model_validate(_payload(source_sha256=digest))
    assert ("source_sha256",) in _locs(excinfo.value)


def test_an_unsupported_envelope_version_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ParsedDocument.model_validate(
            _payload(envelope_version=ENVELOPE_VERSION + 1)
        )


def test_an_unknown_envelope_field_is_rejected() -> None:
    """A producer that added a field this build does not know must fail
    loudly instead of being silently accepted as the current version."""
    with pytest.raises(ValidationError) as excinfo:
        ParsedDocument.model_validate(_payload(future_field="x"))
    assert ("future_field",) in _locs(excinfo.value)


def test_supported_envelope_versions_are_declared() -> None:
    assert ENVELOPE_VERSION in SUPPORTED_ENVELOPE_VERSIONS


# --- declared vs detected format ------------------------------------------


def test_a_declared_detected_mismatch_is_observable() -> None:
    document = ParsedDocument.model_validate(
        _payload(
            declared_format="pdf",
            detected_format="docx",
            diagnostics=[
                {
                    "code": DIAGNOSTIC_DECLARED_FORMAT_MISMATCH,
                    "severity": "error",
                    "message": "declared pdf, detected docx",
                }
            ],
        )
    )
    assert document.format_mismatch is True
    assert document.declared_format == "pdf"
    assert document.detected_format == "docx"


def test_a_declared_detected_mismatch_is_not_silently_resolved() -> None:
    with pytest.raises(ValidationError):
        ParsedDocument.model_validate(
            _payload(declared_format="pdf", detected_format="docx")
        )


def test_a_matching_declared_detected_pair_is_not_a_mismatch() -> None:
    document = ParsedDocument.model_validate(_payload())
    assert document.format_mismatch is False


def test_a_declared_format_without_detection_must_be_disclosed() -> None:
    with pytest.raises(ValidationError):
        ParsedDocument.model_validate(_payload(detected_format=None))
    document = ParsedDocument.model_validate(
        _payload(
            detected_format=None,
            diagnostics=[
                {
                    "code": DIAGNOSTIC_FORMAT_DETECTION_FAILED,
                    "severity": "warning",
                    "message": "format detection did not complete",
                }
            ],
        )
    )
    assert document.detected_format is None


# --- ordered units --------------------------------------------------------


def test_the_unit_order_is_preserved() -> None:
    document = ParsedDocument.model_validate(
        _payload(units=[_unit(0), _unit(1), _unit(2)])
    )
    assert [unit.index for unit in document.units] == [0, 1, 2]
    assert [unit.text_ref for unit in document.units] == [
        _unit(index)["text_ref"] for index in range(3)
    ]


@pytest.mark.parametrize(
    "indices",
    [[1, 0], [0, 2], [1, 1], [0, 1, 3], [2]],
)
def test_a_non_contiguous_unit_order_is_rejected(indices: list[int]) -> None:
    with pytest.raises(ValidationError):
        ParsedDocument.model_validate(
            _payload(units=[_unit(index) for index in indices])
        )


def test_a_negative_unit_index_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ParsedDocument.model_validate(_payload(units=[_unit(-1)]))


def test_an_asset_must_point_at_an_existing_unit() -> None:
    with pytest.raises(ValidationError):
        ParsedDocument.model_validate(
            _payload(
                assets=[
                    {
                        "asset_id": "asset-1",
                        "media_type": "image/png",
                        "ref": "fin-parser/results/job-1/assets/asset-1.png",
                        "unit_index": 7,
                    }
                ]
            )
        )


# --- typed coverage --------------------------------------------------------


def test_coverage_can_express_partial_with_an_explicit_reason() -> None:
    document = ParsedDocument.model_validate(
        _payload(
            coverage={
                "status": "partial",
                "reason": "page 2 could not be decoded",
                "scopes": [
                    {
                        "scope": "text",
                        "status": "partial",
                        "reason": "page 2 could not be decoded",
                    }
                ],
            }
        )
    )
    assert document.coverage.status == "partial"
    assert document.coverage.reason == "page 2 could not be decoded"


def test_coverage_can_express_failed_with_an_explicit_reason() -> None:
    document = ParsedDocument.model_validate(
        _payload(
            units=[],
            assets=[],
            coverage={"status": "failed", "reason": "the source is encrypted"},
        )
    )
    assert document.coverage.status == "failed"


@pytest.mark.parametrize("status", ["partial", "failed"])
def test_a_non_complete_coverage_requires_a_reason(status: str) -> None:
    with pytest.raises(ValidationError):
        ParsedDocument.model_validate(_payload(coverage={"status": status}))


def test_a_complete_coverage_cannot_carry_a_reason() -> None:
    with pytest.raises(ValidationError):
        ParsedDocument.model_validate(
            _payload(coverage={"status": "complete", "reason": "all good"})
        )


def test_missing_required_coverage_cannot_be_reported_as_complete() -> None:
    with pytest.raises(ValidationError):
        ParsedDocument.model_validate(
            _payload(
                coverage={
                    "status": "complete",
                    "scopes": [
                        {"scope": "text", "status": "complete"},
                        {
                            "scope": "tables",
                            "status": "failed",
                            "reason": "no table structure was found",
                        },
                    ],
                }
            )
        )


def test_a_complete_coverage_requires_at_least_one_unit() -> None:
    with pytest.raises(ValidationError):
        ParsedDocument.model_validate(_payload(units=[], assets=[]))


def test_a_scope_without_a_reason_cannot_be_complete() -> None:
    with pytest.raises(ValidationError):
        ParsedDocument.model_validate(
            _payload(
                coverage={
                    "status": "partial",
                    "reason": "one scope is incomplete",
                    "scopes": [{"scope": "text", "status": "partial"}],
                }
            )
        )


# --- typed diagnostics -----------------------------------------------------


def test_a_diagnostic_requires_a_code_and_a_severity() -> None:
    with pytest.raises(ValidationError):
        ParsedDocument.model_validate(
            _payload(
                diagnostics=[{"severity": "error", "message": "no code"}]
            )
        )
    with pytest.raises(ValidationError):
        ParsedDocument.model_validate(
            _payload(diagnostics=[{"code": "some_code", "message": "no severity"}])
        )


@pytest.mark.parametrize("code", ["", "Not A Code", "Declared Format Mismatch"])
def test_an_unstable_diagnostic_code_is_rejected(code: str) -> None:
    with pytest.raises(ValidationError):
        ParsedDocument.model_validate(
            _payload(
                diagnostics=[
                    {"code": code, "severity": "warning", "message": "bad code"}
                ]
            )
        )


def test_an_unknown_severity_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ParsedDocument.model_validate(
            _payload(
                diagnostics=[
                    {"code": "some_code", "severity": "fatal", "message": "bad severity"}
                ]
            )
        )


def test_a_diagnostic_must_point_at_an_existing_unit() -> None:
    with pytest.raises(ValidationError):
        ParsedDocument.model_validate(
            _payload(
                diagnostics=[
                    {
                        "code": "some_code",
                        "severity": "info",
                        "message": "about unit 9",
                        "unit_index": 9,
                    }
                ]
            )
        )


# --- the envelope references derived output -------------------------------


PUBLIC_MODELS = (
    ParserBuild,
    ParsedUnit,
    ExtractedAsset,
    ParseDiagnostic,
    CoverageScope,
    ParseCoverage,
    ParsedDocument,
)

# A field named like content would be a place raw bytes or a full text
# blob could hide; the envelope may only REFERENCE derived output.
FORBIDDEN_FIELD_NAMES = frozenset(
    {
        "text",
        "content",
        "raw",
        "raw_bytes",
        "bytes",
        "data",
        "blob",
        "body",
        "source_bytes",
        "document_text",
        "full_text",
        "extracted_text",
    }
)


def test_no_envelope_field_can_carry_raw_source_bytes() -> None:
    for model in PUBLIC_MODELS:
        for name, field in model.model_fields.items():
            assert name not in FORBIDDEN_FIELD_NAMES, f"{model.__name__}.{name}"
            assert field.annotation is not bytes, f"{model.__name__}.{name}"
            assert field.annotation is not bytearray, f"{model.__name__}.{name}"
            assert "bytes" not in str(field.annotation), f"{model.__name__}.{name}"


def test_every_public_envelope_model_is_covered_by_the_no_bytes_contract() -> None:
    public = {
        getattr(envelope_module, name)
        for name in envelope_module.__all__
        if isinstance(getattr(envelope_module, name), type)
        and issubclass(getattr(envelope_module, name), BaseModel)
    }
    assert public == set(PUBLIC_MODELS)


def test_derived_output_is_carried_by_reference_only() -> None:
    assert ParsedDocument.model_fields["result_ref"].annotation is str
    assert ParsedUnit.model_fields["text_ref"].annotation is str
    assert ExtractedAsset.model_fields["ref"].annotation is str
    reference_fields = {
        name
        for model in PUBLIC_MODELS
        for name in model.model_fields
        if name.endswith("_ref") or name == "ref"
    }
    assert reference_fields == {"result_ref", "text_ref", "ref"}


# --- exception class ------------------------------------------------------


def test_parse_envelope_raises_a_typed_error_for_an_invalid_payload() -> None:
    with pytest.raises(ParserEnvelopeError):
        parse_envelope({"envelope_version": ENVELOPE_VERSION})


def test_the_typed_error_does_not_echo_the_rejected_value() -> None:
    secret = "sensitive-original-bytes"
    with pytest.raises(ParserEnvelopeError) as excinfo:
        parse_envelope({"envelope_version": ENVELOPE_VERSION, "result_ref": secret})
    assert secret not in str(excinfo.value)
