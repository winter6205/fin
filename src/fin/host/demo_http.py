"""Demo-only status and reset routes.

The reset door has a deliberately small interface: when the process was
started in demo mode, it re-runs the repository's fixed demo seed command
using the process environment. Request data never becomes command data.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit

from fastapi import APIRouter, FastAPI, Query, Request
from fastapi.responses import JSONResponse

from fin.corpus.demo_seed import (
    DemoSeedError,
    load_demo_documents,
    parse_seed_result,
)


logger = logging.getLogger(__name__)

DEMO_MODE_ENV = "FIN_DEMO_MODE"
DEMO_EMBED_DIMENSIONS_ENV = "FIN_DEMO_EMBED_DIMENSIONS"
DEMO_EMBED_GENERATION_ENV = "FIN_DEMO_EMBED_GENERATION"
#: The paired iknow-memory checkout the seed runs from. The CLI owns the same
#: name as its default (``scripts/seed_demo.py``); the route passes it
#: explicitly so the seed never has to guess which checkout it writes to.
IKNOW_ROOT_ENV = "FIN_IKNOW_ROOT"
_REPO_ROOT = Path(__file__).resolve().parents[3]
_RESET_LOCK = threading.Lock()
router = APIRouter(prefix="/api/demo", tags=["demo"])
_SECTION_HEADER = re.compile(r"^\s*Section\s+(\d+(?:\.\d+)*)\b", re.IGNORECASE | re.MULTILINE)

#: How much of the seed's own output a log line may carry.
_SEED_OUTPUT_SUMMARY_LIMIT: Final[int] = 400

#: ``scheme://user:pass@host`` -- the userinfo part never reaches the log.
_URL_USERINFO_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*://)[^/\s@]+@"
)

#: ``password=``, ``token=``, ``key=`` and ``secret=`` values, quoted or not.
_CREDENTIAL_VALUE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?i)(?<![A-Za-z0-9])(password|token|key|secret)\s*=\s*"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s&;,]+)"
)


@dataclass(frozen=True)
class DemoSourceChunk:
    """One fixture-backed source location and its trusted text."""

    document_id: str
    version: str
    unit: int
    page: int
    title: str
    section: str
    content: str

    def locator_payload(self) -> dict[str, str | int]:
        return {
            "document_id": self.document_id,
            "version": self.version,
            "unit": self.unit,
            "page": self.page,
            "title": self.title,
            "section": self.section,
        }


@lru_cache(maxsize=1)
def _demo_source_catalog() -> dict[tuple[str, str, int, int], DemoSourceChunk]:
    """Load the fixed fixture allowlist once for this server process.

    The allowlist is keyed by the locator the seed pushed: the unit's own
    ``document_id`` / ``version`` / ``unit`` come from
    :class:`fin.corpus.demo_seed.DemoUnit`, so the two sides name one
    identity by construction. ``page`` is the axis beside it -- the page the
    unit's heading names -- and the section is the host's own.
    """
    sources: dict[tuple[str, str, int, int], DemoSourceChunk] = {}
    for document in load_demo_documents():
        for unit in document.units:
            locator = unit.locator
            content = unit.content
            source = DemoSourceChunk(
                document_id=locator.document_id,
                version=locator.version,
                unit=locator.unit,
                page=unit.page,
                title=document.title,
                section=_fixture_section(content),
                content=content,
            )
            identity = (source.document_id, source.version, source.unit, source.page)
            if identity in sources:
                raise DemoSeedError("demo fixture repeats a source locator")
            sources[identity] = source
    return sources


def _fixture_section(content: str) -> str:
    match = _SECTION_HEADER.search(content)
    if match is None:
        raise DemoSeedError("demo fixture has no section heading")
    return f"Section {match.group(1)}"


class DemoSeedConfigurationError(ValueError):
    """The server lacks valid seed configuration required for reset."""

    def __init__(self, code: str, setting: str) -> None:
        super().__init__(code)
        self.code = code
        self.setting = setting


class DemoSeedExecutionError(RuntimeError):
    """The fixed seed command failed without safe operator detail."""


class DemoSeedRefusedError(DemoSeedExecutionError):
    """The seed command's guard rejected the current demo environment."""


