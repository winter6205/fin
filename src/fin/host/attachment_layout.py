"""Heron page regions behind the page/region core's ``PageLayoutPort``.

The core keeps layout as a port so it stays free of a container and an optional
dependency; this module is where that port is actually filled. The engine is the
pinned Docling pipeline's Heron layout model, reached only through
``scripts/g4_pipeline/run_measured.sh`` — the one harness that runs it under the
hard memory ceiling against the one cached artifact set, and that already
buckets region kinds and boxes per physical page. Docling is never imported
here, so the pinned image stays the only place its version is declared, and the
dependency direction stays host -> corpus.

Two facts about the harness record drive the shape of this adapter:

* The pass is *whole-document* while the port is *per page*, so the container is
  crossed once, lazily, on the first page that asks and every later page is
  answered from that record: a 72-page filing costs one cold start, not
  seventy-two. A pass that hits its cap is the exception: it answered no
  document at all, so the next page may ask again.
* The record's boxes are in Docling's native bottom-left origin (a box reads
  ``t`` greater than ``b``) while a :class:`~fin.corpus.parsers.Region` is
  top-left. The flip happens here because the core rejects a box whose bottom
  edge is not below its top edge — and that rejection aborts the whole document,
  not one page.

Regions are additive metadata with a low measured recall (zero tables on a gold
page that plainly carries one), so nothing here may become a decision the page
lane depends on. Every failure shape — harness absent, ceiling timeout,
unreadable record, a box the media box cannot hold — is named by its own
:class:`HeronPassError` subclass and answered with no regions for that page plus
a state a caller can read.

The one side effect is a subprocess that starts a container, so it is bounded on
both sides of the boundary: the child is its own process group and a cap
SIGTERMs the whole group first, because the harness removes the container it
started from its exit trap and a SIGKILL would skip that trap.
"""

from __future__ import annotations

import errno
import json
import logging
import math
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Final, Protocol, TypeGuard

from fin.corpus.parsers import Region

__all__ = [
    "DEFAULT_STAGING_DIR",
    "PASS_FAILED",
    "PASS_MEASURED",
    "PASS_PENDING",
    "PASS_TIMED_OUT",
    "PASS_UNAVAILABLE",
    "HeronHarnessAbsentError",
    "HeronHarnessAnswerError",
    "HeronHarnessUnreachableError",
    "HeronLayoutConfig",
    "HeronLayoutConfigError",
    "HeronPageLayout",
    "HeronPassError",
    "HeronPassTimeoutError",
    "HeronRecordUnreadableError",
    "HeronStagingRefusedError",
    "LayoutRecordRunner",
    "MeasuredHarnessRunner",
    "Spawn",
]

logger = logging.getLogger(__name__)

# Kinds the record carries coordinates for. It counts text regions too, but a
# text region arrives as a count with no box, so it cannot become a Region.
_BOXED_KINDS: Final[tuple[str, ...]] = ("table", "figure")

# The harness prints exactly this prefix for the artifact holding its measured
# record; its human log and its totals summary are not it.
_MEASURED_MARK: Final[str] = "[run] measured JSON   : "

# Box order as the record writes it: left, top, right, bottom, in points.
_BOX_FIELDS: Final[tuple[str, ...]] = ("l", "t", "r", "b")

# A failure detail carries at most this much: enough to name why a pass died,
# not enough to bury a page record in a container log.
_DETAIL_CHARS: Final[int] = 400

# One crossing is the whole-document pass plus everything that pass shares a
# container with: the pinned harness also renders and recognizes every page, and
# that OCR lane -- not Heron -- sets the floor. Two committed runs of one 3-page
# filing-style gold PDF under the 4 GiB / 4 CPU ceiling bracket what a page can
# wait for: 21.4 s end to end, 15.2 s of it Docling, at load 3.61-6.37
# (results/g4-mixed-chart-001_20260926T000242Z) and 163.6 s, 115.9 s Docling, at
# load 22.8 (results/serial_remeasure_exitfix_20260927T041817Z.txt, run
# g4-mixed-chart-001_20260927T042256Z). The loaded run is 7.6x the quiet one, so
# the cap cannot be the quiet figure with margin on it: 900 s is 5.5x the worst
# committed filing run and 42x the quiet one, which is the spread a busy host
# actually needs.
#
# What that bound deliberately does not cover is stated rather than hidden: the
# same ceiling measured one 3-page *scanned* filing at 64.6 min and again at 69.7
# min (results/g4-scanned-001_20260925T185551Z.meta.json and
# results/g4-scanned-001_20260925T205348Z.meta.json, both total_latency_ms under
# the same 4 GiB / 4 CPU ceiling), and no cap this lane holds a page against
# reaches an hour. A scanned document therefore loses its regions to the timeout
# by design: regions are additive metadata with a measured recall of zero tables
# on a gold page that plainly carries one, so no page's text lane may wait an
# hour for them.
#
# The number is a knob for whoever owns budgets, and the host is free to turn it
# down: a capped pass is the one failure that stays non-terminal, so the next
# page may ask again.
_DEFAULT_TIMEOUT_S: Final[float] = 900.0

