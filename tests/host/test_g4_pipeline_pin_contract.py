"""The G4 pipeline pin: a declared input the build must reproduce, or fail.

Issue 324 task C1 exists because `PIN_MANIFEST.md` pinned the image in prose
while the build resolved the bytes: a rebuild drifted to a different image ID
with three packages moved (`docling-parse` 7.21.0 -> 7.22.1, `Faker`
40.39.0 -> 40.40.0, `regex` 2026.9.10 -> 2026.9.29). L7 in
`docs/validation/2026-09-29-g4-t5-unit-citation-legacy-removal.md` is that
defect, still open.

These tests hold the mechanical half of the fix and are deliberately hostile:
the happy path is one test out of many, because a verifier that only ever
accepts is the tautology that let the drift through. `pin.py` is loaded by path
(it lives with the harness, not under `src/`, because docling stays out of
fin's declared runtime dependencies), and every input it reads is a committed
file, so the whole module runs hermetically with no docker, no network and no
image.
"""

from __future__ import annotations

import hashlib
import importlib.util
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PIPELINE_DIR = PROJECT_ROOT / "scripts" / "g4_pipeline"
PIN_MODULE_PATH = PIPELINE_DIR / "pin.py"
BUILD_SCRIPT = PIPELINE_DIR / "build.sh"
DOCKERFILE = PIPELINE_DIR / "Dockerfile"
REQUIREMENTS_LOCK = PIPELINE_DIR / "requirements.lock"
APT_LOCK = PIPELINE_DIR / "apt-packages.lock"
MODELS_LOCK = PIPELINE_DIR / "models.lock"
PIN_MANIFEST = PIPELINE_DIR / "PIN_MANIFEST.md"
FREEZE = PIPELINE_DIR / "results" / "image_pip_freeze.txt"
IDENTITY = PIPELINE_DIR / "results" / "image_identity.txt"

# The image ID L7 recorded, and the one the committed snapshot actually holds.
# They disagree today; that disagreement is the defect under repair, so it is
# named here as data rather than restated as a constant somewhere it could rot.
L7_RECORDED_IMAGE_ID = "sha256:8e10185fa27ea5adcfca94732adc27a2720ed9a31b67698d0a2c9e7a1a62c800"
COMMITTED_IMAGE_ID = "sha256:0f278c22daa45a8361235f37620fa31a237f0284501436256429e2a8eba35229"


def _pin() -> ModuleType:
    # One module load per run: 45 tests re-executing 500+ lines each adds up,
    # and every caller only reads it.
    cached = _PIN_MODULE
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location("g4_pipeline_pin", PIN_MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = module_from_spec = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module_from_spec
    spec.loader.exec_module(module_from_spec)
    globals()["_PIN_MODULE"] = module_from_spec
    return module_from_spec


_PIN_MODULE: ModuleType | None = None


def _staged_tree(tmp_path: Path) -> Path:
    """A writable copy of the pipeline dir, complete enough for self-check.

    `results/` is copied selectively: self-check reads only the recorded freeze
    and identity out of it, and a staging tree missing those fails for a reason
    that has nothing to do with whatever the test is corrupting. The per-run
    `.jsonl` and page images are 40+ MB and self-check never looks at them.
    """
    import shutil

    staging = tmp_path / "g4_pipeline"
    shutil.copytree(
        PIPELINE_DIR,
        staging,
        ignore=shutil.ignore_patterns("results", "*.jsonl", "*.png", "__pycache__"),
    )
    (staging / "results").mkdir()
    for name in ("image_pip_freeze.txt", "image_identity.txt"):
        shutil.copy2(PIPELINE_DIR / "results" / name, staging / "results" / name)
    return staging


def _redirection_offenders(build: str):
    """Lines that truncate a committed snapshot with a plain `>` redirect.

    Matching the literal `${HERE}/results/` made this a dead test once build.sh
    introduced `RESULTS="${HERE}/results"`, so the guard matches the `snapshot`
    call sites and any raw redirect into `${RESULTS}/`.
    """
    snapshot_sites = re.findall(r'^snapshot\s+"\$\{RESULTS\}/[^"]+"', build, re.MULTILINE)
    assert snapshot_sites, "build.sh writes its snapshots some other way; this guard is blind again"
    return [
        line for line in build.splitlines()
        if re.search(r'>\s*"\$\{RESULTS\}/', line) and not re.match(r'^\s*snapshot\b', line)
    ]


# --------------------------------------------------------------------------
# Normal behavior: the committed artifacts satisfy the declared pin.
# --------------------------------------------------------------------------


def test_committed_lock_covers_every_frozen_requirement() -> None:
    """A lock that omits a package silently lets pip resolve it fresh."""
    pin = _pin()
    lock = pin.parse_requirements_lock(REQUIREMENTS_LOCK.read_text(encoding="utf-8"))
    frozen = pin.parse_freeze(FREEZE.read_text(encoding="utf-8"))
    assert frozen, "the committed freeze must not be empty"
    assert set(lock) == set(frozen), (
        f"lock-vs-freeze drift: only in lock={sorted(set(lock) - set(frozen))} "
        f"only in freeze={sorted(set(frozen) - set(lock))}"
    )


def test_every_locked_requirement_carries_at_least_one_hash() -> None:
    """`--require-hashes` refuses a lock without hashes, but only at install
    time and only for the file it is handed; the check has to be here so a
    truncated lock is a failed test rather than a failed build three minutes in.
    """
    pin = _pin()
    lock = pin.parse_requirements_lock(REQUIREMENTS_LOCK.read_text(encoding="utf-8"))
    unhashed = sorted(name for name, entry in lock.items() if not entry.hashes)
    assert not unhashed, f"requirements without a hash: {unhashed}"


def test_hashes_are_sha256_hex_not_placeholders() -> None:
    pin = _pin()
    lock = pin.parse_requirements_lock(REQUIREMENTS_LOCK.read_text(encoding="utf-8"))
    bad = sorted(
        f"{name}:{digest}"
        for name, entry in lock.items()
        for digest in entry.hashes
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest)
    )
    assert not bad, f"malformed sha256 digests: {bad}"


