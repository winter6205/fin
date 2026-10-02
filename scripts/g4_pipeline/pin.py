#!/usr/bin/env python3
"""Verify the G4 pipeline's declared build inputs, and the image they produce.

Task C1: `PIN_MANIFEST.md` pinned the image in prose while the build
resolved the bytes, so a rebuild drifted (`docs/validation/2026-09-29-g4-t5-unit-citation-legacy-removal.md`,
L7). This module is the mechanical half of the fix. It is deliberately a
separate unit from `build.sh`: the shell stays orchestration, every rule that can
fail lives here where a hermetic test can reach it without docker.

The governing rule is the driver's own -- a failed reading is never a zero.
Every parse that cannot make sense of its input raises `PinError` naming what
was unreadable. None of them returns an empty collection, because an empty
collection is what an unpinned build looks like.

The expected image id lives in `image-pin.json`, which no build step writes.
Reading the expectation back out of the file the build just produced would make
the comparison true by construction.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

REQUIREMENT_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s\\]+)$")
HASH_RE = re.compile(r"^--hash=sha256:([0-9a-f]{64})$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
APT_VERSION_RE = re.compile(r"^[0-9][A-Za-z0-9.+:~-]*$")

# Packages the harness's measured path cannot run without. A lock that drops one
# of these still passes every hash check, so they are asserted by name.
REQUIRED_PACKAGES = ("docling", "torch", "torchvision", "pytesseract")

# Tokens on the Dockerfile's `apt-get install` line that are not packages and so
# cannot carry an `=version`. Deliberately EMPTY: every token there today is a
# real apt package. An exemption added here is a declared downgrade of the OS
# layer's pin, so it must be a one-line decision with a reason, not something
# discovered later as a build failure. `test_apt_tokens_that_cannot_be_pinned_are_named`
# asserts the set is empty, which fails the moment an exemption appears.
UNVERSIONABLE_APT_TOKENS: frozenset[str] = frozenset()

# sysexits.h: EX_DATAERR is "input data is incorrect", EX_TEMPFAIL the rest.
EXIT_INVALID_PIN = 65
EXIT_DRIFT = 75


class PinError(RuntimeError):
    """A declared input could not be read or does not satisfy its own contract."""


@dataclass(frozen=True)
class LockedRequirement:
    name: str
    version: str
    hashes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ModelPin:
    name: str
    repo: str
    revision: str
    file: str
    file_sha256: str
    cache_dir: str
    role: str = ""


def parse_models_lock(text: str, *, source: str) -> dict[str, ModelPin]:
    """Parse the committed model pin: one `[name]` block per artifact."""
    entries: dict[str, dict[str, str]] = {}
    name: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            name = line[1:-1]
            if name in entries:
                raise PinError(f"{source} declares {name!r} twice")
            entries[name] = {}
            continue
        if name is None:
            raise PinError(f"{source} line {line!r} appears before any [artifact] header")
        key, separator, value = line.partition("=")
        if not separator:
            raise PinError(f"{source}: {line!r} is not 'key = value'")
        entries[name][key.strip()] = value.strip()
    if not entries:
        raise PinError(f"{source} pins 0 model artifacts; the layout weights would float")

    pins: dict[str, ModelPin] = {}
    for name, fields in entries.items():
        missing = sorted(
            {"repo", "revision", "file", "file_sha256", "cache_dir"} - set(fields)
        )
        if missing:
            raise PinError(f"{source}: artifact {name!r} is missing {missing}")
        revision = fields["revision"]
        # A branch or tag is not a pin. `main` in particular moves, and a fetch
        # bound to it gets whatever it points at on the day it runs.
        if revision in ("main", "master", "HEAD", "latest") or not re.fullmatch(
            r"[0-9a-f]{40}", revision
        ):
            raise PinError(
                f"{source}: artifact {name!r} revision {revision!r} is not a 40-character commit hash; "
                "a branch or tag moves under the pin"
            )
        digest = fields["file_sha256"]
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise PinError(f"{source}: artifact {name!r} file_sha256 {digest!r} is not a sha256")
        pins[name] = ModelPin(
            name=name,
            repo=fields["repo"],
            revision=revision,
            file=fields["file"],
            file_sha256=digest,
            cache_dir=fields["cache_dir"],
            role=fields.get("role", ""),
        )
    return pins


def read_download_metadata(cache_dir: Path, name: str) -> tuple[str, str]:
    """Read (commit, etag) from the metadata the HF download itself wrote.

    Returns `("", "")` when the file is absent or empty rather than a value that
    could be mistaken for a match: an unrecorded commit is not a correct
    revision, the same rule the driver applies to a cgroup reading.
    """
    path = Path(cache_dir) / ".cache" / "huggingface" / "download" / f"{name}.metadata"
    if not path.is_file():
        return ("", "")
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    commit = lines[0].strip() if lines else ""
    etag = lines[1].strip() if len(lines) > 1 else ""
    return (commit, etag)


def verify_models(pin: ModelPin, cache_root: Path) -> list[str]:
    """Every disagreement between the declared pin and what is on disk.

    A missing artifact is reported, never skipped: "the metadata file is not
    there" must not read as "the revision is correct".

    The payload is hashed here rather than taken on the metadata sidecar's word:
    a swapped `.safetensors` leaves the sidecar untouched, and trusting it would
    make this the same gap the record files against TableFormer, relabelled
    "pinned". The metadata read stays as the cheap commit-revision gate.
    """
    problems: list[str] = []
    directory = Path(cache_root) / pin.cache_dir
    metadata_file = directory / ".cache" / "huggingface" / "download" / f"{pin.file}.metadata"
    payload = directory / pin.file
    if not metadata_file.is_file():
        return [f"{pin.name}: no download metadata at {metadata_file}; the artifact was never fetched"]
    commit, etag = read_download_metadata(directory, pin.file)
    if not commit:
        problems.append(f"{pin.name}: download metadata records no commit")
    elif commit != pin.revision:
        problems.append(f"{pin.name}: commit {commit} does not match the pinned {pin.revision}")
    if not etag:
        problems.append(f"{pin.name}: download metadata records no ETag")
    elif etag != pin.file_sha256:
        problems.append(f"{pin.name}: payload sha256 {etag} does not match the pinned {pin.file_sha256}")
    if not payload.is_file():
        problems.append(f"{pin.name}: no payload at {payload}")
        return problems
    actual = hashlib.sha256()
    with payload.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            actual.update(chunk)
    if actual.hexdigest() != pin.file_sha256:
        problems.append(
            f"{pin.name}: the payload at {payload} hashes to {actual.hexdigest()}, "
            f"not the pinned {pin.file_sha256}"
        )
    return problems


@dataclass(frozen=True)
class AptLock:
    mirror: str
    requested: dict[str, str] = field(default_factory=dict)
    installed: dict[str, str] = field(default_factory=dict)
    mirrors: tuple[str, ...] = ()

    def resolve(self, name: str) -> str | None:
        """Installed version of a requested package, following virtual provides.

        Debian trixie renamed `libglib2.0-0` to the time_t-64 `libglib2.0-0t64`,
        keeping the old name as a virtual provide rather than a real package, so
        an exact-name lookup would report a pinned package as missing.
        """
        if name in self.installed:
            return self.installed[name]
        for suffix in ("t64",):
            if f"{name}{suffix}" in self.installed:
                return self.installed[f"{name}{suffix}"]
        return None


# ---------------------------------------------------------------------------
# Parsing. Each raises rather than returning a partial result.
# ---------------------------------------------------------------------------


def _decode(raw: bytes, source: str) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise PinError(f"{source} is not valid UTF-8 at byte {error.start}: {error.reason}") from error


def _logical_lines(text: str) -> list[str]:
    """Join backslash-continued lines and drop comments, preserving order.

    pip associates *every* `--hash` with its requirement only when the last hash
    line also carries a continuation backslash, so the generated lock ends each
    entry with one and separates entries with a blank line. A blank line is
    therefore the entry terminator, and it takes precedence over a trailing
    continuation -- otherwise the next requirement is swallowed as a hash token.
    """
    joined: list[str] = []
    pending = ""
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            # Terminates a pending requirement even mid-continuation, and is
            # otherwise just a separator.
            if pending.strip():
                joined.append(pending.strip())
                pending = ""
            continue
        if not pending and line.lstrip().startswith("#"):
            continue
        if line.endswith("\\"):
            pending += line[:-1] + " "
            continue
        joined.append((pending + line).strip())
        pending = ""
    if pending.strip():
        # Nothing followed the continuation. The accumulated text is emitted so
        # the caller's parser decides whether it is a requirement; that keeps one
        # rule about what a requirement is, in one place.
        joined.append(pending.strip())
    return [line for line in joined if line]


def parse_requirements_lock(text: str) -> dict[str, LockedRequirement]:
    """Parse a `--require-hashes` requirements file into name -> entry.

    Raises on any line that is not a pinned requirement, because a silently
    dropped line is a package that goes back to resolving from the network.
    """
    entries: dict[str, LockedRequirement] = {}
    for line in _logical_lines(text):
        if line.startswith(("--", "-r ")):  # index/directive options
            continue
        parts = line.split()
        requirement = parts[0]
        match = REQUIREMENT_RE.match(requirement)
        if match is None:
            raise PinError(
                f"requirement {requirement!r} is not a 'name==version' pin; "
                "an unpinned requirement resolves fresh on every build"
            )
        name, version = match.group(1), match.group(2)
        hashes: list[str] = []
        for token in parts[1:]:
            hash_match = HASH_RE.match(token)
            if hash_match is None:
                raise PinError(f"requirement {requirement!r} carries an unreadable hash token {token!r}")
            hashes.append(hash_match.group(1))
        if name in entries:
            raise PinError(f"requirement {name!r} appears more than once in the lock")
        entries[name] = LockedRequirement(name=name, version=version, hashes=tuple(sorted(set(hashes))))
    return entries


def parse_freeze(text: str) -> dict[str, str]:
    """Parse a `pip freeze` snapshot into name -> version."""
    frozen: dict[str, str] = {}
    for line in _logical_lines(text):
        match = REQUIREMENT_RE.match(line)
        if match is None:
            raise PinError(f"freeze line {line!r} is not a 'name==version' pin")
        name, version = match.group(1), match.group(2)
        if name in frozen:
            raise PinError(f"freeze lists {name!r} twice")
        frozen[name] = version
    return frozen


def parse_apt_lock(text: str) -> AptLock:
    """Parse the committed apt record: mirror, requested set, installed set."""
    section: str | None = None
    mirror = ""
    mirrors: list[str] = []
    requested: dict[str, str] = {}
    installed: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        if section == "mirror":
            mirrors.append(line)
            mirror = line
        elif section in ("requested", "installed"):
            name, separator, version = line.partition("=")
            if not separator or not name or not version:
                raise PinError(f"{section} entry {line!r} is not 'name=version'")
            if name in requested if section == "requested" else name in installed:
                raise PinError(f"{section} lists {name!r} twice")
            (requested if section == "requested" else installed)[name] = version
        else:
            raise PinError(f"apt lock line {line!r} appears before any [section] header")
    if not mirror:
        raise PinError("apt lock declares no [mirror]; an unpinned mirror floats the OS layer bytes")
    return AptLock(mirror=mirror, requested=requested, installed=installed, mirrors=tuple(mirrors))


def parse_image_identity(text: str) -> str:
    """Read `image_id=` out of a `docker image inspect` snapshot."""
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() == "image_id":
            digest = value.strip()
            if not DIGEST_RE.match(digest):
                raise PinError(f"image_id {digest!r} is not a sha256 digest")
            return digest
    raise PinError("image identity snapshot has no image_id= line")


def manifest_identity_section(text: str) -> str:
    """The part of `PIN_MANIFEST.md` that declares this image's identity.

    That is `## 1. Image identity` up to its first subsection. The rest of the
    document deliberately quotes other digests -- probe builds, layer diffIDs --
    as evidence, and a guard that swept those in would report a correct manifest
    as stale on every run.
    """
    lines = text.splitlines()
    start = next(
        (index for index, line in enumerate(lines) if re.match(r"^##\s+1\.\s", line)),
        None,
    )
    if start is None:
        return text
    for index in range(start + 1, len(lines)):
        if re.match(r"^#{2,3}\s", lines[index]):
            return "\n".join(lines[start:index])
    return "\n".join(lines[start:])


def manifest_image_digests(text: str) -> list[str]:
    """Digests `PIN_MANIFEST.md` declares as THIS image's identity.

    Matches the truncated `sha256:<hex>…` form as well as a full 64-hex digest,
    because that is how the manifest actually writes them: a matcher that only
    accepted the full form would return `[]` against this manifest and the drift
    guard built on it could never fire.

    Excludes `repo@sha256:...` digests, which name a base image rather than this
    image: naming the base is what makes the image pinned, so requiring the
    manifest to call the base digest the image id would be backwards.
    """
    section = manifest_identity_section(text)
    base_digests = set(re.findall(r"[\w.\-]+@(sha256:[0-9a-f]{64})", section))
    return [
        digest
        for digest in re.findall(r"(?<![@\w])(sha256:[0-9a-f]{8,64}…?)", section)
        if not digest.startswith(tuple(base_digests))
    ]


def stale_manifest_digests(text: str, *, known: Iterable[str]) -> list[str]:
    """Manifest digests that are neither a declared expectation nor a committed id.

    A truncated digest is resolved as a prefix of one of `known`, so a reader
    scanning the table sees the same drift the full-form check would catch.
    """
    return [
        digest
        for digest in manifest_image_digests(text)
        if not any(known_id.startswith(digest.rstrip("…")) for known_id in known)
    ]


# ---------------------------------------------------------------------------
# Validation. Each returns None on success and raises `PinError` otherwise.
# ---------------------------------------------------------------------------


def validate_requirements_lock_text(text: str, *, source: str) -> dict[str, LockedRequirement]:
    if not text.strip():
        raise PinError(f"{source} is empty; an empty lock installs nothing and reverts to unpinned resolution")
    entries = parse_requirements_lock(text)
    if not entries:
        raise PinError(f"{source} declares 0 requirements; an empty lock is the drift this pin exists to stop")
    unhashed = sorted(name for name, entry in entries.items() if not entry.hashes)
    if unhashed:
        raise PinError(f"{source} has requirements with no --hash=sha256: {unhashed}")
    missing = [name for name in REQUIRED_PACKAGES if name not in entries]
    if missing:
        raise PinError(f"{source} is missing packages the measured path requires: {missing}")
    return entries


def validate_requirements_lock_bytes(raw: bytes, *, source: str) -> dict[str, LockedRequirement]:
    return validate_requirements_lock_text(_decode(raw, source), source=source)


def validate_apt_lock_text(text: str, *, source: str) -> AptLock:
    if not text.strip():
        raise PinError(f"{source} is empty; without it the apt layer resolves from a live mirror")
    lock = parse_apt_lock(text)
    if not lock.requested:
        raise PinError(f"{source} requests 0 apt packages; the OCR runtime would be missing")
    # Both sections are validated on their own. Merging them let `[installed]`
    # shadow `[requested]`, so a malformed installed version passed whenever the
    # same package was also requested.
    for name, version in sorted(lock.requested.items()):
        if not APT_VERSION_RE.match(version):
            raise PinError(f"{source}: requested {name} has an unreadable version {version!r}")
    for name, version in sorted(lock.installed.items()):
        if not APT_VERSION_RE.match(version):
            raise PinError(f"{source}: installed {name} has an unreadable version {version!r}")
    unrecorded = sorted(name for name in lock.requested if lock.resolve(name) is None)
    if unrecorded:
        raise PinError(f"{source} requests packages with no recorded installed version: {unrecorded}")
    # The two sections are two records of the same fact, and a disagreement
    # between them is exactly the L7 defect class: the requested version is what
    # the build asks for, the resolved one is what apt gave it.
    disagreeing = sorted(
        f"{name}: requested {requested} but the lock records {lock.resolve(name)} as installed"
        for name, requested in lock.requested.items()
        if lock.resolve(name) != requested
    )
    if disagreeing:
        raise PinError(f"{source}: [requested] and [installed] disagree -- {disagreeing}")
    return lock


def dockerfile_apt_pins(text: str) -> dict[str, str]:
    """The `name=version` tokens in the Dockerfile's `apt-get install` block."""
    pins: dict[str, str] = {}
    for token in dockerfile_apt_tokens(text):
        name, separator, version = token.partition("=")
        if separator and name and version:
            pins[name] = version
    return pins


