"""K1: capability-key snapshot and enumeration sync (T19, ADR 0039)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from fin.retrieve.capabilities._paths import (
    CAPABILITY_COUNT,
    CAPABILITY_SNAPSHOT_PATH,
    SPEC_ENUMERATION_ROW_LABEL,
    published_spec_path,
)
from fin.retrieve.capabilities.errors import (
    CapabilityConfigurationIncompleteError,
    CapabilityEnumerationDriftError,
    CapabilityEnumerationRowMissingError,
    CapabilitySnapshotCountError,
    CapabilitySnapshotMalformedError,
    CapabilitySnapshotMissingError,
    UnknownCapabilityError,
)

_BACKTICKED = re.compile(r"`([^`]+)`")


def _validate_keys(keys: Sequence[Any], *, origin: str) -> tuple[str, ...]:
    for entry in keys:
        if not isinstance(entry, str) or not entry.strip():
            raise CapabilitySnapshotMalformedError(
                f"{origin}: capability key is not a non-blank string: {entry!r}"
            )
        if entry.strip() != entry:
            raise CapabilitySnapshotMalformedError(
                f"{origin}: capability key has surrounding whitespace: {entry!r}"
            )
    duplicates = sorted({key for key in keys if list(keys).count(key) > 1})
    if duplicates:
        raise CapabilitySnapshotMalformedError(
            f"{origin}: duplicate capability keys: {duplicates}"
        )
    if len(keys) != CAPABILITY_COUNT:
        raise CapabilitySnapshotCountError(
            f"{origin}: capability snapshot must hold exactly "
            f"{CAPABILITY_COUNT} entries, found {len(keys)}: {list(keys)}"
        )
    return tuple(keys)


def load_capability_keys(path: Path | None = None) -> tuple[str, ...]:
    """Return the 8 snapshot keys in published order.

    Raises a named :class:`CapabilityError` when the file is absent,
    unreadable, wrongly shaped, blank, duplicated, or not exactly eight long.
    """
    snapshot_path = Path(path) if path is not None else CAPABILITY_SNAPSHOT_PATH
    try:
        raw = snapshot_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise CapabilitySnapshotMissingError(
            f"capability snapshot not found: {snapshot_path}"
        ) from exc
    except OSError as exc:
        raise CapabilitySnapshotMalformedError(
            f"capability snapshot unreadable: {snapshot_path}: {exc}"
        ) from exc
    try:
        document = json.loads(raw)
    except ValueError as exc:
        raise CapabilitySnapshotMalformedError(
            f"capability snapshot is not JSON: {snapshot_path}: {exc}"
        ) from exc
    if not isinstance(document, dict):
        raise CapabilitySnapshotMalformedError(
            f"capability snapshot must be a JSON object: {snapshot_path}"
        )
    keys = document.get("keys")
    if not isinstance(keys, list):
        raise CapabilitySnapshotMalformedError(
            f"capability snapshot has no `keys` list: {snapshot_path}"
        )
    return _validate_keys(keys, origin=str(snapshot_path))


def published_capability_keys(spec_path: Path | None = None) -> tuple[str, ...]:
    """Return the enumeration the retrieval service publishes, read from §B4.

    The published row is a Markdown table row whose first cell is
    :data:`SPEC_ENUMERATION_ROW_LABEL`; the keys are its backticked tokens, in
    order. Reading the enumeration back out of the spec (rather than restating
    it in code) is what makes the sync point single.
    """
    path = Path(spec_path) if spec_path is not None else published_spec_path()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CapabilityEnumerationRowMissingError(
            f"cannot read the published enumeration from {path}: {exc}"
        ) from exc
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if cells and cells[0] == SPEC_ENUMERATION_ROW_LABEL:
            keys = _BACKTICKED.findall("|".join(cells[1:]))
            return _validate_keys(keys, origin=f"{path} 「{cells[0]}」")
    raise CapabilityEnumerationRowMissingError(
        f"{path} publishes no 「{SPEC_ENUMERATION_ROW_LABEL}」 table row; "
        "the capability snapshot has nothing to sync against"
    )


def assert_snapshot_matches_published(
    *,
    snapshot_path: Path | None = None,
    spec_path: Path | None = None,
) -> tuple[str, ...]:
    """The sync point: snapshot must equal the published enumeration.

    Returns the agreed keys, or raises :class:`CapabilityEnumerationDriftError`
    printing both sides — "they differ" alone would not say which one moved.
    """
    snapshot = load_capability_keys(path=snapshot_path)
    published = published_capability_keys(spec_path=spec_path)
    if snapshot != published:
        raise CapabilityEnumerationDriftError(
            "capability enumeration drift: "
            f"snapshot={list(snapshot)} published={list(published)}"
        )
    return snapshot


def all_on_configuration(
    keys: Sequence[str] | None = None,
) -> dict[str, bool]:
    """The eval-path "all on" configuration: every snapshot key set to ``True``.

    This is the baseline the ablation matrix (T22) varies and the configuration
    SC26 compares the product path against.
    """
    resolved = tuple(keys) if keys is not None else load_capability_keys()
    return {key: True for key in resolved}


def validate_capability_configuration(
    configuration: Mapping[str, Any],
    *,
    keys: Sequence[str] | None = None,
) -> dict[str, bool]:
    """Return ``configuration`` normalized to booleans over the 8 snapshot keys.

    An eval configuration has to decide every capability explicitly: silently
    defaulting a missing key would make an ablation run's meaning depend on
    what the caller forgot.
    """
    resolved = tuple(keys) if keys is not None else load_capability_keys()
    unknown = sorted(set(configuration) - set(resolved))
    if unknown:
        raise UnknownCapabilityError(
            f"capability configuration names keys outside the snapshot: {unknown}"
        )
    missing = [key for key in resolved if key not in configuration]
    if missing:
        raise CapabilityConfigurationIncompleteError(
            f"capability configuration does not decide: {missing}"
        )
    return {key: bool(configuration[key]) for key in resolved}
