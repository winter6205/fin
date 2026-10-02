"""The model fetch must land on the commit `models.lock` declares, or fail.

`models.lock` pins four artifacts at 40-character commits with their payload
sha256, and `pin.py verify-models` checks a cache against it. That left the pin
**declared and verified but not enforced at fetch time**: `run_measured.sh` ran

    docling-tools models download --output-dir /models layout tableformer

with no revision anywhere. `docling-tools` cannot express one -- read-only inside
`fin-g4-pipeline:t2a`, `models download --help` offers `--output-dir`,
`--force`, `--all`, `--quiet`, `--easyocr-lang` and `--rapidocr-backend-lang`,
and nothing else -- and the revision it would have used is
`LayoutObjectDetectionOptions.from_preset("layout_heron_default").model_spec.revision`,
which is the string `'main'`. So a fresh host fetched whatever `main` pointed at
that day. Heron is the model the layout stage actually depends on, and it was
the unpinned one.

These tests hold the wiring that closes it, and they are deliberately hostile.
The happy path is one test of many, because a fetch only ever run against an
already-correct cache cannot fail and therefore proves nothing. The two legs
that matter are the fetch *argument list* -- does the declared commit reach the
container? -- and the container-side *program* -- does it bind that argument to
`snapshot_download`? Each is asserted, because a wrong answer in either one
fetches silently and the harness only sees a zero exit from a container that
never fetched.

Hermetic: `run_measured.sh` is executed for real from a staged copy, with a fake
`docker` on `PATH` that records its argv. No image is booted, no bytes are
downloaded, no network is touched, and the real `~/.cache/fin-g4-docling-models`
is never read or written.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import types
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PIPELINE_DIR = PROJECT_ROOT / "scripts" / "g4_pipeline"
RUN_SCRIPT = PIPELINE_DIR / "run_measured.sh"
MODELS_LOCK = PIPELINE_DIR / "models.lock"
PIN_MODULE_PATH = PIPELINE_DIR / "pin.py"

# The delimiters around the container-side fetch program. Named rather than
# matched by a loose regex so this suite cannot silently start reading a
# different heredoc and go blind -- the failure mode
# `_redirection_offenders` in the pin contract suite already had once.
PROGRAM_BEGIN = "# g4-fetch-program: begin"
PROGRAM_END = "# g4-fetch-program: end"

# A branch or tag is a floating pointer, and pin.py already refuses all four.
# These must be refused here too, by the same parser, rather than by a second
# and looser rule in the harness.
MOVABLE_REVISIONS = ("main", "master", "HEAD", "latest")


def _pin() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("g4_pipeline_pin_fetch", PIN_MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _declared() -> dict[str, object]:
    """The committed lock, through the real parser -- not a second copy of it."""
    return _pin().parse_models_lock(
        MODELS_LOCK.read_text(encoding="utf-8"), source=str(MODELS_LOCK)
    )


def _sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


# --------------------------------------------------------------------------
# staging: a writable harness, a fake docker, a model cache
# --------------------------------------------------------------------------


def _staged_pipeline(tmp_path: Path) -> Path:
    """A writable copy of the harness dir, so a test can corrupt its lock.

    `run_measured.sh` derives `HERE` from its own location and writes into
    `${HERE}/results`, so a copy is what keeps a test from planting run
    directories and a `model_fetch.log` in the committed tree. `results/` is
    copied selectively for the reason the pin contract suite's staging tree
    does: its per-run payloads are tens of megabytes and nothing on this path
    reads them.
    """
    staging = tmp_path / "g4_pipeline"
    shutil.copytree(
        PIPELINE_DIR,
        staging,
        ignore=shutil.ignore_patterns(
            "results", "*.jsonl", "*.png", "__pycache__", "local-wheels"
        ),
    )
    (staging / "results").mkdir()
    return staging


def _reseal(lock_path: Path, digests: dict[str, str]) -> None:
    """Rewrite each artifact's `file_sha256` to the bytes the test planted.

    The committed lock carries the real digests of 700 MB of weights, which a
    hermetic test cannot reproduce. The revisions, repos, file paths and cache
    directories are left exactly as committed -- they are what the fetch
    binding is asserted against -- so only the payload digests are re-sealed,
    and the lock stays a lock: every artifact still needs a 64-hex sha256 to
    pass the parser, so this cannot be used to smuggle an unpinned revision in.
    """
    text = lock_path.read_text(encoding="utf-8")
    for name, digest in digests.items():
        block = re.search(
            rf"^\[{re.escape(name)}\]$(.*?)(?=^\[|\Z)", text, re.MULTILINE | re.DOTALL
        )
        assert block, f"the staged lock lost its [{name}] block"
        text = text.replace(
            block.group(1),
            re.sub(
                r"^file_sha256 = .*$",
                f"file_sha256 = {digest}",
                block.group(1),
                flags=re.MULTILINE,
            ),
            1,
        )
    lock_path.write_text(text, encoding="utf-8")


def _plant_cache(cache_root: Path, pins: dict[str, object], payloads: dict[str, bytes]) -> None:
    """Write a cache whose payloads and metadata sidecars agree with each other.

    `verify_models` reads the commit and the Hub ETag from the sidecar the
    fetch itself wrote, and independently hashes the payload. A cache that
    satisfies both therefore has to have sidecar and bytes describing the same
    artifact -- which is what makes a mismatch in a test attributable to the
    mutation and nothing else.
    """
    for name, pin in pins.items():
        directory = cache_root / pin.cache_dir
        payload = directory / pin.file
        payload.parent.mkdir(parents=True, exist_ok=True)
        payload.write_bytes(payloads[name])
        sidecar = directory / ".cache" / "huggingface" / "download" / f"{pin.file}.metadata"
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text(f"{pin.revision}\n{pin.file_sha256}\n", encoding="utf-8")


def _sidecar(cache: Path, pin: object) -> Path:
    return (
        cache
        / pin.cache_dir
        / ".cache"
        / "huggingface"
        / "download"
        / f"{pin.file}.metadata"
    )


class _Harness:
    """One staged harness, its lock re-sealed to whatever it plants."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.staged = _staged_pipeline(root)
        self.lock = self.staged / "models.lock"
        self.committed = _declared()
        self.payloads = {
            name: f"weights of {name} at {pin.revision}".encode()
            for name, pin in self.committed.items()
        }
        _reseal(self.lock, {name: _sha256(b) for name, b in self.payloads.items()})
        # Re-read through the real parser, so every `file_sha256` this harness
        # plants in a sidecar is the one its own lock declares. Reading the
        # committed pins here instead would make a good cache look swapped.
        self.pins = _pin().parse_models_lock(self.lock.read_text(encoding="utf-8"), source=str(self.lock))
        self.docker_log = root / "docker.log"

    def set_heron_revision(self, revision: str) -> None:
        """Replace Heron's revision in the staged lock, leaving all else as is.

        The staged lock is what the harness reads, so this needs no env
        override that exists only for the test.
        """
        text = self.lock.read_text(encoding="utf-8")
        text = text.replace(self.committed["heron"].revision, revision, 1)
        self.lock.write_text(text, encoding="utf-8")

    def run(self, cache: Path) -> subprocess.CompletedProcess[str]:
        """Execute the staged `run_measured.sh` end to end against a fake docker.

        The measurement is not expected to succeed -- a fake `docker` returns no
        JSON line, and the harness says so and exits non-zero. What each test
        asserts is a gate that runs *before* that, named on stderr, and which
        docker calls it reached.
        """
        pdf = self.root / "paper.pdf"
        pdf.write_bytes(b"%PDF-1.7\n% minimal\n")
        bindir = self.root / "bin"
        bindir.mkdir(exist_ok=True)
        fake = bindir / "docker"
        fake.write_text(
            "#!/usr/bin/env bash\n"
            f'printf "%s\\n" "$*" >> "{self.docker_log}"\n'
            "exit 0\n",
            encoding="utf-8",
        )
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        env = dict(os.environ)
        env["PATH"] = f"{bindir}:{env['PATH']}"
        env["G4_MODEL_CACHE"] = str(cache)
        return subprocess.run(
            ["bash", str(self.staged / "run_measured.sh"), str(pdf)],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )

    def calls(self) -> list[list[str]]:
        """Every `docker run` the harness issued.

        The exit trap also issues `docker rm -f <ctr>`, which is cleanup and not
        a measurement, so only `run` calls are read here.
        """
        if not self.docker_log.is_file():
            return []
        return [
            line.split()
            for line in self.docker_log.read_text(encoding="utf-8").splitlines()
            if line.startswith("run ")
        ]

    def fetches(self) -> list[list[str]]:
        """The model-fetch `docker run`s, which are the ones with no `--name`."""
        return [c for c in self.calls() if "run" in c and "--name" not in c]

    def measurements(self) -> list[list[str]]:
        """The measurement `docker run`s, which are the ones that carry `--name`."""
        return [c for c in self.calls() if "--name" in c]


