"""The declaration must agree with the files it describes, or self-check fails.

`image-pin.json` carried `base_image`, `apt_snapshot` and `python_requirements`
as a static copy of facts that also live in the Dockerfile and the locks. Nothing
compared the two, so editing the Dockerfile's base digest or apt snapshot, or a
lock gaining/losing a package, left the declaration silently stale -- the same
"two sources disagree, nothing checks" class the rest of `pin.py` exists to close.

Each mutation leg here fails *before* the gate existed and passes now: edit one
committed file while leaving its declared counterpart untouched, and `pin.py
self-check` must exit non-zero and name the specific drift, not some unrelated
rule. The legs are the point -- a cross-check with only a positive test cannot
fail.

`REQUIRED_PACKAGES` (F3) and the dead `_pypi_hashes` (F4) are held here too, since
both are single-source-of-truth repairs in the same directory.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PIPELINE_DIR = PROJECT_ROOT / "scripts" / "g4_pipeline"
PIN_MODULE_PATH = PIPELINE_DIR / "pin.py"
GEN_MODULE_PATH = PIPELINE_DIR / "gen_requirements_lock.py"
DOCKERFILE = PIPELINE_DIR / "Dockerfile"
APT_LOCK = PIPELINE_DIR / "apt-packages.lock"
IMAGE_PIN = PIPELINE_DIR / "image-pin.json"

_BASE_PIN: ModuleType | None = None


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _pin() -> ModuleType:
    global _BASE_PIN
    if _BASE_PIN is None:
        _BASE_PIN = _load("g4_pipeline_pin_crosscheck", PIN_MODULE_PATH)
    return _BASE_PIN


def _staged_tree(tmp_path: Path) -> Path:
    """A writable copy of the pipeline dir complete enough for self-check."""
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


def _self_check(tree: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(tree / "pin.py"), "self-check", "--pipeline-dir", str(tree)],
        capture_output=True,
        text=True,
        check=False,
    )


# --------------------------------------------------------------------------
# Positive leg: the committed tree's declaration agrees with its files.
# --------------------------------------------------------------------------


def test_self_check_passes_with_the_cross_checks_wired(tmp_path: Path) -> None:
    """The staging tree untouched must validate -- so a non-zero below can only
    be the mutation, never an unrelated gap in the copied fixtures."""
    result = _self_check(_staged_tree(tmp_path))
    assert result.returncode == 0, f"the unmodified staging tree must validate: {result.stderr!r}"


def test_committed_declaration_agrees_with_the_files() -> None:
    pin = _pin()
    declared = pin.load_image_pin(IMAGE_PIN)
    apt = pin.parse_apt_lock(APT_LOCK.read_text(encoding="utf-8"))
    lock_entries = pin.parse_requirements_lock(
        (PIPELINE_DIR / "requirements.lock").read_text(encoding="utf-8")
    )
    pin.validate_image_pin_agrees_with_files(
        pin=declared,
        dockerfile_text=DOCKERFILE.read_text(encoding="utf-8"),
        apt=apt,
        lock_count=len(lock_entries),
    )


# --------------------------------------------------------------------------
# Mutation legs: each breaks the cross-check, and must be reported as drift.
# --------------------------------------------------------------------------


def _mutate(tree: Path, relative: str, old: str, new: str) -> None:
    path = tree / relative
    text = path.read_text(encoding="utf-8")
    assert old in text, f"the {relative} mutation target {old!r} is no longer present"
    path.write_text(text.replace(old, new), encoding="utf-8")


@pytest.mark.parametrize(
    ("relative", "old", "new", "subject"),
    [
        # Dockerfile base digest moves while the declaration stays put.
        (
            "Dockerfile",
            "44ff437bba879d4941b710a369a8f19266aea34b29002807f0c487fabc9eec9b",
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "base_image",
        ),
        # Dockerfile apt snapshot moves while the declaration stays put.
        (
            "Dockerfile",
            "ARG APT_SNAPSHOT=20260929T083940Z",
            "ARG APT_SNAPSHOT=20270101T000000Z",
            "apt_snapshot",
        ),
        # The lock gains a package the declared count does not know about.
        (
            "image-pin.json",
            '"python_requirements": 104,',
            '"python_requirements": 999,',
            "python_requirements",
        ),
    ],
    ids=["base-digest", "apt-snapshot", "requirement-count"],
)
def test_a_stale_declaration_fails_self_check(
    tmp_path: Path, relative: str, old: str, new: str, subject: str
) -> None:
    """The gate's reason to exist: one declared value going stale against its
    file is caught by `self-check`, exits non-zero, and names the specific
    disagreement so the operator is never told a stale pin is fine."""
    tree = _staged_tree(tmp_path)
    _mutate(tree, relative, old, new)
    result = _self_check(tree)
    assert result.returncode != 0, f"a stale {subject} declaration must fail self-check"
    assert subject in result.stderr, (
        f"the failure must name {subject!r}, not some unrelated rule: {result.stderr!r}"
    )


def test_the_base_digest_mutation_is_caught_by_the_base_leg_only(tmp_path: Path) -> None:
    """Non-vacuity: the base-digest mutation must trip the base_image leg, not
    be rejected incidentally by another rule. The mutated Dockerfile still has a
    digest-pinned FROM, still installs the lock under --require-hashes, and keeps
    its apt snapshot, so only the declaration-vs-Dockerfile base comparison can
    reject it."""
    pin = _pin()
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    mutated = dockerfile.replace(
        "44ff437bba879d4941b710a369a8f19266aea34b29002807f0c487fabc9eec9b",
        "b" * 64,
    )
    assert mutated != dockerfile
    # Every other Dockerfile rule still holds for the mutated file.
    apt = pin.parse_apt_lock(APT_LOCK.read_text(encoding="utf-8"))
    pin.validate_dockerfile_text(mutated, apt=apt)
    # Only the cross-check sees the disagreement with the declaration.
    lock_count = len(
        pin.parse_requirements_lock(
            (PIPELINE_DIR / "requirements.lock").read_text(encoding="utf-8")
        )
    )
    with pytest.raises(pin.PinError) as caught:
        pin.validate_image_pin_agrees_with_files(
            pin=pin.load_image_pin(IMAGE_PIN),
            dockerfile_text=mutated,
            apt=apt,
            lock_count=lock_count,
        )
    assert "base_image" in str(caught.value), str(caught.value)


# --------------------------------------------------------------------------
# F3: REQUIRED_PACKAGES is the single source; the inline-pin rule is derived.
# --------------------------------------------------------------------------


def test_the_escape_legs_a_first_match_or_membership_would_miss_are_caught(
    tmp_path: Path,
) -> None:
    """A duplicate `ARG APT_SNAPSHOT` is what Docker actually builds against (the
    last one before first use), and a second `FROM` makes the declared base a
    non-base stage. Both slipped past a first-match read and a membership check;
    self-check must reject each as drift, naming the ambiguity."""
    tree = _staged_tree(tmp_path / "dup")
    dockerfile = tree / "Dockerfile"
    text = dockerfile.read_text(encoding="utf-8")
    duplicated = text.replace(
        "ARG APT_SNAPSHOT=20260929T083940Z",
        "ARG APT_SNAPSHOT=20260929T083940Z\nARG APT_SNAPSHOT=20270101T000000Z",
        1,
    )
    assert duplicated != text
    dockerfile.write_text(duplicated, encoding="utf-8")
    result = _self_check(tree)
    assert result.returncode != 0, "a second APT_SNAPSHOT ARG must not self-check clean"
    assert "APT_SNAPSHOT" in result.stderr, result.stderr

    tree = _staged_tree(tmp_path / "stage2")
    dockerfile = tree / "Dockerfile"
    text = dockerfile.read_text(encoding="utf-8")
    multistage = text.rstrip() + (
        "\nFROM python:3.12-slim@sha256:"
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa AS builder\n"
    )
    dockerfile.write_text(multistage, encoding="utf-8")
    result = _self_check(tree)
    assert result.returncode != 0, "a second FROM stage must not self-check clean"
    assert "FROM references" in result.stderr, result.stderr


def test_pin_get_cli_answers_hits_and_fails_closed_on_misses(tmp_path: Path) -> None:
    """build.sh depends on `pin.py get` for its builder values; the CLI itself is
    the fail-closed surface: a declared path prints its value with exit 0, an
    undeclared dotted path exits 65 naming the path, so a corrupt or incomplete
    declaration can never read as an empty waiver."""
    hit = subprocess.run(
        [
            sys.executable, str(PIN_MODULE_PATH), "get",
            "--pipeline-dir", str(PIPELINE_DIR), "--path", "builder.buildx_builder",
        ],
        capture_output=True, text=True, check=False,
    )
    assert hit.returncode == 0, hit.stderr
    assert hit.stdout.strip(), "a declared builder value must be printed, not blank"

    tree = _staged_tree(tmp_path)
    miss = subprocess.run(
        [
            sys.executable, str(tree / "pin.py"), "get",
            "--pipeline-dir", str(tree), "--path", "builder.no_such_field",
        ],
        capture_output=True, text=True, check=False,
    )
    assert miss.returncode == 65, f"an undeclared path must exit 65: {miss.stderr!r}"
    assert "builder.no_such_field" in miss.stderr, miss.stderr


def test_inline_pin_rule_is_derived_from_required_packages() -> None:
    """`validate_dockerfile_text` rejects inline `<pkg>==` pins. It used to hold
    a second literal copy of the package names, so a package added to
    REQUIRED_PACKAGES was not covered by the inline-pin rule until someone
    remembered to edit both. A Dockerfile pinning any REQUIRED_PACKAGES member
    inline must now be rejected without touching the rule."""
    pin = _pin()
    assert pin.REQUIRED_PACKAGES, "the package tuple must not be empty"
    for name in pin.REQUIRED_PACKAGES:
        offending = (
            f"FROM x@sha256:{'a' * 64}\n"
            "RUN pip install --require-hashes -r requirements.lock\n"
            f"RUN pip install {name}==1.2.3\n"
        )
        with pytest.raises(pin.PinError) as caught:
            pin.validate_dockerfile_text(offending)
        assert name in str(caught.value), (
            f"an inline {name}== pin must be rejected by the derived rule: {str(caught.value)!r}"
        )


# --------------------------------------------------------------------------
# F4: the dead hash helper is gone, and the live one is the sole path.
# --------------------------------------------------------------------------


def test_dead_pypi_hashes_helper_is_removed() -> None:
    """`gen_requirements_lock._pypi_hashes` had no callers (grep-confirmed): the
    resolver path is `_usable_hashes`. Dead code that looks like a hash source is
    a trap, so it is deleted; this guards against it being re-added as a
    second, untested PyPI fetch."""
    gen = _load("g4_pipeline_gen_lock", GEN_MODULE_PATH)
    assert not hasattr(gen, "_pypi_hashes"), "_pypi_hashes is dead code and must stay removed"
    # The real resolver entry point still exists.
    assert hasattr(gen, "_usable_hashes")
