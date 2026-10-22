#!/usr/bin/env python3
"""Scrub private tracker references out of an already published git history.

The public mirror carries no GitHub issues and no pull requests, so every
pointer to a tracker number is dead there. The working tree of the source
repository is cleaned before publication, but the history that was published
earlier still holds those pointers. This tool rewrites that history.

The rules that tell a tracker reference from a number that counts something
else live in `scripts/tracker_scrub.py`. This script collects the evidence
the rules need, runs the rewrite through git-filter-repo, and reports what
changed.

A blob whose exact pre-scrub content the source repository already cleaned
is replaced by the reviewed post-scrub content, so the published history
ends up carrying the same wording the working tree was reviewed with. The
mapping comes from a cleanup commit in the source repository.

The tool never pushes. It writes a rewritten repository into a work
directory, and the caller decides what to do with it. Pushing a rewritten
history needs the owner's approval and a bundle backup first, per the
repository's hard boundaries.

Usage:

    scripts/scrub-published-history.py --published <checkout> --out <dir>
    scripts/scrub-published-history.py --published <checkout> --out <dir> \\
        --mapping-source <source repo> --mapping-commit <sha>

The mapping is optional. Without it the tool falls back to rules only.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tracker_scrub import (  # noqa: E402
    TRACKER_URL,
    WORD_TRACKER_REF,
    collect_known_numbers,
    scrub_text,
)

# The callback git-filter-repo runs. It receives the file name, the mode
# and the blob id, and returns the file name, the mode and a blob id.
#
# git-filter-repo compiles the file as the body of that function, so the
# file holds statements rather than a function definition.
CALLBACK_BODY = '''"""Replace a blob that carries a private tracker reference."""

import sys

sys.path.insert(0, {module_dir!r})

from tracker_scrub import scrub_text  # noqa: E402

KNOWN = {known!r}
MAPPING = {mapping!r}
PRIVATE_ORG = {private_org!r}
PRIVATE_REPO = {private_repo!r}
PRIVATE_HASHES = {private_hashes!r}

hex_id = blob_id.decode("ascii")
mapped = MAPPING.get(hex_id[:8])
if mapped is not None:
    contents = value.get_contents_by_identifier(mapped.encode("ascii"))
    if contents is None:
        return (filename, mode, blob_id)
    return (filename, mode, value.insert_file_with_contents(contents))

contents = value.get_contents_by_identifier(blob_id)
if contents is None or b"\\0" in contents[:8192]:
    return (filename, mode, blob_id)
try:
    text = contents.decode("utf-8")
except UnicodeDecodeError:
    return (filename, mode, blob_id)
scrubbed, count = scrub_text(text, KNOWN, PRIVATE_ORG, PRIVATE_REPO, PRIVATE_HASHES)
if count == 0:
    return (filename, mode, blob_id)
return (filename, mode, value.insert_file_with_contents(scrubbed.encode("utf-8")))
'''


def run(args: list[str], cwd: Path | None = None, check: bool = True) -> str:
    """Run a command and return its stdout."""
    result = subprocess.run(
        args, cwd=cwd, capture_output=True, text=True, check=False
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(args)}\n{result.stderr}"
        )
    return result.stdout


def read_blob(repo: Path, blob: str) -> bytes:
    """Read one blob's content."""
    result = subprocess.run(
        ["git", "cat-file", "blob", blob],
        cwd=repo,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"cannot read blob {blob}")
    return result.stdout


def all_blobs(repo: Path) -> list[str]:
    """Every blob the repository holds."""
    output = run(
        [
            "git",
            "cat-file",
            "--batch-all-objects",
            "--batch-check=%(objectname) %(objecttype)",
        ],
        cwd=repo,
    )
    return [
        line.split()[0]
        for line in output.splitlines()
        if len(line.split()) == 2 and line.split()[1] == "blob"
    ]