class DemoSeedNamedFailureError(DemoSeedExecutionError):
    """The seed failed with one of its own stable, named codes.

    The code is the seed's, not this module's: the operator reads the same
    name here as in the seed's own log line, and the route never invents a
    second vocabulary for the same failure.
    """

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def demo_mode_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Return whether the process explicitly opted into demo mode."""
    return (environ if environ is not None else os.environ).get(DEMO_MODE_ENV) == "1"


def _is_same_origin_request(request: Request) -> bool:
    """Refuse browser writes from another origin or site.

    Missing browser metadata remains valid for command-line callers and
    local automation. When a browser supplies either signal, it must say
    this request came from the same origin.
    """
    fetch_site = request.headers.get("sec-fetch-site", "").strip().lower()
    if fetch_site and fetch_site != "same-origin":
        return False

    origin = request.headers.get("origin")
    if origin is None:
        return True
    try:
        parsed_origin = urlsplit(origin)
        origin_port = parsed_origin.port or _default_port(parsed_origin.scheme)
        request_port = request.url.port or _default_port(request.url.scheme)
    except ValueError:
        return False
    return (
        parsed_origin.scheme.lower() == request.url.scheme.lower()
        and parsed_origin.hostname is not None
        and parsed_origin.hostname.lower() == (request.url.hostname or "").lower()
        and origin_port == request_port
        and parsed_origin.username is None
        and parsed_origin.password is None
        and parsed_origin.path in ("", "/")
        and not parsed_origin.query
        and not parsed_origin.fragment
    )


def _default_port(scheme: str) -> int | None:
    if scheme.lower() == "http":
        return 80
    if scheme.lower() == "https":
        return 443
    return None


def _seed_configuration(environ: Mapping[str, str]) -> tuple[int, str]:
    dimensions_text = environ.get(DEMO_EMBED_DIMENSIONS_ENV, "").strip()
    if not dimensions_text:
        raise DemoSeedConfigurationError(
            "demo_configuration_missing", DEMO_EMBED_DIMENSIONS_ENV
        )
    try:
        dimensions = int(dimensions_text)
    except ValueError as exc:
        raise DemoSeedConfigurationError(
            "demo_configuration_invalid", DEMO_EMBED_DIMENSIONS_ENV
        ) from exc
    if dimensions < 1:
        raise DemoSeedConfigurationError(
            "demo_configuration_invalid", DEMO_EMBED_DIMENSIONS_ENV
        )

    generation = environ.get(DEMO_EMBED_GENERATION_ENV, "").strip()
    if not generation:
        raise DemoSeedConfigurationError(
            "demo_configuration_missing", DEMO_EMBED_GENERATION_ENV
        )
    return dimensions, generation


def run_demo_seed(environ: Mapping[str, str] | None = None) -> None:
    """Restore the demo data with the repository's fixed demo seed command.

    Embedding settings and the paired checkout come from the server
    environment, never from the HTTP request. The seed process receives the
    full server environment because the seed library validates the isolated
    memory home and dev ports.

    The seed runs WITHOUT ``--reset``. The destructive dev-stack wipe belongs
    to the paired repository: its ``--reset`` data-table allowlist is not
    published there, and fin ships no bolt client for the graph half, so a
    wipe request could only ever fail. The non-destructive run restores the
    demo data instead -- it re-submits the fixture documents and rewrites the
    demo catalog rows in place -- and it is the same invocation
    ``scripts/demo.sh`` starts the demo with, so the route and the startup
    agree by construction.
    """
    process_env = dict(os.environ if environ is None else environ)
    dimensions, generation = _seed_configuration(process_env)
    iknow_root = process_env.get(IKNOW_ROOT_ENV, "").strip()
    if not iknow_root:
        raise DemoSeedConfigurationError(
            "demo_seed_configuration_missing", IKNOW_ROOT_ENV
        )
    command = [
        sys.executable,
        str(_REPO_ROOT / "scripts" / "seed_demo.py"),
        "--yes",
        "--embed-dimensions",
        str(dimensions),
        "--embed-generation",
        generation,
        "--iknow-root",
        iknow_root,
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=_REPO_ROOT,
            env=process_env,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        # The seed classifies its own failures and says so on its typed
        # channel. The exit code alone cannot tell a guard refusal (2) from a
        # named failure (1), nor a named failure from an untyped crash, so the
        # code is read off that channel and never guessed from prose.
        stdout = _captured_text(exc.output)
        stderr = _captured_text(exc.stderr)
        code = _seed_failure_code(stdout, stderr)
        _log_seed_failure(exc.returncode, code, stdout, stderr)
        if exc.returncode == 2:
            raise DemoSeedRefusedError from None
        if code is not None:
            raise DemoSeedNamedFailureError(code) from None
        raise DemoSeedExecutionError from None
    except OSError:
        # EXIT: the fixed Python executable or repository seed file could not
        # be started; no shell fallback is attempted.
        logger.warning(
            "demo reset failed: the fixed seed command could not be started"
        )
        raise DemoSeedExecutionError from None
    logger.info(
        "demo reset restored the demo corpus and the demo catalog rows in "
        "place; the dev stack was NOT wiped; seed summary: %s",
        _seed_output_summary(
            _captured_text(getattr(completed, "stdout", None)),
            _captured_text(getattr(completed, "stderr", None)),
        ),
    )


def _captured_text(value: object) -> str:
    """One captured stream's text, whatever the caller handed back."""
    return value if isinstance(value, str) else ""


def _seed_failure_code(stdout: str, stderr: str) -> str | None:
    """The seed's own stable code, read off its typed result line.

    The stderr stream is read first: that is where the seed writes the typed
    line. The code is the seed's -- never a name this module invents -- and
    ``None`` means the run carried no typed result at all.
    """
    for text in (stderr, stdout):
        parsed = parse_seed_result(text)
        if parsed is not None:
            return parsed[1]
    return None