def dockerfile_apt_tokens(text: str) -> list[str]:
    """Every non-flag argument on the Dockerfile's `apt-get install` line.

    A single `_logical_lines` pass joins the whole `RUN` step, so the token list
    has to stop at the first `&&`: `&& rm -rf /var/lib/apt/lists/*` is a later
    command in the same joined line, not an apt argument. The pre-repair parser
    split the whole line and kept only tokens carrying an `=`, so it could not
    see that boundary -- and, having no `=` to find, it never saw a bare,
    silently unpinned package either.
    """
    tokens: list[str] = []
    for line in _logical_lines(text):
        marker = "apt-get install"
        index = line.find(marker)
        if index < 0:
            continue
        arguments = line[index + len(marker):].split("&&", 1)[0]
        tokens.extend(token for token in arguments.split() if not token.startswith("-"))
    return tokens


def validate_apt_install_line(text: str, apt: "AptLock") -> None:
    """Three-way agreement between the Dockerfile's install line and the lock.

    Each direction is a rule, because a one-directional check reads as a closed
    loop while silently passing the cases that matter: a bare package name has no
    `=` for the version parser to read, so downgrading `tesseract-ocr-chi-sim` to
    unpinned validated clean; a package the lock never requested was never asked
    about; and a requested package dropped from the Dockerfile was never reported
    as missing.

    * every token is version-pinned, so no package is silently unpinned;
    * every pinned name is in `[requested]` at the same version;
    * every `[requested]` name appears on the install line.
    """
    tokens = dockerfile_apt_tokens(text)
    if not tokens:
        raise PinError("the Dockerfile declares no `apt-get install` line; the OS layer would carry no pinned apt package at all")
    unpinned = sorted(
        token for token in tokens if "=" not in token and token not in UNVERSIONABLE_APT_TOKENS
    )
    if unpinned:
        raise PinError(
            f"the Dockerfile's apt-get install names packages with no =version: {unpinned}; "
            "an unpinned token resolves to whatever the mirror holds on the day of the build"
        )
    declared = dockerfile_apt_pins(text)
    unrequested = sorted(
        f"{name}={version} is installed but apt-packages.lock [requested] does not name it"
        for name, version in declared.items()
        if name not in apt.requested
    )
    if unrequested:
        raise PinError(f"the Dockerfile installs apt packages the lock does not record: {unrequested}")
    drifted = sorted(
        f"{name}={version} in the Dockerfile but {apt.requested[name]!r} in the apt lock"
        for name, version in declared.items()
        if apt.requested[name] != version
    )
    if drifted:
        raise PinError(f"the Dockerfile's apt pins disagree with apt-packages.lock: {drifted}")
    absent = sorted(name for name in apt.requested if name not in declared)
    if absent:
        raise PinError(
            f"apt-packages.lock [requested] names packages the Dockerfile does not install: {absent}; "
            "the lock would then record a package that is not in the image"
        )


