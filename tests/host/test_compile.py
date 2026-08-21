"""Tests for the durable v1 compile (T8, #17).

These tests cover three responsibilities:

1. **Compose + env wiring** — ``docker compose config`` exits 0
   (or, when docker is not installed, ``docker-compose.yml``
   parses to a valid YAML spec). The Postgres service's URL
   matches the ``FIN_CHECKPOINT_URL`` example in
   ``.env.example`` (scheme, host, port, db, user, password,
   sslmode).
2. **Typed env-var plumbing** — ``compile_for_runtime`` raises
   ``MissingCheckpointURLError`` when ``FIN_CHECKPOINT_URL`` is
   unset; reads ``FIN_MODEL`` (with the documented default) when
   no explicit value is supplied; wraps transient Postgres
   connection failures into a typed error rather than re-raising
   ``psycopg.OperationalError`` directly.

No live Postgres is started. No live LLM is invoked. Both are
mocked via ``monkeypatch`` so the default ``uv run pytest -q``
suite stays hermetic.

Reference: ADR 0015 (``PostgresSaver`` + ``FIN_CHECKPOINT_URL``,
``InMemorySaver`` only in tests), ADR 0013 (``FIN_MODEL`` default
``anthropic:claude-sonnet-4-5-20250929``), spec success-criteria
item 7 (``docker compose config`` exits 0 and the example URL
matches the service).
"""

from __future__ import annotations

import subprocess
import sys
import types
from pathlib import Path
from urllib.parse import urlparse

import psycopg
import pytest
import yaml

from fin.host.compile import (
    DEFAULT_MODEL,
    EXPECTED_CHECKPOINT_URL,
    CheckpointSetupError,
    MissingCheckpointURLError,
    compile_for_runtime,
)


# ----- paths -----


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


COMPOSE_PATH = _project_root() / "docker-compose.yml"
ENV_EXAMPLE_PATH = _project_root() / ".env.example"


# ----- helpers -----


def _load_compose_spec() -> dict:
    """Parse ``docker-compose.yml`` and return its mapping."""
    return yaml.safe_load(COMPOSE_PATH.read_text())


def _load_env_example() -> dict[str, str]:
    """Parse ``.env.example`` as a simple ``KEY=VALUE`` map.

    Comments and blank lines are ignored. Values are taken as the
    literal right-hand side (no shell expansion).
    """
    pairs: dict[str, str] = {}
    for raw in ENV_EXAMPLE_PATH.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        pairs[key.strip()] = value.strip()
    return pairs


def _service_url(env_map: dict[str, str]) -> str:
    """Compose the URL from the ``postgres`` service env + its
    host port mapping.

    Mirrors what docker-compose resolves for the CLI / HTTP
    client: ``${POSTGRES_USER}:${POSTGRES_PASSWORD}@localhost:<port>/${POSTGRES_DB}``.
    The sslmode token is the test's responsibility to assert
    (we hardcode the expected token in EXPECTED_CHECKPOINT_URL).
    """
    pg = _load_compose_spec()["services"]["postgres"]
    ports = pg.get("ports", [])
    published_port: str | None = None
    for entry in ports:
        if isinstance(entry, str):
            short = entry.split(":")
            # "5432:5432" → ["5432", "5432"];
            # "127.0.0.1:5432:5432" → 3 items (published is
            # always the second-to-last).
            published_port = short[-2]
            break
        if isinstance(entry, dict):
            published_port = str(entry.get("published"))
            break
    if published_port is None:
        raise AssertionError(
            "postgres service has no published port mapping"
        )
    return (
        f"postgresql://{env_map['POSTGRES_USER']}:"
        f"{env_map['POSTGRES_PASSWORD']}@localhost:"
        f"{published_port}/{env_map['POSTGRES_DB']}"
    )


# ----- compose + env wiring -----


def test_compose_yaml_parses_with_one_postgres_service() -> None:
    spec = _load_compose_spec()
    services = spec.get("services", {})
    assert "postgres" in services, (
        "docker-compose.yml must define a 'postgres' service per "
        "ADR 0015 + spec success-criteria item 7"
    )
    assert set(services.keys()) == {"postgres"}, (
        "docker-compose.yml must define ONLY the 'postgres' "
        f"service, got {sorted(services.keys())}"
    )


def test_compose_uses_postgres_16_alpine() -> None:
    pg = _load_compose_spec()["services"]["postgres"]
    assert pg.get("image") == "postgres:16-alpine"


def test_env_example_has_required_keys() -> None:
    env = _load_env_example()
    assert "FIN_CHECKPOINT_URL" in env, (
        ".env.example must define FIN_CHECKPOINT_URL (ADR 0015)"
    )
    assert "FIN_MODEL" in env, (
        ".env.example must define FIN_MODEL (ADR 0013)"
    )


def test_env_example_url_matches_expected() -> None:
    env = _load_env_example()
    assert env["FIN_CHECKPOINT_URL"] == EXPECTED_CHECKPOINT_URL


