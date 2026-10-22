#!/usr/bin/env python3
"""Sync the private development checkout onto the public open-source mirror.

The private checkout and the public mirror are two different repositories
with two different jobs. The private one holds development data, campaign
drivers and recorded results; the public one must not. This script performs
that one-way publication and nothing else.

Three inputs define a publication, and all three live outside this file:

1. The SOURCE tree — the private checkout at a given revision.
2. The EXCLUSION policy — the paths that must never be published, taken from
   `.claude/rules/open-source-release.md` at that revision.
3. The PUBLISHED tree — the checkout that already carries the public
   README, `.env.example` and the release-side `.gitignore` block.

The exclusion policy is read from the source checkout's own rule file rather
than duplicated here, so the two cannot drift: the rule file is the authority
and this script follows it. It is read at the published revision, so the rule
file the mirror carries and the boundary this tool enforces are the same
text. The release-side ignore rules are read from the published checkout's
`.gitignore` for the same reason: that file is the authority for what a
source sync must not restore.

Three properties matter more than convenience:

- The source revision is the baseline. The public tree must show the tree the
  source shows, so a path the source no longer carries leaves the public tree:
  a file the source moved or dropped is not public any more. Every removal is
  reported, so it never happens in silence.
- The published world's own files win. Where the published tree and the
  source revision both carry a path, the published copy is the one that
  reaches the mirror: the published README is the public-facing variant.
- The sync must be reproducible. Running it twice on the same source revision
  produces the same tree, so a reviewer can diff two runs instead of
  trusting one.

The script never publishes on its own. It builds a candidate tree, runs the
pre-publication checks that `.claude/rules/security-boundaries.md` mandates,
prints the full inventory of what it would publish, and writes that inventory
to a manifest file. Publication is an explicit `--push`, which the operator
runs after reviewing that output.

Usage:

    scripts/sync-open-source.py --published /path/to/published/checkout
    scripts/sync-open-source.py --published ... --list
    scripts/sync-open-source.py --published ... --manifest /tmp/inventory.txt
    scripts/sync-open-source.py --published ... --push
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

DEFAULT_MIRROR = "https://github.com/winter6205/fin.git"

# The rule file that states the publication boundary. It lives in the source
# checkout because the source checkout is where the boundary is decided; a
# second copy in this script would be a copy that drifts.
POLICY_RULE_FILE = ".claude/rules/open-source-release.md"

# Paths that hold the published world's own content. They are absent from the
# source tree by design, so a source-driven sync would delete them; they are
# restored from the published tree instead, and they win over the source copy
# of the same path.
OVERLAY_PREFIXES = (".cursor/agents/",)

# The published environment template and the public-facing README. The source
# tree ignores `.env*` and therefore cannot carry the template, and its README
# describes the private repository's own routing instead of the public one.
OVERLAY_EXACT = (".env.example", "README.md")

# Paths the public mirror must LOSE. The private checkout keeps `plans/`
# entirely local (its own `.gitignore` and AGENTS.md hard boundary 5), and the
# published copies carry the operator's machine-local paths, so the mirror must
# not carry them either. A path here is dropped from the candidate and
# reported as a removal from the published tree, never silently.
REMOVED_FROM_PUBLISHED_PREFIXES = ("plans/",)

# The published `.gitignore` marks its release-side block with this phrase.
# That block states what a source sync must not restore; the rules come from
# there instead of from a second copy of them inside this script.
RELEASE_IGNORE_MARKER = "source sync"

# A machine-local absolute path in a published file is a leak of the
# operator's own machine layout into a public repository. Each pattern names
# a location whose real value identifies the operator: a home directory, a
# macOS home, or a mounted drive that is not the Windows system drive.
ABSOLUTE_PATH_PATTERNS = (
    re.compile(rb"/home/([A-Za-z0-9._-]+)/"),
    re.compile(rb"/Users/([A-Za-z0-9._-]+)/"),
    re.compile(rb"/mnt/([a-z])/"),
)

# A Windows drive path. The drive letter is upper case, as Windows writes
# it, and the path names a file: either a file on the drive root or a path
# with a directory component. A bare drive root is not a path, and an escape
# sequence is not one either: a letter and a colon followed by an escape is
# a newline in a string literal, not a drive. A single backslash is what a
# path carries, so the pattern reads one.
#
# The examples are spelled without a literal backslash so that this file
# does not match the very pattern it defines.
WINDOWS_PATH_PATTERN = re.compile(
    rb"[A-Z]:\\(?:[A-Za-z0-9_.-]+\.[A-Za-z0-9]+"
    rb"|[A-Za-z0-9._-][A-Za-z0-9._ -]*(?:\\[A-Za-z0-9._ -]+)+)"
)

# The account segment of a Windows path, so the placeholder rule can apply
# to it as it does to a POSIX home directory.
WINDOWS_USER_PATTERN = re.compile(rb"[A-Z]:\\Users\\([A-Za-z0-9._-]+)\\")

# Home directories that stand for "somebody else's machine" in a test or a
# fixture. They are placeholders, not the operator's own layout.
PLACEHOLDER_HOME_NAMES = frozenset(
    {
        "me",
        "operator",
        "secret",
        "user",
        "username",
        "test",
        "tests",
        "example",
        "someone",
        "tmp",
        "temp",
    }
)

# Mounted drives that name a conventional location rather than the operator's
# own layout. `C:` is the Windows system drive, so a WSL path under it names
# a standard install, not a personal directory.
PLACEHOLDER_MOUNT_NAMES = frozenset({"c"})

# A file that only exists to carry local secrets must never be published.
SECRET_FILE_PATTERNS = (
    re.compile(r"(^|/)\.env$"),
    re.compile(r"(^|/)\.env\.(?!example|sample)"),
    re.compile(r"\.(pem|key|p12|pfx|keystore|jks)$"),
    re.compile(r"(^|/)id_rsa(\.pub)?$"),
)

# The rule file states the boundary under one `## Do not publish` heading. The
# bullets under it are grouped by a plain-text sub-heading; every bullet in
# that section is excluded, whatever the sub-heading says.
POLICY_SECTION_HEADING = "do not publish"

# A line in the exclusion section that is neither a bullet nor a sub-category
# label must not silently name a path the parser did not read. These are the
# shapes that mark such a line: a path separator, an inline code span, a glob,
# a Windows separator, or a name with a file extension. A line that carries
# none of them is prose, and prose cannot hide an exclusion.
PATH_LIKE_CHARACTERS = ("/", "`", "*", "\\")
FILENAME_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]+\.[A-Za-z0-9]{1,8}")

# A path inside a `.gitignore` comment: a name with at least one separator,
# such as `.cursor/skills` or `scripts/sync-arthurpower-cursor.sh`.
COMMENT_PATH_PATTERN = re.compile(r"(?<![\w./-])([\w.-]+(?:/[\w.-]+)+)")

# The internal-name denylist lives outside the repository, in the releases
# workspace that the security rule file names. The file itself is not in any
# repository, so it is never published and never printed.
DENYLIST_ENV_VAR = "RELEASES_DIR"
DENYLIST_FILE_NAME = "internal-patterns.txt"

# The shell form the security rule file prints. The tool names the file
# this way in its output: the environment variable's value is never printed.
DENYLIST_REFERENCE = '"${' + DENYLIST_ENV_VAR + ":?}/" + DENYLIST_FILE_NAME + '"'

# The broad path scan the security rule file mandates. Its regex is wider
# than the tool's own scan on purpose: it names the internal release
# directories and every home directory that is not one of the two published
# placeholder forms, so the tool classifies the hits with its own
# placeholder-aware scan and reports both. The rule file owns this regex:
# when it changes, this constant follows it.
BROAD_PATH_REGEX = (
    r"projects/release[s]|/home/(?!operator/)[A-Za-z0-9._-]+/|"
    r"/Users/[A-Za-z0-9._-]+/|/mnt/(?!c/)[a-z]/|\b[A-Z]:\\"
)

# The environment template names the rule file's fourth check accepts.
ENV_TEMPLATE_PATTERN = re.compile(r"\.env\.(example|sample)$")

# The rule file's history check searches the published history in batches,
# the way `xargs -n 100` does.
HISTORY_BATCH_SIZE = 100


class SyncError(RuntimeError):
    """A condition that must stop the sync before anything is published."""


@dataclass
class Policy:
    """The publication boundary, parsed from the source checkout's rule file."""

    excluded_prefixes: list[str] = field(default_factory=list)
    excluded_exact: list[str] = field(default_factory=list)

    def excludes(self, path: str) -> bool:
        if path in self.excluded_exact:
            return True
        for prefix in self.excluded_prefixes:
            trimmed = prefix.rstrip("/")
            if path == trimmed or path.startswith(trimmed + "/"):
                return True
        return False