def test_dockerfile_installs_from_the_lock_not_from_loose_constraints() -> None:
    """The lock only binds the build if the Dockerfile actually consumes it."""
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    assert "requirements.lock" in dockerfile, "the Dockerfile must install from the committed lock"
    assert "--require-hashes" in dockerfile, "the lock must be installed under --require-hashes"
    loose = [
        token
        for token in ("docling==", "torch==", "torchvision==", "pytesseract==")
        if token in dockerfile
    ]
    assert not loose, f"inline version pins bypass the lock: {loose}"


def test_dockerfile_base_image_is_digest_pinned() -> None:
    """A mutable tag is the root of the OS-layer drift: `python:3.12-slim`
    moved Debian bookworm -> trixie under this pin without any edit here."""
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    from_lines = [line for line in dockerfile.splitlines() if line.startswith("FROM ")]
    assert from_lines, "the Dockerfile must declare a base image"
    for line in from_lines:
        assert "@sha256:" in line, f"base image is not digest-pinned: {line!r}"


def test_apt_packages_are_pinned_and_the_actual_resolution_is_recorded() -> None:
    """apt resolves from a live mirror, so an unpinned `apt-get install` floats
    the OS layer bytes on every build. The declared set and the set the build
    actually installed must both be committed, and they must agree.
    """
    pin = _pin()
    lock = pin.parse_apt_lock(APT_LOCK.read_text(encoding="utf-8"))
    assert lock.requested, "the apt lock must name the packages the build requests"
    for name in ("ca-certificates", "curl", "libgl1", "libgomp1", "tesseract-ocr",
                 "tesseract-ocr-eng", "tesseract-ocr-chi-sim"):
        assert name in lock.requested, f"{name} is requested by the Dockerfile but absent from the lock"
    for name, version in sorted(lock.requested.items()):
        assert pin.APT_VERSION_RE.match(version), f"{name} requested as {version!r}, which is not a version"
    # The transitive dependencies are bytes in the image too: a record of only
    # the requested 8 would leave every dependency floating.
    assert len(lock.installed) > len(lock.requested), (
        "the lock records only the requested packages; their apt-resolved "
        "dependencies are in the image and must be recorded as well"
    )


def test_apt_lock_resolves_the_trixie_time_t_64_rename() -> None:
    """Trixie renamed libglib2.0-0 to libglib2.0-0t64 and kept the old name as a
    virtual provide, so the requested name and the installed name differ. An
    exact-match lookup would call a pinned package missing.
    """
    pin = _pin()
    lock = pin.parse_apt_lock(APT_LOCK.read_text(encoding="utf-8"))
    assert "libglib2.0-0" in lock.requested
    assert lock.resolve("libglib2.0-0") is not None
    assert lock.resolve("tesseract-ocr") == lock.requested["tesseract-ocr"]
    assert lock.resolve("definitely-not-installed") is None


def test_apt_lock_names_an_immutable_mirror() -> None:
    pin = _pin()
    lock = pin.parse_apt_lock(APT_LOCK.read_text(encoding="utf-8"))
    assert "snapshot.debian.org" in lock.mirror, (
        f"the apt mirror floats; expected snapshot.debian.org, got {lock.mirror!r}"
    )


# --------------------------------------------------------------------------
# Model pins. The weights are host-mounted, so the image digest does not cover
# them and a recorded sha256 nothing checks is a fact about the past.
# --------------------------------------------------------------------------


def test_every_model_revision_is_a_commit_not_a_branch() -> None:
    """Heron was recorded `@ main` while the fetch command carried no revision
    at all, so a fresh host would get whatever `main` points at today. The one
    model the layout stage actually depends on was the unpinned one.
    """
    pin = _pin()
    models = pin.parse_models_lock(MODELS_LOCK.read_text(encoding="utf-8"), source=str(MODELS_LOCK))
    assert models, "the model lock must pin at least one artifact"
    for name, model in sorted(models.items()):
        assert re.fullmatch(r"[0-9a-f]{40}", model.revision), (
            f"{name} is pinned at {model.revision!r}, which is a movable ref rather than a commit"
        )
    assert models["heron"].repo == "docling-project/docling-layout-heron"
    assert models["heron"].revision == "8f39ad3c0b4c58e9c2d2c84a38465abf757272d8"


def test_model_lock_records_a_payload_hash_for_every_artifact() -> None:
    pin = _pin()
    models = pin.parse_models_lock(MODELS_LOCK.read_text(encoding="utf-8"), source=str(MODELS_LOCK))
    for name, model in sorted(models.items()):
        assert re.fullmatch(r"[0-9a-f]{64}", model.file_sha256), f"{name} has no payload sha256"
        assert model.file and model.cache_dir


def test_model_pin_accepts_matching_metadata(tmp_path: Path) -> None:
    """The positive leg: real bytes whose sha256 is the pin, with matching
    metadata, must pass the whole gate. `verify_models` hashes the payload as
    well as reading the sidecar, so the payload has to be here.
    """
    pin = _pin()
    models = pin.parse_models_lock(MODELS_LOCK.read_text(encoding="utf-8"), source=str(MODELS_LOCK))
    model = models["heron"]
    directory = tmp_path / model.cache_dir / ".cache" / "huggingface" / "download"
    directory.mkdir(parents=True)
    payload = b"the pinned weights, stand-in for a 171 MB safetensors file"
    digest = hashlib.sha256(payload).hexdigest()
    (tmp_path / model.cache_dir / model.file).write_bytes(payload)
    (directory / f"{model.file}.metadata").write_text(
        f"{model.revision}\n{digest}\n", encoding="utf-8"
    )
    matching = pin.ModelPin(
        name=model.name, repo=model.repo, revision=model.revision, file=model.file,
        file_sha256=digest, cache_dir=model.cache_dir,
    )
    assert pin.verify_models(matching, tmp_path) == []


