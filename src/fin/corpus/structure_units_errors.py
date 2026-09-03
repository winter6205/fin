"""Typed failures for structure-aware corpus units."""

from __future__ import annotations


class StructureUnitsError(RuntimeError):
    """Base class for structure-unit failures."""


class InvalidStructureInputError(StructureUnitsError, ValueError):
    """The document identity or section payload is invalid."""


class StructureParseError(StructureUnitsError):
    """The structure parser could not produce sections."""


class StructureUnitLimitError(StructureUnitsError):
    """The parsed document exceeds the configured unit limit."""


class UnsupportedFormatError(StructureUnitsError):
    """No default structure adapter exists for the source format."""


__all__ = [
    "InvalidStructureInputError",
    "StructureParseError",
    "StructureUnitLimitError",
    "StructureUnitsError",
    "UnsupportedFormatError",
]