@pytest.fixture
def harness(tmp_path: Path) -> _Harness:
    return _Harness(tmp_path)


# --------------------------------------------------------------------------
# The fetch: the declared commit has to reach the container
# --------------------------------------------------------------------------


def test_the_fetch_carries_every_declared_commit(harness: _Harness) -> None:
    """The defect, as an assertion: on a cold cache the fetch the harness issues
    must name every repo `models.lock` pins, at the commit it pins it.

    Before this change the same run issued
    `docling-tools models download --output-dir /models layout tableformer`,
    which names neither a repo nor a revision.
    """
    harness.run(harness.root / "cold-cache")

    blob = "\n".join(" ".join(c) for c in harness.calls())
    assert blob, "the harness issued no docker call at all"
    expected = {(p.repo, p.revision) for p in harness.pins.values()}
    missing = sorted(f"{repo}@{rev}" for repo, rev in expected if f"{repo}@{rev}" not in blob)
    assert not missing, f"the fetch does not carry the declared commit(s) {missing}: {blob}"
    assert "docling-tools" not in blob, (
        "docling-tools exposes no --revision and resolved the layout weights from "
        f"`main`; the fetch must not go through it: {blob}"
    )


def test_a_movable_revision_is_refused_before_anything_is_fetched(harness: _Harness) -> None:
    """`main` in the declaration is a floating pointer and pin.py already
    refuses it. The fetch plan is read through that parser, so the harness
    inherits the refusal; a second and looser parser here would be the defect
    this test exists to prevent.
    """
    harness.set_heron_revision(MOVABLE_REVISIONS[0])

    result = harness.run(harness.root / "cold-cache")

    assert result.returncode != 0, "a fetch bound to `main` must not proceed"
    assert "[run] FATAL:" in result.stderr, f"the failure must name itself: {result.stderr}"
    assert not harness.calls(), "nothing may be fetched from a declaration that is not a pin"