def test_model_pin_reports_a_moved_commit(tmp_path: Path) -> None:
    """A moved commit must be reported as a moved commit, not as a missing
    payload: the payload here is genuine, so the only disagreement is the
    revision in the sidecar.
    """
    pin = _pin()
    models = pin.parse_models_lock(MODELS_LOCK.read_text(encoding="utf-8"), source=str(MODELS_LOCK))
    model = models["heron"]
    directory = tmp_path / model.cache_dir / ".cache" / "huggingface" / "download"
    directory.mkdir(parents=True)
    payload = b"genuine bytes, so only the recorded commit is wrong"
    digest = hashlib.sha256(payload).hexdigest()
    (tmp_path / model.cache_dir / model.file).write_bytes(payload)
    (directory / f"{model.file}.metadata").write_text(
        f"{'0' * 40}\n{digest}\n", encoding="utf-8"
    )
    moved = pin.ModelPin(
        name=model.name, repo=model.repo, revision=model.revision, file=model.file,
        file_sha256=digest, cache_dir=model.cache_dir,
    )
    problems = pin.verify_models(moved, tmp_path)
    assert any("does not match the pinned" in problem for problem in problems), problems
    assert not any("no payload" in problem for problem in problems), problems


def test_model_pin_reports_a_swapped_payload(tmp_path: Path) -> None:
    """A sidecar that lies about its own ETag must be caught, with the payload
    present so the report is about the ETag claim.
    """
    pin = _pin()
    models = pin.parse_models_lock(MODELS_LOCK.read_text(encoding="utf-8"), source=str(MODELS_LOCK))
    model = models["heron"]
    directory = tmp_path / model.cache_dir / ".cache" / "huggingface" / "download"
    directory.mkdir(parents=True)
    (tmp_path / model.cache_dir / model.file).write_bytes(b"the actual bytes")
    (directory / f"{model.file}.metadata").write_text(
        f"{model.revision}\n{'f' * 64}\n", encoding="utf-8"
    )
    problems = pin.verify_models(model, tmp_path)
    assert any("payload sha256" in problem for problem in problems), problems


def test_missing_model_metadata_is_a_failure_not_a_pass(tmp_path: Path) -> None:
    """The rule the driver already states for a cgroup reading: a reading that
    could not be taken is not a good reading. An absent metadata file must not
    read as 'the revision is correct'.
    """
    pin = _pin()
    models = pin.parse_models_lock(MODELS_LOCK.read_text(encoding="utf-8"), source=str(MODELS_LOCK))
    model = models["heron"]
    problems = pin.verify_models(model, tmp_path / "empty-cache")
    assert problems, "an empty cache must not verify as a matching revision"
    assert any("never fetched" in problem for problem in problems), problems


def test_empty_metadata_is_a_failure_not_a_pass(tmp_path: Path) -> None:
    pin = _pin()
    models = pin.parse_models_lock(MODELS_LOCK.read_text(encoding="utf-8"), source=str(MODELS_LOCK))
    model = models["heron"]
    directory = tmp_path / model.cache_dir / ".cache" / "huggingface" / "download"
    directory.mkdir(parents=True)
    (directory / f"{model.file}.metadata").write_text("", encoding="utf-8")
    assert pin.read_download_metadata(tmp_path / model.cache_dir, model.file) == ("", "")
    problems = pin.verify_models(model, tmp_path)
    assert any("no commit" in problem for problem in problems), problems


def test_model_lock_pinned_to_a_branch_is_rejected() -> None:
    pin = _pin()
    text = (
        "[heron]\nrepo = docling-project/docling-layout-heron\nrevision = main\n"
        "file = model.safetensors\nfile_sha256 = " + "0" * 64 + "\n"
        "cache_dir = docling-project--docling-layout-heron\n"
    )
    with pytest.raises(pin.PinError) as caught:
        pin.parse_models_lock(text, source="<branch>")
    assert "moves under the pin" in str(caught.value)


def test_empty_model_lock_is_rejected() -> None:
    pin = _pin()
    with pytest.raises(pin.PinError):
        pin.parse_models_lock("# nothing pinned\n", source="<empty>")


def test_model_lock_artifact_without_a_hash_is_rejected() -> None:
    pin = _pin()
    text = (
        "[heron]\nrepo = docling-project/docling-layout-heron\n"
        f"revision = {'a' * 40}\nfile = model.safetensors\ncache_dir = docling-project--heron\n"
    )
    with pytest.raises(pin.PinError) as caught:
        pin.parse_models_lock(text, source="<no-hash>")
    assert "file_sha256" in str(caught.value)


# --------------------------------------------------------------------------
# The declared image identity is the one thing the build must reproduce.
# --------------------------------------------------------------------------


def test_expected_image_id_is_declared_outside_the_build_output() -> None:
    """The expected digest must not be read back from the file the build just
    wrote, or the comparison is true by construction. It is declared in
    `image-pin.json`, which no build step writes.
    """
    pin = _pin()
    expected = pin.declared_expected_image_id(PIPELINE_DIR)
    assert expected.startswith("sha256:"), f"expected id is not a sha256: {expected!r}"
    identity_text = IDENTITY.read_text(encoding="utf-8")
    assert expected != pin.parse_image_identity(identity_text), (
        "the declared expectation and the committed snapshot are the same string; "
        "a drift check between them can never fail"
    )


def test_manifest_does_not_contradict_its_declared_sources() -> None:
    """`PIN_MANIFEST.md:36` cited `8e10185f...` while the two files it names as
    authoritative held `0f278c22...`; the manifest is prose, so only a mechanical
    cross-read catches that class of drift.
    """
    pin = _pin()
    manifest_digests = pin.manifest_image_digests(PIN_MANIFEST.read_text(encoding="utf-8"))
    committed = pin.parse_image_identity(IDENTITY.read_text(encoding="utf-8"))
    stale = pin.stale_manifest_digests(
        PIN_MANIFEST.read_text(encoding="utf-8"), known=(committed, L7_RECORDED_IMAGE_ID)
    )
    assert not stale, (
        f"PIN_MANIFEST.md cites {stale} but results/image_identity.txt records {committed} "
        f"and image-pin.json declares {L7_RECORDED_IMAGE_ID}"
    )
    assert manifest_digests, "the guard below cannot fail if it sees no digests at all"