def test_env_example_url_is_well_formed_postgres() -> None:
    url = _load_env_example()["FIN_CHECKPOINT_URL"]
    parsed = urlparse(url)
    assert parsed.scheme == "postgresql"
    assert parsed.hostname == "localhost"
    assert parsed.port == 5432
    assert parsed.path == "/fin"
    assert parsed.username == "fin"
    assert parsed.password == "fin"
    assert parsed.query == "sslmode=disable"


def test_env_example_default_model_matches_adr_0013() -> None:
    env = _load_env_example()
    assert env["FIN_MODEL"] == DEFAULT_MODEL
    assert env["FIN_MODEL"] == "anthropic:claude-sonnet-4-5-20250929"


def test_compose_service_url_matches_env_example_url() -> None:
    """End-to-end URL-match acceptance gate (spec item 7).

    The URL the operator sees in ``.env.example`` must be the URL
    ``docker compose`` would resolve against the ``postgres``
    service. We do NOT assert a literal string equality because
    compose env vars do not encode the URL query string; we
    instead assert each part (scheme, host, port, db, user,
    password) on the ``.env.example`` URL plus the same parts on
    the compose service.
    """
    pg = _load_compose_spec()["services"]["postgres"]
    env_section = pg.get("environment", {})
    # docker-compose accepts both list-of-strings ("K=V") and
    # map ({K: V}) syntax. PyYAML returns a dict for the map
    # form, which we use here; the list branch is for the
    # short-form fallback so older compose files still parse.
    env_map: dict[str, str] = {}
    if isinstance(env_section, dict):
        env_map.update(env_section)
    else:
        for entry in env_section:
            if isinstance(entry, str):
                k, _, v = entry.partition("=")
                env_map[k.strip()] = v.strip()
            elif isinstance(entry, dict):
                env_map.update(entry)
    for required in ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"):
        assert required in env_map, (
            f"postgres service must set {required} in its environment"
        )
    assert env_map["POSTGRES_USER"] == "fin"
    assert env_map["POSTGRES_PASSWORD"] == "fin"
    assert env_map["POSTGRES_DB"] == "fin"

    # Port mapping must publish 5432 — that's what the
    # ``localhost:5432`` in ``.env.example`` connects to.
    ports = pg.get("ports", [])
    published_ports = [
        entry.split(":")[-2]
        if isinstance(entry, str)
        else str(entry.get("published"))
        for entry in ports
    ]
    assert "5432" in published_ports, (
        f"postgres service must publish port 5432, got "
        f"{published_ports}"
    )

    # The compose-derived URL (without sslmode — compose does
    # not encode query params) must match the .env.example URL
    # on every URL part that compose CAN derive.
    derived = (
        f"postgresql://{env_map['POSTGRES_USER']}:"
        f"{env_map['POSTGRES_PASSWORD']}@localhost:"
        f"5432/{env_map['POSTGRES_DB']}"
    )
    expected = EXPECTED_CHECKPOINT_URL.split("?", 1)[0]
    assert derived == expected, (
        f"compose-derived URL {derived!r} must equal the "
        f".env.example URL minus the sslmode query ({expected!r})"
    )