def collect_repository_numbers(
    repo: Path, publishable: set[str] | None
) -> set[int]:
    """Every number the published content names as a tracker entry.

    Only published content counts. A number that only a file the release
    boundary excludes names is not a tracker reference in the published
    tree, because that file is not published. The published content is
    also what the working tree's own cleanup was judged against, so the
    numbers it names are the numbers the review recognised.
    """
    numbers: set[int] = set()
    seen: set[str] = set()
    raw = run(
        ["git", "log", "--all", "--raw", "--no-abbrev", "--format=%H"], cwd=repo
    )
    for line in raw.splitlines():
        if not line.startswith(":"):
            continue
        # The raw line reads ":mode mode OLD NEW STATUS\tpath". A rename
        # line carries two paths; the first is the old name and the
        # second is the new one, and the content is the new blob.
        parts = line.split("\t")
        fields = parts[0].split()
        if len(fields) < 5:
            continue
        blob = fields[3]
        if not blob or set(blob) == {"0"}:
            # A deletion carries no content to read.
            continue
        paths = parts[1:]
        path = paths[-1] if paths else ""
        if publishable is not None and path not in publishable:
            continue
        if blob in seen:
            continue
        seen.add(blob)
        content = read_blob(repo, blob).decode("utf-8", "replace")
        numbers |= collect_known_numbers([content])
    return numbers


def publishable_paths(repo: Path, rules_file: Path | None) -> set[str] | None:
    """The paths the release boundary publishes.

    The boundary names directories and individual files. Both forms
    exclude a path, and a directory excludes everything under it. Without
    a rules file the whole repository counts as published.
    """
    if rules_file is None or not rules_file.is_file():
        return None
    excluded: list[str] = []
    section = False
    for line in rules_file.read_text(encoding="utf-8").splitlines():
        if line.startswith("## Do not publish"):
            section = True
            continue
        if section and line.startswith("## "):
            break
        if not section:
            continue
        stripped = line.strip()
        if stripped.startswith("- `") and stripped.endswith("`"):
            excluded.append(stripped[3:-1])
    if not excluded:
        return None
    tracked = run(["git", "ls-tree", "-r", "--name-only", "HEAD"], cwd=repo).splitlines()
    result = set()
    for path in tracked:
        skip = False
        for item in excluded:
            base = item.rstrip("/")
            if path == base or path.startswith(base + "/"):
                skip = True
                break
        if not skip:
            result.add(path)
    return result


def dropped_private_hashes(source_repo: Path, commit: str) -> list[str]:
    """The private commit names the cleanup commit dropped.

    The cleanup commit removed a private commit name from a comment
    because the name points at a commit in the private repository. The
    published history carries the same comment in an older form, so the
    same names go. A name that the cleanup kept stays, because the
    review accepted it.
    """
    pattern = re.compile(r"\b[0-9a-f]{7,40}\b")
    raw = run(
        ["git", "show", commit, "--raw", "--format=", "--no-abbrev"], cwd=source_repo
    )
    paths = set()
    for line in raw.splitlines():
        if not line.startswith(":"):
            continue
        parts = line.split("\t")
        if len(parts) > 1:
            paths.add(parts[1])
    dropped: set[str] = set()
    for path in sorted(paths):
        before = run(
            ["git", "show", f"{commit}^:{path}"], cwd=source_repo, check=False
        )
        after = run(
            ["git", "show", f"{commit}:{path}"], cwd=source_repo, check=False
        )
        if not before or not after:
            continue
        before_names = set(pattern.findall(before))
        after_names = set(pattern.findall(after))
        dropped |= before_names - after_names
    return sorted(name for name in dropped if len(name) >= 7)