def test_digest_drift_is_detected() -> None:
    """The check the whole task exists for, exercised positively on a wrong
    pair: the exact L7 disagreement must be reported as drift, and only an
    exact match must read as agreement.
    """
    pin = _pin()
    assert L7_RECORDED_IMAGE_ID != COMMITTED_IMAGE_ID
    assert not pin.compare_image_id(L7_RECORDED_IMAGE_ID, COMMITTED_IMAGE_ID), (
        "a differing expected/built pair must NOT be reported as a match"
    )
    assert pin.compare_image_id(COMMITTED_IMAGE_ID, COMMITTED_IMAGE_ID)


# --------------------------------------------------------------------------
# Failure behavior: every one of these must FAIL, never default.
# --------------------------------------------------------------------------


def test_empty_requirements_lock_is_rejected() -> None:
    """The disease itself: an empty lock installs nothing, the `docling==`
    ARG-based path looks plausible, and the build succeeds while drifting."""
    pin = _pin()
    with pytest.raises(pin.PinError) as caught:
        pin.validate_requirements_lock_text("", source="<empty>")
    assert "empty" in str(caught.value).lower()


def test_lock_entry_without_a_hash_is_rejected() -> None:
    pin = _pin()
    with pytest.raises(pin.PinError) as caught:
        pin.validate_requirements_lock_text("docling==2.130.0\n", source="<no-hash>")
    assert "hash" in str(caught.value).lower()


def test_comment_only_lock_is_rejected() -> None:
    """Comments are not requirements; a file of them must not read as pinned."""
    pin = _pin()
    with pytest.raises(pin.PinError):
        pin.validate_requirements_lock_text("# nothing pinned here\n--extra-index-url x\n", source="<c>")


def test_unparseable_requirements_lock_is_rejected_not_skipped() -> None:
    """The driver's rule -- a failed reading is never a zero -- applied to the
    lock. A syntax error must surface as a named failure.
    """
    pin = _pin()
    with pytest.raises(pin.PinError):
        pin.validate_requirements_lock_text("this is not a requirement line\n", source="<junk>")


def test_non_utf8_lock_is_rejected() -> None:
    pin = _pin()
    with pytest.raises(pin.PinError):
        pin.validate_requirements_lock_bytes(b"docling==2.130.0 --hash=sha256:\xff\xfe\n", source="<b>")


def test_dockerfile_with_a_mutable_base_tag_is_rejected() -> None:
    pin = _pin()
    with pytest.raises(pin.PinError) as caught:
        pin.validate_dockerfile_text("FROM python:3.12-slim\n")
    assert "digest" in str(caught.value).lower()


def test_empty_apt_lock_is_rejected() -> None:
    pin = _pin()
    with pytest.raises(pin.PinError):
        pin.validate_apt_lock_text("", source="<empty>")


def test_apt_lock_without_a_mirror_is_rejected() -> None:
    pin = _pin()
    with pytest.raises(pin.PinError) as caught:
        pin.validate_apt_lock_text(
            "[requested]\nca-certificates=20250419\n[installed]\nca-certificates=20250419\n",
            source="<no-mirror>",
        )
    assert "mirror" in str(caught.value).lower()


# --------------------------------------------------------------------------
# Boundary conditions.
# --------------------------------------------------------------------------


def test_lock_declaring_zero_packages_is_rejected() -> None:
    pin = _pin()
    with pytest.raises(pin.PinError):
        pin.validate_requirements_lock_text("--extra-index-url https://example.invalid\n", source="<zero>")


def test_lock_entry_without_a_version_is_rejected() -> None:
    pin = _pin()
    with pytest.raises(pin.PinError) as caught:
        pin.validate_requirements_lock_text("docling\n", source="<no-version>")
    assert "version" in str(caught.value).lower()


def test_requirements_lock_omitting_the_pipeline_itself_is_rejected() -> None:
    """The lock-vs-Dockerfile boundary: a lock without docling would build an
    image with no pipeline in it and every hash check would still pass.
    """
    pin = _pin()
    lock = pin.parse_requirements_lock(REQUIREMENTS_LOCK.read_text(encoding="utf-8"))
    for required in ("docling", "torch", "torchvision", "pytesseract"):
        assert required in lock, f"{required} is missing from the lock"


def test_freeze_with_a_line_that_is_not_a_pin_is_rejected() -> None:
    pin = _pin()
    with pytest.raises(pin.PinError):
        pin.parse_freeze("docling==2.130.0\n-r other.txt\n")


def test_image_identity_without_a_digest_is_rejected() -> None:
    pin = _pin()
    with pytest.raises(pin.PinError):
        pin.parse_image_identity("created=2026-01-01T00:00:00Z\n")


# --------------------------------------------------------------------------
# build.sh integration: the drift check must actually be wired to a failure.
# --------------------------------------------------------------------------


def test_apt_lock_rejects_a_requested_version_the_installed_set_disagrees_with() -> None:
    """`[requested]` and `[installed]` are two records of the same fact. Nothing
    cross-checked them, so a lock could request curl 8.14.1 while recording
    1.0.0 as installed and still validate -- the L7 defect class (two sources of
    truth disagreeing, nothing checking) reproducing inside the new mechanism.
    """
    pin = _pin()
    text = (
        "[mirror]\nhttp://snapshot.debian.org/archive/debian/20260929T083940Z\n"
        "[requested]\ncurl=8.14.1-2+deb13u5\n"
        "[installed]\ncurl=1.0.0\n"
    )
    with pytest.raises(pin.PinError) as caught:
        pin.validate_apt_lock_text(text, source="<disagree>")
    message = str(caught.value)
    assert "8.14.1-2+deb13u5" in message and "1.0.0" in message, (
        f"the error must cite both versions so the disagreement is readable: {message!r}"
    )


