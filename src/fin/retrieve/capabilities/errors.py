"""Typed failures for capability snapshot and static-check errors."""

from __future__ import annotations


class CapabilityError(Exception):
    """Base for capability-snapshot and static-check failures.

    ``code`` is stable and assertable; subclasses narrow it.
    """

    code = "capability_error"


class CapabilitySnapshotMissingError(CapabilityError):
    """The snapshot file is not there at all."""

    code = "capability_snapshot_missing"


class CapabilitySnapshotMalformedError(CapabilityError):
    """The snapshot is unreadable, wrongly shaped, blank, or duplicated."""

    code = "capability_snapshot_malformed"


class CapabilitySnapshotCountError(CapabilityError):
    """The snapshot holds a number of entries other than eight."""

    code = "capability_snapshot_count_mismatch"


class CapabilityEnumerationRowMissingError(CapabilityError):
    """The spec no longer publishes the enumeration row the snapshot syncs to."""

    code = "capability_enumeration_row_missing"


class CapabilityEnumerationDriftError(CapabilityError):
    """Snapshot and published enumeration disagree; both sides are printed."""

    code = "capability_enumeration_drift"


class CapabilityConfigurationError(CapabilityError):
    """An eval capability configuration is incomplete or names an unknown key."""

    code = "capability_configuration_invalid"


class CapabilityConfigurationIncompleteError(CapabilityConfigurationError):
    """The configuration does not decide every snapshot key."""

    code = "capability_configuration_incomplete"


class UnknownCapabilityError(CapabilityConfigurationError):
    """The configuration names a key that is not in the snapshot."""

    code = "unknown_capability"


class ProductCallPathTargetMissingError(CapabilityError):
    """A file the static check is supposed to scan is not there.

    Scanning nothing would report zero hits, which reads exactly like a clean
    product path. This turns that silence into a failure.
    """

    code = "product_call_path_target_missing"