def build_mapping(source_repo: Path, commit: str) -> dict[str, str]:
    """The reviewed blob mapping of a tree-level cleanup commit.

    The commit replaced pre-cleanup blobs with post-cleanup blobs. The
    mapping is keyed by the pre-cleanup blob and valued by the post-cleanup
    blob, so the published history ends up with the reviewed wording.
    """
    output = run(["git", "show", commit, "--raw", "--format="], cwd=source_repo)
    mapping: dict[str, str] = {}
    for line in output.splitlines():
        if not line.startswith(":"):
            continue
        # The raw line reads ":mode mode OLD NEW STATUS\tpath". The first
        # field carries the old mode and the new mode glued together, so
        # the blob ids are the third and fourth fields.
        fields = line.split("\t")[0].split()
        if len(fields) < 5:
            continue
        old, new = fields[2], fields[3]
        if old != new and len(new) >= 7 and len(old) >= 7:
            mapping[old] = new
    return mapping


def write_callback(
    path: Path,
    known: set[int],
    mapping: dict[str, str],
    module_dir: Path,
    private_org: str,
    private_repo: str,
    private_hashes: list[str],
) -> None:
    """Write the git-filter-repo callback that performs the rewrite."""
    path.write_text(
        CALLBACK_BODY.format(
            module_dir=str(module_dir),
            known=sorted(known),
            mapping=mapping,
            private_org=private_org,
            private_repo=private_repo,
            private_hashes=tuple(private_hashes),
        )
    )


def read_denylist(path: Path) -> list[str]:
    """The internal names the denylist file holds, one per line."""
    names: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        name = line.strip()
        if name:
            names.append(name)
    return names


def private_identifiers(denylist: Path, repo_name: str) -> tuple[str, str]:
    """The private organisation and repository the denylist names.

    The names are read from the denylist rather than written into this
    script, because this script is published and the names are internal.
    """
    for name in read_denylist(denylist):
        if name and "/" not in name:
            return name, repo_name
    return "", repo_name


