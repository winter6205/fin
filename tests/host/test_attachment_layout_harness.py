"""The Heron adapter's configuration and its one crossing into the container.

Two things are pinned here that the port tests cannot see: every knob comes
from the environment rather than from a literal in the module (the pinned image
name and the model cache stay single-sourced in the harness), and the document
reaches the container only through a path that the harness's read-only mount
can actually resolve. The last group is the one the container boundary makes
load-bearing: a capped pass must end the container it started, on both sides of
that boundary, or a 3-page scan that never finished keeps a 4 GiB ceiling
reserved for the rest of the session.
"""

from __future__ import annotations

import dataclasses
import inspect
import json
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Final

import pytest

from fin.host.attachment.attachment_layout import (
    PASS_FAILED,
    PASS_MEASURED,
    PASS_TIMED_OUT,
    PASS_UNAVAILABLE,
    HeronHarnessAbsentError,
    HeronHarnessAnswerError,
    HeronHarnessUnreachableError,
    HeronLayoutConfig,
    HeronLayoutConfigError,
    HeronPageLayout,
    HeronPassError,
    HeronPassTimeoutError,
    HeronRecordUnreadableError,
    HeronStagingRefusedError,
    MeasuredHarnessRunner,
    _HERON_STATE_BY_TYPE,
    _run,
    _state_of,
)

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
HARNESS: Final[Path] = REPO_ROOT / "scripts/g4_pipeline/run_measured.sh"
# The harness reads the model pin before it does anything else, and it reads it
# through pin.py beside itself. Copying the script alone would leave the gate
# exiting 65 before docker ever runs, which is a correct refusal of an unpinned
# fetch and a useless test: the behaviour under test is what the run does AFTER
# the pin is satisfied. So the pin travels with the script.
PIN_MODULE: Final[Path] = REPO_ROOT / "scripts/g4_pipeline/pin.py"
MODELS_LOCK: Final[Path] = REPO_ROOT / "scripts/g4_pipeline/models.lock"
# sha256 of the zero-byte marker files these tests stand in for model weights.
# The harness verifies the cache against models.lock on every run, so a lock
# describing the real 171 MB and 213 MB payloads would correctly refuse a test
# cache. These tests are about what the run does after the pin is satisfied, so
# the lock they ship describes the cache they build.
_EMPTY_SHA256: Final[str] = (
    "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
)


def _test_models_lock(weights: dict[str, str]) -> str:
    """A models.lock plus the metadata the gate reads, matching empty markers.

    ``weights`` maps a cache_dir to the file name inside it. The revisions are
    full 40-character commits because the parser refuses a branch name. The
    sidecar is written because ``verify_models`` reads the commit and ETag from
    it and hashes the payload itself; a lock with no sidecar would correctly be
    reported as "never fetched", which is a real refusal and the wrong thing for
    a test that is not about the model gate.
    """
    sections = []
    for index, (cache_dir, file_name) in enumerate(sorted(weights.items()), start=1):
        revision = f"{index + 1:040x}"
        sections.append(
            f"[fixture-{index}]\n"
            f"repo = fixture/model-{index}\n"
            f"revision = {revision}\n"
            f"file = {file_name}\n"
            f"bytes = 0\n"
            f"file_sha256 = {_EMPTY_SHA256}\n"
            f"cache_dir = {cache_dir}\n"
        )
    return "\n".join(sections)


def _write_fixture_metadata(cache_root: Path, weights: dict[str, str]) -> None:
    """The download sidecars `verify_models` reads, matching the test lock.

    The sidecar sits under the artifact's cache_dir at the path the real Hub
    snapshot uses, which for the table-structure weights is a nested directory
    rather than the cache_dir root -- so the file name from the lock is used
    whole, not flattened to its basename.
    """
    for index, (cache_dir, file_name) in enumerate(sorted(weights.items()), start=1):
        sidecar = cache_root / cache_dir / ".cache" / "huggingface" / "download"
        sidecar.mkdir(parents=True, exist_ok=True)
        target = sidecar / f"{file_name}.metadata"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"{index + 1:040x}\n{_EMPTY_SHA256}\n", encoding="utf-8")
GOLD_MIXED: Final[Path] = REPO_ROOT / "data/corpus/g4_attachment_gold/g4-mixed-chart-001.pdf"
GOLD_RECORD: Final[Path] = (
    REPO_ROOT
    / "scripts/g4_pipeline/results/g4-mixed-chart-001_20260926T000242Z"
    / "g4-mixed-chart-001.measured.jsonl"
)
STAGING_NAME: Final[str] = ".fin-heron-staging"
EMPTY_ENV: Final[dict[str, str]] = {}
MEASURED_HINT: Final[str] = "[run] measured JSON   : "
# A real committed record, so a test about the command the harness was started
# with does not have to pretend the artifact existed.
MEASURED_OK: Final[str] = MEASURED_HINT + str(GOLD_RECORD) + "\n"
# Long enough that only a signal can end it, and short enough that a leaked
# child still sitting there an hour later is obvious in a process list.
_LINGER_S: Final[str] = "20"


