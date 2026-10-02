#!/usr/bin/env python3
"""Regenerate ``requirements.lock`` from a resolved ``pip freeze``.

The freeze is read from a file, not from a live install, so the hash lookup is
the only network step and the emitted lock is a pure function of (freeze, index
state). Every requirement is emitted with the sha256 of every artifact the
resolver is allowed to pick for it, because ``--require-hashes`` rejects a lock
that pins a version without pinning its bytes.

Re-running is only meaningful when a *deliberate* version change is wanted: any
new upstream release changes the available hashes, which is exactly the drift
this lock exists to freeze. Commit the regenerated lock in the same commit as
the freeze it was generated from.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

PYTORCH_INDEX = "https://download.pytorch.org/whl/cpu"
# Only the CPU index publishes these local-version artifacts; PyPI has no
# ``+cpu`` build of torch, so they are looked up there instead of on PyPI.
PYTORCH_LOCAL_VERSION = re.compile(r"\+")

TAG_PYTHON = "cp312"


class LockError(RuntimeError):
    """A hash could not be resolved, so no lock can be written."""


def _fetch_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310 - fixed https hosts
        return json.load(response)


def _pytorch_index_hashes(name: str, version: str) -> list[str]:
    page = f"{PYTORCH_INDEX}/{name}/"
    with urllib.request.urlopen(page, timeout=120) as response:  # noqa: S310
        html = response.read().decode("utf-8")
    # The index href encodes '+' as %2B, so match on the version without it.
    version_token = version.split("+")[0]
    digests: list[str] = []
    for match in re.finditer(r'href="([^"]*?#sha256=([0-9a-f]{64}))"', html):
        href, digest = match.groups()
        filename = href.split("/")[-1].split("#")[0]
        if f"-{version_token}" not in filename and not filename.startswith(f"{name}-{version_token}"):
            continue
        if TAG_PYTHON not in filename:
            continue
        if "linux_x86_64" not in filename:
            continue
        if "win" in filename or "macos" in filename or "aarch64" in filename:
            continue
        digests.append(digest)
    if not digests:
        raise LockError(f"no {TAG_PYTHON} linux x86_64 artifact for {name}=={version} on {PYTORCH_INDEX}")
    return sorted(set(digests))


def _acceptable(filename: str) -> bool:
    """True for an artifact a cp312 manylinux x86_64 install can select.

    Deliberately permissive about the manylinux tag: wheels ship under
    `manylinux_2_17`, `manylinux_2_28`, `manylinux_2_27` and the
    `manylinux2014` aliases, and enumerating them by hand silently drops the one
    a given package actually uses — which then fails the build under
    `--require-hashes` with a correct-looking lock. Excluding a hash only costs
    lock size; excluding the *selected* one costs the build.
    """
    if not filename.endswith(".whl"):
        return False
    if "any.whl" in filename:
        return True
    if TAG_PYTHON not in filename or "linux_x86_64" not in filename:
        return False
    return "aarch64" not in filename and "musl" not in filename


def _usable_hashes(name: str, version: str) -> list[str]:
    """Hashes of the artifacts a linux/cp312 install may actually select.

    Restricted to this platform so the lock is installable here; a hash pip is
    never offered only costs lock size, while a *missing* one costs the build.
    """
    if PYTORCH_LOCAL_VERSION.search(version):
        return _pytorch_index_hashes(name, version)
    data = _fetch_json(f"https://pypi.org/pypi/{name}/{version}/json")
    return sorted(
        {
            artifact["digests"]["sha256"]
            for artifact in data["urls"]
            if artifact["packagetype"] == "bdist_wheel" and _acceptable(artifact["filename"])
        }
        or {
            artifact["digests"]["sha256"]
            for artifact in data["urls"]
            if _acceptable(artifact["filename"])
        }
        or {artifact["digests"]["sha256"] for artifact in data["urls"]}
    )


def _normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--freeze", required=True, type=Path, help="pip freeze output from the pinned image")
    parser.add_argument("--out", required=True, type=Path, help="requirements.lock to write")
    args = parser.parse_args(argv)

    freeze_lines = [
        line.strip()
        for line in args.freeze.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    if not freeze_lines:
        print("[lock] FATAL: freeze is empty; refusing to write a lock with no requirements", file=sys.stderr)
        return 1

    entries: list[str] = []
    failures: list[str] = []
    for line in freeze_lines:
        name, _, version = line.partition("==")
        if not name or not version:
            failures.append(f"{line!r} is not a 'name==version' pin")
            continue
        try:
            hashes = _usable_hashes(name, version)
        except (LockError, urllib.error.URLError, KeyError, TimeoutError) as error:
            failures.append(f"{line}: {error}")
            continue
        # Every line of a hashed requirement ends with a continuation backslash,
        # INCLUDING the last one. Without it pip associates only the first
        # --hash with the requirement and silently discards the rest, so a lock
        # that looks correct still rejects the artifact pip actually selects --
        # observed as `Expected sha256 <first hash> / Got <real hash>`.
        #
        # The continuation on the last line means the NEXT line is still part of
        # this requirement, so entries are separated by a blank line, which
        # terminates the requirement without dropping a hash.
        lines = [f"{name}=={version} \\"]
        lines.extend(f"        --hash=sha256:{digest} \\" for digest in hashes)
        entries.append("\n".join(lines))

    if failures:
        for failure in failures:
            print(f"[lock] FATAL: unresolved: {failure}", file=sys.stderr)
        return 1

    header = (
        "# Generated by scripts/g4_pipeline/gen_requirements_lock.py from the committed\n"
        "# results/image_pip_freeze.txt. Do not hand-edit: regenerate from a new freeze\n"
        "# and commit both together.\n"
        "#\n"
        f"# torch/torchvision are +cpu local-version builds and are served only by\n"
        f"# {PYTORCH_INDEX};\n"
        "# every other requirement comes from PyPI. Hashes are restricted to the\n"
        "# artifacts a cp312 manylinux x86_64 install can select, which is the only\n"
        "# target this harness builds.\n"
        f"--extra-index-url {PYTORCH_INDEX}\n"
    )
    args.out.write_text(header + "\n" + "\n\n".join(entries) + "\n", encoding="utf-8")
    print(f"[lock] wrote {len(entries)} hashed requirements to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
