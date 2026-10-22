"""``python -m fin.eval.acceptance``: the recorded P4 preflight command.

Two subcommands share one exit-code contract, so an operator reading only the
status can tell "the environment is broken" (2) from "this invocation or its
manifest is wrong" (1) and from "the command line itself is wrong" (64). A
successful run prints one JSON object on stdout; diagnostics never carry a
DSN, a credential, or any environment value.

Readiness and browser smoke are *observed by the caller*, so the CLI never
invents them: it fails those checks closed unless ``--probe`` names a callable
that reports readiness (``--probe=runtime_readiness=package.module:function``).
Campaign drivers call :func:`fin.eval.acceptance.preflight.run_preflight` in
process with the same probes and record this command alongside their result.
"""

from __future__ import annotations

import importlib
import json
import shlex
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from fin.eval.acceptance.preflight import (
    CHECK_CASE_MANIFEST,
    PROBE_BROWSER_SMOKE,
    PROBE_MODEL_AND_TOOLS,
    PROBE_PLAYWRIGHT,
    PROBE_REQUEST_EVENT_SCHEMAS,
    PROBE_RUNTIME_READINESS,
    EXIT_INPUT_ERROR,
    EXIT_READY,
    EXIT_USAGE,
    PreflightCheck,
    PreflightResult,
    Probe,
    parse_count,
    run_preflight,
    select_lane,
    validate_campaign_inputs,
)
from fin.eval.acceptance.protocol import (
    AcceptanceInputError,
    CaseManifest,
    RunManifest,
    validate_case_manifest,
)

# ``src/fin/eval/acceptance/__main__.py`` -> ``src`` -> repository root.
DEFAULT_ROOT = Path(__file__).resolve().parents[4]

_PROBE_NAMES = (
    PROBE_RUNTIME_READINESS,
    PROBE_BROWSER_SMOKE,
    PROBE_PLAYWRIGHT,
    PROBE_REQUEST_EVENT_SCHEMAS,
    PROBE_MODEL_AND_TOOLS,
)

_USAGE = """usage: python -m fin.eval.acceptance <command> [options]

commands:
  preflight --lane=<mechanism|browser|business> --manifest=<path>
            --run-manifest=<path> [--root=<path>] [--live-model]
            [--probe=<name>=<module>:<callable>]
  validate  --manifest=<path> [--cases=<count>]

probe names: runtime_readiness, request_event_schemas, browser_smoke,
             model_and_tools, playwright_test_dependency

exit codes:
  0   every required check passed
  1   invalid input (bad manifest, malformed record, unknown lane, bad count)
  2   preflight blocked: a required capability is missing
  64  usage error
"""


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        return _usage("no command given")
    command, rest = args[0], args[1:]
    if command in ("-h", "--help"):
        sys.stdout.write(_USAGE)
        return EXIT_READY
    try:
        if command == "preflight":
            return _run_preflight_command(rest, args)
        if command == "validate":
            return _run_validate_command(rest, args)
    except _UsageError as exc:
        return _usage(str(exc))
    except AcceptanceInputError as exc:
        return _fail(str(exc))
    return _usage(f"unknown command {command!r}")


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def _run_preflight_command(rest: list[str], argv: list[str]) -> int:
    options = _parse_options(
        rest,
        values=("lane", "manifest", "run-manifest", "root"),
        required=("lane", "manifest", "run-manifest"),
        flags=("live-model",),
        repeated=("probe",),
    )
    lane = select_lane(_one(options, "lane"))
    manifest = _load_case_manifest(_one(options, "manifest"))
    run = _load_run_manifest(_one(options, "run-manifest"))
    problems = validate_campaign_inputs(manifest, run, ())
    if problems:
        return _print_problems(problems)
    root = Path(_one(options, "root")) if "root" in options else DEFAULT_ROOT
    probes = _resolve_probes(options.get("probe", ()))

    result = run_preflight(
        lane.name,
        root=root,
        command=_recorded_command(argv),
        case_manifest=manifest,
        requires_live_model="live-model" in options,
        probes=probes,
    )
    sys.stdout.write(json.dumps(result.to_dict(), indent=2) + "\n")
    return result.exit_code