class _StubSpawn:
    """The ``subprocess.run`` seam: answers with what the harness would print."""

    def __init__(self, *, returncode=0, stdout="", stderr="", error=None) -> None:
        self._result = (returncode, stdout, stderr)
        self._error = error
        self.commands: list[list[str]] = []
        self.envs: list[dict[str, str]] = []
        self.timeouts: list[float] = []

    def __call__(self, command, *, env, timeout):
        self.commands.append(list(command))
        self.envs.append(dict(env or {}))
        self.timeouts.append(timeout)
        if self._error is not None:
            raise self._error

        code, out, err = self._result

        class _Done:
            returncode, stdout, stderr = code, out, err

        return _Done()


class _SpySpawn:
    """Records the document path the harness was handed, then answers with it."""

    def __init__(self, inner: _StubSpawn) -> None:
        self._inner = inner
        self.pdfs: list[str] = []

    def __call__(self, command, **kwargs):
        self.pdfs.append(command[2])
        return self._inner(command, **kwargs)


def _record_line(tmp_path: Path, name: str = "out.jsonl") -> Path:
    artifact = tmp_path / name
    artifact.write_text(
        json.dumps({"schema": "fin.g4.t2a.pipeline_measurement/3", "pages": [
            {"page_no": 1, "figure_boxes": []}
        ]})
        + "\n",
        encoding="utf-8",
    )
    return artifact


def _wait_until_dead(pid: int, *, within_s: float = 5.0) -> bool:
    """True once the kernel has stopped answering for a pid."""
    deadline = time.monotonic() + within_s
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        time.sleep(0.02)
    return False


def _copy_harness(tmp_path: Path, *, cache_root: Path | None = None) -> Path:
    """The shipped harness, planted deep enough that its own worktree is tmp_path.

    A copy rather than a fixture: this has to be the file the adapter would
    really run, traps included, and ``results/`` must not be the repository's.
    The pin module and the lock come along, because the harness resolves them
    relative to itself and refuses to run without them. When a ``cache_root`` is
    given, the shipped lock is replaced by one describing that cache, so the
    model gate stays a real gate instead of being switched off for the tests.
    """
    planted = tmp_path / "scripts" / "g4_pipeline" / "run_measured.sh"
    planted.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(HARNESS, planted)
    shutil.copyfile(PIN_MODULE, planted.parent / "pin.py")
    if cache_root is None:
        shutil.copyfile(MODELS_LOCK, planted.parent / "models.lock")
    else:
        weights = {
            "docling-project--docling-layout-heron": "model.safetensors",
            "docling-project--docling-models": (
                "model_artifacts/tableformer/accurate/tableformer_accurate.safetensors"
            ),
        }
        (planted.parent / "models.lock").write_text(
            _test_models_lock(weights), encoding="utf-8"
        )
        _write_fixture_metadata(cache_root, weights)
    return planted


# ---------------------------------------------------------------------------
# configuration: names, defaults, and what a bad value does
# ---------------------------------------------------------------------------


def test_defaults_point_at_the_pinned_harness_and_never_at_a_model():
    config = HeronLayoutConfig.from_environ(environ=EMPTY_ENV)

    assert config.script_path == HARNESS
    assert config.script_path.is_file()
    assert config.staging_dir == REPO_ROOT / STAGING_NAME
    assert config.timeout_s > 0
    assert config.slice_pass is False
    # The image and the artifacts cache are the harness's own facts: a copy of
    # either here would be a second pin free to drift.
    assert config.image is None
    assert config.model_cache is None


def test_every_knob_is_readable_from_the_environment():
    config = HeronLayoutConfig.from_environ(
        environ={
            "FIN_ATTACHMENT_HERON_SCRIPT": "/opt/harness/run_measured.sh",
            "FIN_ATTACHMENT_HERON_TIMEOUT_S": "42.5",
            "FIN_ATTACHMENT_HERON_STAGING_DIR": "/tmp/staging",
            "FIN_ATTACHMENT_HERON_IMAGE": "fin-g4-pipeline:t2a",
            "FIN_ATTACHMENT_HERON_MODEL_CACHE": "/cache/models",
            "FIN_ATTACHMENT_HERON_SLICE_PASS": "true",
        }
    )

    assert config.script_path == Path("/opt/harness/run_measured.sh")
    assert config.timeout_s == pytest.approx(42.5)
    assert config.staging_dir == Path("/tmp/staging")
    assert config.image == "fin-g4-pipeline:t2a"
    assert config.model_cache == Path("/cache/models")
    assert config.slice_pass is True


def test_the_staging_root_follows_a_harness_from_another_checkout(tmp_path):
    # The harness mounts its own worktree, so that mount is the only place a
    # staged document can be read from: a staging root that kept pointing at the
    # installed tree would either refuse to stage or hide the bytes from the
    # container.
    other = tmp_path / "other-checkout"
    config = HeronLayoutConfig.from_environ(
        environ={
            "FIN_ATTACHMENT_HERON_SCRIPT": str(
                other / "scripts" / "g4_pipeline" / "run_measured.sh"
            )
        }
    )

    assert config.worktree == other
    assert config.staging_dir == other / STAGING_NAME
    assert config.staging_dir.is_relative_to(config.worktree)

    # The same authority for a hand-built config: only a default follows the
    # harness, so the pair cannot disagree in either construction path.
    assert HeronLayoutConfig(script_path=other / "scripts/g4_pipeline/run_measured.sh") == config


