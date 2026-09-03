"""Typed failures for the real filing monitor boundary."""

from __future__ import annotations


class FilingMonitorError(Exception):
    """Base class for monitor failures that have a stable wire code."""

    code = "filing_monitor_error"


class FilingSourceUnavailableError(FilingMonitorError):
    """The configured filing source could not provide a poll result."""

    code = "filing_source_unavailable"


__all__ = ["FilingMonitorError", "FilingSourceUnavailableError"]
