"""Structured paragraph-level cross-period redline host tool.

The tool compares frozen extracted page text, not model prose. Every
paragraph change keeps the source locator(s) that produced it, so the
workspace can render a redline without treating the diff as citation
evidence or inventing a numeric result.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from fin.corpus.errors import IngestMissingError
from fin.corpus.extracted import ExtractedPage, Locator, load_extracted_tree


_DATA_DIR = Path(__file__).resolve().parents[4] / "data" / "corpus"
_DEFAULT_MANIFEST = _DATA_DIR / "manifest.json"
_DEFAULT_EXTRACTED = _DATA_DIR / "extracted"


def _empty_redline(
    before_document_id: str, after_document_id: str
) -> dict[str, Any]:
    return {
        "schema": "fin.redline.v1",
        "kind": "paragraph_redline",
        "before": {"document_id": before_document_id},
        "after": {"document_id": after_document_id},
        "changes": [],
        "summary": {"unchanged": 0, "changed": 0, "added": 0, "removed": 0},
        "locators": [],
    }


def _locator_dict(locator: Locator) -> dict[str, Any]:
    return {
        "document_id": locator.document_id,
        "version": locator.version,
        "unit": locator.unit,
    }


def _paragraphs(
    pages: tuple[ExtractedPage, ...],
) -> list[tuple[str, Locator]]:
    paragraphs: list[tuple[str, Locator]] = []
    for page in pages:
        for raw in re.split(r"\n\s*\n", page.text.strip()):
            text = re.sub(r"\s+", " ", raw).strip()
            if text:
                paragraphs.append((text, page.locator))
    return paragraphs


def _change(
    *,
    status: str,
    index: int,
    before: tuple[str, Locator] | None,
    after: tuple[str, Locator] | None,
) -> dict[str, Any]:
    return {
        "paragraph_index": index,
        "status": status,
        "before": before[0] if before else None,
        "after": after[0] if after else None,
        "before_locator": _locator_dict(before[1]) if before else None,
        "after_locator": _locator_dict(after[1]) if after else None,
    }


def _build_redline(
    before_document_id: str,
    after_document_id: str,
    before_version: str,
    after_version: str,
    before_pages: tuple[ExtractedPage, ...],
    after_pages: tuple[ExtractedPage, ...],
) -> dict[str, Any]:
    before = _paragraphs(before_pages)
    after = _paragraphs(after_pages)
    changes: list[dict[str, Any]] = []
    counts = {"unchanged": 0, "changed": 0, "added": 0, "removed": 0}
    locators: list[dict[str, Any]] = []
    seen_locators: set[tuple[str, str, int]] = set()

    def add_locator(entry: tuple[str, Locator] | None) -> None:
        if entry is None:
            return
        locator = entry[1]
        key = (locator.document_id, locator.version, locator.unit)
        if key not in seen_locators:
            seen_locators.add(key)
            locators.append(_locator_dict(locator))

    index = 0
    matcher = SequenceMatcher(
        a=[text for text, _ in before],
        b=[text for text, _ in after],
        autojunk=False,
    )
    for opcode, a_start, a_end, b_start, b_end in matcher.get_opcodes():
        if opcode == "equal":
            for before_entry, after_entry in zip(
                before[a_start:a_end], after[b_start:b_end]
            ):
                changes.append(
                    _change(
                        status="unchanged",
                        index=index,
                        before=before_entry,
                        after=after_entry,
                    )
                )
                counts["unchanged"] += 1
                add_locator(before_entry)
                add_locator(after_entry)
                index += 1
            continue
        if opcode == "replace":
            paired = min(a_end - a_start, b_end - b_start)
            for offset in range(paired):
                before_entry = before[a_start + offset]
                after_entry = after[b_start + offset]
                changes.append(
                    _change(
                        status="changed",
                        index=index,
                        before=before_entry,
                        after=after_entry,
                    )
                )
                counts["changed"] += 1
                add_locator(before_entry)
                add_locator(after_entry)
                index += 1
            a_start += paired
            b_start += paired
        if opcode in ("replace", "delete"):
            for before_entry in before[a_start:a_end]:
                changes.append(
                    _change(
                        status="removed",
                        index=index,
                        before=before_entry,
                        after=None,
                    )
                )
                counts["removed"] += 1
                add_locator(before_entry)
                index += 1
        if opcode in ("replace", "insert"):
            for after_entry in after[b_start:b_end]:
                changes.append(
                    _change(
                        status="added",
                        index=index,
                        before=None,
                        after=after_entry,
                    )
                )
                counts["added"] += 1
                add_locator(after_entry)
                index += 1

    return {
        "redline": {
            "schema": "fin.redline.v1",
            "kind": "paragraph_redline",
            "before": {
                "document_id": before_document_id,
                "version": before_version,
            },
            "after": {
                "document_id": after_document_id,
                "version": after_version,
            },
            "changes": changes,
            "summary": counts,
            "locators": locators,
        }
    }


def redline(
    before_document_id: str,
    before_version: str,
    after_document_id: str,
    after_version: str,
    *,
    manifest_path: Path | None = None,
    extracted_root: Path | None = None,
) -> dict[str, Any]:
    """Return a locator-bound paragraph redline for two filings.

    Unknown document/version inputs return ``unknown_locator``. A
    broken extracted tree returns the explicit ``analysis_unavailable``
    exit rather than an empty redline that could be mistaken for no
    filing change.
    """
    empty = _empty_redline(before_document_id, after_document_id)
    manifest = manifest_path or _DEFAULT_MANIFEST
    extracted = extracted_root or _DEFAULT_EXTRACTED
    try:
        tree = load_extracted_tree(manifest, extracted)
        before_pages = tree.get(before_document_id)
        after_pages = tree.get(after_document_id)
        if (
            not before_pages
            or not after_pages
            or before_pages[0].locator.version != before_version
            or after_pages[0].locator.version != after_version
        ):
            return {"error": "unknown_locator", "redline": empty}
        return _build_redline(
            before_document_id,
            after_document_id,
            before_version,
            after_version,
            before_pages,
            after_pages,
        )
    except (IngestMissingError, OSError, KeyError, TypeError, ValueError):
        return {"error": "analysis_unavailable", "redline": empty}


__all__ = ["redline"]