def test_a_harness_too_shallow_to_be_in_a_checkout_is_a_named_config_error():
    # Every default -- the mount, the staging root -- is read off the harness's
    # position, so guessing above the deepest directory the path can name is how
    # documents get staged where no container can read them.
    for script in ("/run_measured.sh", "one/run_measured.sh", "/one/run_measured.sh"):
        with pytest.raises(HeronLayoutConfigError, match="run_measured.sh") as raised:
            HeronLayoutConfig.from_environ(
                environ={"FIN_ATTACHMENT_HERON_SCRIPT": script}
            )

        assert script in str(raised.value).replace("'", "")


@pytest.mark.parametrize(
    "key,value",
    [
        ("FIN_ATTACHMENT_HERON_TIMEOUT_S", "0"),
        ("FIN_ATTACHMENT_HERON_TIMEOUT_S", "-1"),
        ("FIN_ATTACHMENT_HERON_TIMEOUT_S", "not-a-number"),
        ("FIN_ATTACHMENT_HERON_TIMEOUT_S", "   "),
        ("FIN_ATTACHMENT_HERON_TIMEOUT_S", "１０"),
        ("FIN_ATTACHMENT_HERON_TIMEOUT_S", "+10"),
        ("FIN_ATTACHMENT_HERON_TIMEOUT_S", "1_000"),
        ("FIN_ATTACHMENT_HERON_TIMEOUT_S", "inf"),
        ("FIN_ATTACHMENT_HERON_TIMEOUT_S", "nan"),
        ("FIN_ATTACHMENT_HERON_SCRIPT", "   "),
        ("FIN_ATTACHMENT_HERON_STAGING_DIR", "relative/staging"),
        ("FIN_ATTACHMENT_HERON_SLICE_PASS", "maybe"),
    ],
)
def test_a_malformed_knob_is_a_named_config_error(key, value):
    with pytest.raises(HeronLayoutConfigError) as raised:
        HeronLayoutConfig.from_environ(environ={key: value})

    assert key in str(raised.value)


@pytest.mark.parametrize(
    "environ",
    [
        {"FIN_ATTACHMENT_HERON_IMAGE": ""},
        {"FIN_ATTACHMENT_HERON_MODEL_CACHE": "  "},
        {"FIN_ATTACHMENT_HERON_SLICE_PASS": ""},
        {"FIN_ATTACHMENT_HERON_UNRELATED": "x"},
    ],
)
def test_a_blank_or_unrelated_knob_falls_back_rather_than_failing(environ):
    assert HeronLayoutConfig.from_environ(
        environ=environ
    ) == HeronLayoutConfig.from_environ(environ=EMPTY_ENV)


def test_the_config_is_a_value_nobody_can_mutate_underneath_a_pass():
    config = HeronLayoutConfig.from_environ(environ=EMPTY_ENV)

    with pytest.raises(dataclasses.FrozenInstanceError):
        config.timeout_s = 1.0  # type: ignore[misc]


# ---------------------------------------------------------------------------
# the one crossing: how the document reaches the container
# ---------------------------------------------------------------------------


def test_the_harness_is_invoked_by_its_own_path_with_no_model_literal():
    spawn = _StubSpawn(stdout=MEASURED_OK)

    MeasuredHarnessRunner(spawn=spawn).read_record(
        pdf_path=GOLD_MIXED,
        config=HeronLayoutConfig.from_environ(environ=EMPTY_ENV),
    )

    assert spawn.commands[0] == ["bash", str(HARNESS), str(GOLD_MIXED), "--no-slice-pass"]


def test_the_slice_pass_is_opt_in_only():
    spawn = _StubSpawn(stdout=MEASURED_OK)

    MeasuredHarnessRunner(spawn=spawn).read_record(
        pdf_path=GOLD_MIXED,
        config=HeronLayoutConfig.from_environ(
            environ={"FIN_ATTACHMENT_HERON_SLICE_PASS": "true"}
        ),
    )

    assert spawn.commands[0] == ["bash", str(HARNESS), str(GOLD_MIXED)]


def test_the_image_and_cache_reach_the_harness_as_its_own_environment():
    spawn = _StubSpawn(stdout=MEASURED_OK)

    MeasuredHarnessRunner(spawn=spawn).read_record(
        pdf_path=GOLD_MIXED,
        config=HeronLayoutConfig.from_environ(
            environ={
                "FIN_ATTACHMENT_HERON_IMAGE": "fin-g4-pipeline:t2a",
                "FIN_ATTACHMENT_HERON_MODEL_CACHE": "/cache/models",
            }
        ),
    )

    assert spawn.envs[0]["G4_IMAGE"] == "fin-g4-pipeline:t2a"
    assert spawn.envs[0]["G4_MODEL_CACHE"] == "/cache/models"