# Grace given to a capped harness after SIGTERM. The exit trap it answers to has
# to remove a container, which takes seconds; a grace that expires means the
# whole group goes to SIGKILL and the container is left behind for its ceiling to
# release.
_REAP_GRACE_S: Final[float] = 30.0

# The checkout this module was installed from: the default harness, and with it
# every other default, is read off this one path.
_HARNESS: Final[Path] = (
    Path(__file__).resolve().parents[3] / "scripts/g4_pipeline/run_measured.sh"
)
_DEFAULT_SCRIPT: Final[Path] = _HARNESS
# A document that is not already inside the checkout has to be copied here
# before the container can open it: the harness mounts its own worktree
# read-only at /worktree and resolves the PDF against that mount, so this is the
# only place a staged file can be read from. Staging names are unique per copy
# and the lane is private filings, so the directory is gitignored rather than
# tracked.
_STAGING_NAME: Final[str] = ".fin-heron-staging"

PASS_PENDING: Final[str] = "pending"
PASS_MEASURED: Final[str] = "measured"
PASS_UNAVAILABLE: Final[str] = "unavailable"
PASS_TIMED_OUT: Final[str] = "timed_out"
PASS_FAILED: Final[str] = "failed"

# One stable code per failure shape, declared the way
# :mod:`fin.host.attachment_prepare` declares its door's codes: a module-level
# ``Final[str]`` the class then names, so a code has exactly one spelling in the
# source and a caller can record it without matching a Python type.
_HERON_HARNESS_ABSENT: Final[str] = "heron_harness_absent"
_HERON_HARNESS_UNREACHABLE: Final[str] = "heron_harness_unreachable"
_HERON_STAGING_REFUSED: Final[str] = "heron_staging_refused"
_HERON_PASS_TIMEOUT: Final[str] = "heron_pass_timeout"
_HERON_HARNESS_ANSWER: Final[str] = "heron_harness_answer"
_HERON_RECORD_UNREADABLE: Final[str] = "heron_record_unreadable"

_TRUE: Final[frozenset[str]] = frozenset({"true", "1", "yes", "on"})
_FALSE: Final[frozenset[str]] = frozenset({"false", "0", "no", "off"})


class HeronLayoutConfigError(ValueError):
    """A configured knob is unusable, so the layout lane cannot be built."""


def _worktree_of(script_path: Path) -> Path:
    """The checkout a harness mounts, read off the harness's own location.

    ``run_measured.sh`` sets its worktree two directories above itself, so that
    is the only root a staged document can be opened from and the single
    authority for every path default. A path too shallow to sit that deep is not
    a harness in a checkout, and guessing a root from it would stage documents
    where no container could read them.
    """
    parents = script_path.resolve().parents
    if len(parents) < 3:
        raise HeronLayoutConfigError(
            f"a harness must sit at <checkout>/scripts/g4_pipeline/run_measured.sh"
            f", got {str(script_path)!r}"
        )
    return parents[2]


def _staging_for(script_path: Path) -> Path:
    """The staging root that belongs to the harness a config points at."""
    return _worktree_of(script_path) / _STAGING_NAME


DEFAULT_STAGING_DIR: Final[Path] = _staging_for(_HARNESS)


class HeronPassError(Exception, ABC):
    """Why one container pass did not answer, in one name per shape.

    Each subclass carries exactly one stable :attr:`code`, the same
    ``code``-declaring shape :mod:`fin.host.attachment_prepare` uses, so a caller
    records the code instead of matching a generic exception type -- and so
    "there is no engine here" can never be read out of a permission error on a
    record the engine did write.
    """

    code: ClassVar[str]

    @property
    @abstractmethod
    def detail(self) -> str:
        """What to report: this failure shape, with the cause behind it."""
        ...


class HeronHarnessAbsentError(HeronPassError, FileNotFoundError):
    """The configured harness is not a readable file, so nothing can be crossed.

    Kept an ``OSError`` because that is the truth of it -- an absent path -- and a
    caller that already distinguishes missing files from other refusals keeps
    working.
    """

    code = _HERON_HARNESS_ABSENT

    def __init__(self, script_path: Path) -> None:
        super().__init__(errno.ENOENT, "layout harness is not installed", str(script_path))
        self.filename = str(script_path)

    @property
    def detail(self) -> str:
        return f"layout harness is not installed: {self.filename}"


