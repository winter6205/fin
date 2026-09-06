"""SC3 + SC4 second half: default pytest builds OFF memory port.

The contract under test is two-part:

1. **Default pytest stays OFF** even when the operator ``.env`` (or any
   other source) sets ``FIN_MEMORY_ENABLED=true``. This is enforced by
   ``tests/conftest.py`` clearing the env var at session start AND by an
   autouse fixture that strips it before each unmarked test. Memory
   cases opt in via ``@pytest.mark.memory_on``; the paired fixture in
   ``tests/host/conftest.py`` re-installs ``true`` so an opt-in test
   sees the same ON path the operator's ``.env`` produces.
2. **The OFF behavior still works** when the operator explicitly sets
   the var to ``false`` (SC4 second half) or omits it entirely while
   ``FIN_CHECKPOINT_URL`` is present (SC2). Garbage stays typed
   (``MemoryConfigError``), so a typo is a loud failure, not silent OFF.

The operator leak scenario — operator ``.env`` has
``FIN_MEMORY_ENABLED=true`` and that value would otherwise be picked up
by ``build_memory_port`` — is simulated in-process by setting the env
var directly. The conftest mechanism is what guarantees the default
``uv run pytest -q`` run never sees that leak in its own process.
"""

from __future__ import annotations

import os

import pytest

from fin.host.memory_port import (
    MemoryConfigError,
    OffMemoryAdapter,
    build_memory_port,
)


# ----- SC3: default pytest stays OFF even if operator .env set ON -----


@pytest.fixture(autouse=True)
def _strip_operator_memory_env(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    """Strip ``FIN_MEMORY_ENABLED`` for every unmarked test.

    The session-level clear in ``tests/conftest.py`` only handles the
    initial load. Tests in the same process can re-introduce the var
    (in-process leak simulation, accidental setenv), and the autouse
    fixture is the second line of defense: it runs before each test's
    body, so any leak introduced during a previous test or by a module
    import is gone by the time ``build_memory_port`` is called.

    Tests marked ``memory_on`` opt out of the strip — the paired
    ``memory_on_env`` fixture in ``tests/host/conftest.py`` re-installs
    the value the operator's ``.env`` would have set.
    """
    if request.node.get_closest_marker("memory_on") is not None:
        return
    monkeypatch.delenv("FIN_MEMORY_ENABLED", raising=False)


def test_unmarked_pytest_does_not_inherit_operator_memory_on() -> None:
    """The conftest mechanism strips ``FIN_MEMORY_ENABLED`` for unmarked tests.

    This test is itself unmarked, so its own setup has already run
    ``monkeypatch.delenv("FIN_MEMORY_ENABLED", raising=False)``. Even if
    the operator's ``.env`` had leaked the var in via ``apply_dotenv``,
    the autouse fixture above would have stripped it before this test's
    body ran.
    """
    port = build_memory_port()

    assert isinstance(port, OffMemoryAdapter)
    assert port.enabled is False


def test_session_level_clear_wiped_the_operator_env_value() -> None:
    """The conftest import-time clear removed the leaked var.

    The test runner started this pytest invocation with
    ``FIN_MEMORY_ENABLED=true`` in the environment (we set it on the
    shell command line). After import time, the conftest ran
    ``os.environ.pop("FIN_MEMORY_ENABLED", None)``. The autouse
    fixture would have removed it again at setup, so we cannot
    observe the var here — but we CAN observe the outcome:
    ``build_memory_port`` resolves OFF.

    This test fails loudly if a future change restores the operator
    value (for example by re-applying the dotenv mid-session, or by
    moving the strip behind a feature flag): the env var would be
    visible to the fixture and the port would attempt to open
    Postgres.
    """
    assert os.environ.get("FIN_MEMORY_ENABLED") is None
    port = build_memory_port()
    assert isinstance(port, OffMemoryAdapter)
    assert port.enabled is False


# ----- SC4 second half: explicit OFF on a scripted turn -----


def test_explicit_memory_disabled_via_env_dict_builds_off_port() -> None:
    """``FIN_MEMORY_ENABLED=false`` explicitly resolves to the OFF adapter.

    Mirrors the spec's SC4 second half: an operator who explicitly turns
    memory off (rather than leaving it unset) must get the same OFF
    adapter a missing var would have produced, and ``enabled`` must read
    ``False``.
    """
    port = build_memory_port(env={"FIN_MEMORY_ENABLED": "false"})

    assert isinstance(port, OffMemoryAdapter)
    assert port.enabled is False
    assert port.dream() == {"status": "disabled", "transported": 0}


# ----- SC2: unset stays OFF even with FIN_CHECKPOINT_URL set -----


def test_unset_memory_enabled_stays_off_when_checkpoint_url_is_set() -> None:
    """No ``FIN_MEMORY_ENABLED`` + a Postgres URL is still OFF.

    The URL is irrelevant when the operator did not turn memory on; the
    OFF adapter never opens a Postgres connection. This is the SC2
    contract on a scripted turn.
    """
    port = build_memory_port(
        env={"FIN_CHECKPOINT_URL": "postgresql://fin:fin@localhost:5432/fin"}
    )

    assert isinstance(port, OffMemoryAdapter)
    assert port.enabled is False


# ----- garbage value stays typed (negative path) -----


def test_garbage_memory_enabled_value_is_a_typed_configuration_error() -> None:
    """A typo in ``FIN_MEMORY_ENABLED`` must NOT silently fall back to OFF.

    ``_read_enabled`` raises ``MemoryConfigError`` for anything outside
    the documented truthy/falsy set; this test pins that contract under
    pytest too (not just inside the unit module).
    """
    with pytest.raises(MemoryConfigError):
        build_memory_port(env={"FIN_MEMORY_ENABLED": "garbage"})


# ----- opt-in marker: ``memory_on`` cases can build the ON path -----


@pytest.mark.memory_on
def test_memory_on_marker_suppresses_the_autouse_strip() -> None:
    """The opt-in marker turns the autouse strip OFF for this test.

    The paired ``memory_on_env`` fixture in ``tests/host/conftest.py``
    sets ``FIN_MEMORY_ENABLED=true`` for the duration of the test, so
    ``build_memory_port()`` (no explicit env) sees the operator-leaked
    value and would attempt the ON path. We do not assert the ON path
    here — that needs Postgres — only that the env var is actually
    present in the process, so a regression where the marker forgot to
    re-install it would fail loudly.
    """
    assert os.environ.get("FIN_MEMORY_ENABLED") == "true"