def test_an_unset_knob_leaves_the_harness_default_untouched():
    spawn = _StubSpawn(stdout=MEASURED_OK)

    MeasuredHarnessRunner(spawn=spawn).read_record(
        pdf_path=GOLD_MIXED,
        config=HeronLayoutConfig.from_environ(environ=EMPTY_ENV),
    )

    assert "G4_IMAGE" not in spawn.envs[0]
    assert "G4_MODEL_CACHE" not in spawn.envs[0]


def test_the_wall_clock_cap_is_handed_to_the_child():
    spawn = _StubSpawn(stdout=MEASURED_OK)

    MeasuredHarnessRunner(spawn=spawn).read_record(
        pdf_path=GOLD_MIXED,
        config=HeronLayoutConfig.from_environ(
            environ={"FIN_ATTACHMENT_HERON_TIMEOUT_S": "12.5"}
        ),
    )

    assert spawn.timeouts == [pytest.approx(12.5)]


def test_a_document_outside_the_worktree_is_staged_inside_it(tmp_path):
    # The harness mounts the worktree read-only at /worktree and resolves the
    # PDF against it, so bytes handed over from outside that mount have to be
    # copied in or the container cannot open them.
    outside = tmp_path / "attachment.pdf"
    outside.write_bytes(GOLD_MIXED.read_bytes())
    spy = _SpySpawn(_StubSpawn(stdout=MEASURED_OK))

    MeasuredHarnessRunner(spawn=spy).read_record(
        pdf_path=outside,
        config=HeronLayoutConfig.from_environ(environ=EMPTY_ENV),
    )

    staged = Path(spy.pdfs[0])
    assert staged.is_relative_to(REPO_ROOT)
    assert staged.name.startswith("attachment-")


def test_a_staged_copy_does_not_outlive_the_pass(tmp_path):
    outside = tmp_path / "attachment.pdf"
    outside.write_bytes(GOLD_MIXED.read_bytes())
    spy = _SpySpawn(_StubSpawn(stdout=MEASURED_OK))

    MeasuredHarnessRunner(spawn=spy).read_record(
        pdf_path=outside,
        config=HeronLayoutConfig.from_environ(environ=EMPTY_ENV),
    )

    assert not Path(spy.pdfs[0]).exists()


def test_a_staged_copy_is_cleaned_up_even_when_the_harness_fails(tmp_path):
    outside = tmp_path / "attachment.pdf"
    outside.write_bytes(GOLD_MIXED.read_bytes())
    spy = _SpySpawn(_StubSpawn(returncode=1, stderr="[run] FAIL"))

    with pytest.raises(HeronHarnessAnswerError):
        MeasuredHarnessRunner(spawn=spy).read_record(
            pdf_path=outside,
            config=HeronLayoutConfig.from_environ(environ=EMPTY_ENV),
        )

    assert not Path(spy.pdfs[0]).exists()


def test_a_staged_copy_is_cleaned_up_even_when_the_pass_is_capped(tmp_path):
    # The cap is the expected case on a slow host, not the exotic one, so the
    # copy of a private filing must survive neither the pass nor its timeout.
    outside = tmp_path / "attachment.pdf"
    outside.write_bytes(GOLD_MIXED.read_bytes())
    spy = _SpySpawn(
        _StubSpawn(error=subprocess.TimeoutExpired(cmd=["bash", "run_measured.sh"], timeout=1))
    )

    with pytest.raises(HeronPassTimeoutError):
        MeasuredHarnessRunner(spawn=spy).read_record(
            pdf_path=outside,
            config=HeronLayoutConfig.from_environ(environ=EMPTY_ENV),
        )

    leaked = Path(spy.pdfs[0])
    assert not leaked.exists()
    assert not list((REPO_ROOT / STAGING_NAME).glob("attachment-*.pdf"))