@pytest.mark.parametrize("movable", MOVABLE_REVISIONS)
def test_every_movable_revision_is_refused_not_just_main(
    harness: _Harness, movable: str
) -> None:
    """`master` moves as surely as `main`. Naming one of them in the rule would
    leave the other three floating, which is the same defect under four names.
    """
    harness.set_heron_revision(movable)

    result = harness.run(harness.root / f"cold-cache-{movable}")

    assert result.returncode != 0, f"revision {movable!r} must be refused"
    assert "[run] FATAL:" in result.stderr, result.stderr
    assert not harness.calls(), result.stderr


def test_a_lock_that_declares_nothing_fails_named(harness: _Harness) -> None:
    """An empty declaration would fetch nothing and then measure against
    whatever the cache happened to hold, which reads exactly like a pass.
    """
    harness.lock.write_text("# nothing pinned\n", encoding="utf-8")

    result = harness.run(harness.root / "cold-cache")

    assert result.returncode != 0
    assert "[run] FATAL:" in result.stderr, result.stderr
    assert not harness.measurements(), result.stderr


# --------------------------------------------------------------------------
# The cache gate: what arrived is checked against the lock, on every run
# --------------------------------------------------------------------------


def test_a_moved_commit_stops_the_run_before_the_measurement(harness: _Harness) -> None:
    """The cache's own sidecar records a different commit than the lock.

    This is what a host looks like after `main` moved: the bytes are real, the
    sidecar is intact, and the revision is not the one the pin declared. The
    gate must name it and take no measurement.
    """
    cache = harness.root / "moved-cache"
    _plant_cache(cache, harness.pins, harness.payloads)
    _sidecar(cache, harness.pins["heron"]).write_text(
        f"{'b' * 40}\n{harness.pins['heron'].file_sha256}\n", encoding="utf-8"
    )

    result = harness.run(cache)

    assert result.returncode != 0, "a moved model revision must stop the run"
    assert "[run] FATAL:" in result.stderr, result.stderr
    assert harness.pins["heron"].revision in result.stderr, (
        f"the failure must name the pinned commit it disagreed with: {result.stderr}"
    )
    assert not harness.measurements(), "no measurement may be taken on an unverified cache"


def test_a_mismatched_payload_hash_stops_the_run(harness: _Harness) -> None:
    """The sidecar still carries the pinned ETag, but the bytes on disk are not
    the ones it describes -- a swap performed under an intact sidecar, which is
    the window `verify_models` was hardened against.
    """
    cache = harness.root / "swapped-cache"
    _plant_cache(cache, harness.pins, harness.payloads)
    pin = harness.pins["tableformer-accurate"]
    (cache / pin.cache_dir / pin.file).write_bytes(b"substituted weights of the same kind")

    result = harness.run(cache)

    assert result.returncode != 0, "a payload that does not match the pin must stop the run"
    assert "[run] FATAL:" in result.stderr, result.stderr
    assert not harness.measurements(), result.stderr


