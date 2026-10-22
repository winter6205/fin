#!/usr/bin/env python3
"""Count tracker references in a publishable tree.

A tracker reference is a link to a tracker entry, a tracker word that
introduces a number, or a bare number the tree's own history names as a
tracker entry. The report separates those from numbers that count
something local, which must survive.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tracker_scrub import (  # noqa: E402
    TRACKER_URL,
    WORD_TRACKER_REF,
    BARE_HASH_NUMBER,
    HTML_ENTITY,
    WORD_BEFORE_HASH,
    word_is_internal,
)

REPO = Path(__file__).resolve().parent.parent
REVISION = sys.argv[1] if len(sys.argv) > 1 else "HEAD"


def run(args: list[str]) -> str:
    return subprocess.run(
        args, cwd=REPO, capture_output=True, text=True, check=True
    ).stdout


def known_numbers(paths: list[str]) -> set[int]:
    """Every number the publishable tree names as a tracker entry.

    Only publishable content counts. A number that only a file the
    release boundary excludes names is not a tracker reference in the
    published tree, because that file is not published.
    """
    numbers: set[int] = set()
    for path in paths:
        result = subprocess.run(
            ["git", "show", f"{REVISION}:{path}"],
            cwd=REPO,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            continue
        content = result.stdout.decode("utf-8", "replace")
        for match in TRACKER_URL.finditer(content):
            numbers.add(int(match.group("number")))
        for match in WORD_TRACKER_REF.finditer(content):
            if match.group("number"):
                numbers.add(int(match.group("number")))
    return numbers


def publishable_paths() -> list[str]:
    """The paths the release boundary publishes.

    The boundary names directories and individual files. Both forms
    exclude a path, and a directory excludes everything under it.
    """
    rules = Path(".claude/rules/open-source-release.md").read_text(encoding="utf-8")
    excluded: list[str] = []
    section = False
    for line in rules.splitlines():
        if line.startswith("## Do not publish"):
            section = True
            continue
        if section and line.startswith("## "):
            break
        if not section:
            continue
        stripped = line.strip()
        if not stripped.startswith("- `") or not stripped.endswith("`"):
            continue
        item = stripped[3:-1]
        if "/" not in item and not item.endswith(".md"):
            # A bare name is a file, not a directory.
            excluded.append(item)
            continue
        excluded.append(item)
    tracked = run(["git", "ls-tree", "-r", "--name-only", REVISION]).splitlines()
    result = []
    for path in tracked:
        skip = False
        for item in excluded:
            base = item.rstrip("/")
            if path == base or path.startswith(base + "/"):
                skip = True
                break
        if not skip:
            result.append(path)
    return result


def main() -> int:
    paths = publishable_paths()
    known = known_numbers(paths)
    tracker_hits = 0
    internal_kept = 0
    examples: list[str] = []
    for path in paths:
        result = subprocess.run(
            ["git", "show", f"{REVISION}:{path}"],
            cwd=REPO,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            continue
        text = result.stdout.decode("utf-8", "replace")
        for match in TRACKER_URL.finditer(text):
            tracker_hits += 1
            examples.append(f"{path}: url {match.group(0)[:70]}")
        for match in WORD_TRACKER_REF.finditer(text):
            if match.group("number") and int(match.group("number")) in known:
                tracker_hits += 1
                examples.append(f"{path}: word {match.group(0)[:50]}")
        for match in BARE_HASH_NUMBER.finditer(text):
            number = int(match.group("number"))
            if number not in known:
                continue
            start = match.start()
            if HTML_ENTITY.search(text[max(0, start - 1) : start + 1]):
                continue
            before = text[max(0, start - 60) : start]
            internal = WORD_BEFORE_HASH.search(before)
            if internal and word_is_internal(internal.group("word")):
                internal_kept += 1
                continue
            tracker_hits += 1
            examples.append(f"{path}: bare {match.group(0)}")
    print(f"publishable files: {len(paths)}")
    print(f"known tracker numbers: {len(known)}")
    print(f"TRACKER REFERENCES: {tracker_hits}")
    print(f"internal numbers kept: {internal_kept}")
    for line in examples[:15]:
        print(f"  {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