@pytest.mark.parametrize(
    "break_at",
    ["close", "copy", "unlink"],
)
def test_a_staged_copy_that_could_not_be_written_leaves_nothing_behind(
    tmp_path, monkeypatch, break_at
):
    # A half-written copy is the same private bytes as a whole one, and the
    # directory it sits in is one the repository commits, so the guard has to
    # cover the window between creating the file and handing it to the harness.
    outside = tmp_path / "attachment.pdf"
    outside.write_bytes(GOLD_MIXED.read_bytes())
    staging = REPO_ROOT / STAGING_NAME
    # Start from no staged copies: these cases assert on this directory's
    # contents, and a case that fails before its own cleanup would otherwise
    # leak a staged copy into the next parametrization. Removing the copies
    # (not the directory) keeps a later test's "staging dir does not exist"
    # assertion about the directory itself intact.
    for leftover in staging.glob("attachment-*.pdf"):
        leftover.unlink(missing_ok=True)
    calls: list[str] = []
    real_close = os.close
    real_copyfile = shutil.copyfile
    real_unlink = os.unlink

    def spy_close(fd):
        calls.append("close")
        if break_at == "close":
            real_close(fd)
            raise OSError("descriptor refused")
        real_close(fd)

    def spy_copyfile(src, dst, **kwargs):
        calls.append("copy")
        if break_at in ("copy", "unlink"):
            # "unlink" is the double-failure case: the copy itself is refused,
            # and the best-effort cleanup inside the ``except BaseException``
            # arm is refused too, so that inner ``except OSError`` arm runs.
            raise OSError("disk refused the copy")
        return real_copyfile(src, dst, **kwargs)

    def spy_unlink(path, *args, **kwargs):
        calls.append("unlink")
        if break_at == "unlink":
            raise OSError("directory refused the cleanup")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "close", spy_close)
    monkeypatch.setattr(shutil, "copyfile", spy_copyfile)
    # ``Path.unlink`` routes through ``os.unlink``; breaking that models a
    # staging directory that refuses the best-effort cleanup inside the
    # ``except BaseException`` arm of ``_stage``.
    monkeypatch.setattr(os, "unlink", spy_unlink)

    with pytest.raises(HeronStagingRefusedError):
        MeasuredHarnessRunner(spawn=_StubSpawn(stdout=MEASURED_OK)).read_record(
            pdf_path=outside,
            config=HeronLayoutConfig.from_environ(environ=EMPTY_ENV),
        )

    assert break_at in calls
    if break_at == "unlink":
        # The double-failure case deliberately leaves the staged copy behind:
        # the cleanup inside the ``except BaseException`` arm is itself refused,
        # and the typed staging failure (not a bare OSError) is what surfaces.
        # The whole point is that the leak is recorded, not silently cleaned.
        leftover = list(staging.glob("attachment-*.pdf"))
        assert leftover, "double-failure case must leave the staged copy it could not destroy"
    else:
        assert not list(staging.glob("attachment-*.pdf"))
    # Clean up with the real unlink: this case may have replaced ``os.unlink``
    # with a spy, and ``shutil.rmtree`` deletes through that same name, so
    # rmtree would silently fail to remove the copies this test just staged.
    # Restore only that one name; a blanket monkeypatch.undo() also rolls back
    # patches the surrounding test session owns.
    monkeypatch.setattr(os, "unlink", real_unlink)
    shutil.rmtree(staging, ignore_errors=True)


def test_a_document_already_inside_the_worktree_is_not_copied():
    spy = _SpySpawn(_StubSpawn(stdout=MEASURED_OK))

    MeasuredHarnessRunner(spawn=spy).read_record(
        pdf_path=GOLD_MIXED,
        config=HeronLayoutConfig.from_environ(environ=EMPTY_ENV),
    )

    assert spy.pdfs == [str(GOLD_MIXED)]


def test_the_record_is_read_back_from_the_artifact_the_harness_names(tmp_path):
    artifact = _record_line(tmp_path)
    spawn = _StubSpawn(stdout=f"noise\n{MEASURED_HINT}{artifact}\n")

    record = MeasuredHarnessRunner(spawn=spawn).read_record(
        pdf_path=GOLD_MIXED,
        config=HeronLayoutConfig.from_environ(environ=EMPTY_ENV),
    )

    assert record["pages"][0]["page_no"] == 1


def test_a_harness_that_prints_no_artifact_path_is_a_named_failure(tmp_path):
    spawn = _StubSpawn(stdout="[run] FAIL: expected exactly 1 JSON line\n")

    with pytest.raises(
        HeronHarnessAnswerError, match="no measured record"
    ) as raised:
        MeasuredHarnessRunner(spawn=spawn).read_record(
            pdf_path=GOLD_MIXED,
            config=HeronLayoutConfig.from_environ(environ=EMPTY_ENV),
        )

    assert raised.value.code == "heron_harness_answer"


def test_a_named_artifact_that_is_not_there_is_a_named_failure(tmp_path):
    spawn = _StubSpawn(stdout=f"{MEASURED_HINT}{tmp_path / 'gone.jsonl'}\n")

    with pytest.raises(HeronHarnessAnswerError, match="no measured record"):
        MeasuredHarnessRunner(spawn=spawn).read_record(
            pdf_path=GOLD_MIXED,
            config=HeronLayoutConfig.from_environ(environ=EMPTY_ENV),
        )


def test_an_artifact_that_is_not_one_json_record_is_a_named_failure(tmp_path):
    for index, body in enumerate(["", "not json\n", "{a\nb}\n", "[1, 2]\n", "{}\n"]):
        artifact = _record_line(tmp_path, f"bad{index}.jsonl")
        artifact.write_text(body, encoding="utf-8")
        spawn = _StubSpawn(stdout=f"{MEASURED_HINT}{artifact}\n")

        with pytest.raises(
            HeronRecordUnreadableError, match="unreadable measured record"
        ):
            MeasuredHarnessRunner(spawn=spawn).read_record(
                pdf_path=GOLD_MIXED,
                config=HeronLayoutConfig.from_environ(environ=EMPTY_ENV),
            )