def _log_seed_failure(
    returncode: int, code: str | None, stdout: str, stderr: str
) -> None:
    """Record a failed reset: the seed's code, its exit code, its output.

    A reset that failed used to leave nothing but an opaque 500 in the serve
    log. The output is redacted before it is written, because a crash's raw
    stderr can carry a connection string and the serve log is a file an
    operator shares.
    """
    logger.warning(
        "demo reset failed: seed_code=%s exit_code=%s output=%s",
        code if code is not None else "none",
        returncode,
        _seed_output_summary(stdout, stderr),
    )


def _seed_output_summary(stdout: str, stderr: str) -> str:
    """A bounded, credential-free summary of what the seed printed.

    The tail is kept: the seed writes its refusal last.
    """
    parts: list[str] = []
    for label, text in (("stdout", stdout), ("stderr", stderr)):
        cleaned = " ".join(_redact_secrets(text).split())
        if not cleaned:
            continue
        if len(cleaned) > _SEED_OUTPUT_SUMMARY_LIMIT:
            cleaned = cleaned[-_SEED_OUTPUT_SUMMARY_LIMIT:]
        parts.append(f"{label}={cleaned}")
    return " | ".join(parts) if parts else "none"


def _redact_secrets(text: str) -> str:
    """Mask credentials before any seed output reaches the log."""
    without_userinfo = _URL_USERINFO_PATTERN.sub(r"\g<scheme>***@", text)
    return _CREDENTIAL_VALUE_PATTERN.sub(
        lambda match: f"{match.group(1)}=***", without_userinfo
    )


@router.get("")
def demo_status() -> dict[str, bool]:
    """Tell the SPA whether demo-only controls may be shown."""
    return {"enabled": demo_mode_enabled()}


@router.get("/sources", response_model=None)
def demo_sources() -> JSONResponse | dict[str, list[dict[str, str | int]]]:
    """Return compact metadata for the fixed demo fixture allowlist."""
    if not demo_mode_enabled():
        return JSONResponse(status_code=404, content={"error": "demo_mode_disabled"})
    try:
        sources = _demo_source_catalog().values()
    except DemoSeedError:
        return JSONResponse(
            status_code=503, content={"error": "demo_source_unavailable"}
        )
    return {"sources": [source.locator_payload() for source in sources]}


@router.get("/source", response_model=None)
def demo_source(
    document_id: str = Query(..., min_length=1, max_length=200),
    version: str = Query(..., min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"),
    unit: int = Query(..., ge=1, le=100),
    page: int = Query(..., ge=1, le=100),
) -> JSONResponse | dict[str, str | int]:
    """Return one chunk only when every locator component is allowlisted."""
    if not demo_mode_enabled():
        return JSONResponse(status_code=404, content={"error": "demo_mode_disabled"})
    try:
        source = _demo_source_catalog().get((document_id, version, unit, page))
    except DemoSeedError:
        return JSONResponse(
            status_code=503, content={"error": "demo_source_unavailable"}
        )
    if source is None:
        return JSONResponse(status_code=404, content={"error": "demo_source_not_found"})
    return {**source.locator_payload(), "content": source.content}


@router.post("/reset")
def reset_demo(request: Request) -> JSONResponse:
    """Restore the demo data with the fixed seed invocation."""
    if not demo_mode_enabled():
        return JSONResponse(
            status_code=404, content={"error": "demo_mode_disabled"}
        )
    if not _is_same_origin_request(request):
        return JSONResponse(
            status_code=403, content={"error": "demo_origin_forbidden"}
        )
    if not _RESET_LOCK.acquire(blocking=False):
        return JSONResponse(
            status_code=409, content={"error": "demo_reset_in_progress"}
        )
    try:
        run_demo_seed()
    except DemoSeedConfigurationError as exc:
        return JSONResponse(
            status_code=503,
            content={"error": exc.code, "setting": exc.setting},
        )
    except DemoSeedRefusedError:
        return JSONResponse(
            status_code=409, content={"error": "demo_seed_refused"}
        )
    except DemoSeedNamedFailureError as exc:
        # The demo environment is not ready: the seed named the reason, and
        # its own code is the most useful thing the caller can be told.
        return JSONResponse(status_code=503, content={"error": exc.code})
    except DemoSeedExecutionError:
        return JSONResponse(
            status_code=500, content={"error": "demo_reset_failed"}
        )
    finally:
        _RESET_LOCK.release()
    return JSONResponse(status_code=200, content={"status": "reset"})


def mount(app: FastAPI) -> None:
    """Mount the demo-only HTTP surface before the SPA catch-all."""
    app.include_router(router)


__all__ = [
    "DEMO_EMBED_DIMENSIONS_ENV",
    "DEMO_EMBED_GENERATION_ENV",
    "DEMO_MODE_ENV",
    "IKNOW_ROOT_ENV",
    "DemoSeedConfigurationError",
    "DemoSeedExecutionError",
    "DemoSeedNamedFailureError",
    "DemoSeedRefusedError",
    "demo_mode_enabled",
    "mount",
    "run_demo_seed",
]