@dataclass
class ReleaseRules:
    """The release-side ignore rules, parsed from the published checkout.

    A path is excluded together with everything under it: the rules name
    directories as well as single files, and a rule that names a directory
    without a trailing slash must still keep its contents out.
    """

    excluded_paths: list[str] = field(default_factory=list)

    def excludes(self, path: str) -> bool:
        for excluded in self.excluded_paths:
            if path == excluded or path.startswith(excluded + "/"):
                return True
        return False


@dataclass
class Report:
    """What the sync did, so the operator can review it before publishing."""

    published: list[str] = field(default_factory=list)
    published_new: list[str] = field(default_factory=list)
    excluded: list[str] = field(default_factory=list)
    removed_from_published: list[str] = field(default_factory=list)
    overlay: list[str] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)


@dataclass
class CheckResult:
    """One mandated pre-publication check and what it found."""

    number: int
    name: str
    command: str
    status: str
    output: str = ""
    exit_status: int | None = None
    reason: str = ""
    hits: list[tuple[str, str]] = field(default_factory=list)

    @property
    def blocking(self) -> bool:
        """Does this check stop the publication?"""
        return self.status == "fail"


def run(
    args: Sequence[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    """Run a git command in a checkout and return its raw output."""
    result = subprocess.run(
        ["git", "-C", str(cwd) if cwd else ".", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and result.returncode:
        raise SyncError(
            f"git {' '.join(args)} failed ({result.returncode}): "
            f"{result.stderr.decode('utf-8', 'replace').strip()}"
        )
    return result


def run_command(
    args: Sequence[str], *, cwd: Path
) -> subprocess.CompletedProcess[bytes]:
    """Run a command in a directory and return its raw output."""
    return subprocess.run(
        list(args),
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def git_text(args: Sequence[str], *, cwd: Path | None = None) -> str:
    return run(args, cwd=cwd).stdout.decode("utf-8", "replace")


def read_text_file(path: Path, *, description: str) -> str:
    """Read a text file, turning an unreadable file into a `SyncError`.

    A missing file is a different condition and is reported by the caller;
    an unreadable file must not surface as a traceback.
    """
    try:
        return path.read_text(encoding="utf-8")
    except OSError as error:
        raise SyncError(f"{description} is unreadable: {path} ({error})") from error


def copy_file(origin: Path, destination: Path) -> None:
    """Copy a file, turning an unreadable source into a `SyncError`."""
    try:
        shutil.copy2(origin, destination)
    except OSError as error:
        raise SyncError(f"cannot copy {origin} to {destination} ({error})") from error


def load_policy(source_root: Path, revision: str) -> Policy:
    """Read the publication boundary from the rule file at `revision`.

    The rule file is read from the revision that is about to be published, not
    from the working tree: the published rule file and the enforced boundary
    must be the same text, or the mirror would carry a boundary it does not
    state. A revision that has no rule file has no boundary, so the sync stops.
    """
    location = f"{revision}:{POLICY_RULE_FILE}"
    result = run(["show", location], cwd=source_root, check=False)
    if result.returncode:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise SyncError(
            f"publication policy is missing at {location}"
            + (f": {detail}" if detail else "")
        )
    return parse_policy(result.stdout.decode("utf-8", "replace"), origin=location)


def is_sub_category_label(line: str) -> bool:
    """Is this line a plain-text sub-category label rather than a path?

    The rule file groups its bullets under labels such as `Evaluation
    datasets:`. A label ends in a colon and carries nothing path-like.
    """
    return line.endswith(":") and not contains_path_like_text(line)


def contains_path_like_text(line: str) -> bool:
    """Could this line name a path?

    A separator, an inline code span, a glob, a Windows separator, or a name
    with a file extension: any of them means the line may carry an exclusion
    this parser did not read.
    """
    if any(character in line for character in PATH_LIKE_CHARACTERS):
        return True
    return FILENAME_TOKEN_PATTERN.search(line) is not None


def parse_policy(rule_text: str, *, origin: str) -> Policy:
    """Parse the publication boundary out of the rule file text.

    The rule file states the boundary as bullet lists under the heading that
    names what may not be published. Everything under that heading, down to
    the next heading, is excluded; anything the section does not name is
    publishable, so the parse is deliberately narrow.

    The parse fails closed: inside the exclusion section a line that is
    neither a bullet nor a sub-category label must not name a path in a form
    this parser did not read, because a silently skipped line is a silently
    widened boundary. Such a line stops the sync. A line that carries no
    path-like text is prose, and prose cannot hide an exclusion.
    """
    policy = Policy()
    in_section = False
    for number, raw_line in enumerate(rule_text.splitlines(), start=1):
        line = raw_line.strip()
        if line.startswith("#"):
            heading = line.lstrip("#").strip().lower()
            in_section = heading == POLICY_SECTION_HEADING
            continue
        if not in_section:
            continue
        if not line:
            continue
        if line.startswith("- "):
            entry = line[2:].strip().strip("`")
            if not entry:
                continue
            if "*" in entry:
                # A glob is not a literal path. The rule file names one path
                # per bullet in every case we have seen; a glob means the
                # boundary is expressed in a form this parser cannot honour
                # exactly, so it must stop the sync rather than be silently
                # narrowed.
                raise SyncError(
                    f"publication policy uses a glob ({entry}) in {origin}; "
                    "resolve it to explicit paths before publishing"
                )
            if entry.endswith("/"):
                # A trailing slash names a directory: everything under it is
                # excluded.
                policy.excluded_prefixes.append(entry.rstrip("/"))
            else:
                policy.excluded_exact.append(entry)
            continue
        if is_sub_category_label(line):
            continue
        if contains_path_like_text(line):
            raise SyncError(
                f"publication policy line {number} in {origin} is not a bullet "
                f"and may name a path this parser did not read: {line!r}"
            )
    if not policy.excluded_prefixes and not policy.excluded_exact:
        raise SyncError(f"publication policy in {origin} named no exclusions")
    return policy


def source_paths(source_root: Path, revision: str) -> list[str]:
    """The tracked paths of the source revision."""
    output = git_text(["ls-tree", "-r", "--name-only", revision], cwd=source_root)
    return [line for line in output.splitlines() if line]


def published_paths(published_root: Path) -> list[str]:
    """The tracked paths of the published checkout."""
    output = git_text(["ls-files"], cwd=published_root)
    return [line for line in output.splitlines() if line]


def is_secret_path(path: str) -> bool:
    return any(pattern.search(path) for pattern in SECRET_FILE_PATTERNS)


def is_removed_from_published(path: str) -> bool:
    """Must this path leave the public mirror?"""
    for prefix in REMOVED_FROM_PUBLISHED_PREFIXES:
        trimmed = prefix.rstrip("/")
        if path == trimmed or path.startswith(trimmed + "/"):
            return True
    return False


def comment_block_bounds(lines: Sequence[str], number: int) -> tuple[int, int]:
    """The bounds of the contiguous run of comment lines around `number`."""
    start = number
    while start > 0 and lines[start - 1].strip().startswith("#"):
        start -= 1
    end = number
    while end + 1 < len(lines) and lines[end + 1].strip().startswith("#"):
        end += 1
    return start, end


def parse_release_rules(published_root: Path) -> ReleaseRules:
    """Read the release-side ignore rules from the published checkout.

    The published `.gitignore` states, in one marked block, what a source sync
    must not restore. That block is the authority: a copy of it inside this
    script would be a copy that drifts, and a published checkout without the
    block has lost its guard, so the sync stops instead of publishing without
    it.

    The block names its paths twice: the ignore rules cover the directories,
    and the comment names the single file that has no rule of its own. Both
    are read, so the excluded set is the set the block states.
    """
    ignore_path = published_root / ".gitignore"
    if not ignore_path.is_file():
        raise SyncError(
            f"the published checkout has no .gitignore: {ignore_path}; the "
            "release-side ignore rules live there"
        )
    text = read_text_file(ignore_path, description="the published .gitignore")
    lines = text.splitlines()

    marked = [
        number
        for number, line in enumerate(lines)
        if line.strip().startswith("#") and RELEASE_IGNORE_MARKER in line
    ]
    if not marked:
        raise SyncError(
            f"the published .gitignore has no release-side block (a comment "
            f"naming '{RELEASE_IGNORE_MARKER}'): {ignore_path}"
        )
    if len(marked) > 1:
        raise SyncError(
            f"the published .gitignore marks {len(marked)} release-side blocks; "
            f"the release-side rules must be unambiguous: {ignore_path}"
        )

    start, end = comment_block_bounds(lines, marked[0])
    section_end = end
    number = end + 1
    while number < len(lines):
        line = lines[number].strip()
        if not line or line.startswith("#"):
            break
        section_end = number
        number += 1

    rules = ReleaseRules()
    for line in lines[start : section_end + 1]:
        stripped = line.strip()
        if stripped.startswith("#"):
            candidates = COMMENT_PATH_PATTERN.findall(stripped.lstrip("#"))
        elif stripped:
            candidates = [stripped]
        else:
            continue
        for candidate in candidates:
            path = candidate.strip().rstrip("/")
            if path and path not in rules.excluded_paths:
                rules.excluded_paths.append(path)
    if not rules.excluded_paths:
        raise SyncError(
            f"the release-side block of the published .gitignore names no "
            f"path: {ignore_path}"
        )
    return rules


def is_overlay(path: str) -> bool:
    return path.startswith(OVERLAY_PREFIXES) or path in OVERLAY_EXACT


def build_candidate(
    source_root: Path,
    published_root: Path,
    revision: str,
    policy: Policy,
    release_rules: ReleaseRules,
    work_root: Path,
) -> tuple[Path, Report]:
    """Assemble the candidate publication tree.

    The candidate is the source revision, minus the excluded paths, plus the
    published world's own files. The published `.gitignore` is carried over
    because it carries the release-side rules that keep a private path out of
    a later sync.

    The published world's files are authoritative: where the published tree
    and the source revision both carry a path, the published copy wins, so a
    public-facing file is not replaced by the private checkout's variant.
    """
    report = Report()
    candidate = work_root / "candidate"
    candidate.mkdir(parents=True, exist_ok=True)

    source_files = source_paths(source_root, revision)
    published_files = set(published_paths(published_root))
    excluded: set[str] = set()

    for path in source_files:
        if is_removed_from_published(path):
            # The path must not reach the mirror at all, from either side.
            excluded.add(path)
            continue
        if (
            policy.excludes(path)
            or is_secret_path(path)
            or release_rules.excludes(path)
        ):
            excluded.add(path)
            continue
        destination = candidate / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(run(["show", f"{revision}:{path}"], cwd=source_root).stdout)

    # The published world's own files win over the source copy of the same
    # path: the published README is the public-facing variant, and the source
    # README carries the private repository's own routing.
    for path in sorted(published_files):
        if not is_overlay(path) or is_removed_from_published(path):
            continue
        origin = published_root / path
        if not origin.is_file():
            continue
        destination = candidate / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        copy_file(origin, destination)
        report.overlay.append(path)

    # The source revision is the baseline. A path the public tree carries but
    # the source revision does not is stale: the source moved it or dropped
    # it, and the public tree must show the tree the source shows. The path
    # leaves the candidate and is reported as a removal, never dropped in
    # silence.
    for path in sorted(published_files):
        if is_removed_from_published(path):
            report.removed_from_published.append(path)
            continue
        if (candidate / path).exists():
            continue
        report.removed_from_published.append(path)

    # The published `.gitignore` already carries the source rules plus the
    # release-side block; keeping it keeps a private path out of a later sync.
    published_ignore = published_root / ".gitignore"
    if published_ignore.is_file():
        copy_file(published_ignore, candidate / ".gitignore")

    report.excluded = sorted(excluded)
    report.published = sorted(
        str(path.relative_to(candidate)) for path in candidate.rglob("*") if path.is_file()
    )
    # A path the published tree already carries is public today; a path it
    # does not carry is new to the public repository.
    report.published_new = [
        path for path in report.published if path not in published_files
    ]
    return candidate, report


def is_placeholder_location(pattern_index: int, captured: bytes) -> bool:
    """Does a matched location name a placeholder rather than a real account?

    A test fixture writes `/home/operator/.iknow-memory` to mean "somebody
    else's home", and a WSL path under `/mnt/c/` names the Windows system
    drive. Neither is the operator's own layout, so neither is a leak.
    """
    name = captured.decode("utf-8", "replace").lower()
    if pattern_index == 2:
        return name in PLACEHOLDER_MOUNT_NAMES
    return name in PLACEHOLDER_HOME_NAMES


def is_placeholder_windows(matched: bytes) -> bool:
    r"""Does a Windows path name a placeholder account rather than a real one?

    A path under the `Users\me` directory is the Windows counterpart of
    `/home/operator/`: a fixture that stands for somebody else's machine.
    The example is spelled without a literal backslash so that this file
    does not match the very pattern it inspects.
    """
    user = WINDOWS_USER_PATTERN.match(matched)
    if user is None:
        return False
    return user.group(1).decode("utf-8", "replace").lower() in PLACEHOLDER_HOME_NAMES


@dataclass(frozen=True)
class Leak:
    """One machine-local absolute path, and the line it sits on."""

    line: int
    text: str


def locate_paths(contents: bytes) -> list[tuple[int, bytes, bool]]:
    """Every machine-local path the tool's patterns match in a blob.

    Each entry is the offset of the match, the matched text, and whether the
    location is a known placeholder.
    """
    located: list[tuple[int, bytes, bool]] = []
    for index, pattern in enumerate(ABSOLUTE_PATH_PATTERNS):
        for match in pattern.finditer(contents):
            located.append(
                (
                    match.start(),
                    match.group(0),
                    is_placeholder_location(index, match.group(1)),
                )
            )
    for match in WINDOWS_PATH_PATTERN.finditer(contents):
        located.append(
            (match.start(), match.group(0), is_placeholder_windows(match.group(0)))
        )
    return sorted(located, key=lambda item: item[0])


def find_leaks(contents: bytes) -> list[Leak]:
    """Every machine-local absolute path in a blob, placeholders excluded.

    Every match is reported: a file that carries three leaks carries three
    entries, because a reviewer has to see each one.
    """
    leaks: list[Leak] = []
    for offset, text, placeholder in locate_paths(contents):
        if placeholder:
            continue
        leaks.append(Leak(contents[:offset].count(b"\n") + 1, text.decode("utf-8", "replace")))
    return leaks


def scan_for_absolute_paths(candidate: Path) -> list[str]:
    """Report every machine-local absolute path the candidate carries."""
    violations: list[str] = []
    for path in sorted(candidate.rglob("*")):
        if not path.is_file():
            continue
        relative = str(path.relative_to(candidate))
        try:
            contents = path.read_bytes()
        except OSError:
            continue
        if b"\0" in contents:
            continue
        for leak in find_leaks(contents):
            violations.append(f"{relative}:{leak.line}: {leak.text}")
    return violations


def scan_for_secret_files(candidate: Path) -> list[str]:
    violations: list[str] = []
    for path in sorted(candidate.rglob("*")):
        if path.is_file() and is_secret_path(str(path.relative_to(candidate))):
            violations.append(str(path.relative_to(candidate)))
    return violations


def denylist_file() -> tuple[Path | None, str]:
    """The internal-name denylist, and why it is missing when it is.

    The security rule file names the denylist through the releases
    workspace environment variable. The variable's value is never printed:
    the rule file asks for the file and the line of a hit, not for paths.
    """
    workspace = os.environ.get(DENYLIST_ENV_VAR)
    if not workspace:
        return None, (
            f"{DENYLIST_ENV_VAR} is unset, so {DENYLIST_FILE_NAME} cannot be read "
            "and internal names cannot be screened"
        )
    path = Path(workspace) / DENYLIST_FILE_NAME
    if not path.is_file() or not os.access(path, os.R_OK):
        return None, (
            f"{DENYLIST_ENV_VAR} is set but {DENYLIST_FILE_NAME} is not readable, "
            "so internal names cannot be screened"
        )
    return path, ""


def file_and_line_only(output: str) -> str:
    """Keep the file and the line of every hit, drop the matched text.

    The security rule file asks for the file and the line only: the
    denylist holds internal names, and a hit line would carry one into the
    approval request.
    """
    kept: list[str] = []
    for line in output.splitlines():
        match = re.match(r"^(?P<path>.+?):(?P<line>\d+):", line)
        kept.append(f"{match.group('path')}:{match.group('line')}" if match else line)
    return "\n".join(kept)


def spec_hit_content(line: str) -> str:
    """The matched text of a broad-scan hit.

    The scan prints `path:line:text`; the text is what the tool classifies.
    """
    match = re.match(r"^.+?:\d+:(?P<content>.*)$", line)
    return match.group("content") if match else line


def classify_spec_hits(output: str) -> list[tuple[str, str]]:
    """Classify every hit of the broad path scan.

    The broad scan's regex is wider than the tool's own scan: it flags every
    `/home/<name>/`, including the neutral placeholders. The kind is
    "genuine leak" when the tool's own placeholder-aware scan finds a
    machine-local path in the hit, "placeholder" when that scan finds only a
    known placeholder, and "unclassified" when the hit matches a class the
    tool's own scan does not cover.
    """
    classified: list[tuple[str, str]] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        located = locate_paths(spec_hit_content(line).encode("utf-8", "replace"))
        if any(not placeholder for _, _, placeholder in located):
            kind = "genuine leak"
        elif located:
            kind = "placeholder"
        else:
            kind = "unclassified"
        classified.append((kind, line))
    return classified


def prepare_release_workspace(candidate: Path, work_root: Path) -> Path:
    """A git workspace that carries the tree that would be published.

    The security rule file's checks run in the release workspace: the
    workspace holds the files the sync would install, so the checks read the
    content that goes public. The published checkout keeps its own history,
    and the history check reads that. The published checkout itself is not
    touched before the push.
    """
    workspace = work_root / "release-workspace"
    shutil.copytree(candidate, workspace)
    run_command(["git", "init", "-q", str(workspace)], cwd=work_root)
    # `git grep` reads the tracked files, so the index has to list the
    # candidate; `-f` forces the files the published `.gitignore` names,
    # because a leak scan must not skip a file.
    run(["add", "-A", "-f"], cwd=workspace)
    return workspace


def check_denylist_working_tree(
    workspace: Path, denylist: Path | None, reason: str
) -> CheckResult:
    """The rule file's first check: the denylist against the working tree."""
    command = (
        f"test -r {DENYLIST_REFERENCE} && "
        f"git grep -n -I -i -E -f "
        f"{DENYLIST_REFERENCE} -- . ':!*.lock'"
    )
    if denylist is None:
        return CheckResult(
            1, "internal-name denylist, working tree", command, "not run", reason=reason
        )
    result = run(
        ["grep", "-n", "-I", "-i", "-E", "-f", str(denylist), "--", ".", ":!*.lock"],
        cwd=workspace,
        check=False,
    )
    output = file_and_line_only(result.stdout.decode("utf-8", "replace"))
    return CheckResult(
        1,
        "internal-name denylist, working tree",
        command,
        "fail" if output.strip() else "pass",
        output,
        result.returncode,
    )


def check_broad_paths(workspace: Path) -> CheckResult:
    """The rule file's broad path scan over the working tree.

    The scan's output is sorted: the tool's report has to be the same for
    two runs on one source revision, so a reviewer can diff two runs
    instead of reading them.
    """
    command = (
        "rg -n -uu -P --glob '!.git/' --glob '!**/node_modules/**' "
        f"--glob '!.venv/' '{BROAD_PATH_REGEX}' ."
    )
    result = run_command(
        [
            "rg",
            "-n",
            "-uu",
            "-P",
            "--glob",
            "!.git/",
            "--glob",
            "!**/node_modules/**",
            "--glob",
            "!.venv/",
            BROAD_PATH_REGEX,
            ".",
        ],
        cwd=workspace,
    )
    output = "\n".join(sorted(result.stdout.decode("utf-8", "replace").splitlines()))
    hits = classify_spec_hits(output)
    genuine = [line for kind, line in hits if kind == "genuine leak"]
    return CheckResult(
        2,
        "broad machine-local path scan",
        command,
        "fail" if genuine else "pass",
        output,
        result.returncode,
        hits=hits,
    )


def check_denylist_history(
    published_root: Path, denylist: Path | None, reason: str
) -> CheckResult:
    """The rule file's history check: the denylist against every commit."""
    command = (
        "git rev-list --all | xargs -r -n 100 git grep -l -I -E -f "
        f"{DENYLIST_REFERENCE}"
    )
    if denylist is None:
        return CheckResult(
            3, "internal-name denylist, published history", command, "not run", reason=reason
        )
    revisions = [
        revision.decode("utf-8", "replace")
        for revision in run(["rev-list", "--all"], cwd=published_root).stdout.split()
    ]
    hits: list[str] = []
    exit_status = 0
    for start in range(0, len(revisions), HISTORY_BATCH_SIZE):
        batch = revisions[start : start + HISTORY_BATCH_SIZE]
        result = run(
            ["grep", "-l", "-I", "-E", "-f", str(denylist), *batch],
            cwd=published_root,
            check=False,
        )
        exit_status = result.returncode
        hits.extend(result.stdout.decode("utf-8", "replace").splitlines())
    output = "\n".join(hits)
    return CheckResult(
        3,
        "internal-name denylist, published history",
        command,
        "fail" if output.strip() else "pass",
        output,
        exit_status,
    )


def check_env_files(workspace: Path) -> CheckResult:
    """The rule file's fourth check: a stray `.env` in the release workspace."""
    command = r"ls -a | rg '^\.env' | rg -v '\.env\.(example|sample)$'"
    names = sorted(
        entry.name for entry in workspace.iterdir() if entry.name.startswith(".env")
    )
    hits = [name for name in names if not ENV_TEMPLATE_PATTERN.search(name)]
    output = "\n".join(hits)
    return CheckResult(
        4,
        "stray .env files in the release workspace",
        command,
        "fail" if hits else "pass",
        output,
        0 if hits else 1,
    )


def print_check(check: CheckResult) -> None:
    """Print one mandated check: its command, its output and its status."""
    print(f"--- check {check.number}: {check.name} ---")
    print(f"$ {check.command}")
    if check.status == "not run":
        print(f"Not run: {check.reason}")
        return
    print(f"(exit status: {check.exit_status})")
    print(check.output if check.output.strip() else "(no output)")
    if check.number == 2:
        print(f"spec check hits: {len(check.hits)}")
        for kind in ("genuine leak", "placeholder", "unclassified"):
            lines = [line for hit_kind, line in check.hits if hit_kind == kind]
            print(f"{kind} hits: {len(lines)}")
            for line in lines:
                print(f"  {line}")


def render_inventory(report: Report) -> list[str]:
    """The full inventory of the candidate, one path per line.

    A published path is annotated with where it comes from: NEW when the
    published tree does not carry it today, KEPT when it is already public.
    """
    new_paths = set(report.published_new)
    lines = [f"=== published ({len(report.published)}) ==="]
    for path in report.published:
        lines.append(f"{'NEW ' if path in new_paths else 'KEPT'} {path}")
    lines.append(
        f"=== removed from the published tree ({len(report.removed_from_published)}) ==="
    )
    lines.extend(report.removed_from_published)
    lines.append(f"=== excluded ({len(report.excluded)}) ===")
    lines.extend(report.excluded)
    lines.append(f"=== overlay ({len(report.overlay)}) ===")
    lines.extend(report.overlay)
    return lines


def write_manifest(path: Path, lines: Sequence[str]) -> None:
    """Write the review artifact the approval request can point at."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def backup_published_head(published_root: Path) -> Path:
    """Save the published checkout's current HEAD as a git bundle.

    The reset below moves the checkout's branch pointer, so the state it
    discards must stay recoverable. AGENTS.md hard boundary 8 asks for the
    same prudence before any history rewrite.
    """
    backup_dir = published_root.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    head = git_text(["rev-parse", "HEAD"], cwd=published_root).strip()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    bundle = backup_dir / f"published-{stamp}-{head[:8]}.bundle"
    run(["bundle", "create", str(bundle), "HEAD"], cwd=published_root)
    print(f"backup bundle: {bundle}")
    return bundle


def publish(candidate: Path, published_root: Path, mirror: str, message: str) -> None:
    """Replace the published tree with the candidate and push it.

    The published checkout's history is kept: the candidate becomes a new
    commit on top of it, so a published commit is never rewritten.
    """
    # Start from the mirror's current tip so the publication is a new commit
    # on top of the published history, never a rewrite of it. A failed fetch
    # is fatal: a reset against a stale FETCH_HEAD would publish on top of
    # the wrong base and could discard work.
    run(["fetch", mirror, "main"], cwd=published_root)

    # The checkout must not carry commits the mirror does not have: the reset
    # below would discard them. Refuse instead, and name them.
    unpublished = git_text(
        ["rev-list", "--oneline", "HEAD", "--not", "FETCH_HEAD"], cwd=published_root
    ).strip()
    if unpublished:
        raise SyncError(
            "the published checkout has commits that are not on the mirror; "
            "publish or drop them before syncing:\n" + unpublished
        )

    backup_published_head(published_root)
    run(["reset", "--hard", "FETCH_HEAD"], cwd=published_root)

    # Replace the working tree with the candidate, keeping the checkout's own
    # `.git` directory.
    for entry in published_root.iterdir():
        if entry.name == ".git":
            continue
        if entry.is_dir():
            shutil.rmtree(entry)
        else:
            entry.unlink()

    for path in candidate.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(candidate)
        destination = published_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)

    run(["add", "-A"], cwd=published_root)
    status = git_text(["status", "--porcelain"], cwd=published_root)
    if not status.strip():
        print("nothing to publish: the published tree already matches the source")
        return
    run(["commit", "-m", message], cwd=published_root)
    run(["push", mirror, "HEAD:main"], cwd=published_root)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build (and optionally publish) the open-source mirror tree."
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=Path.cwd(),
        help="the private development checkout (default: the current directory)",
    )
    parser.add_argument(
        "--published",
        type=Path,
        required=True,
        help="the published checkout: it carries the overlay and the release-side ignore rules",
    )
    parser.add_argument(
        "--revision",
        default="origin/main",
        help="the source revision to publish (default: origin/main)",
    )
    parser.add_argument(
        "--mirror",
        default=DEFAULT_MIRROR,
        help=f"the public mirror URL (default: {DEFAULT_MIRROR})",
    )
    parser.add_argument(
        "--message",
        default="chore(release): sync the public mirror with the source tree",
        help="the commit message for the published commit",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="print every path the sync would publish, not only the counts",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="write the inventory to this file (default: unset, print to stdout only)",
    )
    parser.add_argument(
        "--allow-missing-denylist",
        action="store_true",
        help="publish even though the internal-name denylist is unavailable",
    )
    parser.add_argument(
        "--push",
        action="store_true",
        help="publish the candidate to the mirror; without it the sync only reports",
    )
    args = parser.parse_args(argv)

    source_root = args.source.resolve()
    if not (source_root / ".git").exists():
        print(f"error: {source_root} is not a git checkout", file=sys.stderr)
        return 1
    published_root = args.published.resolve()
    if not (published_root / ".git").exists():
        print(f"error: {published_root} is not a git checkout", file=sys.stderr)
        return 1

    try:
        policy = load_policy(source_root, args.revision)
        release_rules = parse_release_rules(published_root)
    except SyncError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory(prefix="fin-open-source-") as temporary:
        work_root = Path(temporary)
        try:
            candidate, report = build_candidate(
                source_root,
                published_root,
                args.revision,
                policy,
                release_rules,
                work_root,
            )
        except SyncError as error:
            print(f"error: {error}", file=sys.stderr)
            return 1

        report.violations = scan_for_absolute_paths(candidate) + scan_for_secret_files(candidate)

        denylist, denylist_reason = denylist_file()
        # The working tree checks read the tree that would be published; the
        # history check reads the published checkout's own history.
        workspace = prepare_release_workspace(candidate, work_root)
        checks = [
            check_denylist_working_tree(workspace, denylist, denylist_reason),
            check_broad_paths(workspace),
            check_denylist_history(published_root, denylist, denylist_reason),
            check_env_files(workspace),
        ]

        summary = [
            f"source revision:  {args.revision}",
            f"published files:  {len(report.published)}",
            f"excluded paths:   {len(report.excluded)}",
            f"removed from the published tree: {len(report.removed_from_published)}",
            f"overlay files:    {len(report.overlay)}",
            f"violations:       {len(report.violations)}",
        ]
        print("\n".join(summary))
        if report.removed_from_published:
            print(
                "\nremoved from the published tree (deleted from the public "
                "repository by this publication):"
            )
            for path in report.removed_from_published:
                print(f"  {path}")
        for violation in report.violations:
            print(f"  {violation}")

        print("\n=== pre-publication checks ===")
        for check in checks:
            print_check(check)

        if denylist is None:
            if args.allow_missing_denylist:
                print("\n!! OVERRIDE: --allow-missing-denylist was given.")
                print(
                    "!! The internal-name denylist is unavailable, so internal names "
                    "were NOT screened."
                )
                print(
                    "!! The operator accepted this risk explicitly; the approval "
                    "request must say so."
                )
            else:
                print(
                    "\n!! the internal-name denylist is unavailable: internal names "
                    "cannot be screened."
                )
                print(
                    "!! pass --allow-missing-denylist to publish anyway, and say so "
                    "in the approval request."
                )
        elif args.allow_missing_denylist:
            print(
                "\nnote: --allow-missing-denylist was given but the denylist is "
                "available, so the checks ran normally."
            )

        inventory = render_inventory(report)
        if args.list or args.manifest:
            print()
            for line in inventory:
                print(line)

        if args.manifest:
            write_manifest(
                args.manifest, [*summary, *(f"  {v}" for v in report.violations), *inventory]
            )
            print(f"\nmanifest written: {args.manifest}")

        blockers: list[str] = []
        if report.violations:
            blockers.append(
                "the candidate tree carries machine-local absolute paths or secret files"
            )
        if denylist is None and not args.allow_missing_denylist:
            blockers.append(
                "the internal-name denylist is unavailable, so internal names were "
                "not screened (--allow-missing-denylist overrides this)"
            )
        for check in checks:
            if check.blocking:
                blockers.append(f"check {check.number} ({check.name}) reported hits")

        if blockers:
            print("\nrefusing to publish:", file=sys.stderr)
            for blocker in blockers:
                print(f"  - {blocker}", file=sys.stderr)
            return 1

        if not args.push:
            print("\ndry run: nothing was published. Re-run with --push to publish.")
            return 0

        try:
            publish(candidate, published_root, args.mirror, args.message)
        except SyncError as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