def test_a_record_this_side_cannot_read_is_not_reported_as_no_engine(tmp_path, monkeypatch):
    # A refused read of an artifact the container did write is a fact about the
    # record, not about whether an image exists. Classified by generic type it
    # used to answer "unavailable", which sends a caller looking for a container
    # that already finished.
    artifact = _record_line(tmp_path)
    spawn = _StubSpawn(stdout=f"{MEASURED_HINT}{artifact}\n")
    monkeypatch.setattr(
        Path, "read_text", lambda *args, **kwargs: (_ for _ in ()).throw(PermissionError("no"))
    )

    with pytest.raises(HeronRecordUnreadableError) as raised:
        MeasuredHarnessRunner(spawn=spawn).read_record(
            pdf_path=GOLD_MIXED,
            config=HeronLayoutConfig.from_environ(environ=EMPTY_ENV),
        )

    assert not isinstance(raised.value, OSError)
    assert raised.value.code == "heron_record_unreadable"

    layout = HeronPageLayout(
        pdf_path=GOLD_MIXED,
        runner=MeasuredHarnessRunner(spawn=spawn),
        config=HeronLayoutConfig.from_environ(environ=EMPTY_ENV),
    )
    assert layout.page_regions(page_no=1, size_pt=(612.0, 792.0)) == ()
    assert layout.pass_state == PASS_FAILED


@pytest.mark.parametrize(
    "spawn,state,code",
    [
        (
            _StubSpawn(returncode=1, stderr="[run] FATAL: docker is not reachable"),
            PASS_FAILED,
            "heron_harness_answer",
        ),
        (
            _StubSpawn(returncode=-15, stderr=""),
            PASS_FAILED,
            "heron_harness_answer",
        ),
        (_StubSpawn(error=FileNotFoundError("docker")), PASS_UNAVAILABLE, "heron_harness_unreachable"),
        (_StubSpawn(error=PermissionError("socket denied")), PASS_UNAVAILABLE, "heron_harness_unreachable"),
        (_StubSpawn(error=TimeoutError("killed at the cap")), PASS_TIMED_OUT, "heron_pass_timeout"),
        (
            _StubSpawn(
                error=subprocess.TimeoutExpired(cmd=["bash", "run_measured.sh"], timeout=1)
            ),
            PASS_TIMED_OUT,
            "heron_pass_timeout",
        ),
    ],
)
def test_the_container_answer_becomes_the_adapters_state_and_nothing_else(
    spawn, state, code
):
    layout = HeronPageLayout(
        pdf_path=GOLD_MIXED,
        runner=MeasuredHarnessRunner(spawn=spawn),
        config=HeronLayoutConfig.from_environ(environ=EMPTY_ENV),
    )

    assert layout.page_regions(page_no=1, size_pt=(612.0, 792.0)) == ()
    assert layout.pass_state == state
    detail = layout.failure_detail or ""
    assert state in detail
    assert code in detail


def test_the_harness_was_ended_by_a_signal_says_so_rather_than_blaming_the_log():
    layout = HeronPageLayout(
        pdf_path=GOLD_MIXED,
        runner=MeasuredHarnessRunner(_StubSpawn(returncode=-15, stderr="")),
        config=HeronLayoutConfig.from_environ(environ=EMPTY_ENV),
    )

    assert layout.page_regions(page_no=1, size_pt=(612.0, 792.0)) == ()
    assert "ended by signal 15" in (layout.failure_detail or "")
    assert layout.pass_state == PASS_FAILED


def test_a_non_zero_exit_names_the_harness_reason_for_the_failure():
    layout = HeronPageLayout(
        pdf_path=GOLD_MIXED,
        runner=MeasuredHarnessRunner(
            _StubSpawn(returncode=1, stderr=f"[run] FATAL: no image at {GOLD_MIXED}")
        ),
        config=HeronLayoutConfig.from_environ(environ=EMPTY_ENV),
    )

    layout.page_regions(page_no=1, size_pt=(612.0, 792.0))

    assert "FATAL" in (layout.failure_detail or "")


def test_a_real_record_from_the_pinned_pipeline_needs_no_second_engine():
    record = json.loads(GOLD_RECORD.read_text(encoding="utf-8"))

    class _StaticRunner:
        def read_record(self, *, pdf_path, config):
            return record

    layout = HeronPageLayout(pdf_path=GOLD_MIXED, runner=_StaticRunner())

    assert layout.page_regions(page_no=1, size_pt=(612.0, 792.0))
    assert layout.pass_state == PASS_MEASURED
    assert layout.failure_detail is None


def test_a_staging_dir_outside_the_harness_mount_is_a_named_failure(tmp_path):
    # Copying the document somewhere the read-only mount cannot see would make
    # the container fail on a path this process invented, so it is refused here
    # with the reason rather than as a docker error later.
    outside = tmp_path / "attachment.pdf"
    outside.write_bytes(GOLD_MIXED.read_bytes())
    layout = HeronPageLayout(
        pdf_path=outside,
        runner=MeasuredHarnessRunner(_StubSpawn(stdout="")),
        config=HeronLayoutConfig.from_environ(
            environ={"FIN_ATTACHMENT_HERON_STAGING_DIR": str(tmp_path / "staging")}
        ),
    )

    assert layout.page_regions(page_no=1, size_pt=(612.0, 792.0)) == ()
    assert layout.pass_state == PASS_FAILED
    assert "heron_staging_refused" in (layout.failure_detail or "")
    assert "outside the harness mount" in (layout.failure_detail or "")
    assert not (REPO_ROOT / STAGING_NAME).exists()