def _installs_under_require_hashes(text: str) -> bool:
    """Whether a real `pip install` line consumes the lock under --require-hashes.

    A bare substring test over the whole file is satisfied by a *comment* that
    mentions the flag -- the committed Dockerfile does exactly that, in the
    paragraph above the install -- so it could not tell a Dockerfile that
    installs the lock under the flag from one that only explains it. The flag
    and the lock reference have to appear on the same non-comment line.
    """
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "pip install" in stripped and "requirements.lock" in stripped:
            return "--require-hashes" in stripped.split()
    return False


def validate_dockerfile_text(text: str, *, apt: "AptLock | None" = None) -> None:
    from_lines = [line for line in text.splitlines() if line.strip().startswith("FROM ")]
    if not from_lines:
        raise PinError("the Dockerfile declares no FROM; the base image is what the OS layer is")
    for line in from_lines:
        if "@sha256:" not in line:
            raise PinError(f"base image is not digest-pinned: {line.strip()!r}; a tag moves under the pin")
    if "requirements.lock" not in text:
        raise PinError("the Dockerfile does not install from requirements.lock")
    if not _installs_under_require_hashes(text):
        raise PinError("the lock is installed without --require-hashes, so its hashes are not enforced")
    loose = sorted(
        token
        for token in (f"{name}==" for name in REQUIRED_PACKAGES)
        if token in text
    )
    if loose:
        raise PinError(f"inline version pins bypass the lock: {loose}")
    if apt is None:
        return
    # The OS layer gets the treatment the Python layer already had: the lock is
    # authoritative, and the Dockerfile must not name a version it does not.
    validate_apt_install_line(text, apt)