def test_compose_config_exits_zero_when_docker_available() -> None:
    """Run ``docker compose config`` if docker is installed.

    Asserts rather than skips when docker is available so a
    broken compose file is caught early. When docker is missing,
    the other YAML-based tests already cover the invariants.
    """
    try:
        result = subprocess.run(
            ["docker", "compose", "-f", str(COMPOSE_PATH), "config"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError:
        pytest.skip("docker not installed in this environment")
    except subprocess.TimeoutExpired:
        pytest.fail("docker compose config timed out after 30s")
    assert result.returncode == 0, (
        f"docker compose config exited {result.returncode}; "
        f"stderr={result.stderr!r}"
    )


# ----- compile_for_runtime env-var plumbing -----


def test_compile_for_runtime_raises_missing_checkpoint_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The function MUST fail fast with a typed error when the
    operator forgets to set ``FIN_CHECKPOINT_URL``. Generic
    ``KeyError`` is not acceptable (ADR 0015: typed stop/error
    kinds)."""
    monkeypatch.delenv("FIN_CHECKPOINT_URL", raising=False)
    monkeypatch.delenv("FIN_MODEL", raising=False)
    with pytest.raises(MissingCheckpointURLError):
        compile_for_runtime()


def _install_fake_langchain_chat_models(
    monkeypatch: pytest.MonkeyPatch, captured: dict[str, str]
) -> None:
    """Inject a fake ``langchain.chat_models`` module so the
    function's lazy import returns a stub ``init_chat_model``
    without requiring the real ``langchain`` meta-package (which
    is intentionally not in v1's pre-add deps)."""
    fake_mod = types.ModuleType("langchain.chat_models")

    def _init_chat_model(name, **_kwargs: object) -> tuple[str, str]:
        captured["name"] = name
        return ("fake-model", name)

    fake_mod.init_chat_model = _init_chat_model
    monkeypatch.setitem(sys.modules, "langchain.chat_models", fake_mod)


def _install_fake_postgres_saver(
    monkeypatch: pytest.MonkeyPatch, saver_factory: type
) -> None:
    """Patch ``langgraph.checkpoint.postgres`` so
    ``PostgresSaver.from_conn_string`` resolves to ``saver_factory``
    without importing the real PostgresSaver."""
    pg_mod = types.ModuleType("langgraph.checkpoint.postgres")
    pg_mod.PostgresSaver = saver_factory
    monkeypatch.setitem(sys.modules, "langgraph.checkpoint.postgres", pg_mod)


def test_compile_for_runtime_uses_default_model_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``FIN_MODEL`` falls back to the ADR 0013 default.

    The model-construction step is stubbed via a fake
    ``init_chat_model``. PostgresSaver is stubbed too, so we
    reach ``compile_graph`` and can confirm the function's
    end-to-end plumbing without a live database.
    """
    monkeypatch.delenv("FIN_MODEL", raising=False)
    monkeypatch.setenv("FIN_CHECKPOINT_URL", "postgresql://placeholder")

    captured: dict[str, str] = {}
    _install_fake_langchain_chat_models(monkeypatch, captured)
    _install_fake_postgres_saver(monkeypatch, _FakePostgresSaver)

    graph_calls: list[dict[str, object]] = []

    def _fake_compile_graph(**kw: object) -> str:
        graph_calls.append(kw)
        return "compiled-graph"

    monkeypatch.setattr(
        "fin.host.compile.compile_graph", _fake_compile_graph
    )

    result = compile_for_runtime()
    assert result == "compiled-graph"
    assert captured["name"] == DEFAULT_MODEL
    assert len(graph_calls) == 1, (
        "compile_for_runtime must call compile_graph exactly once"
    )
    call = graph_calls[0]
    assert call["model"] == ("fake-model", DEFAULT_MODEL)
    assert isinstance(call["checkpointer"], _FakePostgresSaver)


def test_compile_for_runtime_reads_fin_model_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``FIN_MODEL`` from the env is passed to
    ``init_chat_model`` verbatim (ADR 0013)."""
    monkeypatch.setenv("FIN_MODEL", "fake:custom-model-x")
    monkeypatch.setenv("FIN_CHECKPOINT_URL", "postgresql://placeholder")

    captured: dict[str, str] = {}
    _install_fake_langchain_chat_models(monkeypatch, captured)
    _install_fake_postgres_saver(monkeypatch, _FakePostgresSaver)

    monkeypatch.setattr(
        "fin.host.compile.compile_graph", lambda **kw: "compiled-graph"
    )

    compile_for_runtime()
    assert captured["name"] == "fake:custom-model-x"


def test_compile_for_runtime_wraps_setup_failure_as_typed_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Transient Postgres connection failures during ``setup()``
    must surface as ``CheckpointSetupError``, not as a raw
    ``psycopg.OperationalError`` (ADR 0015: typed stop/error
    kinds; spec: error message does not leak sensitive details).
    """
    monkeypatch.setenv("FIN_CHECKPOINT_URL", "postgresql://bad")

    captured: dict[str, str] = {}
    _install_fake_langchain_chat_models(monkeypatch, captured)
    _install_fake_postgres_saver(monkeypatch, _BrokenPostgresSaver)

    with pytest.raises(CheckpointSetupError) as excinfo:
        compile_for_runtime()

    # The original error is chained via __cause__ so operators
    # can still debug, but the visible type is ours.
    assert isinstance(excinfo.value.__cause__, psycopg.OperationalError)


# ----- fake PostgresSaver (no live connect) -----


class _FakePostgresSaver:
    """No-op saver for the happy-path tests."""

    def __init__(self) -> None:
        self.setup_called = False

    def setup(self) -> None:
        self.setup_called = True

    @classmethod
    def from_conn_string(
        cls, url: str, *, pipeline: bool = False
    ) -> "_FakePostgresCM":
        return _FakePostgresCM(url)


class _FakePostgresCM:
    """Context manager returned by ``_FakePostgresSaver.from_conn_string``."""

    def __init__(self, url: str) -> None:
        self._url = url
        self.saver = _FakePostgresSaver()

    def __enter__(self) -> _FakePostgresSaver:
        return self.saver

    def __exit__(self, *exc: object) -> None:
        return None


class _BrokenPostgresSaver:
    """Saver whose ``setup()`` raises a connection-shaped error."""

    def __init__(self, url: str) -> None:
        self._url = url

    def setup(self) -> None:
        import psycopg
        raise psycopg.OperationalError(
            f"could not connect to {self._url} (simulated for test)"
        )

    @classmethod
    def from_conn_string(
        cls, url: str, *, pipeline: bool = False
    ) -> "_BrokenPostgresCM":
        return _BrokenPostgresCM(url)


class _BrokenPostgresCM:
    def __init__(self, url: str) -> None:
        self._url = url

    def __enter__(self) -> _BrokenPostgresSaver:
        return _BrokenPostgresSaver(self._url)

    def __exit__(self, *exc: object) -> None:
        return None