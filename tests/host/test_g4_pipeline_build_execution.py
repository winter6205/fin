"""Behavioral tests for the snapshot writer embedded in ``build.sh``.

The build plan requires committed build artifacts to remain usable when a
snapshot command fails. Execute the actual shell helper in isolation so these
checks cover its argument handling and filesystem behavior without invoking
Docker or the full build orchestration.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BUILD_SCRIPT = PROJECT_ROOT / "scripts" / "g4_pipeline" / "build.sh"
SNAPSHOT_FUNCTION = re.compile(r"(?ms)^snapshot\(\) \{\n.*?^\}")


def _run_snapshot(destination: Path, command: list[str]) -> subprocess.CompletedProcess[str]:
    """Run the function copied from build.sh, without executing build.sh itself."""
    build = BUILD_SCRIPT.read_text(encoding="utf-8")
    match = SNAPSHOT_FUNCTION.search(build)
    assert match is not None, "build.sh must define its snapshot helper"

    harness = (
        f"{match.group(0)}\n"
        'destination="$1"\n'
        "shift\n"
        'snapshot "${destination}" "$@"\n'
    )
    return subprocess.run(
        ["bash", "-c", harness, "snapshot-test", str(destination), *command],
        capture_output=True,
        text=True,
        check=False,
    )


def _temporary_files_for(destination: Path) -> list[Path]:
    return list(destination.parent.glob(f"{destination.name}.*"))


def test_successful_snapshot_command_replaces_destination(tmp_path: Path) -> None:
    destination = tmp_path / "image identity.txt"
    destination.write_text("old evidence\n", encoding="utf-8")

    result = _run_snapshot(
        destination,
        [sys.executable, "-c", "print('new evidence')"],
    )

    assert result.returncode == 0, result.stderr
    assert destination.read_text(encoding="utf-8") == "new evidence\n"
    assert _temporary_files_for(destination) == []


def test_failed_snapshot_command_preserves_destination_and_removes_tempfile(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "image identity.txt"
    destination.write_text("old evidence\n", encoding="utf-8")
    marker = tmp_path / "command-ran"
    failing_command = (
        "from pathlib import Path; import sys; "
        "Path(sys.argv[1]).write_text('ran', encoding='utf-8'); "
        "print('partial evidence'); sys.exit(23)"
    )

    result = _run_snapshot(
        destination,
        [sys.executable, "-c", failing_command, str(marker)],
    )

    assert result.returncode != 0
    assert destination.read_text(encoding="utf-8") == "old evidence\n"
    assert _temporary_files_for(destination) == []
    assert marker.exists(), "snapshot must invoke the requested failing command"
    assert marker.read_text(encoding="utf-8") == "ran"