def test_apt_lock_validates_the_installed_side_independently() -> None:
    """`[installed]` must not shadow `[requested]` in the version check: merging
    the two dicts let a malformed installed version pass whenever the same
    package was also requested.
    """
    pin = _pin()
    text = (
        "[mirror]\nhttp://snapshot.debian.org/archive/debian/20260929T083940Z\n"
        "[requested]\ncurl=8.14.1-2+deb13u5\n"
        "[installed]\ncurl=@@@@@\n"
    )
    with pytest.raises(pin.PinError) as caught:
        pin.validate_apt_lock_text(text, source="<shadowed>")
    assert "curl" in str(caught.value)


def test_dockerfile_apt_versions_must_match_the_apt_lock() -> None:
    """`requirements.lock` is authoritative for the Python layer; until this
    check, `apt-packages.lock` was decorative for the OS layer: a Dockerfile
    installing `curl=99.99.99` validated cleanly against a lock naming 8.14.1.
    """
    pin = _pin()
    apt = pin.parse_apt_lock(APT_LOCK.read_text(encoding="utf-8"))
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    pin.validate_dockerfile_text(dockerfile, apt=apt)

    drifted = dockerfile.replace("curl=8.14.1-2+deb13u5", "curl=99.99.99")
    assert drifted != dockerfile, "the committed Dockerfile no longer pins curl this way"
    with pytest.raises(pin.PinError) as caught:
        pin.validate_dockerfile_text(drifted, apt=apt)
    assert "99.99.99" in str(caught.value), str(caught.value)


# The five mutations below all validated CLEAN before the check became
# three-way. `dockerfile_apt_pins` returned only tokens carrying an `=`, so a
# bare name was invisible to it, and it iterated the Dockerfile's tokens rather
# than the lock's names, so a package absent from the Dockerfile was never
# asked about. Each mutation is a real downgrade of the OS layer, and each
# validated, which is what made apt-packages.lock decorative for that layer.


def _apt_dockerfile_mutations() -> dict[str, tuple[str, str]]:
    """(id, dockerfile mutation, substring the failure must name)."""
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    return {
        # The pin itself is removed: apt resolves the newest version on the day.
        "unpinned_bare_name": (
            dockerfile.replace("tesseract-ocr-chi-sim=1:4.1.0-2", "tesseract-ocr-chi-sim"),
            "tesseract-ocr-chi-sim",
        ),
        # The `=` is dropped but the version is left as a bare word, which apt
        # reads as a second package name, not as a constraint.
        "version_without_equals": (
            dockerfile.replace("curl=8.14.1-2+deb13u5", "curl 8.14.1-2+deb13u5"),
            "8.14.1-2+deb13u5",
        ),
        # Deleting a whole line is invisible to a check that only reads tokens.
        "line_deleted": (
            dockerfile.replace(
                "        tesseract-ocr-chi-sim=1:4.1.0-2 \\\n", ""
            ),
            "tesseract-ocr-chi-sim",
        ),
        # A package the lock never requested enters the image unpinned-by-lock.
        "package_absent_from_the_lock": (
            dockerfile.replace(
                "        ca-certificates=20250419 \\\n",
                "        ca-certificates=20250419 \\\n        wget=1.99 \\\n",
            ),
            "wget",
        ),
        # Fully unpinned, with no version to lose.
        "unpinned_added": (
            dockerfile.replace(
                "        ca-certificates=20250419 \\\n",
                "        ca-certificates=20250419 \\\n        nano \\\n",
            ),
            "nano",
        ),
    }


@pytest.mark.parametrize(
    ("mutation", "subject"),
    [(name, subject) for name, (_, subject) in _apt_dockerfile_mutations().items()],
)
def test_apt_dockerfile_mutations_are_rejected(mutation: str, subject: str) -> None:
    """The negative legs of the Dockerfile↔apt-lock check, one per mutation.

    Each asserts the failure names the offending token, so the test cannot be
    satisfied by an unrelated rule rejecting the mutated file for some other
    reason.
    """
    pin = _pin()
    apt = pin.parse_apt_lock(APT_LOCK.read_text(encoding="utf-8"))
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    pin.validate_dockerfile_text(dockerfile, apt=apt)  # the positive leg

    mutated, _ = _apt_dockerfile_mutations()[mutation]
    assert mutated != dockerfile, f"the {mutation} mutation no longer alters the Dockerfile"
    with pytest.raises(pin.PinError) as caught:
        pin.validate_dockerfile_text(mutated, apt=apt)
    assert subject in str(caught.value), (
        f"the {mutation} failure must name {subject!r}: {str(caught.value)!r}"
    )


def test_every_apt_token_is_version_pinned() -> None:
    """The rule behind the first two mutations, on the token parser itself, so
    the test cannot be vacuous through the rest of the validation: every
    non-flag argument on the `apt-get install` line is returned, and one with no
    `=` is returned as an unpinned token rather than dropped.
    """
    pin = _pin()
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    tokens = pin.dockerfile_apt_tokens(dockerfile)
    assert len(tokens) == 8, f"the install line has 8 packages, got {tokens}"
    assert all("=" in token for token in tokens), f"unpinned tokens: {tokens}"
    # The list stops at the `&&`; a later command in the same joined RUN step is
    # not an apt argument, and treating it as one is how an unpinned package
    # would be reported against a token that was never apt's.
    assert not any("rm" in token or "sources.list" in token for token in tokens), tokens

    unpinned = DOCKERFILE.read_text(encoding="utf-8").replace(
        "        ca-certificates=20250419 \\\n",
        "        ca-certificates=20250419 \\\n        nano \\\n",
    )
    assert pin.dockerfile_apt_tokens(unpinned) != tokens
    assert "nano" in pin.dockerfile_apt_tokens(unpinned)

    with pytest.raises(pin.PinError) as caught:
        pin.validate_dockerfile_text(unpinned, apt=pin.parse_apt_lock(APT_LOCK.read_text(encoding="utf-8")))
    assert "nano" in str(caught.value), str(caught.value)