class HeronHarnessUnreachableError(HeronPassError):
    """Starting the harness was refused: no binary, no socket, no permission."""

    code = _HERON_HARNESS_UNREACHABLE

    def __init__(self, cause: OSError) -> None:
        super().__init__(
            f"{type(cause).__name__} while starting the layout harness: {cause}"
        )
        self.__cause__ = cause

    @property
    def detail(self) -> str:
        return str(self)


class HeronStagingRefusedError(HeronPassError):
    """A staged copy could not be placed where the container can read it."""

    code = _HERON_STAGING_REFUSED

    def __init__(self, message: str, cause: BaseException | None = None) -> None:
        super().__init__(message)
        self._message = message
        if cause is not None:
            self.__cause__ = cause

    @property
    def detail(self) -> str:
        return self._message


class HeronPassTimeoutError(HeronPassError):
    """The pass hit its wall-clock cap, so its answer is only 'not yet'."""

    code = _HERON_PASS_TIMEOUT

    def __init__(self, cause: BaseException) -> None:
        ceiling = _cap_text(getattr(cause, "timeout", None))
        super().__init__(f"layout pass hit its {ceiling}: {cause}")
        self.__cause__ = cause

    @property
    def detail(self) -> str:
        return str(self)


class HeronHarnessAnswerError(HeronPassError):
    """The harness ran and answered with a failure, or with nothing at all."""

    code = _HERON_HARNESS_ANSWER

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self._message = message

    @property
    def detail(self) -> str:
        return self._message


class HeronRecordUnreadableError(HeronPassError):
    """The record the pass wrote cannot be read or does not hold a page list.

    Deliberately not an ``OSError`` subclass: the engine *is* here and did
    measure, and reading a denied artifact as "no engine" would send a caller
    looking for a container that already answered.
    """

    code = _HERON_RECORD_UNREADABLE

    def __init__(self, name: str, reason: str, cause: BaseException | None = None) -> None:
        super().__init__(f"unreadable measured record {name}: {reason}")
        if cause is not None:
            self.__cause__ = cause

    @property
    def detail(self) -> str:
        return str(self)


def _trimmed(raw: object) -> str:
    return "" if raw is None else str(raw).strip()