def compare_image_id(expected: str, observed: str) -> bool:
    """True when the two digests are equal. A mismatch is drift, not an error."""
    return expected == observed


# ---------------------------------------------------------------------------
# image-pin.json is read here and nowhere else. `build.sh` used to reach into it
# with its own `python3 -c 'import json'` accessors, giving the same file two
# readers whose field names could disagree silently. Every declared value the
# build or a check needs is read through `load_image_pin`.
# ---------------------------------------------------------------------------

# Keys the reproducibility check on the buildx path compares. A declaration
# missing one of these is incomplete, so reading it fails loudly rather than
# returning `None` and letting the comparison pass on an absent expectation.
# `cpuset_cpus` is declared, not compared against the live host: the legacy
# `docker build --cpuset-cpus=0-3` bound is dropped on the buildx path (only the
# memory ceiling carries over via `--driver-opt`), and `null` records that drop
# as structured data instead of prose, so the omission is explicit and removable
# only by an edit here.
BUILDER_KEYS = (
    "engine_version",
    "mode",
    "buildx_builder",
    "buildx_driver",
    "buildkit_version",
    "memory_ceiling",
    "network",
    "source_date_epoch",
    "cpuset_cpus",
)


def load_image_pin(path: Path) -> dict:
    """The single reader for `image-pin.json`: parse it or name why it is unreadable."""
    path = Path(path)
    if not path.is_file():
        raise PinError(f"{path} is missing; there is no declaration to check the build against")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise PinError(f"{path} is not a JSON object")
    return data


