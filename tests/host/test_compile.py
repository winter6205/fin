"""Tests for the durable v1 compile (T8).

These tests cover three responsibilities:

1. **Compose + env wiring** — ``docker compose config`` exits 0
   (or, when docker is not installed, ``docker-compose.yml``
   parses to a valid YAML spec). The Postgres service's URL
   matches the ``checkpoint.url`` example in
   ``settings.example.yaml`` (scheme, host, port, db, user, password,
   sslmode).
2. **Typed env-var plumbing** — ``compile_for_runtime`` raises
   ``MissingCheckpointURLError`` when ``FIN_CHECKPOINT_URL`` is
   unset; requires a configured non-empty ``FIN_MODEL`` when no
   explicit value is supplied; wraps transient Postgres
   connection failures into a typed error rather than re-raising
   ``psycopg.OperationalError`` directly.

No live Postgres is started. No live LLM is invoked. Both are
mocked via ``monkeypatch`` so the default ``uv run pytest -q``
suite stays hermetic.

Reference: ADR 0015 (``PostgresSaver`` + ``FIN_CHECKPOINT_URL``,
``InMemorySaver`` only in tests), ADR 0022 (``FIN_MODEL`` configured
by the operator on an OpenAI-compatible Chat Completions endpoint at
``FIN_BASE_URL`` default ``https://api.minimaxi.com/v1``), spec
success-criteria item 7 (``docker compose config`` exits 0 and
the example URL matches the service).
"""

from __future__ import annotations

import subprocess
import sys
import types
from pathlib import Path
from typing import cast
from urllib.parse import urlparse

import psycopg
import pytest
import yaml

from fin.host.compile import (
    DEFAULT_BASE_URL,
    EXPECTED_CHECKPOINT_URL,
    CheckpointSetupError,
    HostCompileError,
    MissingCheckpointURLError,
    MissingModelError,
    compile_for_runtime,
    compile_for_runtime_dev,
)


# ----- paths -----


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


COMPOSE_PATH = _project_root() / "docker-compose.yml"
PRODUCTION_COMPOSE_PATH = (
    _project_root() / "compose" / "docker-compose.production.yml"
)
SETTINGS_EXAMPLE_PATH = _project_root() / "settings.example.yaml"


# ----- helpers -----


def _load_compose_spec() -> dict:
    """Parse ``docker-compose.yml`` and return its mapping."""
    return yaml.safe_load(COMPOSE_PATH.read_text())


def _load_settings_example() -> dict[str, str]:
    """Project ``settings.example.yaml`` through the operator settings loader.

    Placeholders expand from the mapping passed here, the same way boot
    expands ``${NAME}`` from the process environment. The dummy keys are
    not the committed file: the template stores ``${FIN_API_KEY}`` and
    ``${FIN_JEV_API_KEY}``.
    """
    from fin.host.operator_settings import project_operator_settings

    text = SETTINGS_EXAMPLE_PATH.read_text(encoding="utf-8")
    assert "${FIN_API_KEY}" in text
    assert "${FIN_JEV_API_KEY}" in text
    return project_operator_settings(
        SETTINGS_EXAMPLE_PATH,
        environ={
            "FIN_API_KEY": "example-api-key",
            "FIN_JEV_API_KEY": "example-jev-key",
        },
    )


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
            # "5433:5433" → ["5433", "5433"];
            # "127.0.0.1:5433:5433" → 3 items (published is
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
    env = _load_settings_example()
    assert "FIN_CHECKPOINT_URL" in env, (
        "settings.example.yaml must define FIN_CHECKPOINT_URL (ADR 0015)"
    )
    assert "FIN_MODEL" in env, (
        "settings.example.yaml must define FIN_MODEL (ADR 0022)"
    )
    assert "FIN_BASE_URL" in env, (
        "settings.example.yaml must define FIN_BASE_URL (ADR 0022 — "
        "official MiniMax Chat Completions endpoint)"
    )
    assert "FIN_API_KEY" in env, (
        "settings.example.yaml must define FIN_API_KEY "
        "(Bearer token for official MiniMax Chat Completions — "
        "ADR 0022 update 2026-08-27 hard switch)"
    )
    assert "FIN_JEV_API_KEY" in env, (
        "settings.example.yaml must define FIN_JEV_API_KEY "
        "(TypeSafe Jev host judge; operator pastes the key)"
    )
    assert env["FIN_JEV_MODEL"] == "jev-latest"
    assert env["FIN_JEV_BASE_URL"] == "https://api.typesafe.ai"