def _cap_text(value: object) -> str:
    """A ceiling stated only as precisely as the exception actually knows it."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{float(value):g} s ceiling"
    return "unreported ceiling"


def _require_text(key: str, raw: object) -> str:
    """A knob that has no sensible blank spelling."""
    text = _trimmed(raw)
    if not text:
        raise HeronLayoutConfigError(f"{key} must be set to a non-empty value, got {raw!r}")
    return text


def _optional_text(key: str, raw: str | None) -> str | None:
    text = _trimmed(raw)
    if text and not text.isascii():
        raise HeronLayoutConfigError(f"{key} must be printable ASCII, got {raw!r}")
    return text or None


def _require_absolute_path(key: str, raw: object) -> Path:
    text = _require_text(key, raw)
    path = Path(text)
    if not path.is_absolute():
        raise HeronLayoutConfigError(f"{key} must be an absolute path, got {text!r}")
    return path


def _optional_path(key: str, raw: str | None) -> Path | None:
    text = _optional_text(key, raw)
    return _require_absolute_path(key, text) if text else None


def _require_timeout_s(key: str, raw: object) -> float:
    """A wall-clock cap in unsigned ASCII decimal seconds.

    ``float()`` would accept ``"1e3"``, ``"+10"``, ``"1_000"``, ``"inf"``,
    ``"nan"`` and full-width digits; each is a confusable spelling of a cap, and
    a cap that reads as infinite or as nothing is worse than no cap at all.
    """
    text = _require_text(key, raw)
    whole, dot, fraction = text.partition(".")
    valid = (
        text.isascii()
        and whole.isdigit()
        and (not dot or (fraction.isdigit() and "." not in fraction))
    )
    seconds = float(text) if valid else float("nan")
    if not valid or not math.isfinite(seconds) or seconds <= 0.0:
        raise HeronLayoutConfigError(
            f"{key} must be an unsigned ASCII decimal number of seconds, got {raw!r}"
        )
    return seconds


def _require_bool(key: str, raw: object) -> bool:
    lowered = _trimmed(raw).lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise HeronLayoutConfigError(f"{key} must be true or false, got {raw!r}")


def _path_key(environ: Mapping[str, str], key: str, default: Path) -> Path:
    # Setting a knob to nothing is a mistake; leaving it unset is a default.
    if key not in environ:
        return default
    return _require_absolute_path(key, environ[key])


def _seconds_key(environ: Mapping[str, str], key: str, default: float) -> float:
    if key not in environ:
        return default
    return _require_timeout_s(key, environ[key])


def _bool_key(environ: Mapping[str, str], key: str, default: bool) -> bool:
    if not _trimmed(environ.get(key)):
        return default
    return _require_bool(key, environ.get(key))


@dataclass(frozen=True, slots=True)
class HeronLayoutConfig:
    """How to reach the pinned pipeline, with this checkout's harness as default.

    The image name and the artifact cache are deliberately unset unless
    configured: ``run_measured.sh`` and the memory ceiling already declare them,
    and a second copy here would be a pin free to drift from the first.

    Where a document is staged follows the harness rather than this module's own
    installation, so pointing :attr:`script_path` at another checkout moves the
    mount and the staging directory together.
    """

    script_path: Path = _DEFAULT_SCRIPT
    staging_dir: Path = DEFAULT_STAGING_DIR
    timeout_s: float = _DEFAULT_TIMEOUT_S
    image: str | None = None
    model_cache: Path | None = None
    slice_pass: bool = False

    def __post_init__(self) -> None:
        # The harness's own mount is the single authority for where a document
        # may be staged, so a *default* staging root that no longer fits the
        # configured harness follows it. An explicitly configured root is left
        # exactly as given: :meth:`MeasuredHarnessRunner._stage` names the
        # contradiction rather than moving a caller's bytes somewhere unasked.
        if self.staging_dir == DEFAULT_STAGING_DIR and not self.staging_dir.is_relative_to(
            self.worktree
        ):
            object.__setattr__(self, "staging_dir", _staging_for(self.script_path))

    @staticmethod
    def from_environ(*, environ: Mapping[str, str]) -> HeronLayoutConfig:
        """Read the ``FIN_ATTACHMENT_HERON_*`` knobs; keyword-only by convention.

        A malformed value is a named failure rather than a fallback: a typo in
        the cap that silently became the default is how a bounded pass turns into
        an unbounded one.
        """
        script_path = _path_key(
            environ, "FIN_ATTACHMENT_HERON_SCRIPT", _DEFAULT_SCRIPT
        )
        return HeronLayoutConfig(
            script_path=script_path,
            staging_dir=_path_key(
                environ,
                "FIN_ATTACHMENT_HERON_STAGING_DIR",
                _staging_for(script_path),
            ),
            timeout_s=_seconds_key(
                environ, "FIN_ATTACHMENT_HERON_TIMEOUT_S", _DEFAULT_TIMEOUT_S
            ),
            image=_optional_text(
                "FIN_ATTACHMENT_HERON_IMAGE", environ.get("FIN_ATTACHMENT_HERON_IMAGE")
            ),
            model_cache=_optional_path(
                "FIN_ATTACHMENT_HERON_MODEL_CACHE",
                environ.get("FIN_ATTACHMENT_HERON_MODEL_CACHE"),
            ),
            slice_pass=_bool_key(environ, "FIN_ATTACHMENT_HERON_SLICE_PASS", False),
        )

    @property
    def worktree(self) -> Path:
        """The checkout the harness mounts; see :func:`_worktree_of`."""
        return _worktree_of(self.script_path)


class LayoutRecordRunner(Protocol):
    """The one side effect this adapter is allowed: read a whole-document pass."""

    def read_record(
        self, *, pdf_path: Path, config: HeronLayoutConfig
    ) -> Mapping[str, object]: ...


# A ``subprocess.run``-shaped seam, so the container boundary stays the only
# part of this module a test cannot cross.
Spawn = Callable[..., object]


def _signal_group(process: subprocess.Popen[str], signal_number: int) -> bool:
    """Signals the child's whole group; False when there is no group to signal.

    Never raises: a group that already left, or one this process may not strike,
    must not replace the capped-pass failure the caller came home with.
    """
    try:
        os.killpg(os.getpgid(process.pid), signal_number)
    except OSError:
        # EXIT: the group left, or was not ours to strike; either way the pass is
        # already capped and the caller is going home with that answer.
        return False
    return True


def _reap(process: subprocess.Popen[str]) -> None:
    """Ends a capped harness and everything it started, without orphaning any.

    ``subprocess.run`` SIGKILLs only its direct child, and that is not enough
    here twice over: the harness's own ``docker run`` survives as an orphan that
    keeps a 4 GiB container running, and a SIGKILL skips the exit trap written to
    remove it. So the whole group is asked to leave first and given its trap long
    enough to finish, then struck, then drained so no zombie is left behind.
    """
    _signal_group(process, signal.SIGTERM)
    deadline = time.monotonic() + _REAP_GRACE_S
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    if process.poll() is None:
        _signal_group(process, signal.SIGKILL)
    # Draining the pipes is what collects the child, and whatever the harness
    # printed on its way out is the only log of it. Bounded, because a page must
    # not block forever on a pass that was already declared capped.
    try:
        process.communicate(timeout=_REAP_GRACE_S)
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        # EXIT: the pass is already declared capped, so a drain that fails
        # cannot be the pass's answer. The child is struck again below
        # regardless; this only records that its pipes were never read.
        logger.debug("harness child pipes not drained: %s", exc)
    if process.poll() is None:
        _signal_group(process, signal.SIGKILL)


def _run(
    command: Sequence[str], *, env: Mapping[str, str] | None, timeout: float
) -> object:
    """Runs a harness to completion, or ends it and everything under it.

    ``start_new_session`` is what makes the cap reach the container: it puts the
    harness and its ``docker run`` in one group this process can signal as a
    unit. The exit status is never checked here; it is the record's business.
    """
    with subprocess.Popen(
        list(command),
        env=None if env is None else dict(env),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    ) as process:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _reap(process)
            raise
        return subprocess.CompletedProcess(
            process.args, process.returncode, stdout, stderr
        )


class MeasuredHarnessRunner:
    """Runs ``run_measured.sh`` and reads back the record the run named.

    The driver's JSON never reaches this process's stdout — the harness captures
    it into its own run directory and prints where it put it — so that printed
    path is the contract, and its absence is a failed pass rather than a pass
    that found nothing.
    """

    def __init__(self, spawn: Spawn | None = None) -> None:
        self._spawn = spawn if spawn is not None else _run

    def read_record(
        self, *, pdf_path: Path, config: HeronLayoutConfig
    ) -> Mapping[str, object]:
        if not config.script_path.is_file():
            # EXIT: nothing can be crossed, so the container is never started and
            # the caller is told there is no harness rather than that one failed.
            raise HeronHarnessAbsentError(config.script_path)
        staged = self._stage(pdf_path, config)
        try:
            argv = ["bash", str(config.script_path), str(staged or pdf_path)]
            if not config.slice_pass:
                # The per-page latency pass converts every page a second time to
                # price the pipeline. This lane wants regions, not a price.
                argv.append("--no-slice-pass")
            try:
                completed = self._spawn(
                    argv, env=self._child_env(config), timeout=float(config.timeout_s)
                )
            except Exception as exc:
                # EXIT: what the spawn answered is named before it leaves this
                # boundary, and the staged copy is destroyed on the way out.
                raise self._wrap(exc) from exc
        finally:
            if staged is not None:
                # An unlink that is refused here must not become the pass's
                # answer: a cleanup failure replacing the exception would turn a
                # capped pass into an ``OSError`` the table reports as "no
                # engine", and the document's real failure would be gone.
                try:
                    staged.unlink(missing_ok=True)
                except OSError as cleanup_exc:
                    # EXIT: a copy this process could not remove is still reported
                    # by the pass that made it, never by this cleanup.
                    logger.debug("staged copy %s not removed: %s", staged, cleanup_exc)
                self._prune(config)
        return self._record_of(completed)

    @staticmethod
    def _stage(pdf_path: Path, config: HeronLayoutConfig) -> Path | None:
        """Copy a document into the mount when it is not already inside it.

        The copy is this function's own responsibility to destroy: a filing that
        is private to one thread must not survive a failed copy in a directory
        the repository auto-commits, so every step after the file exists is
        guarded and the guard unlinks before it re-raises.
        """
        resolved = pdf_path.resolve()
        staging = config.staging_dir
        if not staging.is_relative_to(config.worktree):
            raise HeronStagingRefusedError(
                f"staging dir {staging} is outside the harness mount "
                f"{config.worktree}, so the container could not open a staged file"
            )  # EXIT: copying where no mount could read it would blame docker later
        if resolved.is_relative_to(config.worktree):
            return None  # EXIT: the harness can open this path as it stands
        staging.mkdir(parents=True, exist_ok=True)
        # A name unique per copy, because two documents of one stem staged by
        # one process would otherwise share a file that the first to finish
        # deletes out from under the second. 0600 from ``mkstemp`` is kept: the
        # bytes are one thread's filing, and the container reads them as one.
        handle, named = tempfile.mkstemp(dir=staging, prefix=f"{pdf_path.stem}-", suffix=".pdf")
        target = Path(named)
        try:
            os.close(handle)
            shutil.copyfile(resolved, target)
        except BaseException as exc:
            # The removal is best-effort on purpose: a directory that refuses the
            # unlink must not replace the named staging failure with a bare
            # OSError, which the state table would report as "no engine" rather
            # than as the leak it is.
            try:
                target.unlink(missing_ok=True)
            except OSError as cleanup_exc:
                # EXIT: a copy this process could not destroy is still reported
                # by the staging failure about to be raised, not by this cleanup.
                # Bound to ``cleanup_exc``, not ``exc``: an ``except ... as`` name
                # is unbound at handler end, so reusing ``exc`` here would shadow
                # the outer binding and turn the typed re-raise below into
                # ``UnboundLocalError``.
                logger.debug("staging copy %s not destroyed: %s", target, cleanup_exc)
            raise HeronStagingRefusedError(
                f"could not stage {resolved.name} for the layout pass: "
                f"{type(exc).__name__}",
                exc,
            ) from exc  # EXIT: the copy is destroyed before the reason is reported
        return target

    @staticmethod
    def _prune(config: HeronLayoutConfig) -> None:
        try:
            config.staging_dir.rmdir()
        except OSError as exc:
            # EXIT: another document is still staged, or the dir was not ours.
            logger.debug("staging dir %s kept: %s", config.staging_dir, exc)

    @staticmethod
    def _child_env(config: HeronLayoutConfig) -> dict[str, str] | None:
        """Override only what was configured and inherit the rest untouched."""
        overrides: dict[str, str] = {}
        if config.image:
            overrides["G4_IMAGE"] = config.image
        if config.model_cache:
            overrides["G4_MODEL_CACHE"] = str(config.model_cache)
        return {**os.environ, **overrides} if overrides else None

    @staticmethod
    def _record_of(completed: object) -> Mapping[str, object]:
        code = getattr(completed, "returncode", None)
        if not _plain_int(code):
            raise HeronHarnessAnswerError(
                "layout harness answered no exit status"
            )  # EXIT: an answer this shape cannot be read as a measurement
        stderr = str(getattr(completed, "stderr", "") or "").strip()
        stdout = str(getattr(completed, "stdout", "") or "")
        detail = stderr[-_DETAIL_CHARS:] or "no log"
        if code < 0:
            # The death of the harness process itself is the one failure that has
            # no harness log to quote, so the signal is named here rather than
            # left for a caller to work out from "-15".
            raise HeronHarnessAnswerError(
                f"layout harness was ended by signal {-code} with no output: {detail}"
            )  # EXIT: a signal, not a pipeline verdict; never a successful pass
        if code != 0:
            # A captured record is not a succeeded run: the harness exits
            # non-zero when the container died on its ceiling even with a line
            # printed, so the status is checked before the artifact.
            raise HeronHarnessAnswerError(
                f"layout harness exit={code}: {detail}"
            )  # EXIT: the run failed, so its artifact is not evidence
        artifact = _artifact_path(stdout)
        if artifact is None or not artifact.is_file():
            raise HeronHarnessAnswerError(
                f"no measured record from the layout harness: "
                f"{detail or 'it printed no artifact path'}"
            )  # EXIT: a succeeded run that named no record is not a run that found none
        return _read_record(artifact)

    @staticmethod
    def _wrap(cause: BaseException) -> BaseException:
        """Names what the spawn answered, without inventing a state for it.

        The cap gets its own type because it is the only failure that is *not* a
        result: the harness may still be inside its container, and its exit trap
        is what removes it.
        """
        if isinstance(cause, (TimeoutError, subprocess.TimeoutExpired)):
            return HeronPassTimeoutError(cause)
        if isinstance(cause, OSError):
            return HeronHarnessUnreachableError(cause)
        return cause


def _artifact_path(stdout: str) -> Path | None:
    for line in stdout.splitlines():
        if line.startswith(_MEASURED_MARK):
            text = line[len(_MEASURED_MARK) :].strip()
            return Path(text) if text else None
    return None


def _read_record(artifact: Path) -> Mapping[str, object]:
    name = artifact.name
    try:
        record = json.loads(artifact.read_text(encoding="utf-8").strip())
    except Exception as exc:
        raise HeronRecordUnreadableError(
            name, f"{type(exc).__name__}: {exc}", exc
        ) from exc
    if not isinstance(record, Mapping) or not isinstance(record.get("pages"), Sequence):
        raise HeronRecordUnreadableError(name, "no per-page list")
    return record


# Each named failure shape reports exactly one state. The set is closed over the
# subclasses above, so a permission error on a record the engine did write can
# never be read out as "there is no engine".
_HERON_STATE_BY_TYPE: Final[Mapping[type[HeronPassError], str]] = {
    HeronHarnessAbsentError: PASS_UNAVAILABLE,
    HeronHarnessUnreachableError: PASS_UNAVAILABLE,
    HeronPassTimeoutError: PASS_TIMED_OUT,
    HeronStagingRefusedError: PASS_FAILED,
    HeronHarnessAnswerError: PASS_FAILED,
    HeronRecordUnreadableError: PASS_FAILED,
}


def _state_of(exc: BaseException) -> str:
    """The pass state a failure shape reports.

    A named :class:`HeronPassError` is read out of the table along its own
    inheritance chain, so a shape that extends one of the six still reports that
    one's state: a lookup keyed on the exact type would raise here, and a raise
    inside the failure path would leave the pass ``pending`` and make every later
    page of the document pay for the cold start again. The generic branches are
    only ever reached by a runner someone else wrote.
    """
    if isinstance(exc, HeronPassError):
        for klass in type(exc).__mro__:
            state = _HERON_STATE_BY_TYPE.get(klass)
            if state is not None:
                return state
        # EXIT: a named shape nobody declared a state for; it ran, so it is not
        # "no engine", and it did answer, so it is not a cap.
        return PASS_FAILED
    if isinstance(exc, (subprocess.TimeoutExpired, TimeoutError)):
        return PASS_TIMED_OUT
    if isinstance(exc, OSError):
        # An absent binary, an unreachable socket, an unmounted path: the engine
        # is not here, which is a different answer from "it ran and found none".
        return PASS_UNAVAILABLE
    return PASS_FAILED  # EXIT: an unnamed failure, reported rather than raised


def _terminal(state: str) -> bool:
    """Whether a state is an answer, rather than a boundary not yet crossed.

    A capped pass is the one failure that reported nothing about the document, so
    it is not cached: the next page may ask again. Caching it would strand a
    document whose only sin was that the host was busy.
    """
    return state != PASS_TIMED_OUT


class HeronPageLayout:
    """``PageLayoutPort`` filled by the pinned Heron pass, once per document.

    Build one per document: the pass it caches belongs to a single file, and the
    core is deliberately free of cross-document state. ``page_regions`` never
    raises and never returns text, so an absent image, a killed container, or a
    layout that found nothing all leave the page lane — native text and raster —
    exactly as ``NullLayout`` would, with only the boxes gone.

    Invariant: the pass state, its record and its details are read or written
    only under ``_state_lock``, and the crossing that fills them is serialized by
    ``_pass_lock``, so one document pays for one container per answer.
    """

    def __init__(
        self,
        *,
        pdf_path: Path,
        runner: LayoutRecordRunner | None = None,
        config: HeronLayoutConfig | None = None,
    ) -> None:
        if not isinstance(pdf_path, Path):
            raise TypeError(f"pdf_path must be a Path, got {type(pdf_path).__name__}")
        if not pdf_path.is_file():
            raise ValueError(f"pdf_path is not a readable file: {pdf_path}")
        self._pdf_path = pdf_path
        self._config = (
            config
            if config is not None
            else HeronLayoutConfig.from_environ(environ=os.environ)
        )
        self._runner: LayoutRecordRunner = (
            runner if runner is not None else MeasuredHarnessRunner()
        )
        self._pass_lock = threading.Lock()
        # Guards the three fields below and ``_answer_detail``, and is never held
        # across the crossing: a caller reading its state while a page waits on
        # the container must not queue behind that container.
        self._state_lock = threading.Lock()
        self._pages: Mapping[int, Mapping[str, object]] | None = None
        self._state = PASS_PENDING
        self._detail: str | None = None
        self._answer_detail: str | None = None

    @property
    def pdf_path(self) -> Path:
        return self._pdf_path

    @property
    def pass_state(self) -> str:
        """How the one container pass ended; ``pending`` until a page asks."""
        with self._state_lock:
            return self._state

    @property
    def failure_detail(self) -> str | None:
        """Why the pass answered nothing, with its state as the prefix."""
        with self._state_lock:
            return self._detail

    @property
    def answer_failure_detail(self) -> str | None:
        """Why one page's boxes could not be read, if that ever happened."""
        with self._state_lock:
            return self._answer_detail

    @property
    def capped_pages(self) -> frozenset[int] | None:
        """Pages whose boxes the harness truncated at its own per-line bound.

        A page can therefore report more regions than it has boxes; a caller
        that counts boxes must read this rather than assume the two agree.
        ``None`` is that bound's other answer: no pass has been taken yet, so
        nothing is known about any page. An empty set means the pass did happen
        and no page was capped.
        """
        with self._state_lock:
            pages = self._pages
        if pages is None:
            return None
        return frozenset(
            page_no
            for page_no, entry in pages.items()
            if any(entry.get(f"{kind}_boxes_capped") is True for kind in _BOXED_KINDS)
        )

    # -- the port --

    def page_regions(
        self, *, page_no: int, size_pt: tuple[float, float]
    ) -> tuple[Region, ...]:
        if not _plain_int(page_no) or page_no < 1:
            return ()  # EXIT: a page the source cannot have has nothing to look up
        if not _media_box(size_pt):
            return ()  # EXIT: without a trusted media box there is no scale to answer in
        try:
            pages = self._ensure_pass()
            if pages is None:
                return ()  # EXIT: a pass that answered nothing answers no page either
            return _regions_of(pages.get(int(page_no)), size_pt=size_pt)
        except Exception as exc:  # a page is never lost to the region lane
            # Kept apart from the pass state on purpose: how the container
            # answered stays reportable even if this page's arithmetic died,
            # and a caller can still tell "no engine" from "bad box".
            with self._state_lock:
                self._answer_detail = f"{type(exc).__name__}: {exc}"[:_DETAIL_CHARS]
            return ()  # EXIT: one unanswerable page, reported, never a raised error

    # -- the one pass, taken lazily and shared by every page --

    def _ensure_pass(self) -> Mapping[int, Mapping[str, object]] | None:
        with self._pass_lock:
            with self._state_lock:
                if self._state != PASS_PENDING and _terminal(self._state):
                    # A second page that arrived while the first was still
                    # crossing the boundary waits here and then reads the same
                    # record: the cold start is the dominant cost of this lane
                    # and runs once per document.
                    return self._pages
            try:
                # Indexing belongs to the pass, not to the page: a record this
                # side cannot lay out is one unreadable answer, and leaving the
                # state pending would have every later page pay for the cold
                # start again.
                pages = _index_pages(
                    self._runner.read_record(
                        pdf_path=self._pdf_path, config=self._config
                    )
                )
            except Exception as exc:
                state = _state_of(exc)
                with self._state_lock:
                    self._pages = None
                    self._note(state, exc)
                return None  # EXIT: the failure is the answer, and it is cached
            with self._state_lock:
                self._pages = pages
                self._state = PASS_MEASURED
                self._detail = None
                return self._pages

    def _note(self, state: str, exc: BaseException) -> None:
        """Records how the pass ended; the caller holds ``_state_lock``.

        A named failure reports its own code, so a reader of the detail never has
        to recognize a Python type to know which shape killed the pass.
        """
        reason = (
            f"{exc.code}: {exc.detail}"
            if isinstance(exc, HeronPassError)
            else f"{type(exc).__name__}: {exc}"
        )
        self._state = state
        self._detail = f"{state}: {reason}"[:_DETAIL_CHARS]