def declared_value(pipeline_dir: Path, dotted: str):
    """Read one (possibly nested) value from the declaration, addressed by dotted path."""
    node = load_image_pin(Path(pipeline_dir) / "image-pin.json")
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            raise PinError(f"image-pin.json declares no {dotted!r}")
        node = node[part]
    return node


def _extract_expected(data: dict) -> str:
    digest = data.get("expected_image_id")
    if not isinstance(digest, str) or not DIGEST_RE.match(digest):
        raise PinError(f"image-pin.json declares no sha256 expected_image_id (got {digest!r})")
    return digest


def declared_expected_image_id(pipeline_dir: Path) -> str:
    return _extract_expected(load_image_pin(Path(pipeline_dir) / "image-pin.json"))


def _require_builder_keys(builder: object) -> dict:
    if not isinstance(builder, dict) or not builder:
        raise PinError("image-pin.json declares no [builder] block; the build path would be prose")
    missing = sorted(key for key in BUILDER_KEYS if key not in builder)
    if missing:
        raise PinError(f"image-pin.json builder block is missing {missing}")
    return builder


def declared_builder(pipeline_dir: Path) -> dict:
    builder = load_image_pin(Path(pipeline_dir) / "image-pin.json").get("builder")
    return _require_builder_keys(builder)