def test_an_absent_cache_is_a_named_failure_not_a_skipped_check(harness: _Harness) -> None:
    """The cold-host path in full: the fetch is attempted, and when it does not
    deliver, the run stops by name.

    A gate skipped when there is nothing to gate is indistinguishable, from the
    outside, from a cache that passed -- so "the artifact was never fetched" has
    to be an error, not a quiet continuation.
    """
    result = harness.run(harness.root / "no-such-cache")

    assert result.returncode != 0, "an unfetched cache must not measure anything"
    assert "[run] FATAL:" in result.stderr, result.stderr
    assert "never fetched" in result.stderr, (
        f"the failure must say the artifact was never fetched: {result.stderr}"
    )
    assert not harness.measurements(), result.stderr


def test_a_verified_cache_is_not_refetched(harness: _Harness) -> None:
    """The gate is on the cache, not only on the fetch: a cache warmed by an
    earlier harness, or by a fetch that has since drifted, is checked on every
    run. A correct cache is used as it stands.
    """
    cache = harness.root / "good-cache"
    _plant_cache(cache, harness.pins, harness.payloads)

    result = harness.run(cache)

    # The harness announces on whichever stream it chose; both are read so the
    # test does not fail on a routing decision that is not the contract.
    announced = result.stdout + result.stderr
    # The lock path is named because G4_MODELS_LOCK can move it; a fixed
    # "models.lock" here would point a reader at the wrong file when it does.
    assert f"[run] model cache verified against {harness.lock}" in announced, announced
    assert not [c for c in harness.calls() if "snapshot_download" in " ".join(c)], (
        "a complete cache must not be re-downloaded"
    )


def test_verification_runs_before_the_measurement_is_attempted(harness: _Harness) -> None:
    """Ordering is the whole point: a gate placed after the measurement is a
    report, not a gate. This asserts the gate is *reached* on a correct cache --
    the only way to tell "ran and passed" from "never ran" is a cache that is
    known good.
    """
    cache = harness.root / "ordered-cache"
    _plant_cache(cache, harness.pins, harness.payloads)

    result = harness.run(cache)

    announced = result.stdout + result.stderr
    assert "[run] model cache verified" in announced, announced
    assert harness.measurements(), (
        f"a verified cache must let the measurement start: {result.stderr}"
    )


# --------------------------------------------------------------------------
# The container-side program: the argument has to be bound to the download
# --------------------------------------------------------------------------


def _fetch_program() -> str:
    text = RUN_SCRIPT.read_text(encoding="utf-8")
    assert PROGRAM_BEGIN in text, (
        f"run_measured.sh no longer marks the fetch program with {PROGRAM_BEGIN!r}, so this "
        "suite reads nothing and every program-level test below is vacuous"
    )
    assert PROGRAM_END in text, f"run_measured.sh has no {PROGRAM_END!r} marker"
    return text[text.index(PROGRAM_BEGIN) + len(PROGRAM_BEGIN) : text.index(PROGRAM_END)]