def test_env_example_url_matches_expected() -> None:
    env = _load_settings_example()
    assert env["FIN_CHECKPOINT_URL"] == EXPECTED_CHECKPOINT_URL


def test_env_example_url_is_well_formed_postgres() -> None:
    url = _load_settings_example()["FIN_CHECKPOINT_URL"]
    parsed = urlparse(url)
    assert parsed.scheme == "postgresql"
    assert parsed.hostname == "localhost"
    assert parsed.port == 5433
    assert parsed.path == "/fin"
    assert parsed.username == "fin"
    assert parsed.password == "fin"
    assert parsed.query == "sslmode=disable"


def test_env_example_requires_an_explicit_nonempty_model() -> None:
    env = _load_settings_example()
    assert env["FIN_MODEL"].strip()


def test_env_example_default_base_url_matches_default_constant() -> None:
    env = _load_settings_example()
    assert env["FIN_BASE_URL"] == DEFAULT_BASE_URL
    # The default SHOULD include /v1 — the official OpenAI SDK
    # appends /chat/completions itself. Without /v1 the SDK
    # would hit /chat/completions (404).
    assert env["FIN_BASE_URL"] == "https://api.minimaxi.com/v1"
    assert env["FIN_BASE_URL"].endswith("/v1"), (
        "settings.example.yaml FIN_BASE_URL must end with /v1; the "
        "OpenAI SDK appends /chat/completions"
    )