def parse_buildx_inspect(text: str) -> dict[str, str]:
    """Top-level `Key: value` fields of a `docker buildx inspect` render.

    The first occurrence of a key wins: the builder-level `Driver`/`Status`/
    `BuildKit version`/`Driver Options` are printed once before the `Nodes:`
    sub-block, whose per-node lines reuse some key names (`Name`, `Endpoint`).
    An empty render (the builder does not exist) yields no fields, which the
    caller treats as "unverifiable", never as agreement.
    """
    fields: dict[str, str] = {}
    for line in text.splitlines():
        key, separator, value = line.partition(":")
        if not separator:
            continue
        key = key.strip()
        if key and key not in fields:
            fields[key] = value.strip()
    return fields


def validate_builder_identity(
    *, declared: dict, live_engine: str, live_mode: str, inspect: str,
) -> None:
    """The declared buildx path against the live host, every rule in one place.

    This is the half `build.sh`'s header promised lived here: nine fail-capable
    comparisons that used to be inlined in shell and could only be tested by
    extracting shell text. A hermetic test feeds the same shapes the real
    `docker` CLI prints and asserts the exit without docker, a build, or a
    network. A declaration missing a compared key is rejected up front, so the
    comparison never passes on an absent expectation. Every disagreement raises
    `PinError`, which the CLI maps to exit 65 ("input invalid, not attempted");
    the image-identity drift code 75 stays with `compare-image-id` alone.
    """
    declared = _require_builder_keys(declared)
    if not live_engine:
        raise PinError(
            "the docker daemon reported no version, so the builder is unverified; "
            "the pin cannot be checked on an unidentified builder"
        )
    if live_engine != declared["engine_version"]:
        raise PinError(
            f"the builder is engine {live_engine}, not the declared {declared['engine_version']}; "
            "this host was not the one the measurements were taken on"
        )
    if live_mode != declared["mode"]:
        raise PinError(
            f"this host runs builds in {live_mode} mode, not the declared {declared['mode']}; "
            "the reproducible path needs the docker buildx plugin"
        )
    if not inspect.strip():
        raise PinError(
            f"buildx builder {declared['buildx_builder']} does not exist on this host; create it with: "
            f"docker buildx create --name {declared['buildx_builder']} --driver {declared['buildx_driver']} "
            f"--driver-opt memory={declared['memory_ceiling']},network={declared['network']} --use"
        )
    fields = parse_buildx_inspect(inspect)
    status = fields.get("Status", "")
    if status != "running":
        raise PinError(
            f"buildx builder {declared['buildx_builder']} is {status or 'unreachable'}, not running"
        )
    driver = fields.get("Driver", "")
    if driver != declared["buildx_driver"]:
        raise PinError(
            f"the builder driver is {driver or 'none'}, not the declared {declared['buildx_driver']}; "
            "only the container driver honours the memory ceiling"
        )
    buildkit = fields.get("BuildKit version", "")
    if buildkit != declared["buildkit_version"]:
        raise PinError(
            f"the builder runs BuildKit {buildkit or 'none'}, not the declared "
            f"{declared['buildkit_version']}; rewrite-timestamp semantics may differ"
        )
    driver_opts = fields.get("Driver Options", "")
    if f'memory="{declared["memory_ceiling"]}"' not in driver_opts:
        raise PinError(
            f"the builder driver options are {driver_opts or 'none'}, which do not bound builds at "
            f"memory={declared['memory_ceiling']}; the 4 GiB target contract is not enforced here"
        )
    if f'network="{declared["network"]}"' not in driver_opts:
        raise PinError(
            f"the builder driver options do not set network={declared['network']}; "
            "the digest-pinned base cannot resolve from a containerised BuildKit without it"
        )


# ---------------------------------------------------------------------------
# Cross-checks between image-pin.json and the committed files it is meant to
# describe. Before these, `image-pin.json` carried `base_image`, `apt_snapshot`
# and `python_requirements` as a static copy nothing compared: an edit to the
# Dockerfile's base digest or apt snapshot, or a lock gaining/losing a package,
# left the declaration silently stale -- the same two-sources-disagreeing class
# the rest of this module exists to close.
# ---------------------------------------------------------------------------


def dockerfile_from_images(text: str) -> list[str]:
    """The base-image references on the Dockerfile's `FROM` lines."""
    images: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("FROM "):
            continue
        tokens = stripped[len("FROM "):].split()
        for token in tokens:
            if token.startswith("--"):  # --platform=..., --progress=...
                continue
            if token.upper() in ("AS",):
                break
            images.append(token)
    return images


def dockerfile_apt_snapshot(text: str) -> str:
    """The single `APT_SNAPSHOT` ARG default, or "" when the Dockerfile does not declare one.

    Exactly one declaration is required: Docker substitutes the LAST ARG before
    first use, so a second, undeclared line would silently change the built
    snapshot while the first-match read kept agreeing with the declaration.
    """
    matches = re.findall(r"^ARG\s+APT_SNAPSHOT=(\S+)", text, re.MULTILINE)
    if len(matches) > 1:
        raise PinError(
            f"the Dockerfile declares APT_SNAPSHOT {len(matches)} times "
            f"({matches}); only one declaration is checkable against the pin"
        )
    return matches[0] if matches else ""