def test_apt_tokens_that_cannot_be_pinned_are_named() -> None:
    """A token the check cannot require a version on must be named in the
    module, not left to be discovered as a build failure. The set is empty
    today: every token on the `apt-get install` line is a real apt package.
    """
    pin = _pin()
    assert pin.UNVERSIONABLE_APT_TOKENS == frozenset(), (
        f"tokens exempt from the version-pin rule: {sorted(pin.UNVERSIONABLE_APT_TOKENS)}; "
        "an exemption must be added here deliberately, not discovered by a build failing"
    )


def test_snapshot_truncation_guard_still_sees_a_raw_redirect(tmp_path: Path) -> None:
    """The guard must match how build.sh writes now. It looked for the literal
    `"${HERE}/results/"`, which the rewrite replaced with `"${RESULTS}/..."`, so
    appending the exact regression it exists to prevent left it green.
    """
    build = BUILD_SCRIPT.read_text(encoding="utf-8")
    offenders = [line.strip() for line in _redirection_offenders(build)]
    assert not offenders, f"direct truncation of a committed snapshot: {offenders}"

    regressed = build + '\ndocker image inspect x > "${RESULTS}/image_identity.txt"\n'
    offenders = [line.strip() for line in _redirection_offenders(regressed)]
    assert offenders, "a raw > redirect into results/ must be caught"


def test_build_script_delegates_verification_to_the_pin_module() -> None:
    """build.sh stays orchestration; the pin rules live in one testable unit.

    Asserting the bare substrings "pin.py" and "verify" is satisfied by a
    comment and an echo, so this required each subcommand to appear in a real
    `python3 .../pin.py <subcommand>` invocation -- deleting every invocation
    while leaving the prose would otherwise leave this green.
    """
    build = BUILD_SCRIPT.read_text(encoding="utf-8")
    invoked = {
        match.group(1)
        for line in build.splitlines()
        if "pin.py" in line
        for match in [re.search(r"pin\.py\"?\s+(\S+)", line)]
        if match
    }
    missing = sorted({"self-check", "verify-models", "compare-image-id"} - invoked)
    assert not missing, f"build.sh never invokes pin.py {missing}"


def test_manifest_cites_at_least_one_digest_the_guard_can_see() -> None:
    """The guard `test_manifest_does_not_contradict_its_declared_sources` is
    built on must not match nothing. The C1 rewrite of the manifest cites every
    image digest truncated (`sha256:8e10185f…`), so a 64-hex-only matcher
    returns `[]` and the assertion below can never fail -- the L7 regression
    test green because it is blind.
    """
    pin = _pin()
    digests = pin.manifest_image_digests(PIN_MANIFEST.read_text(encoding="utf-8"))
    assert digests, (
        "the manifest guard matches no digest at all, so it cannot detect drift; "
        "the manifest cites its image digests in truncated form"
    )


def test_a_stale_truncated_digest_in_the_manifest_is_caught() -> None:
    """The negative leg of the same guard. A guard with no negative leg is the
    assertion that cannot fail. The stale digest goes into the identity section,
    which is where a real L7 drift would appear.
    """
    pin = _pin()
    text = PIN_MANIFEST.read_text(encoding="utf-8")
    assert pin.manifest_identity_section(text).endswith("\n")
    contaminated = text.replace(
        pin.manifest_identity_section(text),
        pin.manifest_identity_section(text) + "\n| stale | `sha256:deadbeef…` | drift |\n",
    )
    stale = pin.stale_manifest_digests(
        contaminated, known=(COMMITTED_IMAGE_ID, L7_RECORDED_IMAGE_ID)
    )
    assert stale, "a truncated digest matching no known id must be reported as stale"


def test_a_truncated_digest_matching_a_known_id_is_not_stale() -> None:
    pin = _pin()
    text = PIN_MANIFEST.read_text(encoding="utf-8")
    assert not pin.stale_manifest_digests(
        text, known=(COMMITTED_IMAGE_ID, L7_RECORDED_IMAGE_ID)
    ), "the committed manifest must agree with the ids it cites"


def test_dockerfile_does_not_claim_a_check_that_does_not_exist() -> None:
    """C1 exists to delete prose that disagrees with its artifact, so the
    Dockerfile itself is held to it. It used to say `pin.py` "checks the
    build's dpkg status against" the lock; `pin.py` mentioned dpkg nowhere, and
    `results/image_apt_status.txt` was written by the build and read by nothing.

    `pin.py` now cross-checks the Dockerfile's apt `=version` tokens against
    `apt-packages.lock`, so that phrasing is allowed again -- but only for the
    apt pins. A claim that the dpkg *installed set* is gated stays false, because
    nothing compares the two.
    """
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    claims = [
        line.strip()
        for line in dockerfile.splitlines()
        if "pin.py" in line and "dpkg" in line.lower()
    ]
    assert not claims, (
        f"the Dockerfile claims pin.py gates the dpkg installed set, which no code does: {claims}"
    )


def test_model_gate_runs_before_anything_can_exit_75() -> None:
    """The model gate was dead code: `compare-image-id` exited 75 first, so on
    this builder -- where the build always fails the identity check -- the
    `verify-models` block was never reached. Ordering is asserted statically
    because exercising it needs a real build; a gate that only runs on the
    success path of an unsatisfiable check is not a gate.
    """
    lines = BUILD_SCRIPT.read_text(encoding="utf-8").splitlines()

    def first_index(needle: str) -> int:
        # Match the pin.py invocation, not a comment about it, or the comment
        # that explains the ordering would be counted as the gate itself.
        matches = [index for index, line in enumerate(lines) if "pin.py\"" in line and needle in line]
        assert matches, f"build.sh no longer invokes pin.py {needle}"
        return matches[0]

    model_gate = first_index("verify-models")
    identity_check = first_index("compare-image-id")
    assert model_gate < identity_check, (
        "the model gate must run before the identity check, or a build that "
        "fails compare-image-id can never reach it"
    )
    early_exits = [index for index, line in enumerate(lines) if "exit 75" in line]
    assert early_exits, "build.sh no longer has an exit-75 path to order against"
    assert model_gate < min(early_exits), (
        f"the model gate at line {model_gate + 1} is after the first exit 75 at "
        f"line {min(early_exits) + 1}, so it is unreachable while that check fails"
    )