def test_a_harness_that_is_not_installed_never_reaches_the_spawn():
    calls: list[list[str]] = []

    def spy(command, **kwargs):
        calls.append(list(command))
        raise AssertionError("the container must not be started for a missing harness")

    layout = HeronPageLayout(
        pdf_path=GOLD_MIXED,
        runner=MeasuredHarnessRunner(spy),
        config=HeronLayoutConfig.from_environ(
            environ={
                "FIN_ATTACHMENT_HERON_SCRIPT":
                "/nonexistent/checkout/scripts/g4_pipeline/run_measured.sh"
            }
        ),
    )

    assert layout.page_regions(page_no=1, size_pt=(612.0, 792.0)) == ()
    assert layout.pass_state == PASS_UNAVAILABLE
    assert "heron_harness_absent" in (layout.failure_detail or "")
    assert not calls


def test_a_missing_harness_is_still_findable_as_a_missing_file(tmp_path):
    # A reader that already distinguishes an absent path from other refusals keeps
    # working with one vocabulary: the named failure is the FileNotFoundError.
    missing = tmp_path / "scripts" / "g4_pipeline" / "run_measured.sh"
    with pytest.raises(FileNotFoundError) as raised:
        MeasuredHarnessRunner(spawn=_StubSpawn()).read_record(
            pdf_path=GOLD_MIXED,
            config=HeronLayoutConfig(script_path=missing),
        )

    assert isinstance(raised.value, HeronPassError)
    assert str(missing) in str(raised.value)


# Every shape this boundary can produce, with the one state and the one code it
# is allowed to report. Written out here rather than derived from the module so a
# new shape, a shared code, or a re-pointed state fails as a contract change.
_FAILURE_VOCABULARY: Final[dict[type, tuple[str, str]]] = {
    HeronHarnessAbsentError: (PASS_UNAVAILABLE, "heron_harness_absent"),
    HeronHarnessUnreachableError: (PASS_UNAVAILABLE, "heron_harness_unreachable"),
    HeronPassTimeoutError: (PASS_TIMED_OUT, "heron_pass_timeout"),
    HeronStagingRefusedError: (PASS_FAILED, "heron_staging_refused"),
    HeronHarnessAnswerError: (PASS_FAILED, "heron_harness_answer"),
    HeronRecordUnreadableError: (PASS_FAILED, "heron_record_unreadable"),
}


def _concrete_shapes(cls: type, found: set[type]) -> set[type]:
    for sub in cls.__subclasses__():
        if not inspect.isabstract(sub):
            found.add(sub)
        _concrete_shapes(sub, found)
    return found


def test_the_named_failure_shapes_are_exactly_the_declared_six():
    # The set is closed: :func:`_state_of` can only reach the states below, so an
    # added shape that nobody mapped would surface here instead of leaving the
    # pass pending and charging every later page a fresh cold start.
    assert _concrete_shapes(HeronPassError, set()) == set(_FAILURE_VOCABULARY)


def test_each_shape_declares_the_state_and_the_code_it_is_read_by():
    for shape, (state, code) in _FAILURE_VOCABULARY.items():
        assert _HERON_STATE_BY_TYPE[shape] == state, shape
        assert shape.code == code, shape
        assert _state_of(_instance_of(shape)) == state, shape


def test_no_two_shapes_share_one_code():
    codes = [shape.code for shape in _FAILURE_VOCABULARY]
    assert len(codes) == len(set(codes))
    assert all(code == code.strip().lower().replace("-", "_") for code in codes)


def _instance_of(shape: type) -> HeronPassError:
    """A real instance of one shape, built the way the boundary builds it."""
    if shape is HeronHarnessAbsentError:
        return shape(Path("/nonexistent/checkout/scripts/g4_pipeline/run_measured.sh"))
    if shape is HeronHarnessUnreachableError:
        return shape(PermissionError("socket denied"))
    if shape is HeronPassTimeoutError:
        return shape(subprocess.TimeoutExpired(cmd=["bash", "run_measured.sh"], timeout=1))
    if shape is HeronStagingRefusedError:
        return shape("staging dir is outside the harness mount")
    if shape is HeronHarnessAnswerError:
        return shape("layout harness exit=1: no image")
    return shape("out.jsonl", "no per-page list")


def test_a_refused_cleanup_does_not_rewrite_the_pass_answer(tmp_path, monkeypatch):
    # The leak guard must never become the story: when the staged copy cannot be
    # removed, the caller still gets the timeout that actually happened. A bare
    # PermissionError escaping from the cleanup would be classified as "no
    # engine", erasing the one fact the next page's retry depends on.
    outside = tmp_path / "attachment.pdf"
    outside.write_bytes(GOLD_MIXED.read_bytes())
    staging = REPO_ROOT / STAGING_NAME
    monkeypatch.setattr(Path, "unlink", lambda *args, **kwargs: (_ for _ in ()).throw(
        PermissionError("staging dir is pinned")))

    try:
        with pytest.raises(HeronPassTimeoutError):
            MeasuredHarnessRunner(
                spawn=_StubSpawn(
                    error=subprocess.TimeoutExpired(cmd=["bash", "run_measured.sh"], timeout=1)
                )
            ).read_record(
                pdf_path=outside,
                config=HeronLayoutConfig.from_environ(environ=EMPTY_ENV),
            )
    finally:
        monkeypatch.undo()
        shutil.rmtree(staging, ignore_errors=True)