def validate_image_pin_agrees_with_files(
    *, pin: dict, dockerfile_text: str, apt: "AptLock", lock_count: int,
) -> None:
    """Every static copy in image-pin.json must match the artifact it describes."""
    base = pin.get("base_image")
    if not isinstance(base, str) or not base:
        raise PinError("image-pin.json declares no base_image; the base layer would be undeclared")
    froms = dockerfile_from_images(dockerfile_text)
    if len(froms) != 1:
        raise PinError(
            f"the Dockerfile has {len(froms)} FROM references {froms}; image-pin.json "
            "declares one base image, so multi-stage would leave the base undeclared"
        )
    if base != froms[0]:
        raise PinError(
            f"image-pin.json base_image {base!r} is not the Dockerfile's FROM {froms}; "
            "the declared base and the built base are two sources that no longer agree"
        )

    snapshot = pin.get("apt_snapshot")
    if not isinstance(snapshot, str) or not snapshot:
        raise PinError("image-pin.json declares no apt_snapshot; the OS layer timestamp is undeclared")
    declared_in_dockerfile = dockerfile_apt_snapshot(dockerfile_text)
    if declared_in_dockerfile != snapshot:
        raise PinError(
            f"image-pin.json apt_snapshot {snapshot!r} is not the Dockerfile's APT_SNAPSHOT "
            f"{declared_in_dockerfile!r}; the build would fetch a different snapshot than declared"
        )
    unagreed = sorted(uri for uri in apt.mirrors if snapshot not in uri)
    if unagreed:
        raise PinError(
            f"apt-packages.lock mirror URIs {unagreed} do not carry the declared snapshot {snapshot!r}"
        )

    declared_count = pin.get("python_requirements")
    if not isinstance(declared_count, int) or isinstance(declared_count, bool):
        raise PinError(f"image-pin.json python_requirements {declared_count!r} is not an integer")
    if declared_count != lock_count:
        raise PinError(
            f"image-pin.json declares {declared_count} python_requirements but requirements.lock "
            f"parses {lock_count}; the static count has gone stale against the lock"
        )



# ---------------------------------------------------------------------------
# Composite checks.
# ---------------------------------------------------------------------------


def self_check(pipeline_dir: Path) -> list[str]:
    """Validate every committed declared input. Raises on the first failure."""
    pipeline_dir = Path(pipeline_dir)
    notes: list[str] = []

    lock_path = pipeline_dir / "requirements.lock"
    entries = validate_requirements_lock_bytes(lock_path.read_bytes(), source=str(lock_path))
    notes.append(f"requirements.lock: {len(entries)} hashed requirements")

    freeze_path = pipeline_dir / "results" / "image_pip_freeze.txt"
    frozen = parse_freeze(freeze_path.read_text(encoding="utf-8"))
    only_lock = sorted(set(entries) - set(frozen))
    only_freeze = sorted(set(frozen) - set(entries))
    if only_lock or only_freeze:
        raise PinError(
            f"requirements.lock and {freeze_path.name} disagree: "
            f"only in lock={only_lock} only in freeze={only_freeze}"
        )
    mismatched = sorted(
        f"{name}: lock={entries[name].version} freeze={frozen[name]}"
        for name in entries
        if entries[name].version != frozen[name]
    )
    if mismatched:
        raise PinError(f"requirements.lock pins a different version than the recorded freeze: {mismatched}")
    notes.append(f"image_pip_freeze.txt: {len(frozen)} requirements, all covered by the lock")

    apt_path = pipeline_dir / "apt-packages.lock"
    apt = validate_apt_lock_text(apt_path.read_text(encoding="utf-8"), source=str(apt_path))
    notes.append(f"apt-packages.lock: {len(apt.requested)} requested, {len(apt.installed)} recorded at {apt.mirror}")

    dockerfile = (pipeline_dir / "Dockerfile").read_text(encoding="utf-8")
    validate_dockerfile_text(dockerfile, apt=apt)
    notes.append("Dockerfile: digest-pinned base, installs requirements.lock under --require-hashes")

    pin = load_image_pin(pipeline_dir / "image-pin.json")
    validate_image_pin_agrees_with_files(
        pin=pin, dockerfile_text=dockerfile, apt=apt, lock_count=len(entries)
    )
    notes.append(
        "image-pin.json: base_image, apt_snapshot and python_requirements agree "
        "with the Dockerfile and the locks"
    )

    builder = _require_builder_keys(pin.get("builder"))
    cpuset = builder["cpuset_cpus"]
    notes.append(
        "builder: memory ceiling %s enforced via driver-opt; CPU pinning %s"
        % (
            builder["memory_ceiling"],
            "declared dropped on the buildx path (cpuset_cpus=null)"
            if cpuset is None
            else f"declared as {cpuset}",
        )
    )

    identity_path = pipeline_dir / "results" / "image_identity.txt"
    committed = parse_image_identity(identity_path.read_text(encoding="utf-8"))
    expected = _extract_expected(pin)
    notes.append(f"declared expected image id: {expected}")
    notes.append(f"committed image_identity.txt: {committed}")
    if compare_image_id(expected, committed):
        notes.append("declared expectation and committed snapshot agree")

    models_path = pipeline_dir / "models.lock"
    models = parse_models_lock(models_path.read_text(encoding="utf-8"), source=str(models_path))
    notes.append(
        f"models.lock: {len(models)} artifacts, each at a 40-character commit "
        f"(heron {models['heron'].revision[:12]}…)"
    )

    manifest_path = pipeline_dir / "PIN_MANIFEST.md"
    if manifest_path.is_file():
        manifest = manifest_path.read_text(encoding="utf-8")
        if not manifest_image_digests(manifest):
            raise PinError(
                f"{manifest_path.name} cites no image digest this guard can read; the drift "
                "check would be blind, which is the L7 regression restated"
            )
        stale = stale_manifest_digests(manifest, known=(expected, committed))
        if stale:
            raise PinError(
                f"PIN_MANIFEST.md cites image digest(s) {stale} that are neither the declared "
                f"expectation {expected} nor the committed snapshot {committed}"
            )
        notes.append("PIN_MANIFEST.md: no image digest contradicts the declared or committed id")
    return notes