def _run_fetch_program(tmp_path: Path, specs: list[str]) -> list[dict[str, str]]:
    """Execute the harness's own fetch program in a subprocess, with no network.

    The program is extracted from `run_measured.sh` rather than restated, so
    this asserts the bytes that will actually run in the container. It runs in a
    subprocess because it does its own `import sys` and reads `sys.argv[1:]`:
    an in-process `exec` would either rebind `sys` to pytest's or have to fake a
    module the program imports for itself.

    A stub `huggingface_hub` on `PYTHONPATH` records each call instead of
    fetching, which is what makes the binding observable at all -- a real
    `snapshot_download` would have to download 700 MB before it could be
    believed, and this suite must stay hermetic.
    """
    stub_dir = tmp_path / "stub"
    stub_dir.mkdir(exist_ok=True)
    record = tmp_path / "calls.jsonl"
    (stub_dir / "huggingface_hub.py").write_text(
        "import json, os\n"
        "def snapshot_download(**kwargs):\n"
        "    with open(os.environ['G4_FETCH_RECORD'], 'a', encoding='utf-8') as handle:\n"
        "        handle.write(json.dumps({k: str(v) for k, v in kwargs.items()}) + '\\n')\n"
        "    return str(kwargs.get('local_dir', ''))\n",
        encoding="utf-8",
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = f"{stub_dir}{os.pathsep}{env.get('PYTHONPATH', '')}"
    env["G4_FETCH_RECORD"] = str(record)
    result = subprocess.run(
        [sys.executable, "-c", _fetch_program(), *specs],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert result.returncode == 0, f"the fetch program failed: {result.stderr}"
    if not record.is_file():
        return []
    return [
        json.loads(line)
        for line in record.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _spec(repo: str, revision: str, local_dir: str) -> str:
    return f"{repo}@{revision}:{local_dir}"


def test_the_fetch_program_binds_each_spec_to_its_revision(tmp_path: Path) -> None:
    """The other half of the path.

    `test_the_fetch_carries_every_declared_commit` proves the revision reaches
    the container; this proves the program there binds it to the download
    rather than dropping it. A program that unpacked the spec wrongly raised
    `IndexError` on its first argument -- and run inside a `docker run` whose
    exit status the harness only checks, that was a fetch which never happened
    and the run continued on whatever the cache already held.
    """
    pins = _declared()
    specs = [
        _spec(pins["heron"].repo, pins["heron"].revision, "/models/heron"),
        _spec(pins["tableformer-accurate"].repo, pins["tableformer-accurate"].revision, "/models/tf"),
    ]

    calls = _run_fetch_program(tmp_path, specs)

    assert len(calls) == len(specs), f"one download per declared repo, got {calls!r}"
    for spec, call in zip(specs, calls):
        repo, _, rest = spec.partition("@")
        revision, _, local_dir = rest.partition(":")
        assert call.get("repo_id") == repo, f"{call!r} is not bound to {repo!r}"
        assert call.get("revision") == revision, (
            f"{call!r} is not bound to the declared revision {revision!r}"
        )
        assert call.get("local_dir") == local_dir, f"{call!r} is not bound to {local_dir!r}"


def test_the_fetch_program_never_falls_back_to_a_default_revision(tmp_path: Path) -> None:
    """`snapshot_download` defaults to the repo's default branch when
    `revision` is absent, so a program that dropped the argument would fetch
    `main` and look like it had worked. The key must be present on every call
    and the value must be the one the spec named.
    """
    pins = _declared()
    revision = pins["heron"].revision

    calls = _run_fetch_program(tmp_path, [_spec(pins["heron"].repo, revision, "/models/heron")])

    assert calls, "the program downloaded nothing"
    assert all("revision" in call for call in calls), (
        f"a download with no revision is a download of the default branch: {calls!r}"
    )
    assert all(call["revision"] == revision for call in calls), calls
    assert all(len(call["revision"]) == 40 for call in calls), calls


@pytest.mark.parametrize(
    "spec",
    [
        "docling-project/docling-layout-heron",  # no revision, no destination
        "docling-project/docling-models@abc",  # no destination
    ],
)
def test_a_malformed_spec_fails_loudly_instead_of_fetching_something_else(
    tmp_path: Path, spec: str
) -> None:
    """A spec without a revision, or with no destination, must make the program
    exit non-zero. Silently defaulting either one is the exact defect: a
    download that happens, at a revision nobody declared, into a directory the
    pin does not name. The harness only sees the container's exit status, so a
    raise here is the whole of the signal.
    """
    stub = tmp_path / "stub"
    stub.mkdir(exist_ok=True)
    record = tmp_path / "calls.jsonl"
    (stub / "huggingface_hub.py").write_text(
        "import json, os\n"
        "def snapshot_download(**kwargs):\n"
        "    with open(os.environ['G4_FETCH_RECORD'], 'a', encoding='utf-8') as handle:\n"
        "        handle.write(json.dumps(kwargs) + '\\n')\n"
        "    return ''\n",
        encoding="utf-8",
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = f"{stub}{os.pathsep}{env.get('PYTHONPATH', '')}"
    env["G4_FETCH_RECORD"] = str(record)

    result = subprocess.run(
        [sys.executable, "-c", _fetch_program(), spec],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert result.returncode != 0, (
        f"a malformed spec {spec!r} must fail the fetch, not default: {result.stdout!r}"
    )
    assert not record.is_file() or not record.read_text(encoding="utf-8").strip(), (
        "nothing may be downloaded from a spec the pin cannot name"
    )


def test_the_harness_shell_is_syntactically_valid() -> None:
    result = subprocess.run(
        ["bash", "-n", str(RUN_SCRIPT)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_the_fetch_program_is_reachable_text_not_a_comment() -> None:
    """The extraction above is the only thing binding this suite to the shell's
    real content. If the sentinels stop bracketing a program, every
    program-level test fails for an unrelated reason -- assert here that what is
    extracted can actually download, so the failure names itself.
    """
    program = _fetch_program()
    assert "snapshot_download" in program, (
        f"the extracted fetch program downloads nothing: {program!r}"
    )
    assert not program.lstrip().startswith("#"), "the extracted region is a comment, not a program"