def report_scrub(
    out: Path,
    published: Path,
    mapping_source: Path | None,
    mapping_commit: str | None,
    known: set[int],
    private_org: str,
) -> None:
    """Report what the rewrite left behind.

    The report is the evidence for the push, so every count is printed.
    """
    print("--- scrub report ---")
    print(f"commits: {run(['git', 'rev-list', '--all', '--count'], cwd=out).strip()}")

    org_hits = 0
    url_hits = 0
    word_hits = 0
    for line in run(
        ["git", "log", "--all", "--raw", "--no-abbrev", "--format=%H"], cwd=out
    ).splitlines():
        if not line.startswith(":"):
            continue
        parts = line.split("\t")
        fields = parts[0].split()
        if len(fields) < 5:
            continue
        blob = fields[3]
        if not blob or set(blob) == {"0"}:
            continue
        content = read_blob(out, blob).decode("utf-8", "replace")
        if private_org and private_org.lower() in content.lower():
            org_hits += 1
        url_hits += len(TRACKER_URL.findall(content))
        word_hits += len(WORD_TRACKER_REF.findall(content))
    print(f"blobs still naming the private organisation: {org_hits}")
    print(f"tracker links left: {url_hits}")
    print(f"tracker words left: {word_hits}")

    if mapping_source and mapping_commit:
        old = run(
            ["git", "rev-list", "--all", "--count"], cwd=published
        ).strip()
        print(f"commits before: {old}")

    if mapping_source and mapping_commit:
        # The published tree has to carry the same content the reviewed
        # cleanup produced, or the rewrite changed more than it had to.
        tip = run(
            ["git", "rev-parse", "refs/heads/main"], cwd=out
        ).strip()
        source_paths = {}
        for line in run(
            ["git", "ls-tree", "-r", mapping_commit], cwd=mapping_source
        ).splitlines():
            meta, path = line.split("\t", 1)
            source_paths[path] = meta.split()[2]
        new_paths = {}
        for line in run(["git", "ls-tree", "-r", tip], cwd=out).splitlines():
            meta, path = line.split("\t", 1)
            new_paths[path] = meta.split()[2]
        common = set(source_paths) & set(new_paths)
        differing = sorted(
            path for path in common if source_paths[path] != new_paths[path]
        )
        print(f"published paths in both trees: {len(common)}")
        print(f"paths whose content differs from the reviewed cleanup: {len(differing)}")
        for path in differing[:10]:
            print(f"    {path}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Scrub private tracker references out of a published history."
    )
    parser.add_argument("--published", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--mapping-source", type=Path)
    parser.add_argument("--mapping-commit")
    parser.add_argument("--report", type=Path)
    parser.add_argument(
        "--callback-out",
        type=Path,
        help="write the git-filter-repo callback here and stop",
    )
    parser.add_argument(
        "--filter-repo",
        default="git-filter-repo",
        help="the git-filter-repo executable to run",
    )
    parser.add_argument(
        "--denylist",
        type=Path,
        help="the internal-name denylist; it names the private organisation",
    )
    parser.add_argument(
        "--rules",
        type=Path,
        help="the release boundary rules; they name the paths that are published",
    )
    args = parser.parse_args()

    published = args.published.resolve()
    out = args.out.resolve()

    mapping: dict[str, str] = {}
    mapping_source: Path | None = None
    if args.mapping_source and args.mapping_commit:
        mapping_source = args.mapping_source.resolve()
        mapping = build_mapping(mapping_source, args.mapping_commit)

    publishable = publishable_paths(published, args.rules)
    if publishable is None:
        print("no release boundary rules given: every path counts as published")
    else:
        print(f"published paths: {len(publishable)}")

    # Every path in the published history is public, so a number that any
    # of them names as a tracker entry is a tracker number there.
    known = collect_repository_numbers(published, None)
    print(f"known tracker numbers: {len(known)}")
    print(f"reviewed blob mappings: {len(mapping)}")

    private_org = ""
    private_repo = ""
    private_hashes: list[str] = []
    if args.denylist:
        private_org, private_repo = private_identifiers(
            args.denylist.resolve(), published.name
        )
        print(f"private organisation: {'named' if private_org else 'not named'}")
    if args.mapping_source and args.mapping_commit:
        private_hashes = dropped_private_hashes(
            args.mapping_source.resolve(), args.mapping_commit
        )
        print(f"private commit names dropped by the review: {len(private_hashes)}")

    module_dir = Path(__file__).resolve().parent
    callback = out.parent / "scrub-callback.py"
    write_callback(
        callback, known, mapping, module_dir, private_org, private_repo, private_hashes
    )

    if args.callback_out:
        args.callback_out.write_text(callback.read_text())
        print(f"callback written to {args.callback_out}")
        return 0

    if out.exists():
        shutil.rmtree(out)
    run(["git", "clone", "--no-local", str(published), str(out)], check=True)

    # The reviewed replacement blobs live in the source repository, so the
    # clone has to carry them before the rewrite can reach them.
    if mapping:
        copied = 0
        for new in set(mapping.values()):
            content = read_blob(mapping_source, new)
            process = subprocess.run(
                ["git", "hash-object", "-w", "--stdin"],
                cwd=out,
                input=content,
                capture_output=True,
                check=True,
            )
            written = process.stdout.decode().strip()
            if not written.startswith(new[:8]):
                raise RuntimeError(
                    f"blob {new} was written as {written}; the content differs"
                )
            copied += 1
        print(f"copied {copied} reviewed blobs into the clone")

    filter_repo = shutil.which(args.filter_repo)
    if filter_repo is None:
        raise RuntimeError(
            f"{args.filter_repo} is not on the PATH; install git-filter-repo "
            "or pass its path with --filter-repo"
        )
    run(
        [filter_repo, "--force", "--file-info-callback", str(callback)],
        cwd=out,
        check=True,
    )

    report_scrub(out, published, mapping_source, args.mapping_commit, known, private_org)

    if args.report:
        args.report.write_text(
            json.dumps(
                {
                    "known_numbers": sorted(known),
                    "mapping_entries": len(mapping),
                },
                indent=2,
            )
        )
    print(f"rewritten history written to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