def _fatal(message: str) -> None:
    print(f"[pin] FATAL: {message}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    self_parser = sub.add_parser("self-check", help="validate every committed declared input")
    self_parser.add_argument("--pipeline-dir", required=True, type=Path)

    compare = sub.add_parser("compare-image-id", help="fail when the built image is not the declared one")
    compare.add_argument("--pin", required=True, type=Path)
    compare.add_argument("--observed", required=True, type=Path)

    models = sub.add_parser("verify-models", help="fail when the fetched weights are not the pinned ones")
    models.add_argument("--pin", required=True, type=Path)
    models.add_argument("--cache-root", required=True, type=Path)

    builder = sub.add_parser(
        "check-builder",
        help="fail (65) unless the live builder matches the declared buildx path",
    )
    builder.add_argument("--pipeline-dir", required=True, type=Path)
    builder.add_argument("--live-engine", required=True)
    builder.add_argument("--live-mode", required=True)
    builder.add_argument(
        "--inspect-file",
        required=True,
        type=Path,
        help="text of `docker buildx inspect <declared builder>`; empty means the builder is absent",
    )

    get = sub.add_parser("get", help="print one declared value from image-pin.json (dotted path)")
    get.add_argument("--pipeline-dir", required=True, type=Path)
    get.add_argument("--path", required=True)

    args = parser.parse_args(argv)

    try:
        if args.command == "self-check":
            for note in self_check(args.pipeline_dir):
                print(f"[pin] {note}")
        elif args.command == "get":
            value = declared_value(args.pipeline_dir, args.path)
            print("null" if value is None else value)
        elif args.command == "check-builder":
            inspect_text = (
                args.inspect_file.read_text(encoding="utf-8")
                if args.inspect_file.is_file()
                else ""
            )
            validate_builder_identity(
                declared=declared_builder(args.pipeline_dir),
                live_engine=args.live_engine,
                live_mode=args.live_mode,
                inspect=inspect_text,
            )
            print("[pin] builder matches the declared buildx path")
        elif args.command == "verify-models":
            pins = parse_models_lock(args.pin.read_text(encoding="utf-8"), source=str(args.pin))
            problems: list[str] = []
            for model in pins.values():
                problems.extend(verify_models(model, args.cache_root))
            if problems:
                for problem in problems:
                    _fatal(f"model pin drift: {problem}")
                return EXIT_DRIFT
            print(f"[pin] {len(pins)} model artifacts match the declared revisions and hashes")
        else:
            declared = _extract_expected(load_image_pin(args.pin))
            built = parse_image_identity(args.observed.read_text(encoding="utf-8"))
            if not compare_image_id(declared, built):
                _fatal(
                    "image identity drift: "
                    f"declared {declared} in {args.pin.name}, built {built}. "
                    "Either a declared input moved (base digest, apt snapshot, wheel hashes) "
                    "or the builder produced different bytes; do NOT re-baseline the "
                    "expectation to whatever this build produced without an explicit decision."
                )
                return EXIT_DRIFT
            print(f"[pin] image id matches the declaration: {built}")
    except PinError as error:
        _fatal(str(error))
        return EXIT_INVALID_PIN
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as error:
        _fatal(f"unreadable declared input: {type(error).__name__}: {error}")
        return EXIT_INVALID_PIN
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