def _index_pages(record: object) -> Mapping[int, Mapping[str, object]]:
    """Key the record's page entries by physical page number."""
    pages: dict[int, Mapping[str, object]] = {}
    entries = record.get("pages") if isinstance(record, Mapping) else None
    if isinstance(entries, (str, bytes)) or not isinstance(entries, Sequence):
        return {}  # EXIT: an entry list the harness did not write answers nothing
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        page_no = entry.get("page_no")
        if not _plain_int(page_no) or int(page_no) < 1:
            continue  # EXIT: an entry that names no page belongs to no page
        pages.setdefault(int(page_no), entry)
    return pages


def _regions_of(entry: object, *, size_pt: Sequence[float]) -> tuple[Region, ...]:
    if not isinstance(entry, Mapping):
        return ()  # EXIT: a page the record did not describe has no boxes to flip
    width, height = float(size_pt[0]), float(size_pt[1])
    regions: list[Region] = []
    for kind in _BOXED_KINDS:
        boxes = entry.get(f"{kind}_boxes")
        if isinstance(boxes, (str, bytes)) or not isinstance(boxes, Sequence):
            continue  # EXIT: a kind the record gave no coordinate list for
        for raw in boxes:
            region = _region(kind, raw, width=width, height=height)
            if region is not None:
                regions.append(region)
    # Reading order, independent of which bucket the record kept the box in: a
    # consumer cropping pixels needs the same list every time for one page.
    regions.sort(key=lambda box: (box.top_pt, box.left_pt))
    return tuple(regions)