def _run_validate_command(rest: list[str], argv: list[str]) -> int:
    options = _parse_options(
        rest, values=("manifest", "cases"), required=("manifest",), flags=()
    )
    manifest = _load_case_manifest(_one(options, "manifest"))
    if "cases" in options:
        parse_count(_one(options, "cases"))
    problems = validate_case_manifest(manifest)
    if problems:
        return _print_problems(problems)
    check = PreflightCheck(
        check_id=CHECK_CASE_MANIFEST,
        ok=True,
        detail=f"{len(manifest.cases)} case(s) satisfy the frozen contract",
        required=True,
    )
    result = PreflightResult(
        lane=manifest.lane,
        checks=(check,),
        command=_recorded_command(argv),
        exit_code=EXIT_READY,
        missing_capability=None,
    )
    sys.stdout.write(json.dumps(result.to_dict(), indent=2) + "\n")
    return EXIT_READY


# ---------------------------------------------------------------------------
# Input handling
# ---------------------------------------------------------------------------


class _UsageError(Exception):
    """A malformed command line; maps to exit 64."""


def _parse_options(
    rest: Sequence[str],
    *,
    values: Sequence[str],
    required: Sequence[str],
    flags: Sequence[str],
    repeated: Sequence[str] = (),
) -> dict[str, list[str]]:
    parsed: dict[str, list[str]] = {}
    for token in rest:
        name, separator, value = token.partition("=")
        if not name.startswith("--"):
            raise _UsageError(f"unexpected argument {token!r}")
        key = name[2:]
        if key in flags:
            if separator:
                raise _UsageError(f"--{key} takes no value")
            parsed[key] = [""]
            continue
        if key not in values and key not in repeated:
            raise _UsageError(f"unknown option {name!r}")
        if not separator or not value:
            raise _UsageError(f"{name} requires a value")
        parsed.setdefault(key, []).append(value)
    for key in required:
        if key not in parsed:
            raise _UsageError(f"--{key} is required")
    return parsed


def _one(options: dict[str, list[str]], key: str) -> str:
    values = options.get(key, [])
    if not values:
        raise _UsageError(f"--{key} is required")
    if len(values) > 1:
        raise _UsageError(f"--{key} accepts one value")
    return values[0]


def _resolve_probes(specs: Sequence[str]) -> dict[str, Probe]:
    probes: dict[str, Probe] = {}
    for spec in specs:
        name, separator, target = spec.partition("=")
        if not separator or not target:
            raise _UsageError("--probe requires <name>=<module>:<callable>")
        if name not in _PROBE_NAMES:
            raise _UsageError(f"unknown probe {name!r}")
        if name in probes:
            raise _UsageError(f"probe {name!r} was given twice")
        probes[name] = _load_probe(target)
    return probes


def _load_probe(target: str) -> Probe:
    module_name, separator, attribute = target.partition(":")
    if not separator or not module_name or not attribute:
        raise _UsageError("--probe requires <name>=<module>:<callable>")
    try:
        module = importlib.import_module(module_name)
    except Exception:  # noqa: BLE001 - reported as input, never traced
        raise AcceptanceInputError(
            f"probe module {module_name!r} cannot be imported"
        ) from None
    probe = getattr(module, attribute, None)
    if not callable(probe):
        raise AcceptanceInputError(
            f"probe {module_name}:{attribute} is not a callable"
        )
    return _as_probe(probe)


def _as_probe(probe: Callable[[], object]) -> Probe:
    return lambda: _probe_outcome(probe())


def _probe_outcome(value: object) -> str | None:
    if value is None or isinstance(value, str):
        return value
    return f"probe returned {type(value).__name__}, not a reason"


def _load_case_manifest(path_text: str) -> CaseManifest:
    return CaseManifest.from_dict(_read_payload(path_text))


def _load_run_manifest(path_text: str) -> RunManifest:
    return RunManifest.from_dict(_read_payload(path_text))


def _read_payload(path_text: str) -> Any:
    path = Path(path_text)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AcceptanceInputError(
            f"cannot read {path.name!r}: {type(exc).__name__}"
        ) from None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise AcceptanceInputError(f"{path.name!r} is not valid JSON") from None


def _recorded_command(argv: Sequence[str]) -> str:
    return "python -m fin.eval.acceptance " + " ".join(
        shlex.quote(part) for part in argv
    )


def _print_problems(problems: Sequence[str]) -> int:
    sys.stderr.write("invalid acceptance input:\n")
    for problem in problems:
        sys.stderr.write(f"  {problem}\n")
    return EXIT_INPUT_ERROR


def _fail(problem: str) -> int:
    sys.stderr.write(f"invalid acceptance input: {problem}\n")
    return EXIT_INPUT_ERROR


def _usage(problem: str) -> int:
    sys.stderr.write(f"usage error: {problem}\n\n{_USAGE}")
    return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
