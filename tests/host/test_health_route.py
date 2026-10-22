"""``GET /health`` — process health, distinct from ``/api/status``.

Spec `specs/enterprise-kb-agent.md` Focus 5 / SC9: ``GET /health``
reports Postgres reachability when ``FIN_CHECKPOINT_URL`` is set.
Both failure shapes are payload data, never a blank 500:

* URL unset -> typed degraded (``unconfigured``);
  product ``fin-serve`` refuses boot in this state.
* URL pointing at a socket nobody listens on -> typed degraded
  (``unreachable``).

ADR 0033 keeps ``GET /api/status`` boot-scoped (retrieve
reachability / adapter kind / model slug / corpus count). Health is
its own route: dependency reachability that changes while the
process runs does not belong on the boot surface, and the status
route must not grow a per-request database probe.

No live database is required. The reachable path is exercised
through an injected connector; the unreachable path dials a closed
localhost port.
"""

from __future__ import annotations

import socket
from typing import Any

import pytest
from fastapi.testclient import TestClient


# ----- helpers -----


def _closed_local_port() -> int:
    """Bind port 0, read the assigned port, close it.

    Nothing listens on the returned port, so a connection attempt
    fails immediately with ``ECONNREFUSED`` — no timeout wait, no
    outbound network.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def client() -> TestClient:
    from fin.host.http import app

    yield TestClient(app)
    app.dependency_overrides.clear()


# ----- probe unit contracts -----


def test_probe_reports_unconfigured_when_checkpoint_url_is_unset() -> None:
    from fin.host.health import (
        CHECKPOINT_UNCONFIGURED,
        REASON_CHECKPOINT_URL_UNSET,
        probe_checkpoint,
    )

    probe = probe_checkpoint(environ={})
    assert probe.state == CHECKPOINT_UNCONFIGURED
    assert probe.reason == REASON_CHECKPOINT_URL_UNSET


def test_probe_treats_blank_checkpoint_url_as_unconfigured() -> None:
    """A whitespace-only value is the same operator state as unset —
    the thread-store selector already reads it that way."""
    from fin.host.health import CHECKPOINT_UNCONFIGURED, probe_checkpoint

    probe = probe_checkpoint(environ={"FIN_CHECKPOINT_URL": "   "})
    assert probe.state == CHECKPOINT_UNCONFIGURED


def test_probe_reports_reachable_when_the_connector_answers() -> None:
    from fin.host.health import CHECKPOINT_REACHABLE, probe_checkpoint

    dialed: list[str] = []

    probe = probe_checkpoint(
        environ={"FIN_CHECKPOINT_URL": "postgresql://fin@db/fin"},
        connect=dialed.append,
    )
    assert probe.state == CHECKPOINT_REACHABLE
    assert probe.reason == ""
    assert dialed == ["postgresql://fin@db/fin"]


def test_probe_reports_unreachable_when_the_connector_raises_psycopg_error() -> None:
    import psycopg

    from fin.host.health import (
        CHECKPOINT_UNREACHABLE,
        REASON_CHECKPOINT_UNREACHABLE,
        probe_checkpoint,
    )

    def _boom(url: str) -> None:
        raise psycopg.OperationalError("connection refused")

    probe = probe_checkpoint(
        environ={"FIN_CHECKPOINT_URL": "postgresql://fin@db/fin"},
        connect=_boom,
    )
    assert probe.state == CHECKPOINT_UNREACHABLE
    assert probe.reason == REASON_CHECKPOINT_UNREACHABLE


def test_probe_reports_unreachable_on_an_unexpected_connector_failure() -> None:
    """A driver / DNS / TLS failure that is not a ``psycopg.Error``
    still degrades the route instead of escaping as a 500."""
    from fin.host.health import CHECKPOINT_UNREACHABLE, probe_checkpoint

    def _boom(url: str) -> None:
        raise RuntimeError("resolver exploded")

    probe = probe_checkpoint(
        environ={"FIN_CHECKPOINT_URL": "postgresql://fin@db/fin"},
        connect=_boom,
    )
    assert probe.state == CHECKPOINT_UNREACHABLE


def test_health_report_is_ok_only_when_the_checkpointer_answers() -> None:
    from fin.host.health import health_report

    ok = health_report(
        environ={"FIN_CHECKPOINT_URL": "postgresql://fin@db/fin"},
        connect=lambda url: None,
    )
    assert ok["status"] == "ok"
    assert ok["checkpoint"]["state"] == "reachable"
    assert ok["checkpoint"]["detail"]

    degraded = health_report(environ={})
    assert degraded["status"] == "degraded"


# ----- route contracts -----


def test_health_with_unset_url_is_typed_degraded_not_blank_500(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FIN_CHECKPOINT_URL", raising=False)

    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "degraded"
    assert body["checkpoint"]["state"] == "unconfigured"
    assert body["checkpoint"]["reason"] == "checkpoint_url_unset"
    # Typed degraded means the operator gets copy, not an empty body.
    assert body["checkpoint"]["detail"].strip()
    assert "memory" not in body["checkpoint"]["detail"].lower()


def test_health_with_url_to_a_down_socket_is_typed_degraded(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exception path: a set URL whose Postgres is not listening
    degrades with a typed body — never a blank 500."""
    port = _closed_local_port()
    monkeypatch.setenv(
        "FIN_CHECKPOINT_URL",
        f"postgresql://fin:fake-probe-password@127.0.0.1:{port}/fin",
    )

    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "degraded"
    assert body["checkpoint"]["state"] == "unreachable"
    assert body["checkpoint"]["reason"] == "checkpoint_unreachable"
    assert body["checkpoint"]["detail"].strip()


def test_health_body_never_echoes_the_connection_url(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The URL carries the operator's Postgres password. The route's
    copy is hand-written per reason, so no driver text — and no URL
    fragment — can ride the response."""
    port = _closed_local_port()
    monkeypatch.setenv(
        "FIN_CHECKPOINT_URL",
        f"postgresql://fin:fake-probe-password@127.0.0.1:{port}/fin",
    )

    response = client.get("/health")
    assert response.status_code == 200
    raw = response.text
    assert "fake-probe-password" not in raw
    assert "postgresql://" not in raw
    assert str(port) not in raw


def test_health_stays_out_of_the_boot_scoped_status_route(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR 0033: ``/api/status`` keeps its boot-scoped field set (four
    ADR 0033 fields + T4's bind field + the three lifecycle counts) and
    grows no database probe; ``/health`` keeps no boot config."""
    monkeypatch.delenv("FIN_CHECKPOINT_URL", raising=False)

    status_body: dict[str, Any] = client.get("/api/status").json()
    # The three lifecycle counts are aggregates with no identity, so they
    # are admitted here; every other key still has to reopen this set.
    assert set(status_body) == {
        "knowledge_mcp",
        "retrieve_adapter_kind",
        "model_identity",
        "corpus_filing_count",
        "bound_collection_name",
        "run_counts",
    }

    health_body: dict[str, Any] = client.get("/health").json()
    assert set(health_body) == {"status", "checkpoint"}
