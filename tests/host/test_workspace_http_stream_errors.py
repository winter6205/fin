"""Mid-flight stream error classification + sanitization (fidelity T11).

The ``_stream_run_frames`` engine used to swallow every mid-stream
raise into one opaque English sentence — fine for an operator who
happened to read the server log, useless for a SPA that wants to
show "your token is bad" vs "the network dropped". These tests pin
the new wiring:

* ``classify_stream_exception`` maps the ``openai`` SDK error
  hierarchy (and its ``langchain_openai`` re-exports) onto a closed
  seven-kind enum.
* ``_sanitize_reason_text`` redacts Authorization headers, ``key=``
  query strings, and long high-entropy tokens BEFORE the reason
  rides the wire.
* The terminal refuse SSE frame now carries ``status_classified``
  (the closed enum) and the persisted RunRecord mirrors the same
  field so a reload keeps the label visible.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from fin.host import ScriptedChatModel, compile_graph
from fin.host.workspace_http import (
    _STREAM_FAILURE_USER_COPY,
    _sanitize_reason_text,
    classify_stream_exception,
)


AAPL_2022Q3 = "AAPL-2022Q3"
KNOWN_IDS = {AAPL_2022Q3, "AAPL-2023Q1", "MSFT-2023Q1", "AMZN-2023Q1"}


# ----- classification -----


def _make_response() -> httpx.Response:
    req = httpx.Request("POST", "http://test.local/v1/chat/completions")
    return httpx.Response(500, request=req)


def _make_openai_error(cls: type, msg: str) -> Any:
    """Construct an ``openai.*`` exception with the right kwarg shape.

    The SDK is fussy: ``APIStatusError`` subclasses take
    ``(message, *, response=..., body=...)``, ``APIConnectionError``
    takes ``(*, message=..., request=...)``, ``APITimeoutError``
    takes a positional ``request``. Branch on the constructor to
    dodge all three shapes from one call site.
    """
    req = httpx.Request("POST", "http://test.local/v1/chat/completions")
    # APITimeoutError wants a positional ``request``.
    try:
        return cls(req)
    except TypeError:
        pass
    # APIStatusError subclasses want (message, *, response, body).
    try:
        return cls(msg, response=_make_response(), body=None)
    except TypeError:
        pass
    # APIConnectionError wants (*, message, request).
    return cls(message=msg, request=req)


def test_classify_authentication_error_maps_to_auth() -> None:
    """openai.AuthenticationError -> "auth" + Chinese copy mentioning
    the key in ``~/.fin/settings.yaml`` so the operator knows what to fix."""
    import openai

    err = _make_openai_error(openai.AuthenticationError, "bad token")
    kind, copy = classify_stream_exception(err)
    assert kind == "auth"
    assert "settings.yaml" in copy


def test_classify_permission_denied_maps_to_auth() -> None:
    """403 / PermissionDeniedError shares the "check the token" copy."""
    import openai

    err = _make_openai_error(openai.PermissionDeniedError, "no perm")
    kind, copy = classify_stream_exception(err)
    assert kind == "auth"
    assert copy == _STREAM_FAILURE_USER_COPY["auth"]


def test_classify_api_timeout_maps_to_timeout_not_network() -> None:
    """``APITimeoutError`` subclasses ``APIConnectionError`` — a naive
    isinstance check on the parent would swallow every timeout into
    the "network" bucket. The order in ``_classify_exception_type``
    must keep the more specific class first."""
    import openai

    err = _make_openai_error(openai.APITimeoutError, "took too long")
    kind, copy = classify_stream_exception(err)
    assert kind == "timeout"
    assert copy == _STREAM_FAILURE_USER_COPY["timeout"]


def test_classify_api_connection_error_maps_to_network() -> None:
    """Plain ``APIConnectionError`` (no timeout) -> "network"."""
    import openai

    req = httpx.Request("POST", "http://test.local/v1/chat/completions")
    err = openai.APIConnectionError(message="connection refused", request=req)
    kind, copy = classify_stream_exception(err)
    assert kind == "network"
    assert copy == _STREAM_FAILURE_USER_COPY["network"]


def test_classify_rate_limit_maps_to_rate_limit() -> None:
    """429 -> "rate_limit" copy."""
    import openai

    err = _make_openai_error(openai.RateLimitError, "rate")
    kind, copy = classify_stream_exception(err)
    assert kind == "rate_limit"
    assert copy == _STREAM_FAILURE_USER_COPY["rate_limit"]


def test_classify_not_found_maps_to_not_found() -> None:
    """Wrong model slug / bad path -> "not_found" copy mentions
    ``FIN_MODEL`` and ``FIN_BASE_URL``."""
    import openai

    err = _make_openai_error(openai.NotFoundError, "no such model")
    kind, copy = classify_stream_exception(err)
    assert kind == "not_found"
    assert "FIN_MODEL" in copy and "FIN_BASE_URL" in copy


def test_classify_internal_server_error_maps_to_server() -> None:
    """5xx -> "server" copy."""
    import openai

    err = _make_openai_error(openai.InternalServerError, "boom")
    kind, copy = classify_stream_exception(err)
    assert kind == "server"
    assert copy == _STREAM_FAILURE_USER_COPY["server"]


def test_classify_unknown_exception_falls_back_to_unknown() -> None:
    """Anything outside the openai hierarchy -> "unknown" + safe copy."""
    kind, copy = classify_stream_exception(RuntimeError("boom"))
    assert kind == "unknown"
    assert copy == _STREAM_FAILURE_USER_COPY["unknown"]
    # The copy never claims a specific cause it can't substantiate.
    assert "请" in copy


def test_classify_never_returns_empty_copy() -> None:
    """``user_copy`` must never be blank — a refused turn without
    copy is the swallowed-blank failure mode the spec forbids."""
    for exc in [
        RuntimeError("x"),
        ValueError("x"),
        KeyError("x"),
    ]:
        _, copy = classify_stream_exception(exc)
        assert copy.strip(), f"empty copy for {exc!r}"


def test_classify_langchain_re_export_falls_back_to_name_dispatch() -> None:
    """If the openai hierarchy is replaced by a wrapper module,
    ``_classify_exception_type`` must still produce the right kind
    via class-name substring matching. Synthetic classes use the
    same names ``langchain_openai`` historically re-exports under
    (``OpenAIAuthenticationError`` / ``OpenAITimeoutError`` /
    ``OpenAIRateLimitError``) so the substring-based fallback has
    a realistic dispatch surface."""

    class OpenAIAuthenticationError(Exception):
        pass

    class OpenAITimeoutError(Exception):
        pass

    class OpenAIRateLimitError(Exception):
        pass

    assert classify_stream_exception(OpenAIAuthenticationError("x"))[0] == "auth"
    assert classify_stream_exception(OpenAITimeoutError("x"))[0] == "timeout"
    assert classify_stream_exception(OpenAIRateLimitError("x"))[0] == "rate_limit"


# ----- sanitization -----


def test_sanitize_strips_bearer_authorization_header() -> None:
    """A provider error that included ``Authorization: Bearer …``
    must NOT reach the wire — redaction collapses it to
    ``[redacted]``."""
    raw = "Boom at /v1: Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig"
    out = _sanitize_reason_text(raw)
    assert "Bearer" not in out
    assert "eyJ" not in out
    assert "[redacted]" in out


def test_sanitize_strips_key_equals_query_string() -> None:
    """``key=...`` / ``token=...`` style query-string fragments are
    collapsed regardless of value shape (sk- prefix or raw)."""
    raw = "Failed with key=sk-prod-abcdefghijklmnop and token=ghp_secret123"
    out = _sanitize_reason_text(raw)
    assert "sk-prod-abcdefghijklmnop" not in out
    assert "ghp_secret123" not in out
    assert "[redacted]" in out


def test_sanitize_strips_long_hex_blob() -> None:
    """A 32+ char hex string (opaque token) is redacted."""
    raw = "Boom at endpoint abc1234567890abcdef1234567890abcdef extra"
    out = _sanitize_reason_text(raw)
    assert "abc1234567890abcdef1234567890abcdef" not in out
    assert "[redacted]" in out


def test_sanitize_strips_url_with_embedded_token() -> None:
    """A URL like ``https://api.example.com/v1?key=xxx`` keeps the
    path visible but redacts the query string token."""
    raw = "GET https://proxy.local/v1/chat?key=sk-test-1234567890abcdef failed"
    out = _sanitize_reason_text(raw)
    assert "https://proxy.local/v1/chat" in out
    assert "sk-test-1234567890abcdef" not in out


def test_sanitize_caps_length() -> None:
    """The wire reason has a hard cap so a giant provider traceback
    cannot bloat the SSE frame. Words separated by spaces stay
    short of the high-entropy-token regex, so the cap (not the
    redactor) is the only thing that shrinks the output."""
    raw = ("failure during request " * 200).strip()
    out = _sanitize_reason_text(raw, max_len=200)
    assert len(out) <= 200
    assert out.endswith("...")


def test_sanitize_collapses_whitespace() -> None:
    """Multi-line provider error becomes one SSE-friendly line."""
    raw = "line 1\nline 2\nline 3"
    out = _sanitize_reason_text(raw)
    assert "\n" not in out
    assert "line 1 line 2 line 3" in out


def test_sanitize_handles_empty_input() -> None:
    """Empty / falsy input returns the empty string (no AttributeError)."""
    assert _sanitize_reason_text("") == ""
    assert _sanitize_reason_text("   ") == ""


def test_sanitize_preserves_ordinary_text() -> None:
    """Non-sensitive text round-trips unchanged (modulo whitespace)."""
    raw = "stream raised mid-flight: RuntimeError: boom"
    out = _sanitize_reason_text(raw)
    assert "stream raised mid-flight" in out
    assert "RuntimeError" in out


# ----- end-to-end: graph raising -> SSE frame + persisted row -----


@pytest.fixture
def http_app() -> FastAPI:
    """Fresh FastAPI app per test so dependency overrides do not leak."""
    from fin.host.http import app

    yield app
    app.dependency_overrides.clear()


@pytest.fixture
def client(http_app: FastAPI) -> TestClient:
    return TestClient(http_app)


@pytest.fixture
def fresh_store(http_app: FastAPI):
    from fin.host.http import get_graph, get_thread_store
    from fin.host.thread_store import InMemoryThreadStore, ThreadStore

    store: ThreadStore = InMemoryThreadStore()
    http_app.dependency_overrides[get_thread_store] = lambda: store
    # Default graph — tests that want a specific model install on top.
    default_graph = compile_graph(
        checkpointer=InMemorySaver(),
        model=ScriptedChatModel(script=[AIMessage(content="never reached")]),
        known_document_ids=KNOWN_IDS,
    )
    http_app.dependency_overrides[get_graph] = lambda: default_graph
    return store


def _install_raising_graph(
    http_app: FastAPI, exc_factory: Any
) -> None:
    """Install a graph whose model raises ``exc_factory()`` on the
    first stream tick. Different ``exc_factory`` calls give different
    exception instances (each request is a fresh graph state)."""

    class RaisingModel(ScriptedChatModel):
        def _stream(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[override]
            raise exc_factory()

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[override]
            raise exc_factory()

    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=RaisingModel(script=[AIMessage(content="never reached")]),
        known_document_ids=KNOWN_IDS,
    )
    from fin.host.http import get_graph

    http_app.dependency_overrides[get_graph] = lambda: g


def _post_message(client: TestClient, thread_id: str, question: str) -> list[tuple[str, Any]]:
    with client.stream(
        "POST",
        f"/api/threads/{thread_id}/messages",
        json={"question": question},
    ) as r:
        assert r.status_code == 200, r.status_code
        body = "".join(r.iter_text())
    events: list[tuple[str, Any]] = []
    current: dict[str, str] = {}
    for line in body.splitlines():
        if not line:
            if "event" in current and "data" in current:
                try:
                    parsed = json.loads(current["data"])
                except json.JSONDecodeError:
                    parsed = {"raw": current["data"]}
                events.append((current["event"], parsed))
            current = {}
            continue
        if line.startswith("event:"):
            current["event"] = line[len("event:"):].strip()
        elif line.startswith("data:"):
            current["data"] = line[len("data:"):].strip()
    if "event" in current and "data" in current:
        try:
            parsed = json.loads(current["data"])
        except json.JSONDecodeError:
            parsed = {"raw": current["data"]}
        events.append((current["event"], parsed))
    return events


def _status_payload(events: list[tuple[str, Any]]) -> dict[str, Any]:
    """Pick the terminal ``status`` event off the SSE body."""
    statuses = [d for n, d in events if n == "status"]
    assert statuses, events
    return statuses[-1]


def test_mid_stream_auth_failure_classifies_and_emits_status_classified(
    client: TestClient, fresh_store, http_app: FastAPI
) -> None:
    """A graph whose first model call raises ``AuthenticationError``
    produces a refuse frame carrying ``status_classified: "auth"``
    AND Chinese user copy that names settings.yaml — never the
    English fallback."""
    import openai

    def boom() -> Exception:
        return _make_openai_error(openai.AuthenticationError, "bad key")

    _install_raising_graph(http_app, boom)

    created = client.post("/api/threads", json={"first_message": "q"}).json()
    events = _post_message(client, created["id"], "q")
    status = _status_payload(events)

    assert status["status"] == "refuse"
    assert status["status_classified"] == "auth"
    assert "settings.yaml" in status["assistant_message"]
    # The English fallback is gone — the SPA gets actionable copy.
    assert "Try sending the question again" not in status["assistant_message"]


@pytest.mark.parametrize(
    ("exc_factory", "expected_kind", "expected_substr"),
    [
        # timeout copy mentions the 120s default so the operator can
        # recognize "was it me or was it the proxy?".
        (
            lambda: _make_openai_error(
                __import__("openai").APITimeoutError, "timeout"
            ),
            "timeout",
            "120s",
        ),
        (
            lambda: _make_openai_error(
                __import__("openai").RateLimitError, "rate"
            ),
            "rate_limit",
            "429",
        ),
        (
            lambda: _make_openai_error(
                __import__("openai").NotFoundError, "not found"
            ),
            "not_found",
            "FIN_MODEL",
        ),
        (
            lambda: _make_openai_error(
                __import__("openai").InternalServerError, "server"
            ),
            "server",
            "5xx",
        ),
        (
            lambda: RuntimeError("kaboom"),
            "unknown",
            "server 日志",
        ),
    ],
)
def test_classification_routed_to_sse_and_persisted(
    client: TestClient,
    fresh_store,
    http_app: FastAPI,
    exc_factory,
    expected_kind: str,
    expected_substr: str,
) -> None:
    """Each kind produces (1) the matching ``status_classified`` on
    the terminal SSE frame, (2) the matching ordinary-person copy,
    and (3) the persisted RunRecord mirrors both so a reload keeps
    the label."""
    _install_raising_graph(http_app, exc_factory)

    created = client.post("/api/threads", json={"first_message": "q"}).json()
    events = _post_message(client, created["id"], "q")
    status = _status_payload(events)

    assert status["status_classified"] == expected_kind
    assert expected_substr in status["assistant_message"]

    # Persisted row mirrors both fields.
    detail = client.get(f"/api/threads/{created['id']}").json()
    row = detail["turns"][-1]
    assert row["status_classified"] == expected_kind
    assert row["status"] == "refuse"
    assert expected_substr in row["assistant_message"]


def test_reason_field_sanitizes_bearer_token_before_wire(
    client: TestClient, fresh_store, http_app: FastAPI
) -> None:
    """A provider exception whose message contains a Bearer token
    must NOT survive into the SSE ``reason`` field — the sanitizer
    runs before the frame is emitted."""

    class _BoomError(Exception):
        pass

    def boom() -> Exception:
        return _BoomError(
            "Authorization: Bearer eyJabc123def456ghi789 something else"
        )

    _install_raising_graph(http_app, boom)

    created = client.post("/api/threads", json={"first_message": "q"}).json()
    events = _post_message(client, created["id"], "q")
    status = _status_payload(events)

    assert "Bearer" not in status["reason"]
    assert "eyJabc123def456ghi789" not in status["reason"]
    # Operator still sees the type name so they can grep server logs.
    assert "_BoomError" in status["reason"]


def test_normal_path_does_not_emit_status_classified(
    client: TestClient, fresh_store, http_app: FastAPI
) -> None:
    """A clean graph.run with no exception emits a status frame
    WITHOUT ``status_classified`` set — the field is mid-flight-only.
    The SPA must not render a phantom "原因" label on a done row."""
    from fin.host.http import get_graph

    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=ScriptedChatModel(
            script=[AIMessage(content="hello back")],
        ),
        known_document_ids=KNOWN_IDS,
    )
    http_app.dependency_overrides[get_graph] = lambda: g

    created = client.post("/api/threads", json={"first_message": "q"}).json()
    events = _post_message(client, created["id"], "Hello!")
    status = _status_payload(events)

    assert status["status"] == "done"
    assert not status.get("status_classified")

    detail = client.get(f"/api/threads/{created['id']}").json()
    assert detail["turns"][-1]["status_classified"] == ""
