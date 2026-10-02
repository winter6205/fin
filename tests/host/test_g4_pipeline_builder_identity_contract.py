"""The builder is a declared input checked in pin.py, not a shell paragraph.

Task C1 asks for builder provenance checkable "without prose-only
pins". The engine version, the buildx builder, its BuildKit version and the
4 GiB ceiling were compared by ~9 fail-capable rules inlined in `build.sh`, so
the only way to test them was to extract shell text and run it against a stub
`docker` -- fragile, and it contradicted `build.sh`'s own header ("every rule
that can fail lives in pin.py").

The comparison now lives in `pin.py` (`validate_builder_identity` / the
`check-builder` subcommand). `build.sh` only feeds it the live host's answers:
the daemon version, whether buildx answers, and the raw `docker buildx inspect`
text. These tests drive `pin.py` directly with those shapes -- no docker, no
build, no network -- which is exactly the reachability the refactor was for.

The declaration is read once, by pin.py. `build.sh` no longer runs its own
`python3 -c 'import json'` accessors against `image-pin.json`.

This change did **not** re-baseline `expected_image_id`; that was a separate,
later operator decision. `expected_image_id` now carries the identity two
independent clean builds agreed on (`b1b5270c…`), and the tests below assert
that value explicitly so a quiet edit to any other digest fails. The gate
itself is unchanged: it still refuses any image that is not the declared one.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PIPELINE_DIR = PROJECT_ROOT / "scripts" / "g4_pipeline"
BUILD_SCRIPT = PIPELINE_DIR / "build.sh"
IMAGE_PIN = PIPELINE_DIR / "image-pin.json"
PIN_MODULE_PATH = PIPELINE_DIR / "pin.py"

RECORDED_ENGINE = "29.1.3"
# The baseline the operator adopted on 2026-10-02: two independent clean builds
# of commit 16adabe produced it identically (T4, docs/validation/
# 2026-10-02-c1-pin-policy.md). Named here so the declaration is checked against
# a recorded value rather than against whatever the file happens to hold.
ADOPTED_IMAGE_ID = "sha256:b1b5270c49cffbd2165aed904226612f2ccf925072c91766b4d4979f24bb9296"
# The two digests the declaration must NOT carry: L7's legacy-builder value
# (unreachable by any current builder) and the drifted pre-repair rebuild.
L7_HISTORICAL_IMAGE_ID = "sha256:8e10185fa27ea5adcfca94732adc27a2720ed9a31b67698d0a2c9e7a1a62c800"
DRIFTED_IMAGE_ID = "sha256:0f278c22daa45a8361235f37620fa31a237f0284501436256429e2a8eba35229"

_PIN_MODULE: ModuleType | None = None


def _pin() -> ModuleType:
    global _PIN_MODULE
    if _PIN_MODULE is None:
        spec = importlib.util.spec_from_file_location("g4_pipeline_pin_builder", PIN_MODULE_PATH)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        _PIN_MODULE = module
    return _PIN_MODULE


def _declared() -> dict:
    return _pin().declared_builder(PIPELINE_DIR)


def _build() -> str:
    return BUILD_SCRIPT.read_text(encoding="utf-8")


def _inspect_for(
    declared: dict,
    *,
    driver: str | None = None,
    status: str | None = "running",
    buildkit: str | None = None,
    memory: str | None = None,
    network: str | None = None,
) -> str:
    """A `docker buildx inspect` render matching the shipped declaration.

    Field-by-field so each failure leg breaks exactly one input. Defaults read
    the declared values rather than restating them, so the passing render tracks
    `image-pin.json` and cannot silently diverge from it.
    """
    driver = declared["buildx_driver"] if driver is None else driver
    buildkit = declared["buildkit_version"] if buildkit is None else buildkit
    memory = declared["memory_ceiling"] if memory is None else memory
    network = declared["network"] if network is None else network
    lines = [f"Driver: {driver}"]
    opts = []
    if memory is not None:
        opts.append(f'memory="{memory}"')
    if network is not None:
        opts.append(f'network="{network}"')
    lines.append("Driver Options: " + " ".join(opts))
    lines.append(f"Status: {status}")
    lines.append(f"BuildKit version: {buildkit}")
    return "\n".join(lines)


def _validate(*, live_engine, live_mode, inspect, declared=None) -> None:
    pin = _pin()
    pin.validate_builder_identity(
        declared=declared if declared is not None else _declared(),
        live_engine=live_engine,
        live_mode=live_mode,
        inspect=inspect,
    )


# --------------------------------------------------------------------------
# parse_buildx_inspect: reads the fields the real CLI prints.
# --------------------------------------------------------------------------


def test_parse_buildx_inspect_reads_the_declared_fields() -> None:
    pin = _pin()
    fields = pin.parse_buildx_inspect(_inspect_for(_declared()))
    assert fields["Driver"] == _declared()["buildx_driver"]
    assert fields["Status"] == "running"
    assert fields["BuildKit version"] == _declared()["buildkit_version"]
    assert 'memory="4g"' in fields["Driver Options"]


def test_parse_buildx_inspect_first_value_wins_over_the_nodes_block() -> None:
    """The `Nodes:` sub-block reuses key names (`Name`, `Endpoint`). The
    builder-level Driver/Status are printed once before it, so the first
    occurrence is the one to read."""
    pin = _pin()
    text = "Driver: docker-container\nNodes:\n  Name: g4repro0\n  Endpoint: unix:///run/x\nDriver: other\n"
    fields = pin.parse_buildx_inspect(text)
    assert fields["Driver"] == "docker-container"


# --------------------------------------------------------------------------
# Normal behavior: a host with the declared buildx builder passes.
# --------------------------------------------------------------------------


def test_a_matching_host_passes() -> None:
    _validate(live_engine=RECORDED_ENGINE, live_mode="buildx", inspect=_inspect_for(_declared()))


def test_the_declared_engine_is_the_one_the_record_names() -> None:
    """The declaration is fixed by the C1 record and lives in `image-pin.json`,
    not in a constant inside build.sh."""
    assert _declared()["engine_version"] == RECORDED_ENGINE
    assert RECORDED_ENGINE not in _build(), (
        "the engine version must not be restated in build.sh; pin.py reads it from the declaration"
    )


# --------------------------------------------------------------------------
# Failure behavior: every one of these must be a named non-zero.
# --------------------------------------------------------------------------


def test_a_different_engine_version_is_a_named_failure() -> None:
    pin = _pin()
    with pytest.raises(pin.PinError) as caught:
        _validate(live_engine="27.4.1", live_mode="buildx", inspect=_inspect_for(_declared()))
    message = str(caught.value)
    assert RECORDED_ENGINE in message and "27.4.1" in message, message


def test_an_unidentifiable_daemon_is_a_failure_not_a_pass() -> None:
    pin = _pin()
    with pytest.raises(pin.PinError) as caught:
        _validate(live_engine="", live_mode="buildx", inspect=_inspect_for(_declared()))
    assert "no version" in str(caught.value)


def test_a_host_without_buildx_is_a_named_failure() -> None:
    pin = _pin()
    with pytest.raises(pin.PinError) as caught:
        _validate(live_engine=RECORDED_ENGINE, live_mode="docker-build", inspect=_inspect_for(_declared()))
    assert "buildx" in str(caught.value).lower()


def test_a_missing_declared_builder_is_a_named_failure_with_the_fix() -> None:
    pin = _pin()
    declared = _declared()
    with pytest.raises(pin.PinError) as caught:
        _validate(live_engine=RECORDED_ENGINE, live_mode="buildx", inspect="", declared=declared)
    message = str(caught.value)
    assert declared["buildx_builder"] in message
    assert "buildx create" in message, message


def test_a_stopped_builder_is_a_named_failure() -> None:
    pin = _pin()
    with pytest.raises(pin.PinError) as caught:
        _validate(
            live_engine=RECORDED_ENGINE,
            live_mode="buildx",
            inspect=_inspect_for(_declared(), status="stopped"),
        )
    assert "stopped" in str(caught.value)


def test_a_driver_drift_is_a_named_failure() -> None:
    pin = _pin()
    with pytest.raises(pin.PinError) as caught:
        _validate(
            live_engine=RECORDED_ENGINE,
            live_mode="buildx",
            inspect=_inspect_for(_declared(), driver="kubernetes"),
        )
    assert "kubernetes" in str(caught.value)


def test_a_buildkit_version_drift_is_a_named_failure() -> None:
    pin = _pin()
    with pytest.raises(pin.PinError) as caught:
        _validate(
            live_engine=RECORDED_ENGINE,
            live_mode="buildx",
            inspect=_inspect_for(_declared(), buildkit="v0.13.0"),
        )
    assert "v0.13.0" in str(caught.value)


def test_a_memory_ceiling_drift_is_a_named_failure() -> None:
    pin = _pin()
    with pytest.raises(pin.PinError) as caught:
        _validate(
            live_engine=RECORDED_ENGINE,
            live_mode="buildx",
            inspect=_inspect_for(_declared(), memory="8g"),
        )
    assert "8g" in str(caught.value)


def test_a_network_drift_is_a_named_failure() -> None:
    pin = _pin()
    with pytest.raises(pin.PinError) as caught:
        _validate(
            live_engine=RECORDED_ENGINE,
            live_mode="buildx",
            inspect=_inspect_for(_declared(), network="bridge"),
        )
    assert _declared()["network"] in str(caught.value)


def test_an_incomplete_declaration_is_rejected() -> None:
    """A builder block missing a compared key would let the comparison read
    `None` and pass on an absent expectation -- the tautology this whole module
    refuses."""
    pin = _pin()
    with pytest.raises(pin.PinError) as caught:
        _validate(
            live_engine=RECORDED_ENGINE,
            live_mode="buildx",
            inspect=_inspect_for(_declared()),
            declared={k: v for k, v in _declared().items() if k != "memory_ceiling"},
        )
    assert "memory_ceiling" in str(caught.value)


# --------------------------------------------------------------------------
# The real CLI, so build.sh's invocation shape is exercised, not just the lib.
# --------------------------------------------------------------------------


def test_check_builder_cli_passes_a_matching_host(tmp_path: Path) -> None:
    inspect_file = tmp_path / "inspect.txt"
    inspect_file.write_text(_inspect_for(_declared()), encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable, str(PIN_MODULE_PATH), "check-builder",
            "--pipeline-dir", str(PIPELINE_DIR),
            "--live-engine", RECORDED_ENGINE,
            "--live-mode", "buildx",
            "--inspect-file", str(inspect_file),
        ],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, f"{result.stdout!r} {result.stderr!r}"


def test_check_builder_cli_refuses_a_drifted_host_with_65(tmp_path: Path) -> None:
    inspect_file = tmp_path / "inspect.txt"
    inspect_file.write_text(_inspect_for(_declared(), buildkit="v0.9.9"), encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable, str(PIN_MODULE_PATH), "check-builder",
            "--pipeline-dir", str(PIPELINE_DIR),
            "--live-engine", RECORDED_ENGINE,
            "--live-mode", "buildx",
            "--inspect-file", str(inspect_file),
        ],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 65, (
        "an unusable builder must exit 65 (input invalid, not attempted), leaving "
        f"75 to the identity check: {result.stderr!r}"
    )
    assert "FATAL" in result.stderr


# --------------------------------------------------------------------------
# F2: one JSON reader.
# --------------------------------------------------------------------------


def test_build_shell_no_longer_parses_the_declaration_itself() -> None:
    """`build.sh` used to run `python3 -c 'import json, ...'` and its own
    `declared_builder_field` accessors against `image-pin.json`, giving the same
    file two readers. pin.py is now the single reader; the script only feeds live
    host answers to `check-builder` and asks pin.py for the values it runs with."""
    build = _build()
    assert "declared_builder_field" not in build
    for shell_json_tell in ("import json", "json.load", "json.loads", "open(sys.argv"):
        assert shell_json_tell not in build, (
            f"build.sh still parses the declaration itself ({shell_json_tell!r})"
        )
    # The values build.sh runs with are read through pin.py, and the identity
    # check hands the file to pin.py rather than reading it.
    assert 'pin.py" get' in build, "build.sh must read declared values via pin.py get"
    assert "check-builder" in build
    assert '--pin "${HERE}/image-pin.json"' in build, (
        "the only image-pin.json reference should be the one passed to pin.py"
    )


# --------------------------------------------------------------------------
# build.sh orchestration invariants this change must preserve.
# --------------------------------------------------------------------------


def test_the_builder_check_runs_before_the_build() -> None:
    build = _build()
    assert build.index("check-builder") < build.index("docker buildx build"), (
        "the builder gate must run before a 4 GiB build, not after"
    )


def test_the_builder_check_runs_before_the_identity_check() -> None:
    build = _build()
    assert build.index("check-builder") < build.index("compare-image-id")


def test_the_identity_check_still_runs_and_still_exits_75() -> None:
    build = _build()
    check = build[build.index('if ! python3 "${HERE}/pin.py" compare-image-id'):]
    check = check[: check.index("\nfi\n")]
    assert "exit 75" in check, "the image-identity drift check must keep its exit 75"
    assert "compare-image-id" in check


def test_the_builder_check_uses_65_and_not_75() -> None:
    build = _build()
    start = build.index('if ! python3 "${HERE}/pin.py" check-builder')
    block = build[start : build.index("\nfi\n", start) + len("\nfi\n")]
    assert "exit 65" in block, f"an unusable builder must exit 65: {block!r}"
    assert "exit 75" not in block, "the builder gate must not use the drift code"


def test_the_build_exports_reproducibly_from_the_declaration() -> None:
    build = _build()
    assert "rewrite-timestamp=true" in build
    assert "--provenance=false" in build
    assert 'SOURCE_DATE_EPOCH="${EPOCH}"' in build
    assert "builder.source_date_epoch" in build, (
        "the epoch must be read through pin.py from the declaration"
    )


def test_clean_build_is_the_default() -> None:
    build = _build()
    assert "--no-cache" in build
    assert "G4_BUILD_USE_CACHE" in build


def test_the_expected_image_id_is_the_adopted_baseline() -> None:
    """The declaration must carry the one identity an operator decision adopted
    on 2026-10-02 — and not any other. The original form of this test asserted
    the file was *untouched*, which a substring check satisfies for any value
    that happens to be mentioned anywhere in the file; a quiet re-baseline onto
    a drifted digest (or back onto the unreachable legacy one) would have left
    it green. It now reads the field and names the digests it must reject.
    """
    import json

    declared = json.loads(IMAGE_PIN.read_text(encoding="utf-8"))["expected_image_id"]
    assert declared == ADOPTED_IMAGE_ID, (
        "expected_image_id must be the adopted two-build baseline; a different "
        f"value is an unrecorded re-baseline: {declared!r}"
    )
    for rejected, why in (
        (L7_HISTORICAL_IMAGE_ID, "legacy builder, unreachable by any current builder"),
        (DRIFTED_IMAGE_ID, "the drifted pre-repair rebuild, the artifact that WAS the open defect"),
    ):
        assert declared != rejected, f"expected_image_id must not be {why}"


def test_the_gate_still_refuses_an_image_that_is_not_the_declared_one() -> None:
    """Adopting a baseline must not disarm the gate. The refusal leg this module
    used to leave implied is executed here against the *shipped* pin file: the
    real CLI, the committed declaration, and an observed identity that is not it.
    """
    import json
    import subprocess
    import tempfile

    declared = json.loads(IMAGE_PIN.read_text(encoding="utf-8"))["expected_image_id"]
    with tempfile.TemporaryDirectory() as staging:
        observed = Path(staging) / "image_identity.txt"
        observed.write_text(f"image_id={DRIFTED_IMAGE_ID}\n", encoding="utf-8")
        result = subprocess.run(
            [
                sys.executable, str(PIN_MODULE_PATH), "compare-image-id",
                "--pin", str(IMAGE_PIN),
                "--observed", str(observed),
            ],
            capture_output=True, text=True, check=False,
        )
    assert result.returncode != 0, (
        f"a non-matching image must still be refused, not accepted against {declared!r}"
    )
    assert "FATAL" in result.stderr, result.stderr
    assert "do NOT re-baseline" in result.stderr, (
        f"the refusal must name the decision it refuses to make on its own: {result.stderr!r}"
    )


def test_the_builder_check_does_not_claim_the_build_reproduces() -> None:
    claims = [
        line.strip()
        for line in _build().splitlines()
        if "check-builder" in line and "byte-identical" in line.lower()
    ]
    assert not claims, f"the builder gate overclaims what it proves: {claims}"


def test_build_shell_is_syntactically_valid() -> None:
    result = subprocess.run(
        ["bash", "-n", str(BUILD_SCRIPT)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


# --------------------------------------------------------------------------
# F7: the CPU-ceiling drop is recorded as data, not prose.
# --------------------------------------------------------------------------


def test_the_cpuset_drop_is_declared_as_data() -> None:
    """The legacy `docker build --cpuset-cpus=0-3` bound does not carry onto the
    buildx path. Declaring `cpuset_cpus` in the builder block makes the omission
    a structured fact `pin.py` reads, so removing it is a deliberate edit rather
    than a silent gap discovered later."""
    declared = _declared()
    assert "cpuset_cpus" in declared, "the builder block must declare how the CPU ceiling is handled"
    assert declared["cpuset_cpus"] is None, (
        "the buildx path enforces memory only; cpuset_cpus must record the drop as null"
    )


def test_removing_the_cpuset_declaration_fails_the_reader(tmp_path: Path) -> None:
    pin = _pin()
    import json

    data = json.loads(IMAGE_PIN.read_text(encoding="utf-8"))
    del data["builder"]["cpuset_cpus"]
    staged = tmp_path / "image-pin.json"
    staged.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(pin.PinError) as caught:
        pin._require_builder_keys(data["builder"])
    assert "cpuset_cpus" in str(caught.value)