def test_build_script_never_truncates_a_committed_snapshot_before_it_succeeds() -> None:
    """`results/image_identity.txt` is one side of the L7 reconciliation. A `>`
    redirection that empties it before the `docker run` that refills it fails
    leaves a committed file destroyed by a failed build, so every snapshot write
    must go to a temp path and be moved into place only on success.
    """
    build = BUILD_SCRIPT.read_text(encoding="utf-8")
    offenders = [line.strip() for line in _redirection_offenders(build)]
    assert not offenders, f"direct truncation of a committed snapshot: {offenders}"


def test_drift_check_is_reachable_and_fails_loudly(tmp_path: Path) -> None:
    """End to end through the real CLI, with no docker: a declared expected id
    that does not match the observed one must exit non-zero and name itself.
    """
    observed = tmp_path / "image_identity.txt"
    observed.write_text(f"image_id={COMMITTED_IMAGE_ID}\n", encoding="utf-8")
    declared = tmp_path / "image-pin.json"
    declared.write_text(f'{{"expected_image_id": "{L7_RECORDED_IMAGE_ID}"}}\n', encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(PIN_MODULE_PATH),
            "compare-image-id",
            "--pin",
            str(declared),
            "--observed",
            str(observed),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0, "a mismatched digest must fail the build"
    assert "FATAL" in result.stderr, f"the failure must name itself: {result.stderr!r}"


def test_matching_declared_digest_passes_the_same_cli(tmp_path: Path) -> None:
    observed = tmp_path / "image_identity.txt"
    observed.write_text(f"image_id={COMMITTED_IMAGE_ID}\n", encoding="utf-8")
    declared = tmp_path / "image-pin.json"
    declared.write_text(f'{{"expected_image_id": "{COMMITTED_IMAGE_ID}"}}\n', encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(PIN_MODULE_PATH),
            "compare-image-id",
            "--pin",
            str(declared),
            "--observed",
            str(observed),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"an exact match must pass: {result.stderr!r}"


def test_self_check_subcommand_validates_the_committed_pin() -> None:
    """What `build.sh` runs before it builds anything."""
    result = subprocess.run(
        [sys.executable, str(PIN_MODULE_PATH), "self-check", "--pipeline-dir", str(PIPELINE_DIR)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"the committed pin must validate: {result.stderr!r}"


def test_self_check_fails_when_the_lock_is_corrupted(tmp_path: Path) -> None:
    """The negative leg of the same code path, on a copied tree so the committed
    pin is untouched. `results/` is copied in: without it self-check dies on the
    missing freeze before the lock is ever examined, and the generic
    `returncode != 0` / `"FATAL" in stderr` assertion this test used to make
    would pass on that unrelated failure. The subject is therefore named in the
    assertion, so the rule it actually exercises -- `validate_requirements_lock_text`
    rejecting a lock entry with no `--hash=sha256:` -- is the only one that can
    produce this failure.

    It does **not** exercise the Dockerfile's `--require-hashes` rule. This
    docstring used to claim it did, and that was false: the corruption below
    trips the lock-entry rule, so deleting the `--require-hashes` rule left
    this test green. The missing leg is
    `test_a_dockerfile_without_require_hashes_is_rejected` below.
    """
    import shutil

    staging = _staged_tree(tmp_path)
    (staging / "requirements.lock").write_text("docling==2.130.0\n", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(staging / "pin.py"), "self-check", "--pipeline-dir", str(staging)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0, "a lock with unhashed entries must fail self-check"
    assert "--hash=sha256" in result.stderr, (
        f"self-check must fail on the corrupted lock, not on something else: {result.stderr!r}"
    )


def test_staging_tree_with_results_is_otherwise_valid(tmp_path: Path) -> None:
    """Guards the guard: the staging tree the negative leg above builds must
    pass self-check untouched, so a pass/fail there can only be the corruption.
    """
    result = subprocess.run(
        [sys.executable, str(_staged_tree(tmp_path) / "pin.py"), "self-check",
         "--pipeline-dir", str(tmp_path / "g4_pipeline")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"the unmodified staging tree must validate: {result.stderr!r}"


def test_a_dockerfile_without_require_hashes_is_rejected() -> None:
    """The rule `test_self_check_fails_when_the_lock_is_corrupted` used to
    claim it protected. `pin.py`'s `validate_dockerfile_text` rejects a
    Dockerfile that installs the lock without `--require-hashes`; without that
    flag pip treats the `--hash` lines as decoration and re-resolves every
    artifact, so a hash-locked file that looks pinned enforces nothing.

    The subject is named in the assertion, so a file that fails the rule for
    some other reason (say, a mutated base digest) cannot satisfy this.
    """
    pin = _pin()
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    assert "--require-hashes" in dockerfile, "the committed Dockerfile installs under --require-hashes"

    # Anchored on the flag itself rather than on the whole command line, so
    # adding another pip flag to the install cannot silently turn this into a
    # no-op replacement and leave the rule untested.
    without_flag = re.sub(
        r"^(\s*RUN\s+pip install\b[^\n]*?)\s*--require-hashes\b",
        r"\1",
        dockerfile,
        flags=re.MULTILINE,
    )
    assert without_flag != dockerfile, "the committed Dockerfile no longer installs the lock this way"
    with pytest.raises(pin.PinError) as caught:
        pin.validate_dockerfile_text(without_flag)
    assert "--require-hashes" in str(caught.value), str(caught.value)


def test_the_require_hashes_rule_is_what_rejects_a_flagless_dockerfile() -> None:
    """Non-vacuity for the test above, without leaving the repo: the mutated
    Dockerfile must still pass every *other* Dockerfile rule, so the only rule
    that can reject it is the `--require-hashes` one. Deleting the rule from a
    copy of `pin.py` makes this fail; that is the same fact demonstrated by
    hand.
    """
    pin = _pin()
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    # Anchored on the flag itself rather than on the whole command line, so
    # adding another pip flag to the install cannot silently turn this into a
    # no-op replacement and leave the rule untested.
    without_flag = re.sub(
        r"^(\s*RUN\s+pip install\b[^\n]*?)\s*--require-hashes\b",
        r"\1",
        dockerfile,
        flags=re.MULTILINE,
    )
    # The apt lock is not passed, so the three-way apt rules are out of scope
    # here; every remaining rule must be satisfied by the mutated file. The
    # flag survives in the file's own prose, which is exactly why the rule is
    # stated over a real install line rather than a substring.
    assert any(
        "--require-hashes" in line for line in dockerfile.splitlines()
    ), "the committed Dockerfile no longer mentions the flag at all"
    assert not pin._installs_under_require_hashes(without_flag), (
        "the mutated Dockerfile must fail the --require-hashes rule, not pass it"
    )
    assert pin._installs_under_require_hashes(dockerfile)
    for name, value in (
        ("base image digest-pinned", "@sha256:" in without_flag),
        ("installs the lock", "requirements.lock" in without_flag),
        ("no inline pins", not any(
            token in without_flag for token in ("docling==", "torch==", "torchvision==", "pytesseract==")
        )),
    ):
        assert value, f"{name} fails independently, so the --require-hashes rule is not isolated"
    with pytest.raises(pin.PinError) as caught:
        pin.validate_dockerfile_text(without_flag)
    assert "--require-hashes" in str(caught.value), (
        f"the only failing rule must be --require-hashes: {str(caught.value)!r}"
    )


def test_a_payload_swapped_under_intact_metadata_is_detected(tmp_path: Path) -> None:
    """`verify_models` used to trust the metadata sidecar's own ETag claim and
    never open the payload, so replacing the bytes while leaving the sidecar
    intact was invisible -- the same gap the record files against TableFormer,
    merely relabelled "pinned".
    """
    pin = _pin()
    models = pin.parse_models_lock(MODELS_LOCK.read_text(encoding="utf-8"), source=str(MODELS_LOCK))
    model = models["heron"]
    directory = tmp_path / model.cache_dir
    (directory / ".cache" / "huggingface" / "download").mkdir(parents=True)
    (directory / ".cache" / "huggingface" / "download" / f"{model.file}.metadata").write_text(
        f"{model.revision}\n{model.file_sha256}\n", encoding="utf-8"
    )
    # Metadata claims the correct commit AND the correct hash; the bytes differ.
    (directory / model.file).write_bytes(b"not the pinned weights")
    problems = pin.verify_models(model, tmp_path)
    assert any("payload" in problem for problem in problems), (
        f"a payload that does not hash to the pin must be reported: {problems}"
    )


def test_verify_models_accepts_a_genuinely_matching_payload(tmp_path: Path) -> None:
    """The positive leg, so the hash check above is not satisfied by refusing
    everything: real bytes with the correct hash and metadata must pass.
    """
    pin = _pin()
    models = pin.parse_models_lock(MODELS_LOCK.read_text(encoding="utf-8"), source=str(MODELS_LOCK))
    model = models["heron"]
    directory = tmp_path / model.cache_dir
    (directory / ".cache" / "huggingface" / "download").mkdir(parents=True)
    (directory / model.file).write_bytes(b"the real weights stand-in")
    real_digest = hashlib.sha256(b"the real weights stand-in").hexdigest()
    (directory / ".cache" / "huggingface" / "download" / f"{model.file}.metadata").write_text(
        f"{model.revision}\n{real_digest}\n", encoding="utf-8"
    )
    patched = pin.ModelPin(
        name=model.name, repo=model.repo, revision=model.revision, file=model.file,
        file_sha256=real_digest, cache_dir=model.cache_dir,
    )
    assert pin.verify_models(patched, tmp_path) == []


def test_model_gate_is_not_skipped_when_there_is_no_cache() -> None:
    """A never-populated cache is a real state, not an absent check: build.sh
    else-branched to a `note:` and exited 0, so on any host without a warm cache
    the model pin was never checked at all.
    """
    build = BUILD_SCRIPT.read_text(encoding="utf-8")
    gate = build[build.index('if [[ -d "${MODEL_CACHE}" ]]'):]
    gate = gate[: gate.index("\nfi")]
    else_branch = gate[gate.index("else"):]
    assert "exit" in else_branch, (
        "the no-cache branch must fail the build, not continue past a check "
        "that never ran"
    )


def test_build_shell_is_syntactically_valid() -> None:
    result = subprocess.run(["bash", "-n", str(BUILD_SCRIPT)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_verify_models_cli_fails_loudly_on_a_moved_revision(tmp_path: Path) -> None:
    """The real CLI against a cache whose Heron commit has moved."""
    models = _pin().parse_models_lock(MODELS_LOCK.read_text(encoding="utf-8"), source=str(MODELS_LOCK))
    model = models["heron"]
    directory = tmp_path / model.cache_dir / ".cache" / "huggingface" / "download"
    directory.mkdir(parents=True)
    (directory / f"{model.file}.metadata").write_text(f"{'b' * 40}\n{model.file_sha256}\n", encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(PIN_MODULE_PATH),
            "verify-models",
            "--pin",
            str(MODELS_LOCK),
            "--cache-root",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0, "a moved model revision must fail verification"
    assert "FATAL" in result.stderr, f"the failure must name itself: {result.stderr!r}"


def test_verify_models_cli_fails_on_an_empty_cache(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(PIN_MODULE_PATH),
            "verify-models",
            "--pin",
            str(MODELS_LOCK),
            "--cache-root",
            str(tmp_path / "nothing-here"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0, "an empty cache must not verify"
    assert "never fetched" in result.stderr