# ---------------------------------------------------------------------------
# the leak: a capped pass must end the container it started, on both sides
# ---------------------------------------------------------------------------
_FAKE_DOCKER: Final[str] = """#!/usr/bin/env bash
# Answers the harness's docker calls well enough to show which ones it made. The
# run is a client that lingers, exactly like the real one, and its own child is
# what a caller that only kills the direct child would orphan.
printf '%s\\n' "$*" >> "$FAKE_DOCKER_LOG"
case "$1" in
    run) sleep "${FAKE_DOCKER_LINGER}" & echo $! > "$FAKE_DOCKER_PID"; wait ;;
esac
exit 0
"""

_HERON_WEIGHT: Final[str] = (
    "docling-project--docling-layout-heron/model.safetensors"
)
_TABLEFORMER_WEIGHT: Final[str] = (
    "docling-project--docling-models/model_artifacts/tableformer/accurate"
    "/tableformer_accurate.safetensors"
)


def _planted_harness(tmp_path: Path, monkeypatch) -> tuple[Path, Path, Path]:
    """The shipped harness with a fake docker, a complete cache, and no real run.

    The cache markers are what keep this off the network: the harness fetches
    weights only when they are absent, and the fetch is not the path under test.
    """
    cache = tmp_path / "model-cache"
    for weight in (_HERON_WEIGHT, _TABLEFORMER_WEIGHT):
        marker = cache / weight
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_bytes(b"")
    script = _copy_harness(tmp_path, cache_root=cache)
    binary = tmp_path / "bin"
    binary.mkdir()
    docker = binary / "docker"
    docker.write_text(_FAKE_DOCKER, encoding="utf-8")
    docker.chmod(0o755)
    log = tmp_path / "docker.log"
    log.touch()
    pid_file = tmp_path / "client.pid"
    monkeypatch.setenv("PATH", f"{binary}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("G4_MODEL_CACHE", str(cache))
    monkeypatch.setenv("FAKE_DOCKER_LOG", str(log))
    monkeypatch.setenv("FAKE_DOCKER_PID", str(pid_file))
    monkeypatch.setenv("FAKE_DOCKER_LINGER", _LINGER_S)
    return script, log, pid_file


def _docker_log(log: Path) -> str:
    return log.read_text(encoding="utf-8") if log.exists() else ""


def test_a_capped_pass_ends_the_harness_group_not_only_its_direct_child(tmp_path):
    # The reviewer's controlled experiment, run against the real spawn: killing
    # only the direct child leaves the grandchild reparented and alive, so the
    # cap has to reach the whole group.
    marker = tmp_path / "grandchild.pid"
    script = f"sleep {_LINGER_S} & echo $! > '{marker}'; wait"

    with pytest.raises(subprocess.TimeoutExpired):
        _run(
            ["bash", "-c", script],
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
            timeout=0.2,
        )

    pid = int(marker.read_text(encoding="utf-8"))
    assert _wait_until_dead(pid), f"pid {pid} outlived the timeout"


def test_a_capped_pass_leaves_no_harness_child_running(tmp_path, monkeypatch):
    script, log, pid_file = _planted_harness(tmp_path, monkeypatch)
    pdf = tmp_path / "attachment.pdf"
    pdf.write_bytes(GOLD_MIXED.read_bytes())
    config = HeronLayoutConfig(script_path=script, timeout_s=1.0)

    with pytest.raises(HeronPassTimeoutError):
        MeasuredHarnessRunner().read_record(pdf_path=pdf, config=config)

    log_text = _docker_log(log)
    assert "run --name fin-g4-t2a-attachment-" in log_text
    assert "rm -f fin-g4-t2a-attachment-" in log_text
    pid = int(pid_file.read_text(encoding="utf-8"))
    assert _wait_until_dead(pid), f"container client pid {pid} outlived the timeout"
    assert not (REPO_ROOT / STAGING_NAME).exists()


def test_a_signalled_harness_reaps_its_own_container(tmp_path, monkeypatch):
    # The script side on its own: signalling the harness's whole group -- which
    # is what a caller's wall-clock cap does, and what a person's Ctrl-C does to a
    # foreground run -- has to end the container, because the trap is the only
    # thing standing between a dead script and a live 4 GiB reservation. A signal
    # aimed at only the script would not: bash defers a trapped signal until its
    # foreground child returns.
    script, log, pid_file = _planted_harness(tmp_path, monkeypatch)
    pdf = tmp_path / "attachment.pdf"
    pdf.write_bytes(GOLD_MIXED.read_bytes())

    harness = subprocess.Popen(
        ["bash", str(script), str(pdf), "--no-slice-pass"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    deadline = time.monotonic() + 20.0
    while not pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    os.killpg(harness.pid, signal.SIGTERM)
    outcome = harness.communicate(timeout=60)

    assert "rm -f fin-g4-t2a-attachment-" in _docker_log(log)
    assert _wait_until_dead(int(pid_file.read_text(encoding="utf-8"))), outcome[0]