def test_compose_service_url_matches_env_example_url() -> None:
    """End-to-end URL-match acceptance gate (spec item 7).

    The URL the operator sees in ``settings.example.yaml`` must be the URL
    ``docker compose`` would resolve against the ``postgres``
    service. We do NOT assert a literal string equality because
    compose env vars do not encode the URL query string; we
    instead assert each part (scheme, host, port, db, user,
    password) on the ``settings.example.yaml`` URL plus the same parts on
    the production compose service.
    """
    pg = yaml.safe_load(PRODUCTION_COMPOSE_PATH.read_text(encoding="utf-8"))[
        "services"
    ]["postgres"]
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

    # Host and container use the same port (5433:5433). 5432 is
    # left for iknow-memory-infra, which is also 5432:5432.
    ports = pg.get("ports", [])
    mappings: list[tuple[str, str]] = []
    for entry in ports:
        if isinstance(entry, str):
            short = entry.split(":")
            mappings.append((short[-2], short[-1]))
        elif isinstance(entry, dict):
            mappings.append(
                (str(entry.get("published")), str(entry.get("target")))
            )
    assert mappings == [("5433", "5433")], (
        f"postgres must publish 5433:5433 (host=container), got {mappings}"
    )
    command = pg.get("command")
    assert command == ["postgres", "-p", "5433"], (
        f"postgres must listen on 5433 inside the container, got {command}"
    )

    # The compose-derived URL (without sslmode — compose does
    # not encode query params) must match the settings.example.yaml URL
    # on every URL part that compose CAN derive.
    derived = (
        f"postgresql://{env_map['POSTGRES_USER']}:"
        f"{env_map['POSTGRES_PASSWORD']}@localhost:"
        f"5433/{env_map['POSTGRES_DB']}"
    )
    expected = EXPECTED_CHECKPOINT_URL.split("?", 1)[0]
    assert derived == expected, (
        f"compose-derived URL {derived!r} must equal the "
        f"settings.example.yaml URL minus the sslmode query ({expected!r})"
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


@pytest.mark.parametrize("configured_model", [None, " \t "])
def test_compile_for_runtime_rejects_missing_or_blank_model(
    monkeypatch: pytest.MonkeyPatch, configured_model: str | None
) -> None:
    env = {} if configured_model is None else {"FIN_MODEL": configured_model}
    captured: dict[str, object] = {}
    _install_fake_langchain_openai(monkeypatch, captured)
    _install_fake_postgres_saver(monkeypatch, _FakePostgresSaver)
    monkeypatch.setattr(
        "fin.host.compile.compile_graph", lambda **_kw: "compiled-graph"
    )

    with pytest.raises(HostCompileError) as excinfo:
        compile_for_runtime(
            checkpointer_url="postgresql://placeholder", env=env
        )

    assert isinstance(excinfo.value, MissingModelError)
    assert "FIN_MODEL" in str(excinfo.value)
    assert captured == {}, "missing configuration must not build a model"


def test_compile_for_runtime_rejects_blank_argument_before_environment_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    _install_fake_langchain_openai(monkeypatch, captured)
    _install_fake_postgres_saver(monkeypatch, _FakePostgresSaver)
    monkeypatch.setattr(
        "fin.host.compile.compile_graph", lambda **_kw: "compiled-graph"
    )

    with pytest.raises(HostCompileError) as excinfo:
        compile_for_runtime(
            checkpointer_url="postgresql://placeholder",
            model_name=" \t ",
            env={"FIN_MODEL": "environment-model"},
        )

    assert isinstance(excinfo.value, MissingModelError)
    assert captured == {}, "a blank explicit value must not fall back to env"


def test_compile_for_runtime_rejects_non_string_explicit_model() -> None:
    with pytest.raises(MissingModelError):
        compile_for_runtime(
            checkpointer_url="postgresql://placeholder",
            model_name=cast(str, 7),
            env={"FIN_MODEL": "environment-model"},
        )


def test_compile_for_runtime_prefers_explicit_model_argument(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    _install_fake_langchain_openai(monkeypatch, captured)
    _install_fake_postgres_saver(monkeypatch, _FakePostgresSaver)
    monkeypatch.setattr(
        "fin.host.compile.compile_graph", lambda **_kw: "compiled-graph"
    )

    compile_for_runtime(
        checkpointer_url="postgresql://placeholder",
        model_name="argument-model",
        env={"FIN_MODEL": "environment-model"},
    )

    assert captured["model"] == "argument-model"


def _install_fake_langchain_openai(
    monkeypatch: pytest.MonkeyPatch, captured: dict[str, object]
) -> None:
    """Inject a fake ``langchain_openai`` module so the function's
    lazy import resolves to a stub ``ChatOpenAI`` without requiring
    the real ``langchain-openai`` package (which the prod boot
    path needs but tests don't).

    The stub captures ``model``, ``base_url``, and ``api_key`` so
    each test can assert the contract end-to-end without making
    any network call.
    """
    fake_mod = types.ModuleType("langchain_openai")

    class _FakeChatOpenAI:
        def __init__(
            self,
            *,
            model: str,
            base_url: str | None = None,
            api_key: str | None = None,
            **_kwargs: object,
        ) -> None:
            captured["model"] = model
            captured["base_url"] = base_url
            captured["api_key"] = api_key
            captured["extra_body"] = _kwargs.get("extra_body")
            captured["model_kwargs"] = _kwargs.get("model_kwargs")
            captured["timeout"] = _kwargs.get("timeout")

        # ``compile_graph`` calls ``model.bind_tools([...])`` to wire
        # the LangChain tool binding. The fake returns ``self`` so the
        # downstream code sees an object with the same captured
        # attributes; tests asserting on the captured dict keep working.
        def bind_tools(self, _tools: object) -> "_FakeChatOpenAI":
            return self

        # T3: ``compile_graph`` layers the no-thinking
        # preference onto the tool binding via ``.bind(extra_body=...)``
        # — the real ChatOpenAI (Runnable) exposes ``bind``; the fake
        # mirrors it so the compile path runs end-to-end.
        def bind(self, **_kwargs: object) -> "_FakeChatOpenAI":
            return self

    fake_mod.ChatOpenAI = _FakeChatOpenAI
    monkeypatch.setitem(sys.modules, "langchain_openai", fake_mod)


def _install_fake_postgres_saver(
    monkeypatch: pytest.MonkeyPatch, saver_factory: type
) -> None:
    """Patch ``langgraph.checkpoint.postgres`` so
    ``PostgresSaver.from_conn_string`` resolves to ``saver_factory``
    without importing the real PostgresSaver."""
    pg_mod = types.ModuleType("langgraph.checkpoint.postgres")
    pg_mod.PostgresSaver = saver_factory
    monkeypatch.setitem(sys.modules, "langgraph.checkpoint.postgres", pg_mod)


def test_compile_for_runtime_keeps_endpoint_and_key_behavior_with_configured_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A configured model keeps endpoint and API-key fallback behavior intact.

    The model-construction step is stubbed via a fake
    ``ChatOpenAI``. PostgresSaver is stubbed too, so we reach
    ``compile_graph`` and can confirm the function's end-to-end
    plumbing without a live database.
    """
    monkeypatch.setenv("FIN_MODEL", "synthetic-test-model")
    monkeypatch.delenv("FIN_BASE_URL", raising=False)
    monkeypatch.delenv("FIN_API_KEY", raising=False)
    monkeypatch.setenv("FIN_CHECKPOINT_URL", "postgresql://placeholder")

    captured: dict[str, object] = {}
    _install_fake_langchain_openai(monkeypatch, captured)
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
    assert captured["model"] == "synthetic-test-model"
    assert captured["base_url"] == DEFAULT_BASE_URL
    # The OpenAI SDK appends /chat/completions; base_url must
    # include /v1 so the SDK hits /v1/chat/completions.
    assert captured["base_url"] == "https://api.minimaxi.com/v1"
    assert captured["api_key"] == "sk-no-key-required", (
        "api_key must fall back to the no-key placeholder when "
        "FIN_API_KEY is unset"
    )
    assert len(graph_calls) == 1, (
        "compile_for_runtime must call compile_graph exactly once"
    )
    call = graph_calls[0]
    assert isinstance(call["checkpointer"], _FakePostgresSaver)


def test_compile_for_runtime_reads_fin_model_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``FIN_MODEL`` from the env is passed to ``ChatOpenAI``
    verbatim (ADR 0022). The slash in the model id must NOT be
    parsed as a provider separator — we wire ``ChatOpenAI``
    directly with ``model=``."""
    monkeypatch.setenv("FIN_MODEL", "fake:custom-model-x")
    monkeypatch.setenv("FIN_CHECKPOINT_URL", "postgresql://placeholder")

    captured: dict[str, object] = {}
    _install_fake_langchain_openai(monkeypatch, captured)
    _install_fake_postgres_saver(monkeypatch, _FakePostgresSaver)

    monkeypatch.setattr(
        "fin.host.compile.compile_graph", lambda **kw: "compiled-graph"
    )

    compile_for_runtime()
    assert captured["model"] == "fake:custom-model-x"


def test_compile_passes_minimax_extra_body_as_chatopenai_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default compile is a plain OpenAI-compatible ChatOpenAI.

    MiniMax ``thinking.type=adaptive`` must NOT ride every request:
    other gateways (Volcengine Ark, etc.) reject it as InvalidParameter.
    """
    monkeypatch.setenv("FIN_CHECKPOINT_URL", "postgresql://placeholder")
    monkeypatch.setenv("FIN_MODEL", "synthetic-test-model")
    captured: dict[str, object] = {}
    _install_fake_langchain_openai(monkeypatch, captured)
    _install_fake_postgres_saver(monkeypatch, _FakePostgresSaver)
    monkeypatch.setattr(
        "fin.host.compile.compile_graph", lambda **kw: "compiled-graph"
    )

    compile_for_runtime()
    assert captured["extra_body"] is None
    assert captured["timeout"] is not None


def test_compile_omits_minimax_thinking_body_for_non_minimax_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Volcengine / any OpenAI-compatible gateway must not receive
    MiniMax ``thinking.type=adaptive``. The wire is ``FIN_MODEL`` +
    ``FIN_BASE_URL`` + ``FIN_API_KEY`` only."""
    monkeypatch.setenv(
        "FIN_BASE_URL", "https://arkcn-beijing.volces.com/api/coding/v1"
    )
    monkeypatch.setenv("FIN_MODEL", "synthetic-nondefault-model")
    monkeypatch.setenv("FIN_CHECKPOINT_URL", "postgresql://placeholder")
    captured: dict[str, object] = {}
    _install_fake_langchain_openai(monkeypatch, captured)
    _install_fake_postgres_saver(monkeypatch, _FakePostgresSaver)
    monkeypatch.setattr(
        "fin.host.compile.compile_graph", lambda **kw: "compiled-graph"
    )

    compile_for_runtime()
    assert captured["extra_body"] is None
    assert captured["model"] == "synthetic-nondefault-model"


def test_compile_for_runtime_reads_fin_base_url_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``FIN_BASE_URL`` from the env is forwarded to
    ``ChatOpenAI`` as ``base_url=``. Operators override the
    official Chat Completions URL via env (e.g. overseas
    ``https://api.minimax.io/v1``)."""
    monkeypatch.setenv("FIN_BASE_URL", "https://api.example.com/v1")
    monkeypatch.setenv("FIN_MODEL", "synthetic-test-model")
    monkeypatch.setenv("FIN_CHECKPOINT_URL", "postgresql://placeholder")

    captured: dict[str, object] = {}
    _install_fake_langchain_openai(monkeypatch, captured)
    _install_fake_postgres_saver(monkeypatch, _FakePostgresSaver)

    monkeypatch.setattr(
        "fin.host.compile.compile_graph", lambda **kw: "compiled-graph"
    )

    compile_for_runtime()
    assert captured["base_url"] == "https://api.example.com/v1"
    # base_url override does not change the configured model.
    assert captured["model"] == "synthetic-test-model"


def test_compile_for_runtime_passes_fin_api_key_as_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``FIN_API_KEY`` from the env is forwarded to
    ``ChatOpenAI`` as ``api_key=`` (official Chat Completions
    uses ``Authorization: Bearer``). The user's
    secret value must reach ChatOpenAI without ever being
    logged or echoed."""
    monkeypatch.setenv("FIN_CHECKPOINT_URL", "postgresql://placeholder")
    monkeypatch.setenv("FIN_MODEL", "synthetic-test-model")
    monkeypatch.setenv("FIN_API_KEY", "sk-test-fake-key")

    captured: dict[str, object] = {}
    _install_fake_langchain_openai(monkeypatch, captured)
    _install_fake_postgres_saver(monkeypatch, _FakePostgresSaver)

    monkeypatch.setattr(
        "fin.host.compile.compile_graph", lambda **kw: "compiled-graph"
    )

    compile_for_runtime()
    assert captured["api_key"] == "sk-test-fake-key"
    # The captured dict is in-memory only; the test process
    # never writes the value to disk or stdout.


def test_compile_for_runtime_wraps_setup_failure_as_typed_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Transient Postgres connection failures during ``setup()``
    must surface as ``CheckpointSetupError``, not as a raw
    ``psycopg.OperationalError`` (ADR 0015: typed stop/error
    kinds; spec: error message does not leak sensitive details).
    """
    monkeypatch.setenv("FIN_CHECKPOINT_URL", "postgresql://bad")
    monkeypatch.setenv("FIN_MODEL", "synthetic-test-model")

    captured: dict[str, object] = {}
    _install_fake_langchain_openai(monkeypatch, captured)
    _install_fake_postgres_saver(monkeypatch, _BrokenPostgresSaver)

    with pytest.raises(CheckpointSetupError) as excinfo:
        compile_for_runtime()

    # The original error is chained via __cause__ so operators
    # can still debug, but the visible type is ours.
    assert isinstance(excinfo.value.__cause__, psycopg.OperationalError)


def test_compile_runtime_handle_closes_postgres_after_graph_compile_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A graph factory failure releases the saver acquired earlier in boot."""
    from fin.host import compile as compile_module

    close_calls: list[tuple[object, ...]] = []

    class _TrackedSaver:
        def setup(self) -> None:
            return None

        @classmethod
        def from_conn_string(
            cls, _url: str, *, pipeline: bool = False
        ) -> "_TrackedCM":
            return _TrackedCM()

    class _TrackedCM:
        def __enter__(self) -> _TrackedSaver:
            return _TrackedSaver()

        def __exit__(self, *exc: object) -> None:
            close_calls.append(exc)

    _install_fake_postgres_saver(monkeypatch, _TrackedSaver)
    monkeypatch.setattr(compile_module, "_build_default_model", lambda **_kw: object())
    monkeypatch.setattr(compile_module, "build_jev_client", lambda **_kw: None)
    monkeypatch.setattr(
        compile_module, "_resolve_retrieve_adapter", lambda _env: object()
    )

    def _compile_graph(**_kwargs: object) -> object:
        raise RuntimeError("graph construction failed")

    monkeypatch.setattr(compile_module, "compile_graph", _compile_graph)

    with pytest.raises(RuntimeError, match="graph construction failed"):
        compile_module.compile_runtime_handle(
            checkpointer_url="postgresql://placeholder"
        )

    assert len(close_calls) == 1


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


# ----- compile_for_runtime_dev (in-memory local-dev path) -----


def test_compile_for_runtime_dev_uses_in_memory_saver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The helper exists so pytest and eval can compile without
    Postgres. It MUST use ``InMemorySaver`` (no Postgres) and
    emit a loud WARNING. Product ``fin-serve`` must not call it
    when the URL is unset — that is ``MissingCheckpointURLError``.
    The contract on ``compile_for_runtime`` stays INTACT.
    """
    captured: dict[str, object] = {}
    _install_fake_langchain_openai(monkeypatch, captured)
    monkeypatch.delenv("FIN_CHECKPOINT_URL", raising=False)
    monkeypatch.delenv("FIN_BASE_URL", raising=False)
    monkeypatch.delenv("FIN_MODEL", raising=False)
    monkeypatch.setenv("FIN_MODEL", "synthetic-test-model")

    import warnings

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        graph = compile_for_runtime_dev()

    # 1. Dev path emitted a warning that names the env var.
    messages = [str(w.message) for w in caught]
    assert any("FIN_CHECKPOINT_URL" in m for m in messages), messages
    # 2. The dev path returned a non-None compiled graph (proxy).
    assert graph is not None
    # 3. The explicit test model flowed through to ChatOpenAI.
    assert captured["model"] == "synthetic-test-model"
    assert captured["base_url"] == DEFAULT_BASE_URL


def test_compile_for_runtime_dev_does_not_require_postgres(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dev path must NOT touch Postgres — no
    ``langgraph.checkpoint.postgres`` import. We confirm by NOT
    installing a fake for it and by clearing ``FIN_CHECKPOINT_URL``;
    if the dev path reached for Postgres, this would raise
    ``MissingCheckpointURLError`` instead of returning a graph.
    """
    captured: dict[str, object] = {}
    _install_fake_langchain_openai(monkeypatch, captured)
    monkeypatch.delenv("FIN_CHECKPOINT_URL", raising=False)
    monkeypatch.delenv("FIN_BASE_URL", raising=False)
    monkeypatch.delenv("FIN_MODEL", raising=False)
    monkeypatch.setenv("FIN_MODEL", "synthetic-test-model")

    graph = compile_for_runtime_dev()
    assert graph is not None
    # Model wiring happened end-to-end (the prod path's checkpointer
    # plumbing was bypassed — if it had not been, the missing
    # URL would have raised).
    assert captured["model"] == "synthetic-test-model"


def test_cursor_start_does_not_invent_a_model() -> None:
    """Cloud Agent ``start`` may supply the endpoint but not model identity."""
    text = (_project_root() / "scripts" / "cursor-start.sh").read_text()
    assert (
        f'FIN_BASE_URL="${{FIN_BASE_URL:-{DEFAULT_BASE_URL}}}"' in text
    )
    assert not any(
        line.lstrip().startswith("FIN_MODEL=")
        or line.lstrip().startswith("export FIN_MODEL=")
        for line in text.splitlines()
    )
    # Must not bake a key. Comments may name the variable.
    assigned = [
        line
        for line in text.splitlines()
        if line.lstrip().startswith("FIN_API_KEY=")
        or line.lstrip().startswith("export FIN_API_KEY=")
    ]
    assert assigned == []


def test_default_mcp_command_fallback_shared_without_serve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``compile.DEFAULT_MCP_COMMAND`` and ``graph.compile_graph``'s MCP
    fallback must stay aligned — ``iknow-memory`` alone, no ``serve``."""
    from typing import Any

    from langgraph.checkpoint.memory import InMemorySaver

    from fin.corpus.extracted import Locator
    from fin.host import ScriptedChatModel, compile_graph
    from fin.host import graph as graph_module
    from fin.host.compile import DEFAULT_MCP_COMMAND
    from fin.retrieve import MCPRetrieveAdapter

    assert DEFAULT_MCP_COMMAND == ("iknow-memory",)
    assert "serve" not in DEFAULT_MCP_COMMAND

    built: list[dict[str, Any]] = []

    class _RecordingMCP(MCPRetrieveAdapter):
        def __init__(self, **kwargs: Any) -> None:
            built.append(kwargs)
            super().__init__(**kwargs)

    monkeypatch.setattr(graph_module, "MCPRetrieveAdapter", _RecordingMCP)

    compile_graph(
        checkpointer=InMemorySaver(),
        model=ScriptedChatModel(script=[]),
        retrieve_adapter="mcp",
        locator_map={"iknow-1": Locator("DOC-2023Q1", "v" * 64, 1)},
    )
    assert len(built) == 1
    assert built[0]["command"] == list(DEFAULT_MCP_COMMAND)
