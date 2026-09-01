"""Shared paths and constants for the capability snapshot and scanner."""

from __future__ import annotations

from pathlib import Path

CAPABILITY_COUNT = 8

SPEC_ENUMERATION_ROW_LABEL = "消融的 8 个能力项"

_RETRIEVE_DIR = Path(__file__).resolve().parent.parent


def project_root() -> Path:
    """Repository root (``src/fin/retrieve/capabilities/`` → three up)."""
    return _RETRIEVE_DIR.parents[2]


CAPABILITY_SNAPSHOT_PATH = (
    project_root() / "data" / "retrieval" / "capability_keys.json"
)


def published_spec_path() -> Path:
    """The spec carrying the retrieval service's published enumeration."""
    return project_root() / "specs" / "corpus-retrieval.md"
