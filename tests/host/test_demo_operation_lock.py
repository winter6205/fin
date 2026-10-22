"""Contract for the demo operation lock in ``scripts/demo.sh``.

``fin demo`` serializes its start and stop operations with an ``flock`` held
on file descriptor 9. The long-lived service must not inherit that
descriptor: a server holding the lock makes every later ``fin demo --stop``
report "another demo start or stop is in progress", so the demo could never
be stopped and the browser acceptance could never finish its cleanup.
"""

from __future__ import annotations

from pathlib import Path


_DEMO_SH = Path(__file__).resolve().parents[2] / "scripts" / "demo.sh"


def _demo_sh_source() -> str:
    return _DEMO_SH.read_text(encoding="utf-8")


def test_the_service_is_started_without_the_demo_operation_lock() -> None:
    lines = _demo_sh_source().splitlines()
    start = next(
        index for index, line in enumerate(lines) if 'nohup bash "$REPO_ROOT/scripts/dev-up.sh"' in line
    )
    end = start
    while not lines[end].rstrip().endswith("&"):
        end += 1
    command = "\n".join(lines[start : end + 1])
    assert "9>&-" in command, "the service child must not inherit the demo operation lock"
    script_end = command.index("dev-up.sh\"") + len("dev-up.sh\"")
    assert "9>&-" in command[script_end:], "the lock must be closed on the service command itself"


def test_the_lock_is_still_taken_for_the_start_and_stop_operations() -> None:
    source = _demo_sh_source()
    assert 'exec 9>"$LOCK_PATH"' in source
    assert "if ! flock -n 9; then" in source


def test_the_stop_action_is_reachable_after_the_lock_is_taken() -> None:
    source = _demo_sh_source()
    lock = source.index("if ! flock -n 9; then")
    stop = source.index('if [[ "$ACTION" == "stop" ]]; then')
    assert lock < stop, "the stop action must run under the same lock"