def _region(kind: str, raw: object, *, width: float, height: float) -> Region | None:
    """One record box, flipped into the core's top-left space, or nothing.

    The flip is ``top = page height - t``: the record keeps Docling's
    bottom-left origin, where ``t`` is the *higher* edge. A box that arrives the
    other way round is malformed rather than mirrored, because swapping it would
    invent geometry the model never reported — so it is dropped. An overhang is
    clamped instead, because there the region is real and only its edge is a
    rounding artifact.
    """
    if not isinstance(raw, Mapping):
        return None
    edges: list[float] = []
    for field in _BOX_FIELDS:
        value = raw.get(field)
        if not _number(value):
            return None  # EXIT: a box with no four finite edges is not a place
        edges.append(float(value))
    left, top, right, bottom = edges
    clamped = (
        min(max(left, 0.0), width),
        min(max(height - top, 0.0), height),
        min(max(right, 0.0), width),
        min(max(height - bottom, 0.0), height),
    )
    if clamped[2] <= clamped[0] or clamped[3] <= clamped[1]:
        return None  # EXIT: no area is no region, whatever the model reported
    return Region(
        kind=kind, left_pt=clamped[0], top_pt=clamped[1],
        right_pt=clamped[2], bottom_pt=clamped[3],
    )


def _plain_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _number(value: object) -> TypeGuard[float]:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _media_box(value: object) -> bool:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        return False
    if len(value) != 2 or not all(_number(edge) for edge in value):
        return False
    return float(value[0]) > 0.0 and float(value[1]) > 0.0
